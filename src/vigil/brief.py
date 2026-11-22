"""The presence brief: what the agent knows walking into this turn.

One short block of text, composed fresh for every prompt from memory the current
audience may know. It is appended after the person's own content so session titles
and replays stay about what was said. It carries a context-snapshot key, so runtimes
that support the profile keep only the newest brief in the model's context instead
of one per turn.
"""

from __future__ import annotations

import datetime
import time
from typing import Any
from zoneinfo import ZoneInfo

from .config import Config
from .memory import OWNER_WORK_CHARS, Memory, clip, owner_work
from .surfaces import Scene

OPEN = "[Vigil presence brief]"
CLOSE = "[/Vigil presence brief]"
META = "dev.vigil"
SNAPSHOT_META = "com.microsoft.amplifier.context-snapshot"

AUDIENCE_TEXT = {
    "owner": "private, with your owner",
    "dm": "private, one to one",
    "group": "a group; others can read this",
    "unknown": "unverified; treat it as a group",
}
MODE_TEXT = {
    "ambient": "nobody addressed you directly",
    "continuation": "a continuation of work you already accepted",
    "wake": "nobody spoke; this is a turn that was asked for so you could come back here",
    "voice": "a live voice call, so your reply is read aloud and the caller cannot skim it: give the answer in one or two short sentences, about 30 words at most, no lists or formatting, and offer more if they want it",
}


def ago(ts: float) -> str:
    seconds = max(0, time.time() - ts)
    if seconds < 90:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)} min ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)} h ago"
    return f"{int(seconds // 86400)} d ago"


def local_now(config: Config) -> datetime.datetime:
    try:
        zone = ZoneInfo(config.timezone)
    except Exception:
        zone = datetime.UTC
    return datetime.datetime.now(zone)


def _names_here(scene: Scene, memory: Memory) -> list[str]:
    """Who is who in a shared place where a linked person goes by another name, a game
    name typically. Without it the agent is told its owner is speaking, sees a line
    from a name it has never been given, and cannot tell that its owner is present."""
    if scene.audience != "group":
        return []
    out: list[str] = []
    for who in ([scene.actor] if scene.actor else []) + list(scene.participants):
        person = memory.person(scene.people.get(f"{who.surface}|{who.handle}"))
        shown = who.name or who.handle
        if not person or not shown or not memory.known_here(scene, person):
            continue
        known = person["name"]
        if known.casefold() in shown.casefold() or shown.casefold() in known.casefold():
            continue
        line = f"{shown} is {known}" + (" (your owner)" if person["is_owner"] else "")
        if line not in out:
            out.append(line)
    return out[:5]


#: What goes first when a brief is over its budget. Recall and news from other places
#: are the least certain to matter here; who is present, what is due and the owner's
#: standing instructions are never cut.
_TRIM_ORDER = ("recall", "elsewhere", "people", "activity", "notebook")


def _fit(rows: list[tuple[str, bool, str]], budget: int) -> str:
    """Join the rows, dropping optional ones until the text fits the budget.

    A row is (group, is_heading, text); an empty group marks a row that always stays.
    Within a group the last row goes first, and a heading goes with its last row."""
    def text() -> str:
        return "\n".join(row[2] for row in rows)

    for group in _TRIM_ORDER:
        while len(text()) > budget:
            members = [i for i, row in enumerate(rows) if row[0] == group and not row[1]]
            if not members:
                break
            rows.pop(members[-1])
        if not any(row[0] == group and not row[1] for row in rows):
            rows[:] = [row for row in rows if row[0] != group]
        if len(text()) <= budget:
            break
    return text()


