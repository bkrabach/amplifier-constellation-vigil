"""Read a prompt into a Scene: where the agent is, who is there, who can hear.

Each experience phrases its prompts differently. Vigil needs a few facts from every
one of them so one mind can serve all of them: the place, the audience, the person
speaking and what they said. Readers here are tolerant. A prompt from an experience
Vigil has never seen still yields a usable Scene with the most restrictive audience,
so a new adapter works on day one and gets sharper when a reader is added.

The audience is the part that carries privacy. The strongest evidence is the binding
Instance verified with the source provider and placed in its conversation
orientation. Adapter text is the fallback.
"""

from __future__ import annotations

import datetime
import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any

INSTANCE_META = "io.github.bkrabach.agent-instance"
INSTANCE_OPEN = "[Configured agent instance context]"
CONVERSATIONS_META = "com.microsoft.amplifier.conversations"

_ORIENTATION = re.compile(r"\[Conversation orientation\]\n(.*?)\n\[/Conversation orientation\]", re.S)
_SLACK_MARK = "Latest conversation projection:\n"
#: A Slack update that arrives while a turn is running: guidance, this line, then one JSON
#: object with the original `trigger` and, for a peer's message, an `awareness_input`.
_SLACK_UPDATE = "Incremental conversation update:\n"
#: A game update that arrives while a turn is running: guidance, then one JSON object
#: whose `messages` are the public chat lines said since the last one.
_GAME_UPDATE = "Public Minecraft conversation updates"
_ROOM = re.compile(r"Room participants: (\{.*?\})\. Human speaker: (.*?)\. Trigger message: ", re.S)

#: Vigil's own follow-up marker, placed in the goal of an adapter-scheduled continuation
#: so the daemon can recognise its own wake-ups without spending a model call.
KEEPALIVE = re.compile(r"\[vigil:wake:([0-9a-f]{6,32})\]")


@dataclass
class Who:
    """A participant as one surface names them. `surface` is the identity namespace:
    the same human has a different handle in each Slack workspace and in Minecraft."""

    surface: str
    handle: str
    name: str = ""
    is_agent: bool = False


@dataclass
class Scene:
    session: str
    surface: str = "direct"  # slack | conversations | companion | minecraft | direct
    place: str = ""
    place_label: str = ""
    thread: str = ""
    audience: str = "unknown"  # owner | dm | group | unknown
    mode: str = "direct"  # direct | ambient | continuation | voice
    actor: Who | None = None
    participants: list[Who] = field(default_factory=list)
    heard: str = ""
    binding: dict[str, Any] | None = None
    identity_name: str | None = None
    self_handle: str | None = None
    wake: str | None = None
    #: The source's own id for the message that started this turn, when it has one.
    source_id: str | None = None
    #: Other messages the experience showed with this turn that the agent did not take
    #: part in: (source id, who, text, source time or 0). Memory keeps them, so a room's
    #: history is the same from every place the agent later looks from.
    overheard: list[tuple[str, Who, str, float]] = field(default_factory=list)
    #: How much of the recent conversation is the agent's own: (its messages, messages
    #: counted, messages since it last spoke or None).
    share: tuple[int, int, int | None] | None = None
    #: Filled in by the mind once identities are resolved to people.
    actor_person: str | None = None
    people: dict[str, str] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def private(self) -> bool:
        return self.audience in ("owner", "dm")


def _texts(blocks: list[dict[str, Any]]) -> list[tuple[dict[str, Any], str]]:
    return [(b, b["text"]) for b in blocks if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)]


def _is_instance_block(block: dict[str, Any], text: str) -> bool:
    meta = (block.get("_meta") or {}).get(INSTANCE_META) or {}
    return meta.get("context") is True or text.startswith(INSTANCE_OPEN)


def _json_tail(text: str, start: int) -> Any:
    try:
        value, _ = json.JSONDecoder().raw_decode(text[start:])
        return value
    except (ValueError, IndexError):
        return None


def _caller_words(section: str) -> str:
    """What the caller said, from a Companion request's "Current human source".

    A current Companion gives that section as a JSON list of timed speech fragments
    from both sides of the call. The caller's words are the user fragments, in order;
    the rest is the voice's own speech and timing. An older Companion gives the words
    as plain text, which is returned unchanged."""
    data = _json_tail(section, 0) if section.startswith("[") else None
    if not isinstance(data, list):
        return section
    words = [str(part.get("text") or "").strip() for part in data if isinstance(part, dict) and part.get("role") == "user"]
    return " ".join(word for word in words if word)


