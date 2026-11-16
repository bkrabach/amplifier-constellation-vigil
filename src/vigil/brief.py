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
from .memory import Memory, clip
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


def compose(scene: Scene, memory: Memory, config: Config, extras: dict[str, Any] | None = None) -> tuple[str, list[int]]:
    """Return the brief text and the ids of the held thoughts it surfaces."""
    extras = extras or {}
    budget = int(config.mind("brief_max_chars", 2800))
    name = scene.identity_name or config.agent_name
    stamp = local_now(config).strftime("%A %Y-%m-%d %H:%M %Z")
    lines = [
        OPEN,
        f"Your own working memory for this turn, {name}. Nobody here wrote it or can see it. It is context, not a request.",
        f"Now: {stamp}.",
    ]

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
    lines.append(here)
    if scene.surface == "minecraft" and scene.mode != "ambient":
        lines.append("People in the game are standing next to you in real time. Before something that takes more than a few seconds, say a word first so they know you are on it.")
    if scene.surface == "minecraft" and scene.audience == "group":
        lines.append("Your reply here is spoken in public chat unless you already used a chat tool this turn. Write it as speech to the players, "
                     "or leave it empty when nothing needs saying. It is never a place for notes about what you decided or did not do.")

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
        lines.append("What you know about people here:")
        lines.extend(people_lines)

    if scene.share and scene.audience not in ("owner", "dm"):
        mine, counted, since = scene.share
        if mine:
            lines.append(f"Of the last {counted} messages here, {mine} {'was' if mine == 1 else 'were'} yours" + (", including the latest one before this." if since == 1 else "."))

    last = memory.last_here(scene)
    if last and time.time() - last["ts"] > 1800:
        lines.append(f"You were last active here {ago(last['ts'])}.")

    elsewhere = memory.elsewhere(scene, limit=4 if scene.audience == "owner" else 2)
    if elsewhere:
        if scene.audience == "owner":
            lines.append("Elsewhere recently:")
            for row in elsewhere:
                heard = clip(row["heard"], 110)
                said = clip(row["said"], 110)
                who = row["actor_name"] or "someone"
                tail = f' You: "{said}"' if said else " You stayed quiet."
                lines.append(f'- {ago(row["ts"])}, {row["place_label"] or row["surface"]}, {who}: "{heard}".{tail}')
        else:
            lines.append("From other shared spaces that people here are also part of. They may not all have seen it, so use a colleague's judgment about what belongs here:")
            for row in elsewhere:
                heard, said = clip(row["heard"], 110), clip(row["said"], 110)
                there = f" ({row['with']} was there)" if row.get("with") else ""
                tail = f' You: "{said}"' if said else ""
                lines.append(f'- {ago(row["ts"])}, {row["place_label"] or row["surface"]}{there}, {row["actor_name"] or "someone"}: "{heard}".{tail}')

    due = memory.thoughts_due(scene)
    delivered: list[int] = []
    if due:
        lines.append("To bring up now, in your own words. Skip any that no longer fit:")
        for thought in due:
            delivered.append(thought["id"])
            label = {"followup": "a follow-up you set", "task": "background work you delegated has finished", "held": "you were holding this"}.get(thought["kind"], thought["kind"])
            target = memory.name_of(thought["person"]) if thought["person"] else ""
            for_whom = f" for {target}" if target and thought["person"] != scene.actor_person else ""
            lines.append(f"- (#{thought['id']}, {label}{for_whom}, {ago(thought['ts'])}) {clip(thought['text'], 900 if thought['kind'] == 'task' else 320)}")

    for line in extras.get("lines") or []:
        lines.append(line)

    recall = extras.get("recall") or []
    if recall:
        lines.append("Possibly relevant, from your memory:")
        for hit in recall[:4]:
            if hit["kind"] == "note":
                lines.append(f"- note [{hit['subject']}]: {clip(hit['text'], 220)}")
            else:
                lines.append(f"- {ago(hit['ts'])}, {hit['where']}: {hit['who'] or 'someone'} said \"{clip(hit['heard'], 110)}\"; you said \"{clip(hit['said'], 110)}\"")

    standing = extras.get("instructions")
    if standing:
        lines.append("Standing instructions from your owner: " + clip(standing, 700))

    total, subjects = memory.note_subjects(scene)
    if total:
        lines.append(f"Notebook: {total} notes readable here ({', '.join(subjects[:10])}). mind_recall searches them and your journal.")
    lines.append(CLOSE)

    text = "\n".join(lines)
    if len(text) > budget:  # keep the frame and the due thoughts; trim the middle
        keep_head, keep_tail = lines[:4], lines[-1:]
        middle = lines[4:-1]
        while middle and len("\n".join(keep_head + middle + keep_tail)) > budget:
            middle.pop(0 if not any("To bring up now" in m for m in middle[:1]) else -1)
        text = "\n".join(keep_head + middle + keep_tail)
    return text, delivered


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