def compose(scene: Scene, memory: Memory, config: Config, extras: dict[str, Any] | None = None) -> tuple[str, list[int]]:
    """Return the brief text and the ids of the held thoughts it surfaces."""
    extras = extras or {}
    budget = int(config.mind("brief_max_chars", 2800))
    name = scene.identity_name or config.agent_name
    stamp = local_now(config).strftime("%A %Y-%m-%d %H:%M %Z")
    rows: list[tuple[str, bool, str]] = []

    def add(text: str, group: str = "", heading: bool = False) -> None:
        rows.append((group, heading, text))

    add(OPEN)
    add(f"Your own working memory for this turn, {name}. Nobody here wrote it or can see it. It is context, not a request.")
    add(f"Now: {stamp}.")

    where = scene.place_label or scene.surface
    if scene.thread:
        where += ", in a thread"
    here = f"Here: {where}. Audience: {AUDIENCE_TEXT.get(scene.audience, scene.audience)}."
    if scene.actor_person:
        who = memory.person(scene.actor_person)
        if who:
            role = " (your owner)" if who["is_owner"] else (" (another agent)" if who["is_agent"] else "")
            here += f" Speaking: {who['name']}{role}."
    if scene.mode in MODE_TEXT:
        here += f" This turn: {MODE_TEXT[scene.mode]}."
    add(here)
    names = _names_here(scene, memory)
    if names:
        add("Names here: " + "; ".join(names) + ".")
    if scene.surface == "minecraft" and scene.mode not in ("ambient", "continuation", "wake"):
        add("People in the game are standing next to you in real time. Before something that takes more than a few seconds, say a word first so they know you are on it.")
    if scene.surface == "minecraft" and scene.audience == "group":
        if scene.reply_hidden:
            add("Here you speak with chat or tell. The reply that ends this turn is shown to nobody, so leave it empty: "
                "it is not a place for notes about what you decided or did.")
        else:
            add("Your reply here is spoken in public chat, unless you use chat or tell this turn: after either, the reply is shown to nobody, "
                "so leave it empty. It is never a place for notes about what you decided or did.")

    # People present, with what may be known about them here.
    people_lines = []
    seen: set[str] = set()
    ordered = ([scene.actor_person] if scene.actor_person else []) + list(scene.people.values())
    for person_id in ordered:
        if not person_id or person_id in seen:
            continue
        seen.add(person_id)
        notes = memory.notes_about(scene, person_id, limit=2)
        if not notes:
            continue
        who = memory.person(person_id)
        if who:
            people_lines.append(f"- {who['name']}: " + " / ".join(clip(n["text"], 180) for n in notes))
        if len(people_lines) >= 5:
            break
    if people_lines:
        add("What you know about people here:", "people", True)
        for line in people_lines:
            add(line, "people")

    if scene.share and scene.audience not in ("owner", "dm"):
        mine, counted, since = scene.share
        if mine:
            add(f"Of the last {counted} messages here, {mine} {'was' if mine == 1 else 'were'} yours" + (", including the latest one before this." if since == 1 else "."), "activity")

    last = memory.last_here(scene)
    if last and time.time() - last["ts"] > 1800:
        add(f"You were last active here {ago(last['ts'])}.", "activity")

    elsewhere = memory.elsewhere(scene, limit=4 if scene.audience == "owner" else 2)
    if elsewhere:
        if scene.audience == "owner":
            add("Elsewhere recently:", "elsewhere", True)
            for row in elsewhere:
                heard = clip(row["heard"], 110)
                said = clip(row["said"], 110)
                who = row["actor_name"] or "someone"
                tail = f' You: "{said}"' if said else " You stayed quiet."
                add(f'- {ago(row["ts"])}, {row["place_label"] or row["surface"]}, {who}: "{heard}".{tail}', "elsewhere")
        else:
            add("From other shared spaces that people here are also part of. They may not all have seen it, so use a colleague's judgment about what belongs here:", "elsewhere", True)
            for row in elsewhere:
                heard, said = clip(row["heard"], 110), clip(row["said"], 110)
                there = f" ({row['with']} was there)" if row.get("with") else ""
                tail = f' You: "{said}"' if said else ""
                add(f'- {ago(row["ts"])}, {row["place_label"] or row["surface"]}{there}, {row["actor_name"] or "someone"}: "{heard}".{tail}', "elsewhere")

    pending = memory.thoughts_due(scene)
    delivered: list[int] = [thought["id"] for thought in pending]
    work = [thought for thought in pending if owner_work(thought)]
    due = [thought for thought in pending if not owner_work(thought)]
    if work:
        add("Your owner asked this of you in private, and you set it for this place. It is work to carry forward when the moment allows, not something to raise on every turn. "
            "It stays in this brief until you close it with mind_cancel (kind follow_up, its id), once it is done or your owner withdraws it"
            + (". You can come back to it here without waiting for someone to speak: set a follow-up for this place:" if extras.get("can_return") else ":"))
        for thought in work:
            add(f"- (#{thought['id']}, set {ago(thought['ts'])}) {clip(thought['text'], OWNER_WORK_CHARS)}")
    if due:
        # A wake-up turn exists for these, and nobody spoke: they are as often work as words.
        add("Due now, and the reason for this turn. Each is yours to act on, by doing it here or by saying it. Skip any that no longer fit:" if scene.wake
            else "To bring up now, in your own words. Skip any that no longer fit:")
        for thought in due:
            label = {"followup": "a follow-up you set", "task": "background work you delegated has finished", "held": "you were holding this"}.get(thought["kind"], thought["kind"])
            if thought["place"] != scene.place and thought["place_label"]:  # held somewhere else: say where, so it can be judged from here
                label += f" in {thought['place_label']}"
            sent = thought["kind"] == "followup" and '"sent_by_owner": true' in (thought.get("data") or "")
            if sent:  # a repeating one the owner set for this place
                label = "your owner asked this of you in private, and you set it for this place"
            target = memory.name_of(thought["person"]) if thought["person"] else ""
            for_whom = f" for {target}" if target and thought["person"] != scene.actor_person else ""
            # In the turn it woke, a follow-up is shown whole: it is the plan for the turn.
            room = OWNER_WORK_CHARS if sent or thought["kind"] == "task" or scene.wake else 320
            add(f"- (#{thought['id']}, {label}{for_whom}, {ago(thought['ts'])}) {clip(thought['text'], room)}")

    for line in extras.get("lines") or []:
        add(line)

    recall = extras.get("recall") or []
    if recall:
        add("Possibly relevant, from your memory:", "recall", True)
        for hit in recall[:4]:
            if hit["kind"] == "note":
                add(f"- note [{hit['subject']}]: {clip(hit['text'], 220)}", "recall")
            else:
                add(f"- {ago(hit['ts'])}, {hit['where']}: {hit['who'] or 'someone'} said \"{clip(hit['heard'], 110)}\"; you said \"{clip(hit['said'], 110)}\"", "recall")

    standing = extras.get("instructions")
    if standing:
        add("Standing instructions from your owner: " + clip(standing, 700))

    total, subjects = memory.note_subjects(scene)
    if total:
        add(f"Notebook: {total} notes readable here ({', '.join(subjects[:10])}). mind_recall searches them and your journal.", "notebook")
    add(CLOSE)
    return _fit(rows, budget), delivered


def block(text: str) -> dict[str, Any]:
    return {
        "type": "text",
        "text": text,
        "_meta": {
            META: {"brief": True},
            SNAPSHOT_META: {
                "version": 1,
                "key": "vigil:presence-brief",
                "retainedText": "(An earlier presence brief stood here. The newest one, later in the conversation, replaces it.)",
            },
        },
    }


class ReplayFilter:
    """Remove presence briefs from replayed user messages.

    When a client loads a session, the runtime replays stored prompts. The brief was
    never something a person said, so it is cut from what the client shows. This is
    presentation cleanup, not a secrecy boundary.
    """

    def __init__(self) -> None:
        self._inside: set[str] = set()

    def apply(self, session: str, text: str) -> str | None:
        """Return the text to forward, or None to drop the chunk."""
        out = []
        rest = text
        while rest:
            if session in self._inside:
                end = rest.find(CLOSE)
                if end < 0:
                    rest = ""
                    break
                self._inside.discard(session)
                rest = rest[end + len(CLOSE):]
                continue
            start = rest.find(OPEN)
            if start < 0:
                out.append(rest)
                break
            out.append(rest[:start])
            self._inside.add(session)
            rest = rest[start + len(OPEN):]
        result = "".join(out)
        return result if result.strip() else None