def _instance(scene: Scene, text: str) -> None:
    for line in text.splitlines()[1:4]:
        if line.startswith("Name: "):
            scene.identity_name = line[6:].strip() or None
            break
    match = _ORIENTATION.search(text)
    if not match:
        return
    body = match.group(1)
    brace = body.find("{")
    data = _json_tail(body, brace) if brace >= 0 else None
    if not isinstance(data, dict):
        return
    current = data.get("current")
    if isinstance(current, dict) and current.get("conversation_id"):
        scene.binding = {
            "provider_id": str(current.get("provider_id") or ""),
            "conversation_id": str(current.get("conversation_id") or ""),
            "thread_id": str(current.get("thread_id") or ""),
            "owner": current.get("owner") is True,
            "actor_id": str(current.get("actor_id") or ""),
            "label": str(current.get("label") or ""),
            "surface": str(current.get("surface") or ""),
        }
    else:
        # Orientation without a conversation grant is only produced for an owner
        # credential (the dashboard, Observatory Talk, a local owner client).
        scene.binding = {"owner": True, "provider_id": "", "conversation_id": "", "thread_id": "", "actor_id": "", "label": "", "surface": ""}


def _slack(scene: Scene, text: str) -> bool:
    at = text.find(_SLACK_MARK)
    if not text.startswith("Slack conversation delivery.") or at < 0:
        return False
    data = _json_tail(text, at + len(_SLACK_MARK))
    if not isinstance(data, dict):
        return False
    conv = data.get("conversation") or {}
    team, channel = str(conv.get("workspace") or ""), str(conv.get("channel") or "")
    namespace = f"slack:{team}"
    scene.surface = "slack"
    scene.place = f"{namespace}:{channel}"
    scene.thread = str(conv.get("thread") or "")
    scene.self_handle = data.get("your_slack_user_id")
    names: dict[str, str] = {}
    for member in data.get("participants_from_slack") or []:
        if not isinstance(member, dict) or not member.get("id"):
            continue
        handle = str(member["id"])
        names[handle] = str(member.get("name") or "")
        if handle != scene.self_handle:
            scene.participants.append(Who(namespace, handle, names[handle], bool(member.get("is_bot"))))
    trigger = data.get("trigger") or {}
    sender = str(trigger.get("sender") or "")
    if sender and sender != scene.self_handle:
        scene.actor = Who(namespace, sender, names.get(sender, ""), bool(trigger.get("bot")))
    scene.heard = str(trigger.get("text") or "")
    prefix = f"{namespace}:{channel}:"
    if trigger.get("ts"):
        scene.source_id = prefix + str(trigger["ts"])
    for message in (data.get("history") or [])[-60:]:
        if not isinstance(message, dict) or message.get("deleted") or not message.get("ts") or not str(message.get("text") or "").strip():
            continue
        who = str(message.get("sender") or "")
        if not who or who == scene.self_handle or prefix + str(message["ts"]) == scene.source_id:
            continue
        try:
            at = float(message["ts"])
        except (TypeError, ValueError):
            at = 0.0
        scene.overheard.append((prefix + str(message["ts"]), Who(namespace, who, names.get(who, ""), bool(message.get("bot"))), str(message["text"])[:4000], at))
    recent = [m for m in (data.get("history") or [])[-12:] if isinstance(m, dict) and not m.get("deleted")]
    if scene.self_handle and len(recent) >= 4:
        mine = [i for i, m in enumerate(recent) if m.get("sender") == scene.self_handle]
        scene.share = (len(mine), len(recent), (len(recent) - 1 - mine[-1]) if mine else None)
    kind = ((data.get("orientation") or {}).get("conversation_kind") or "").lower()
    if channel.startswith("D") or kind == "direct":
        scene.audience = "dm"
        scene.place_label = "a Slack direct message"
    else:
        scene.audience = "group"
        scene.place_label = "a Slack channel"
    if "accepted_work_continuation" in data:
        scene.mode = "continuation"
        note = json.dumps(data.get("accepted_work_continuation")) + str(data.get("continuation_note") or "")
        found = KEEPALIVE.search(note)
        scene.wake = found.group(1) if found else None
    elif "\nRead the room." in text[:400] or "Decide quietly" in text[:600]:
        scene.mode = "ambient"
    return True


def _conversations(scene: Scene, text: str, meta: dict[str, Any]) -> bool:
    info = meta.get(CONVERSATIONS_META)
    if not isinstance(info, dict) or not info.get("conversationId"):
        return False
    namespace = "conversations"
    author = str(info.get("author") or "")
    scene.surface = "conversations"
    scene.place = f"conversations:{info['conversationId']}"
    scene.place_label = "a Conversations room"
    scene.heard = text
    if author:
        scene.actor = Who(namespace, author, author)
    if text.startswith("The live voice conversation delegated help"):
        scene.surface = "companion"
        scene.mode = "voice"
        scene.audience = "dm"
        # Every call is a new private room. Key the place on the caller so the agent
        # experiences "my calls with this person", not unrelated rooms.
        scene.place = f"companion:{author}" if author else scene.place
        scene.place_label = "a live Companion call"
        mark = "Current human source"
        at = text.find(mark)
        end = text.find("\n\nRecent conversation", at)
        if at >= 0:
            scene.heard = _caller_words(text[text.find("\n", at) + 1 : end if end > at else at + 4000].strip())
        if author:
            scene.participants.append(Who(namespace, author, author))
        return True
    room = _ROOM.search(text) if text.startswith("You are participating as ") else None
    if room:
        try:
            members = json.loads(room.group(1))
        except ValueError:
            members = {}
        people = [str(p if isinstance(p, str) else (p.get("username") or p.get("id") or p.get("name") or "")) for p in members.get("people") or []]
        agents = members.get("agents") or []
        for person in people:
            if person:
                scene.participants.append(Who(namespace, person, person))
        for agent in agents:
            ident = str(agent if isinstance(agent, str) else (agent.get("id") or agent.get("agent_id") or ""))
            label = ident if isinstance(agent, str) else str(agent.get("label") or ident)
            if ident:
                scene.participants.append(Who("conversations-agent", ident, label, True))
        scene.audience = "group" if len(people) > 1 or len(agents) > 1 else "dm"
        if "This is a group conversation." in text[:400]:
            scene.mode = "ambient"
        tail = text.rfind('"text":"')
        scene.heard = text[tail + 8 : tail + 2008] if tail >= 0 else ""
    else:
        scene.audience = "dm"
        if author:
            scene.participants.append(Who(namespace, author, author))
    return True


def _game_line(message: Any, self_handle: str | None) -> tuple[str, Who, str, float] | None:
    """One public line of game chat from someone else, as an overheard entry.

    The connector writes a chat line two ways: `username` and `message` for the message
    that starts a turn and for the human words that authorise activity, `author` and
    `text` for the room's recent conversation and for updates during a turn. A line
    without an id cannot be kept once, so it is left out."""
    if not isinstance(message, dict) or not message.get("id") or message.get("whisper"):
        return None
    who = str(message.get("author") or message.get("username") or "")
    text = str(message.get("text") or message.get("message") or "")
    source = str(message.get("source") or "human")
    if not who or who == self_handle or not text.strip() or source not in ("human", "agent"):
        return None
    at = 0.0
    fact = message.get("source_fact")
    stamp = fact.get("occurredAt") if isinstance(fact, dict) else None
    if isinstance(stamp, (int, float)) and not isinstance(stamp, bool):
        at = stamp / 1000.0
    elif isinstance(message.get("time"), str):
        try:
            at = datetime.datetime.fromisoformat(message["time"].replace("Z", "+00:00")).timestamp()
        except ValueError:
            at = 0.0
    return (f"minecraft:{message['id']}", Who("minecraft", who, who, source == "agent"), text[:4000], at)


def _minecraft(scene: Scene, texts: list[str]) -> bool:
    if not texts or not texts[0].startswith("Current adapter permission policy for this turn:"):
        return False
    scene.surface = "minecraft"
    scene.place = "minecraft:world"
    scene.place_label = "the Minecraft world"
    scene.audience = "group"
    namespace = "minecraft"
    body = texts[-1]
    at = body.rfind('{"messages": ')
    data = _json_tail(body, at) if at >= 0 else None
    if not isinstance(data, dict):
        return True
    world = data.get("world") or {}
    scene.self_handle = world.get("username")
    for player in world.get("players") or []:
        name = str(player if isinstance(player, str) else (player.get("username") or player.get("name") or ""))
        if name and name != scene.self_handle:
            scene.participants.append(Who(namespace, name, name))
    messages = [m for m in data.get("messages") or [] if isinstance(m, dict)]
    if messages:
        last = messages[-1]
        name = str(last.get("username") or "")
        source = str(last.get("source") or "human")
        if name and source in ("human", "agent"):
            scene.actor = Who(namespace, name, name, source == "agent")
        scene.heard = str(last.get("message") or "")
        if last.get("id"):
            scene.source_id = f"minecraft:{last['id']}"
        for message in messages[:-1][-40:]:
            who, text = str(message.get("username") or ""), str(message.get("message") or "")
            if not message.get("id") or not who or who == scene.self_handle or not text.strip() or message.get("whisper"):
                continue
            if str(message.get("source") or "human") not in ("human", "agent"):
                continue
            scene.overheard.append((f"minecraft:{message['id']}", Who(namespace, who, who, message.get("source") == "agent"), text[:4000], 0.0))
        if all(m.get("whisper") for m in messages):
            scene.audience = "dm"
        origin = last.get("installation_source")
        if isinstance(origin, dict):
            # Not game chat: another experience (a voice call, typically) passed a
            # person's words to this player privately, and the reply returns there.
            who = origin.get("actor") or {}
            scene.actor = Who("conversations", str(who.get("id") or "caller"), str(who.get("label") or ""))
            scene.participants = [scene.actor]
            scene.audience = "dm"
            if "voice" in str((origin.get("audience") or {}).get("experience") or ""):
                scene.mode = "voice"
        if source in ("activity", "continuation"):
            scene.mode = "ambient" if source == "activity" else "continuation"
    if world.get("ambient_activity") or world.get("peer_proposal"):
        scene.mode = "ambient"
    if scene.audience == "group":
        # The connector starts a turn for some chat lines and holds the rest. Held lines
        # reach the agent only here: the room's recent conversation, and the human words
        # that authorise its activity. A person's question can arrive no other way.
        room = world.get("conversation") if isinstance(world.get("conversation"), dict) else {}
        grants = world.get("task_capabilities") if isinstance(world.get("task_capabilities"), dict) else {}
        seen = {entry[0] for entry in scene.overheard} | {scene.source_id}
        lines = []
        for message in [*(grants.get("delegation_sources") or []), *(room.get("messages") or [])]:
            entry = _game_line(message, scene.self_handle)
            if entry and entry[0] not in seen:
                seen.add(entry[0])
                lines.append(entry)
        scene.overheard.extend(sorted(lines, key=lambda entry: entry[3])[-40:])
    return True


def _slack_update(text: str, names: dict[str, str]) -> list[str] | None:
    """The one new message in a Slack update, or None when the text is not one.

    The update repeats the turn's original trigger, which the prompt already gave, so
    only a peer's `awareness_input` is new. A recognised update never falls through to
    the plain-text reading, whatever it holds: its guidance is not something anyone said."""
    at = text.find(_SLACK_UPDATE)
    data = _json_tail(text, at + len(_SLACK_UPDATE)) if at >= 0 else None
    if not isinstance(data, dict) or not isinstance(data.get("trigger"), dict):
        return None
    said = data.get("awareness_input")
    if not isinstance(said, dict) or said.get("deleted") or not str(said.get("text") or "").strip():
        return []
    sender = str(said.get("sender") or "")
    if sender and sender == data.get("your_slack_user_id"):
        return []
    who = names.get(sender) or (f"<@{sender}>" if sender else "someone")
    return [f'{who}: {said["text"]}']


def steering_heard(blocks: list[Any], names: dict[str, str] | None = None) -> str:
    """What people said in input that arrives while a turn is running.

    Experiences wrap that input the way they wrap a prompt: an Instance reference
    block, a line of guidance, then the facts as JSON. Memory should keep the words,
    one line per speaker, and none of the wrapping. `names` maps a surface handle to a
    display name for input that carries only handles. Never raises."""
    lines: list[str] = []
    for block in blocks or []:
        try:
            if not isinstance(block, dict) or block.get("type") != "text":
                continue
            text = str(block.get("text") or "")
            if not text.strip() or _is_instance_block(block, text) or text.startswith("[Original input reference]"):
                continue
            probe = Scene(session="")
            if _slack(probe, text) and probe.heard:
                lines.append(f"{probe.actor.name}: {probe.heard}" if probe.actor and probe.actor.name else probe.heard)
                continue
            update = _slack_update(text, names or {})
            if update is not None:
                lines.extend(update)
                continue
            at = text.find("\n{")
            data = _json_tail(text, at + 1) if at >= 0 else None
            messages = data.get("messages") if isinstance(data, dict) else None
            if isinstance(messages, list):
                game = text.startswith(_GAME_UPDATE)
                for message in messages:
                    if not isinstance(message, dict) or (game and _game_line(message, None)):
                        continue  # steering_overheard keeps a game line under its speaker
                    said = message.get("message") or message.get("text")
                    if said:
                        lines.append(f'{message.get("username") or message.get("author") or "someone"}: {said}')
                continue
            lines.append(text.strip()[:1500])
        except Exception:  # a reader must never break a turn
            continue
    return "\n".join(lines)


def steering_overheard(blocks: list[Any], self_handle: str | None = None) -> list[tuple[str, Who, str, float]]:
    """Game chat that arrives while a turn is running, as overheard entries.

    The game connector sends each public line said during a turn as an update, and does
    not start a turn for it afterwards. Each line has its own speaker and id, so memory
    keeps it once, under the person who said it. Never raises."""
    found: list[tuple[str, Who, str, float]] = []
    for block in blocks or []:
        try:
            text = str(block.get("text") or "") if isinstance(block, dict) and block.get("type") == "text" else ""
            if not text.startswith(_GAME_UPDATE):
                continue
            at = text.find("\n{")
            data = _json_tail(text, at + 1) if at >= 0 else None
            for message in (data.get("messages") or []) if isinstance(data, dict) else []:
                entry = _game_line(message, self_handle)
                if entry:
                    found.append(entry)
        except Exception:  # a reader must never break a turn
            continue
    return found


def with_updates(heard: str, added: str, limit: int = 4000) -> str:
    """What a turn heard, with later input appended, within `limit` characters.

    When a long turn collects more than fits, the middle goes. The start stays because
    it is what began the turn and what the reply answers; the end stays because it is
    the newest."""
    joined = f"{heard}\n{added}" if heard else added
    if len(joined) <= limit:
        return joined
    head = joined[: limit // 2]
    return f"{head}\n…\n{joined[-(limit - len(head) - 3):]}"


def read(session: str, blocks: list[dict[str, Any]], meta: dict[str, Any] | None = None) -> Scene:
    """Build the Scene for one prompt. Never raises on unfamiliar input."""
    scene = Scene(session=session)
    meta = meta or {}
    user_texts: list[str] = []
    try:
        for block, text in _texts(blocks or []):
            if _is_instance_block(block, text):
                _instance(scene, text)
            else:
                user_texts.append(text)
        first = user_texts[0] if user_texts else ""
        if not (_slack(scene, first) or _conversations(scene, first, meta) or _minecraft(scene, user_texts)):
            scene.heard = "\n\n".join(user_texts)
            scene.place = "direct"
            scene.place_label = "a direct session"
    except Exception:  # a reader must never break a turn
        scene.heard = scene.heard or "\n\n".join(user_texts)

    binding = scene.binding
    if binding:
        if binding.get("conversation_id"):
            # Instance verified this with the provider; prefer its place key so every
            # thread of one conversation, and every reconnect, lands in the same place.
            scene.place = f"{binding['provider_id']}|{binding['conversation_id']}"
            scene.thread = scene.thread or binding.get("thread_id") or ""
            if binding.get("label"):
                scene.place_label = binding["label"]
        if binding.get("owner"):
            scene.audience = "owner"
        elif scene.audience == "unknown":
            scene.audience = "group"
    if not scene.place:
        scene.place = f"session:{session}"
    scene.heard = scene.heard[:6000]
    return scene
