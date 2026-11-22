"""The mind: what Vigil adds around a runtime.

A turn arrives from some experience. The mind reads the scene, takes a few reflex
decisions, writes the presence brief, lets the runtime think, then records what
happened and arranges to come back if something is owed. Between turns it runs
delegated work and a nightly consolidation pass.

Order of spend, cheapest first: rules, then a typed decision model, then the main
model. A wake-up with nothing to deliver ends without a model call.
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime
import json
import logging
import os
import re
import secrets
from typing import Any

import aiohttp

from . import bridges, brief, decisions, github, prompts, surfaces
from .conductor import VIGIL_META, RuntimeProc
from .mcp import ToolError, call_tool, list_tools
from .memory import OWNER_WORK_CHARS, Memory, audience_fits, clip, owner_work_seen
from .store import now
from .surfaces import Scene

log = logging.getLogger("vigil.mind")

MIND_SERVER = "mind"
MAINTENANCE_META = "com.microsoft.amplifier.maintenance"
#: The native engine's statement of which tool a call is for, taken from its own tool
#: registry and not from anything the model wrote (runtime `docs/acp-optional-capabilities.md`).
TOOL_META = "com.microsoft.amplifier.tool"
MESSAGE_META = "com.microsoft.amplifier.message"
EFFORTS = ("low", "medium", "high")
#: The scene of the nightly pass. It belongs to no conversation, so "here" in it names
#: no place that anyone but the owner, in private, could read a note from.
DREAM_PLACE = "background:dream"
_GREETING = re.compile(r"^\W*(hi|hey|hello|yo|thanks|thank you|thx|ok|okay|cool|nice|great|good (morning|night|evening)|gm|gn|lol|👍|🙏)\W*$", re.I)
_RECALL_HINT = re.compile(r"\b(remember|recall|earlier|last time|yesterday|before|again|previously|we (talked|discussed|agreed|decided)|you said|you mentioned|as usual|still)\b", re.I)
_FOLLOW_UP_LINE = re.compile(r"\n?SLACK_FOLLOW_UP \{.*\}\s*$", re.S)

TOOLS: list[dict[str, Any]] = [
    {
        "name": "mind_recall",
        "description": "Search your notebook and your journal of past conversations for what you already know. Returns only what this audience may know. Use it when a conversation leans on earlier context.",
        "inputSchema": {"type": "object", "properties": {"query": {"type": "string", "description": "Words to search for."}, "limit": {"type": "integer", "minimum": 1, "maximum": 12}}, "required": ["query"], "additionalProperties": False},
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "mind_note",
        "description": "Write a lasting note to your notebook: who someone is, a preference, a decision and its reason, a commitment, a lesson. Subject is 'person:Name', 'place:Label' or a short topic. Visibility: 'here' keeps it to this place (the default), 'anywhere' lets you recall it in any room and suits only things the person would be comfortable having known anywhere, 'owner' keeps it to private sessions with your owner.",
        "inputSchema": {"type": "object", "properties": {"subject": {"type": "string"}, "note": {"type": "string"}, "visibility": {"type": "string", "enum": ["here", "anywhere", "owner"]}, "place": {"type": "string", "description": "Only during consolidation: the place key an entry came from."}}, "required": ["subject", "note"], "additionalProperties": False},
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False},
    },
    {
        "name": "mind_forget",
        "description": "Retire a note that is wrong or stale, by the id mind_recall returned. Write the corrected note separately if one is needed.",
        "inputSchema": {"type": "object", "properties": {"note_id": {"type": "integer"}}, "required": ["note_id"], "additionalProperties": False},
        "annotations": {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": False},
    },
    {
        "name": "mind_people",
        "description": "Look up who someone is: their name, whether they are a person or another agent, and where else you know them. Pass 'here' to list the people in this conversation.",
        "inputSchema": {"type": "object", "properties": {"name": {"type": "string", "description": "A person's name, or 'here'."}}, "required": ["name"], "additionalProperties": False},
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "mind_link_identity",
        "description": "Record that an account in another place belongs to a person you know, for example when someone says their Minecraft name. Surface is 'slack:TEAMID', 'minecraft', 'conversations'. The link takes effect only when the claim can be vouched for; otherwise it waits for your owner to confirm.",
        "inputSchema": {"type": "object", "properties": {"person": {"type": "string", "description": "The person's name as you know them, or 'speaker' for whoever is talking to you."}, "surface": {"type": "string"}, "handle": {"type": "string"}, "evidence": {"type": "string", "description": "What they said that supports the link."}}, "required": ["person", "surface", "handle", "evidence"], "additionalProperties": False},
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False},
    },
    {
        "name": "mind_follow_up",
        "description": "Hold a thought to return to later. Give 'in_minutes' or an ISO 'at' time. Scope 'here' brings it back in this conversation, 'person' the next time you talk with that person in a suitable place (give 'who' when it is someone other than the speaker, or when it belongs in another place), 'owner' the next time you talk privately with your owner. Leave scope out to let your core choose the one most likely to reach them. Use it whenever you promise to check, remind or get back to someone. When your owner asks you in private to do something in another of your places, give 'where' instead of a scope: the thought waits for you in that place.",
        "inputSchema": {"type": "object", "properties": {"about": {"type": "string", "description": "What to bring up, written so it makes sense later without this conversation."}, "in_minutes": {"type": "number", "minimum": 0}, "at": {"type": "string"}, "scope": {"type": "string", "enum": ["here", "person", "owner"]}, "who": {"type": "string", "description": "For scope 'person': their name. Defaults to whoever is speaking."}, "where": {"type": "string", "description": "Another of your places, by the name your brief or mind_elsewhere gives it, for something to do there. Only in a private conversation with your owner. Write 'about' for that place's audience: it holds what to do, not what was said to you in private."}, "repeat_every_minutes": {"type": "number", "minimum": 5}}, "required": ["about"], "additionalProperties": False},
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False},
    },
    {
        "name": "mind_agenda",
        "description": "List what you are holding that this audience may know: pending follow-ups and delegated background work, with their ids.",
        "inputSchema": {"type": "object", "properties": {"show": {"type": "string", "enum": ["all", "follow_ups", "tasks"]}}, "required": ["show"], "additionalProperties": False},
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "mind_cancel",
        "description": "Cancel a pending follow-up or a delegated task by id.",
        "inputSchema": {"type": "object", "properties": {"kind": {"type": "string", "enum": ["follow_up", "task"]}, "id": {"type": "integer"}}, "required": ["kind", "id"], "additionalProperties": False},
        "annotations": {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": False},
    },
    {
        "name": "mind_elsewhere",
        "description": "What you have been doing recently in your other places, limited to what this audience may know.",
        "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 12}}, "required": ["limit"], "additionalProperties": False},
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "mind_delegate",
        "description": "Hand a longer piece of work to a background session of your own, in your workspace, so this conversation stays free. State the goal so it stands on its own: what to produce, where, and how to tell it is done. The result comes back to you in a later brief for you to report. A background session costs a full separate run, so use it for work of several minutes, not for something you can do in this turn.",
        "inputSchema": {"type": "object", "properties": {"goal": {"type": "string"}, "runtime": {"type": "string", "description": "Optional: a configured runtime to use instead of your default."}}, "required": ["goal"], "additionalProperties": False},
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False},
    },
]

MIND_INSTRUCTIONS = "Your own notebook, journal, people and follow-ups. Results are already limited to what the current audience may know."


_MIND_TOOLS = {tool["name"] for tool in TOOLS}
_MIND_PARAMS = {tool["name"]: set((tool.get("inputSchema") or {}).get("properties") or {}) for tool in TOOLS}
#: A whole tool identifier and nothing after it, except an argument list in brackets
#: (one runtime titles a call `mind_note(subject: ...)`). A title is display text and
#: can contain what the model wrote, so a title that merely starts with a tool name is
#: not enough: `mind_note; curl ...` is a shell command.
_TOOL_TITLE = re.compile(r"(?:mcp__(?P<a>[A-Za-z0-9-]+)__|(?P<b>[A-Za-z0-9-]+)[./:])?(?P<tool>mind_[a-z_]+)(?P<args>\(.*\))?", re.S)
#: What kind of action a runtime says a call is. Its harness sets this, not the model.
#: An MCP tool is "other" (or unlabelled); a shell command, an edit or a fetch never is.
_MCP_KINDS = (None, "", "other")


def _own_tool(call: dict[str, Any], meta: dict[str, Any] | None = None) -> str | None:
    """Which mind tool a permission request is for, or None.

    When the engine states the call's identity itself, that statement has to name the
    same mind tool. It can only withdraw an approval, never grant one: a runtime that
    relays metadata it did not write gains nothing by sending it.
    """
    tool = _claimed_tool(call, meta)
    if tool is None:
        return None
    carried = call.get("_meta")
    identity = carried.get(TOOL_META) if isinstance(carried, dict) else None
    if isinstance(identity, dict) and identity.get("version") == 1:
        if (identity.get("kind"), identity.get("server"), identity.get("name")) != ("mcp", MIND_SERVER, tool):
            return None
    return tool


def _claimed_tool(call: dict[str, Any], meta: dict[str, Any] | None = None) -> str | None:
    """Which mind tool a permission request's title and arguments say it is for, or None.

    The answer decides an automatic approval, so it must not rest on text the model
    controls. Two shapes are accepted:

    - The native runtime's envelope, `rawInput == {server, tool, arguments}`, exactly.
      A different tool cannot carry its own required arguments inside those three keys.
    - A runtime that sends only a title: the title must be the whole identifier of a
      mind tool, the call's kind must be the one harnesses give MCP tools, the raw
      arguments (when present) must all be parameters of that tool, and the harness's
      own tool name (when it supplies one) must agree.
    """
    raw = call.get("rawInput")
    if isinstance(raw, dict) and set(raw) == {"server", "tool", "arguments"}:
        ok = raw["server"] == MIND_SERVER and raw["tool"] in _MIND_TOOLS and isinstance(raw["arguments"], dict)
        return raw["tool"] if ok else None
    if call.get("kind") not in _MCP_KINDS:
        return None
    match = _TOOL_TITLE.fullmatch(str(call.get("title") or "").strip())
    if not match or match.group("tool") not in _MIND_TOOLS or (match.group("a") or match.group("b")) not in (None, MIND_SERVER):
        return None
    tool = match.group("tool")
    if match.group("args") and call.get("kind") != "other":
        return None  # the `name(arguments)` form is only trusted from a harness that labels the call
    if raw is not None and not (isinstance(raw, dict) and set(raw) <= _MIND_PARAMS[tool]):
        return None
    harness = ((meta or {}).get("claudeCode") or {}) if isinstance(meta, dict) else {}
    if isinstance(harness, dict) and harness.get("toolName") not in (None, tool, f"mcp__{MIND_SERVER}__{tool}"):
        return None
    return tool


#: White space and the characters that print as nothing: zero-width space, joiners, word joiner, byte-order mark.
_BLANK = " \t\r\n\x0b\x0c\u00a0\u200b\u200c\u200d\u2060\ufeff"


def spoken(text: str) -> str:
    """What was actually said aloud, given the participation conventions experiences use."""
    # A model that must end a turn with some text and has nothing to say writes a
    # zero-width space, which is not something anyone was told.
    text = _FOLLOW_UP_LINE.sub("", text or "").strip(_BLANK)
    if not text or text == "[[PASS]]":
        return ""
    if text.startswith("{") and '"decision"' in text[:40]:
        try:
            value = json.loads(text)
        except ValueError:
            return text
        if isinstance(value, dict):
            return str(value.get("text") or "") if value.get("decision") == "speak" else ""
    return text


#: Said in the brief when an engine has not opened the mind's tools. Without it a model
#: asked to remember something goes looking for a notebook in files, the shell and the web.
TOOLS_MISSING = (
    "Your notebook, people and follow-up tools are not connected in this conversation, so you cannot save a note or schedule "
    "anything from here. If you are asked to remember something or to come back to it, say plainly that you cannot do that from "
    "this conversation right now. Do not look for a notebook in files, the shell or the web."
)


#: Where an experience gives the agent tools to speak with, by surface. In such a place
#: what the agent said is what went through those tools, not the text that ends its turn.
SPEECH_TOOLS = {"minecraft": ("minecraft", frozenset({"chat", "tell"}))}
#: How a conversation is asked to give the agent another turn later: the name its
#: connector's tool server starts with, the tool, and whether the tool is newer than
#: some versions of the connector. A newer one is looked for before it is relied on,
#: and may be called outside a turn in that conversation.
WAKE_TOOLS = {"slack": ("slack", "slack_follow_up", False), "minecraft": ("minecraft", "follow_up", True)}
#: A wake tool that keeps no time: asked, the connector sends the conversation one
#: prompt at once, and Vigil keeps the time. It is used where a connector's tool
#: server lists it. The game connector plans nothing since its PR 162, and this is
#: the shape agreed with its owner: `wake({id, note})`, answered `{queued, id}`, the
#: same answer and no second prompt for an id it has seen.
WAKE_NOW = {"minecraft": "wake"}
#: The note sent with such a request. The connector shows it to the agent in the prompt.
WAKE_NOTE = "Something you are holding for this conversation is due."
#: The request Vigil last made of such a tool for a place: its id, when, and whether
#: it was accepted ("queued"), refused, or left without an answer ("unknown").
WAKE_ASKED = "wake-asked:"
#: Where the tool servers of places that can be asked from outside a turn are kept, so
#: a restart does not leave held work waiting for someone to speak there. A descriptor
#: carries the connector's own credential: it lives beside the home's other secrets,
#: private to the owner, and never in the state database or a log.
WAKE_SERVERS = "wake-servers.json"
#: How long past its time a wake-up is given before it is taken for lost. The connector
#: admits a reminder when its own pacing allows; asking again sooner would replace one
#: that is still waiting and push it back.
WAKE_GRACE = 900.0
#: Where an operator's holds are kept: a JSON list of surface names or place-key
#: prefixes. It is read on every decision, so `vigil wake --hold` and `--release` take
#: effect on a running daemon without a restart.
WAKE_HOLD = "wake-hold"
#: What is kept about a place whose connector will not set a wake-up: since when, how
#: many times in a row, and what it answered. `vigil status` shows it, because nothing
#: else does. On a live agent a game refused every wake-up for 28 hours, after a turn
#: rejected before it reached Vigil left effects unsettled there, and only a log line
#: repeated every five minutes said so.
WAKE_REFUSED = "wake-refused:"


class WakeRefused(RuntimeError):
    """The connector answered, and the answer was no. Its words are the message."""


def refusal_words(result: dict[str, Any]) -> str:
    """What a connector said when it refused a tool call, without the wrapping."""
    words = []
    for part in result.get("content") or []:
        text = part.get("text") if isinstance(part, dict) else None
        if not isinstance(text, str):
            continue
        try:
            inner = json.loads(text)
            text = str(inner.get("error") or text) if isinstance(inner, dict) else text
        except ValueError:
            pass
        words.append(text)
    structured = result.get("structuredContent")
    return clip(" ".join(words) or (json.dumps(structured) if structured else "refused"), 160)


def wake_held(store: Any, scene: Scene) -> bool:
    """Whether an operator is holding wake-ups for this place: while something else is
    being repaired there, say. Nothing is asked of the connector for it by any path.
    What the agent holds for the place stays held, and stays in its brief."""
    try:
        entries = json.loads(store.meta_get(WAKE_HOLD) or "[]")
    except ValueError:
        return False
    return any(isinstance(entry, str) and entry and (entry == scene.surface or (scene.place or "").startswith(entry)) for entry in entries)


class Turn:
    """What the runtime produced during one prompt, as seen passing through."""

    def __init__(self, speech: tuple[str, frozenset[str]] | None = None) -> None:
        self.messages: dict[str, dict[str, str]] = {}
        self.order: list[str] = []
        self.tools: list[str] = []
        self.own_follow_up = False
        #: How far ahead the agent set its own reminder this turn (0 when it cancelled one).
        self.own_follow_up_after = 3600.0
        self.cancelled = False
        self.speech_server, self.speech_tools = speech or ("", frozenset())
        self.speech_tried = False
        self.spoke: list[str] = []
        self._speech: dict[str, str] = {}

    def chunk(self, update: dict[str, Any]) -> None:
        content = update.get("content") or {}
        if content.get("type") != "text":
            return
        key = str(update.get("messageId") or "_")
        phase = ((update.get("_meta") or {}).get(MESSAGE_META) or {}).get("phase") or ""
        if key not in self.messages:
            self.messages[key] = {"phase": phase, "text": ""}
            self.order.append(key)
        if phase:
            self.messages[key]["phase"] = phase
        self.messages[key]["text"] += content.get("text") or ""

    def tool(self, update: dict[str, Any]) -> None:
        raw = update.get("rawInput") if isinstance(update.get("rawInput"), dict) else {}
        name = str(raw.get("tool") or update.get("title") or "")
        if name and name not in self.tools:
            self.tools.append(name)
        if ("slack_follow_up" in name or "slack follow up" in name.lower()
                or self.speech_server == "minecraft" and raw.get("server") in (None, "minecraft") and re.fullmatch(r"(?:.*[.:]|.*__)?follow_up", name)):
            # The connector keeps one reminder for the conversation, and the agent set it.
            self.own_follow_up = True
            asked = raw.get("arguments") if isinstance(raw.get("arguments"), dict) else raw
            if isinstance(asked.get("after_seconds"), (int, float)) and not isinstance(asked.get("after_seconds"), bool):
                self.own_follow_up_after = float(max(0, min(3600, asked["after_seconds"])))
        call = str(update.get("toolCallId") or "")
        if call and name in self.speech_tools and raw.get("server") in (None, self.speech_server):
            args = raw.get("arguments") if isinstance(raw.get("arguments"), dict) else raw
            text = str(args.get("text") or "").strip()
            if text:
                self.speech_tried = True
                to = str(args.get("player") or "").strip()
                self._speech[call] = f"@{to} {text}" if to else text
        # The call is announced with its arguments and settles in a later update that
        # carries only its id. Speech counts once the experience reports it delivered.
        if call in self._speech and update.get("status") in ("completed", "failed"):
            text = self._speech.pop(call)
            if update["status"] == "completed":
                self.spoke.append(text)

    def said(self, reply_hidden: bool) -> str:
        """What the people present were told this turn.

        With speech tools, that is what those tools delivered. The text that ends the
        turn reaches people only where the experience posts it and the agent tried no
        speech tool. Where it is shown to nobody (``Scene.reply_hidden``), memory must
        not record it as said."""
        final = spoken(self.final())
        if not self.speech_tools:
            return final
        if self.spoke:
            return "\n".join(self.spoke)
        return "" if self.speech_tried or reply_hidden else final

    def final(self) -> str:
        for key in reversed(self.order):
            if self.messages[key]["phase"] != "commentary" and self.messages[key]["text"].strip():
                return self.messages[key]["text"]
        return ""


class Mind:
    def __init__(self, daemon: Any):
        self.daemon = daemon
        self.config = daemon.config
        self.store = daemon.store
        self.memory: Memory = daemon.memory
        self.decider: decisions.Decider = daemon.decider
        self.tokens: dict[str, str] = {}
        self.used_tokens: set[str] = set()
        self.quiet_hops: dict[str, int] = {}
        #: The wake tool a connector's tool server offers ("" for none), and when it was asked.
        self.wake_offered: dict[str, tuple[str, float]] = {}
        #: A place the owner has just sent work to, to be woken once the tool call returns.
        self.wake_target: tuple[str, Scene] | None = None
        #: Wake-up turns in a row, per place, that left what they surfaced undelivered.
        self.wake_misses: dict[str, int] = {}
        #: The saved tool servers, read from the secrets folder on first use.
        self.wake_places: dict[str, dict[str, Any]] | None = None
        #: Raw input the runtime announced for recent tool calls, by call id.
        self.tool_inputs: dict[str, Any] = {}
        self.tools_ok: set[str] = set()
        self.tools_warned: set[str] = set()
        #: The places tonight's consolidation entries came from, while the pass runs.
        self.dream_places: list[str] = []
        self.scenes: dict[str, Scene] = {}
        self.servers: dict[str, list[dict[str, Any]]] = {}
        self.options: dict[str, list[dict[str, Any]]] = {}
        self.turns: dict[str, Turn] = {}
        self.replaying: set[str] = set()
        self.replay = brief.ReplayFilter()
        self.home = Home(self)
        self.bridge = bridges.load(self.config)
        self._http: aiohttp.ClientSession | None = None
        self._task_lock = asyncio.Lock()
        #: The agent's read access to GitHub, when its owner has switched it on.
        self.github = github.GitHub(self.config, self.store, lambda: self.http) if github.enabled(self.config) else None

    @property
    def http(self) -> aiohttp.ClientSession:
        if self._http is None:
            self._http = aiohttp.ClientSession()
        return self._http

    async def close(self) -> None:
        await self.home.stop()
        if self._http:
            await self._http.close()

    # ---- sessions ------------------------------------------------------------------
    def descriptor(self, token: str) -> dict[str, Any]:
        return {"type": "http", "name": MIND_SERVER, "url": self.config.mind_url,
                "headers": [{"name": "Authorization", "value": f"Bearer {token}"}]}

    def supports_http_mcp(self, runtime: RuntimeProc) -> bool:
        if not runtime.spec.mind_tools:
            return False
        caps = ((runtime.initialize_result or {}).get("agentCapabilities") or {}).get("mcpCapabilities") or {}
        return caps.get("http") is True

    async def session_setup(self, link: Any, method: str, params: dict[str, Any]) -> Any:
        client_servers = [s for s in params.get("mcpServers") or [] if isinstance(s, dict) and s.get("name") not in (MIND_SERVER, github.SERVER)]
        token = secrets.token_urlsafe(24)
        sid = params.get("sessionId")
        if self.supports_http_mcp(link.runtime):
            params["mcpServers"] = client_servers + [self.descriptor(token)]
            if self.github is not None and github.offered(self.config, client_servers):
                # Last, so that every other server keeps its place: the engine names a
                # tool by its server's position in this list.
                params["mcpServers"].append(self.github.descriptor(token))
            # The runtime discovers tools while it sets the session up, before a new
            # session has an id, so the token must be valid from this moment.
            self.tokens[token] = sid or ""
        if sid and method == "session/load":
            self.replaying.add(sid)
        try:
            result = await link.runtime.request(method, params)
        except BaseException:
            self.tokens.pop(token, None)
            raise
        finally:
            if sid:
                self.replaying.discard(sid)
        sid = sid or (result or {}).get("sessionId")
        if sid:
            if token in self.tokens:
                self.tokens[token] = sid
            link.sessions.add(sid)
            self.servers[sid] = client_servers
            if isinstance(result, dict) and isinstance(result.get("configOptions"), list):
                self.options[sid] = result["configOptions"]
            self.store.session_touch(sid, runtime=link.runtime.spec.name)
            if token in self.used_tokens:
                self._tools_opened(sid)
        return result

    def link_closed(self, link: Any) -> None:
        for sid in link.sessions:
            self.servers.pop(sid, None)
            self.turns.pop(sid, None)
        dead = [token for token, sid in self.tokens.items() if sid in link.sessions]

        def drop() -> None:
            for token in dead:
                self.tokens.pop(token, None)
                self.used_tokens.discard(token)

        # A tool call can still be in flight when a client hangs up; keep its token briefly.
        asyncio.get_running_loop().call_later(120, drop)

    def valid_token(self, token: str) -> bool:
        """Called for every request to the mind's tool server. A valid request is also
        the evidence that this session's runtime actually opened the tools."""
        if token not in self.tokens:
            return False
        self.used_tokens.add(token)
        if self.tokens[token]:
            self._tools_opened(self.tokens[token])
        return True

    def _tools_opened(self, sid: str) -> None:
        if sid not in self.tools_ok:
            self.tools_ok.add(sid)
            self.store.session_touch(sid, tools_seen=now())

    def _tools_missing(self, sid: str, runtime: RuntimeProc | None = None) -> bool:
        """True when this session's runtime will not have the mind tools: it was never
        offered them (no HTTP MCP, or configured without), or it has completed a turn
        without opening them. A working runtime lists tools before its first reply."""
        if runtime is not None and not self.supports_http_mcp(runtime):
            return True
        if sid in self.tools_ok:
            return False
        row = self.store.session(sid)
        if row and row["tools_seen"]:
            self.tools_ok.add(sid)
            return False
        return bool(row and (row["turns"] or 0) >= 1)

    # ---- observing the stream ------------------------------------------------------
    def observe_update(self, link: Any, params: dict[str, Any]) -> dict[str, Any] | None:
        sid = params.get("sessionId") or ""
        update = params.get("update") or {}
        kind = update.get("sessionUpdate")
        if kind == "user_message_chunk":
            content = update.get("content") or {}
            if content.get("type") == "text" and isinstance(content.get("text"), str):
                kept = self.replay.apply(sid, content["text"])
                if kept is None:
                    return None
                if kept != content["text"]:
                    params = json.loads(json.dumps(params))
                    params["update"]["content"]["text"] = kept
            return params
        if sid in self.replaying:
            return params
        if kind in ("tool_call", "tool_call_update") and update.get("toolCallId") and "rawInput" in update:
            self.tool_inputs[str(update["toolCallId"])] = update["rawInput"]
            while len(self.tool_inputs) > 256:
                self.tool_inputs.pop(next(iter(self.tool_inputs)))
        turn = self.turns.get(sid)
        if turn is not None:
            if kind == "agent_message_chunk":
                turn.chunk(update)
            elif kind in ("tool_call", "tool_call_update"):
                turn.tool(update)
        if kind == "config_option_update" and isinstance(update.get("configOptions"), list):
            self.options[sid] = update["configOptions"]
        return params

    def own_permission(self, params: dict[str, Any]) -> dict[str, Any] | None:
        """Approve the runtime's request to call one of the mind's own tools. Those
        only touch Vigil's memory; everything else goes to the client to decide."""
        call = params.get("toolCall") or {}
        if not isinstance(call, dict) or _own_tool(call, params.get("_meta") or call.get("_meta")) is None:
            return None
        options = params.get("options") or []
        chosen = next((o for o in options if o.get("kind") == "allow_once"), None) or next((o for o in options if str(o.get("kind", "")).startswith("allow")), None)
        if not chosen:
            return None
        return {"outcome": {"outcome": "selected", "optionId": chosen.get("optionId")}}

    def imitates_tool_identity(self, params: dict[str, Any]) -> bool:
        """True for a permission request whose own arguments name a server and a tool.

        A runtime identifies a connector's tool to the client with an envelope of exactly
        `{server, tool, arguments}`. Anything else that carries both of those keys is a
        tool's arguments dressed to look like that identity, and a client that decides
        from those keys would approve it. Such a request is not passed on."""
        call = params.get("toolCall") or {}
        if not isinstance(call, dict):
            return False
        candidates = [call.get("rawInput"), self.tool_inputs.get(str(call.get("toolCallId") or ""))]
        for raw in candidates:
            if not isinstance(raw, dict) or "server" not in raw or "tool" not in raw:
                continue
            if not (set(raw) == {"server", "tool", "arguments"} and isinstance(raw["arguments"], dict)):
                return True
        return False

    def note_cancel(self, link: Any, params: dict[str, Any]) -> None:
        turn = self.turns.get(params.get("sessionId") or "")
        if turn:
            turn.cancelled = True

    def note_steering(self, link: Any, params: dict[str, Any]) -> None:
        sid = params.get("sessionId") or ""
        scene = self.scenes.get(sid)
        if scene is None:
            return
        names = {who.handle: who.name for who in [*scene.participants, scene.actor] if who and who.name}
        added = surfaces.steering_heard(params.get("prompt") or [], names)
        if added:
            scene.heard = surfaces.with_updates(scene.heard, added)
        if scene.surface == "minecraft" and scene.audience == "group":
            try:
                lines = surfaces.steering_overheard(params.get("prompt") or [], scene.self_handle)
                if lines:
                    self.memory.overhear(dataclasses.replace(scene, overheard=lines))
            except Exception:  # memory must never hold up input on its way to the runtime
                log.exception("session %s: could not keep chat that arrived during a turn", sid)

    # ---- a turn --------------------------------------------------------------------
    async def prompt(self, link: Any, params: dict[str, Any]) -> Any:
        sid = params.get("sessionId") or ""
        meta = params.get("_meta") or {}
        if MAINTENANCE_META in meta or sid in self.turns:
            # A compaction request is not a conversation turn. A second prompt while one
            # is in flight is the runtime's to accept or refuse; the mind stays with the first.
            return await link.runtime.request("session/prompt", params)
        blocks = [b for b in params.get("prompt") or [] if isinstance(b, dict)]
        scene = surfaces.read(sid, blocks, meta)
        if scene.mode == "wake":
            self._wake_arrived(scene)
        self.memory.settle(scene)
        self._label(scene)
        self.memory.overhear(scene)
        self.scenes[sid] = scene
        await self._probe_wake(sid, scene)
        turn = self.turns[sid] = Turn(SPEECH_TOOLS.get(scene.surface))
        event = self.store.event_begin("prompt", sid, scene.place, {"surface": scene.surface, "mode": scene.mode, "audience": scene.audience})
        try:
            if scene.wake or scene.reminder:
                # The connector's one reminder for this conversation has just been used. A
                # game's ordinary continuation turn is not that: on a live agent, taking it
                # for one set the reminder again after every such turn, and dropped the note
                # of a reminder that was still pending.
                self.store.meta_set(f"wake:{scene.place}", "0")
                self.store.meta_set(f"wake-own:{scene.place}", "0")
            if scene.mode in ("continuation", "wake") and scene.wake and all(owner_work_seen(thought) for thought in self.memory.thoughts_due(scene)):
                # Our own wake-up, and nothing is ready: answer it without a model call.
                await self._arm_wake(sid, scene, None)
                self.store.event_finish(event, "done", {"wake": "quiet"})
                return {"stopReason": "end_turn", "_meta": {VIGIL_META: {"wake": "quiet"}}}

            extras: dict[str, Any] = {"instructions": self._standing_instructions(scene)}
            if scene.surface == "companion" and self.bridge and self.bridge.voice and scene.heard:
                routed = await self._voice_to_game(link, sid, scene, meta, extras)
                if routed is not None:
                    self.store.event_finish(event, "done", {"routed": "minecraft"})
                    return routed
            effort, recall = await self._triage(scene)
            if recall and scene.heard:
                extras["recall"] = self.memory.search(scene, scene.heard, 4, asked=False)
            extras["can_return"] = self._wake_tool(sid, scene) is not None
            if scene.wake:
                extras["lines"] = ["This turn exists because something you were holding is due. Deliver it now, or set a new follow-up if it should wait: it will not come back by itself."]
            if self._tools_missing(sid, link.runtime):
                extras.setdefault("lines", []).append(TOOLS_MISSING)
                if sid not in self.tools_warned:
                    self.tools_warned.add(sid)
                    log.warning("session %s: runtime %s never opened the mind tools; the agent has no notebook or follow-ups there", sid, link.runtime.spec.name)
            text, surfaced = brief.compose(scene, self.memory, self.config, extras)
            params["prompt"] = blocks + [brief.block(text)]
            if effort:
                await self._set_effort(link, sid, effort)
            if turn.cancelled:
                self.store.event_finish(event, "cancelled")
                return {"stopReason": "cancelled"}

            result = await link.runtime.request("session/prompt", params)

            stop = (result or {}).get("stopReason") or ""
            said = turn.said(scene.reply_hidden)
            # A wake-up turn exists for what was due. If the agent ended it without trying
            # to speak, it chose not to raise it now; held open, the same thought would
            # wake the conversation again within seconds, one model call each time.
            # What counts is what reached people (`said`), not text that ends a turn where
            # nobody is shown it: a chat line the game refused, followed by a private
            # closing note, has delivered nothing.
            settled = bool(surfaced) and stop == "end_turn" and bool(said or (scene.wake and not turn.speech_tried))
            if settled:
                self.memory.thoughts_delivered(surfaced, sid)
            if scene.wake:
                # A wake-up that keeps failing to deliver comes back more and more slowly.
                self.wake_misses[scene.place] = 0 if settled or not surfaced else self.wake_misses.get(scene.place, 0) + 1
            if scene.heard or said:
                self.memory.journal_add(scene, said, ", ".join(turn.tools[:8]), stop)
            row = self.store.session(sid)
            self.store.session_touch(sid, surface=scene.surface, place=scene.place, place_label=scene.place_label,
                                     thread=scene.thread, audience=scene.audience, turns=(row["turns"] if row else 0) + 1)
            await self._arm_wake(sid, scene, turn)
            self.store.event_finish(event, "done", {"stop": stop, "spoke": bool(said), "tools": len(turn.tools)})
            return result
        except asyncio.CancelledError:
            self.store.event_finish(event, "cancelled")
            raise
        except Exception as error:
            self.store.event_finish(event, "failed", {"error": f"{type(error).__name__}: {error}"[:300]})
            raise
        finally:
            self.turns.pop(sid, None)

    _GAME_WORDS = re.compile(r"\b(mine(craft)?|block|build|craft|dig|chest|creeper|follow me|come (here|over|to me)|bring me|pickaxe|sword|torch|village|cave|farm|wood|stone|iron|diamond|inventory|hut|base|portal)\b", re.I)

    async def _voice_to_game(self, link: Any, sid: str, scene: Scene, meta: dict[str, Any], extras: dict[str, Any]) -> dict[str, Any] | None:
        """A spoken remark about the game goes straight to the agent's presence in the
        game, and that presence's reply is the spoken answer. Returns None to let the
        call session handle the turn in the ordinary way."""
        said = scene.heard.strip()
        if self.decider.available:
            answers = await self.decider.ask("voice_route", {"caller_said": said[-800:]}, [decisions.GAME_TALK], timeout=2.0)
            verdict = answers.get("route") if answers else None
            game = bool(verdict and not verdict.refused and verdict.value == "game" and verdict.confidence >= 0.65) if answers else bool(self._GAME_WORDS.search(said))
        else:
            game = bool(self._GAME_WORDS.search(said))
        if not game:
            return None
        call = str(((meta.get(surfaces.CONVERSATIONS_META) or {}).get("conversationId")) or sid)
        actor = scene.actor.handle if scene.actor else "caller"
        result = await self.bridge.ask(self.http, said, actor, self.memory.name_of(scene.actor_person), call)
        if result["state"] == "replied":
            await link.front.send_notification("session/update", {"sessionId": sid, "update": {
                "sessionUpdate": "agent_message_chunk", "messageId": secrets.token_hex(8), "content": {"type": "text", "text": result["text"]}}})
            self.memory.journal_add(scene, result["text"], f"passed to {self.bridge.label}", "end_turn")
            return {"stopReason": "end_turn", "_meta": {VIGIL_META: {"routed": "minecraft"}}}
        line = self._PASSED_ON.get(result["state"])
        if line:
            extras.setdefault("lines", []).append(line.format(place=self.bridge.label))
        return None

    #: What the call session is told when the game presence gave no reply in time, by
    #: what the connector said had become of the caller's words. Only "working" may say
    #: that anything is under way. The game connector since its PR 162 accepts such
    #: words and gives them to nobody, and "it is acting on them now" was said of that.
    _PASSED_ON = {
        "working": "You already passed the caller's words to your own presence in {place}, and it is acting on them now. It has not replied yet. "
                   "Tell the caller in a few words that you are on it. Do not send the request again.",
        "waiting": "You passed the caller's words to your own presence in {place}. They are still waiting there: it has not taken them up, and nothing says when it will. "
                   "Tell the caller that their request is waiting and has not been started. Do not say it is under way, and do not send it again.",
        "ended": "You passed the caller's words to your own presence in {place}. Its turn on them ended without a reply for the caller. "
                 "Tell the caller you passed it on and have heard nothing back. Do not say it is done, and do not send it again.",
        "passed": "You passed the caller's words to your own presence in {place}. Nothing shows yet whether it has taken them up. "
                  "Tell the caller you passed it on and cannot yet say that it has started. Do not send it again.",
        "unknown": "You tried to pass the caller's words to your own presence in {place}, and cannot tell whether they arrived. "
                   "Tell the caller that plainly. Do not say it is under way.",
        "unavailable": "The caller's words sound like they are about {place}. Your own presence there could not be reached just now, so nothing was passed to it. "
                       "If they asked for something to happen there, tell them plainly that you cannot reach it at the moment. Do not say it is under way.",
    }

    def _label(self, scene: Scene) -> None:
        """Replace generic place labels with the names the owner configured."""
        labels = self.config.get("places", {}) or {}
        for key in (scene.place, scene.place.split("|")[0], scene.place.rsplit(":", 1)[0]):
            label = labels.get(key)
            if isinstance(label, str) and label:
                scene.place_label = label if key == scene.place else f"{scene.place_label} in {label}"
                return

    def _standing_instructions(self, scene: Scene) -> str:
        path = self.config.memory_dir / "instructions.md"
        try:
            return path.read_text().strip()[:1200]
        except OSError:
            return ""

    async def _triage(self, scene: Scene) -> tuple[str | None, bool]:
        """Return (effort for this turn or None to leave it, whether to read memory)."""
        heard = scene.heard.strip()
        hinted = bool(_RECALL_HINT.search(heard))
        if not heard:
            return None, False
        if len(heard) < 40 and _GREETING.match(heard):
            return "low", False
        if not self.decider.available or not self.config.mind("triage", True):
            return None, hinted
        state = {
            "place": scene.place_label or scene.surface,
            "audience": scene.audience,
            "turn": scene.mode,
            "latest_message": heard[-1500:],
        }
        answers = await self.decider.ask("triage", state, decisions.triage_questions(), timeout=float(self.config.mind("triage_timeout", 2.5)))
        if not answers:
            return None, hinted
        effort = None
        choice = answers.get("effort")
        if choice and not choice.refused and choice.value in EFFORTS:
            needed = {"low": 0.7, "medium": 0.0, "high": 0.55}[choice.value]
            effort = choice.value if choice.confidence >= needed else "medium"
            if scene.mode == "voice" and effort == "high":
                effort = "medium"  # someone is waiting on the line
        recall = answers.get("recall")
        wants = hinted or bool(recall and not recall.refused and float(recall.value or 0) >= 0.55)
        return effort, wants

    async def _set_effort(self, link: Any, sid: str, effort: str) -> None:
        if self.config.mind("effort_routing", "sticky") == "off":
            return
        option = next((o for o in self.options.get(sid) or [] if o.get("category") == "thought_level"), None)
        if not option:
            return
        values = [str(v.get("value")) for v in option.get("options") or [] if isinstance(v, dict)]
        if effort not in values or option.get("currentValue") == effort:
            return
        try:
            result = await link.runtime.request("session/set_config_option", {"sessionId": sid, "configId": option.get("id"), "value": effort})
            if isinstance(result, dict) and isinstance(result.get("configOptions"), list):
                self.options[sid] = result["configOptions"]
            else:
                option["currentValue"] = effort
            self.store.session_touch(sid, effort=effort)
        except Exception as error:
            log.info("effort change skipped: %s", error)

    # ---- coming back ---------------------------------------------------------------
    def _next_wake(self, scene: Scene) -> float | None:
        """Seconds until something a wake-up in this conversation could deliver. Only a
        thought held for this place counts: one that follows a person is delivered when
        that person speaks, and a wake-up with nobody speaking cannot deliver it."""
        soonest: float | None = None
        for thought in self.memory.thoughts_pending(scene.place):
            if thought["scope"] != "here" or owner_work_seen(thought) or not audience_fits(thought["audience"], scene.audience):
                continue  # a wake-up here could not deliver it, or must not: it was held before fewer people
            wait = max(0.0, thought["due"] - now())
            soonest = wait if soonest is None else min(soonest, wait)
        running = self.store.one("SELECT count(*) AS n FROM tasks WHERE place=? AND state IN ('queued','running')", (scene.place,))
        if running and running["n"]:
            soonest = 45.0 if soonest is None else min(soonest, 45.0)
        return soonest

    def _saved_wakes(self) -> dict[str, dict[str, Any]]:
        places = getattr(self, "wake_places", None)
        if places is None:
            try:
                path = self.config.secrets_dir / WAKE_SERVERS
                info = path.lstat()
                # Like every secret here: a file that others can read is not used.
                places = json.loads(path.read_text()) if not info.st_mode & 0o077 else {}
            except (OSError, ValueError):
                places = {}
            self.wake_places = places = places if isinstance(places, dict) else {}
        return places

    def _save_wakes(self) -> None:
        folder = self.config.secrets_dir
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        scratch = folder / (WAKE_SERVERS + ".new")
        handle = os.open(scratch, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(handle, "w") as out:
            json.dump(self._saved_wakes(), out)
        os.replace(scratch, folder / WAKE_SERVERS)

    def _remember_wake(self, sid: str, scene: Scene, server: dict[str, Any], tool: str) -> None:
        """Keep a place's tool server for after a restart, when its tool may be called
        from outside a turn. Only the public conversation of the place is kept."""
        entry = {"session": sid, "surface": scene.surface, "label": scene.place_label, "server": server, "tool": tool}
        if scene.place and self._saved_wakes().get(scene.place) != entry:
            self._saved_wakes()[scene.place] = entry
            self._save_wakes()

    def _forget_wake(self, place: str) -> None:
        if self._saved_wakes().pop(place, None) is not None:
            self._save_wakes()

    def _wake_tool(self, sid: str, scene: Scene, held: bool = False) -> tuple[dict[str, Any], str] | None:
        """The connector tool that can bring this conversation back later, with the
        server to call it on, or None when the conversation has no such tool or an
        operator is holding wake-ups for it. ``held`` asks regardless of a hold."""
        spec = WAKE_TOOLS.get(scene.surface)
        if spec is None or not self.config.mind("followup_chain", True):
            return None
        if not held and wake_held(self.store, scene):
            return None
        prefix, tool, newer = spec
        if scene.surface == "minecraft" and (scene.audience != "group" or scene.mode == "voice"):
            return None  # the game's reminder returns to the public conversation only
        servers = (getattr(self, "servers", None) or {}).get(sid) or []
        server = next((s for s in servers if str(s.get("name", "")).startswith(prefix) and s.get("url")), None)
        if server is not None and newer:
            tool = (getattr(self, "wake_offered", None) or {}).get(server["url"], ("", 0.0))[0]
            if not tool:
                return None  # this session's connector does not have one
        if server is None and newer:
            # No session of this place is attached, as after a restart: the server the
            # tool was last found on, and the tool found there.
            saved = self._saved_wakes().get(scene.place)
            if saved and saved.get("surface") == scene.surface:
                server, tool = saved["server"], str(saved.get("tool") or tool)
        return (server, tool) if server is not None else None

    async def _probe_wake(self, sid: str, scene: Scene) -> None:
        """Ask a connector whether it has a wake tool that some of its versions lack.
        Asked once per server, and again every ten minutes while the answer is no, so
        an upgraded connector is noticed without a restart here."""
        spec = WAKE_TOOLS.get(scene.surface)
        if spec is None or not spec[2]:
            return
        server = next((s for s in self.servers.get(sid) or [] if str(s.get("name", "")).startswith(spec[0]) and s.get("url")), None)
        if server is None:
            return
        offered, asked = self.wake_offered.get(server["url"], ("", 0.0))
        if not offered and now() - asked >= 600:
            try:
                listed = await list_tools(self.http, server, timeout=3.0)
                offered = next((name for name in (WAKE_NOW.get(scene.surface), spec[1]) if name and name in listed), "")
                if not offered:
                    self._wake_gone(scene.place)  # the server answered, and no such tool is on it
            except Exception as error:
                log.info("could not ask %s for its tools: %s", scene.surface, error)
            self.wake_offered[server["url"]] = (offered, now())
        if offered and self._wake_tool(sid, scene, held=True):
            # Kept during a hold too, so that releasing it needs no further turn there.
            self._remember_wake(sid, scene, server, offered)

    def _wake_gone(self, place: str) -> None:
        """A place's connector no longer has a wake tool: stop asking it. What is held
        for the place then waits for the next message there, as in any place without
        one. On a live agent the game connector removed its reminder tool in an update,
        and the daemon would otherwise have called the saved server every half hour for
        a tool that was not there, with a warning in `vigil status` that never cleared."""
        if place in self._saved_wakes() or self.store.meta_get(WAKE_REFUSED + place) is not None:
            log.info("%s no longer has a wake tool; what is held for it waits for the next message there", place)
        self._forget_wake(place)
        self._wake_refused(place, None)
        self.store.meta_set(f"wake:{place}", "0")
        self.store.run("DELETE FROM meta WHERE key=?", (WAKE_ASKED + place,))

    async def _arm_wake(self, sid: str, scene: Scene, turn: Turn | None, outside: bool = False, tending: bool = False) -> bool:
        """Ask the experience to give this conversation another turn when something
        held for it comes due. Slack and the game offer this through a follow-up tool,
        for up to an hour at a time, so longer waits are a chain of quiet hops.
        ``outside`` marks a call made from outside a turn in this conversation: the
        owner has just sent work to it, or (``tending``) the daemon is looking between
        turns for a wake-up that is owed and not set. Returns whether one is now set."""
        found = self._wake_tool(sid, scene)
        if found is None:
            return False
        server, tool = found
        if tool == WAKE_NOW.get(scene.surface):
            return await self._ask_wake(scene, turn, server, tool, between=outside)
        key, own = f"wake:{scene.place}", f"wake-own:{scene.place}"
        if turn is not None and turn.own_follow_up:
            # The connector keeps one reminder per conversation and the agent has just
            # set it, replacing any of ours. Remember until when: setting ours before
            # then would replace the agent's, and its goal would be lost.
            self.store.meta_set(key, "0")
            self.store.meta_set(own, str(now() + turn.own_follow_up_after) if turn.own_follow_up_after else "0")
            return False
        try:
            # A connector's reminder is at most an hour ahead; a later time here is not one.
            if now() < float(self.store.meta_get(own) or 0) <= now() + 3600:
                return True  # the turn the agent's own reminder starts will end here too
        except ValueError:
            pass
        wait = self._next_wake(scene)
        if wait is None or wait > float(self.config.mind("followup_chain_horizon_hours", 24)) * 3600:
            self.quiet_hops.pop(scene.place, None)
            if wait is None:
                self._wake_refused(scene.place, None)  # nothing is owed there, so nothing is being refused
            return False
        after = int(min(3600, max(5, wait + 2)))
        if turn is None and not outside:
            # A wake-up that found nothing to deliver. It should only happen on the way
            # to something due later; never let it turn into a tight loop.
            hops = self.quiet_hops.get(scene.place, 0) + 1
            self.quiet_hops[scene.place] = hops
            after = max(after, 30)
            if hops > 40:
                log.warning("wake chain for %s stopped after %d quiet hops with nothing delivered", scene.place, hops)
                return False
        else:
            self.quiet_hops.pop(scene.place, None)
        if turn is not None and scene.wake:
            # This turn was itself a wake-up and left something undelivered: a chat line
            # the connector refused, a turn cut short. Come back, but not at once, and
            # more slowly each time it happens again.
            after = max(after, min(3600, 300 * 2 ** max(0, self.wake_misses.get(scene.place, 1) - 1)))
        # One wake-up per place is enough. Every turn ends here, and without this each
        # one schedules its own, so they all fire together when the thought comes due.
        fire_at = now() + after
        try:
            armed = float(self.store.meta_get(key) or 0)
        except ValueError:
            armed = 0.0
        if now() < armed <= fire_at + 5:
            return True
        if tending and armed and now() < armed + WAKE_GRACE:
            return True  # one is set, or is waiting for the connector to admit it
        goal = f"[vigil:wake:{secrets.token_hex(5)}] Return to this conversation when something you are holding for it is due. Stay quiet if nothing is ready."
        try:
            result = await call_tool(self.http, server, tool, {"after_seconds": after, "goal": goal}, timeout=5.0)
            if result.get("isError"):
                # A refusal is an answer (the conversation is stopped, or has effects
                # to settle): nothing is set, and recording it as set would hide that.
                raise WakeRefused(refusal_words(result))
            self.store.meta_set(key, str(fire_at))
            self._wake_refused(scene.place, None)
            return True
        except Exception as error:
            # Only a refusal's own words are kept. A transport error can name the
            # connector's address, and the record is in the state database.
            http = re.search(r"HTTP \d{3}", str(error))
            reason = str(error) if isinstance(error, WakeRefused) else f"the connector answered {http.group(0)}" if http else f"the connector could not be reached ({type(error).__name__})"
            if await self._wake_tool_gone(scene, server, tool):
                return False
            count = self._wake_refused(scene.place, reason)
            log.info("wake not armed for %s (%d in a row): %s", scene.place, count, error)
            if "HTTP 401" in str(error) or "HTTP 403" in str(error):
                self._forget_wake(scene.place)  # the connector's credential changed; the next turn there brings the new one
            elif tending:
                # A refusal or an unreachable connector: look again after a while, not on
                # every pass, and less often while the answer stays the same. Half an hour
                # at most, so a place that has been repaired is asked again soon.
                self.store.meta_set(key, str(now() - WAKE_GRACE + min(1800, 300 * 2 ** min(count - 1, 6))))
            return False

    async def _wake_tool_gone(self, scene: Scene, server: dict[str, Any], tool: str) -> bool:
        """After a call that failed: whether the connector answers and no longer lists the
        tool. Some versions of a connector have no such tool, and a server may answer a
        call to a missing tool as an error or as a refusal. Asking again is then no use."""
        if not WAKE_TOOLS[scene.surface][2]:
            return False
        try:
            if tool in await list_tools(self.http, server, timeout=3.0):
                return False
        except Exception:
            return False  # it cannot be asked just now: keep the failure, and try again later
        self.wake_offered[server["url"]] = ("", now())
        self._wake_gone(scene.place)
        return True

    def _wake_asked(self, place: str) -> dict[str, Any]:
        try:
            record = json.loads(self.store.meta_get(WAKE_ASKED + place) or "{}")
        except ValueError:
            record = {}
        return record if isinstance(record, dict) else {}

    def _wake_record(self, place: str, ident: str, state: str) -> None:
        self.store.meta_set(WAKE_ASKED + place, json.dumps({"id": ident, "at": now(), "state": state}))

    def _wake_arrived(self, scene: Scene) -> None:
        """A turn the connector's wake tool started has come. It is Vigil's own wake-up
        only if it carries the id of the request Vigil has outstanding for the place: an
        id is random, used once, and never shown to people. Any other is a turn the agent
        asked for itself, or text made to look like one, and settles nothing it surfaces
        by staying quiet."""
        if scene.wake and scene.wake == self._wake_asked(scene.place).get("id"):
            self.store.run("DELETE FROM meta WHERE key=?", (WAKE_ASKED + scene.place,))
        else:
            scene.wake = None

    async def _ask_wake(self, scene: Scene, turn: Turn | None, server: dict[str, Any], tool: str, between: bool) -> bool:
        """`_arm_wake` for a connector whose tool keeps no time (`WAKE_NOW`). Vigil keeps
        the time: nothing is asked at the end of a turn, and the daemon asks between
        turns (``between``) once what is held for the place is due. The connector then
        sends the conversation an ordinary prompt of its own, so every approval in that
        turn is decided where it always is.

        Each request carries an id. The connector answers an id it has seen with its
        first answer and sends nothing more, so a request whose answer was lost is asked
        again under the same id and cannot start two turns. A new id is used only once
        the last one's outcome is known: it was refused, or it was accepted and its turn
        is fifteen minutes overdue."""
        key = f"wake:{scene.place}"  # for such a tool: the time before which nothing is asked
        wait = self._next_wake(scene)
        if wait is None or wait > float(self.config.mind("followup_chain_horizon_hours", 24)) * 3600:
            if wait is None:
                self._wake_refused(scene.place, None)  # nothing is owed there, so nothing is being refused
            return False
        asked = self._wake_asked(scene.place)
        if not between:
            if turn is not None and scene.wake:
                # This turn was itself a wake-up and left something undelivered. Come
                # back, but not at once, and more slowly each time it happens again.
                self.store.meta_set(key, str(now() + min(3600, 300 * 2 ** max(0, self.wake_misses.get(scene.place, 1) - 1))))
            if asked.get("state") == "queued":
                # A request the connector accepted while this turn ran can only be sent
                # now: its fifteen minutes start here, not when it was asked.
                self._wake_record(scene.place, str(asked.get("id") or ""), "queued")
            return True
        try:
            not_before = float(self.store.meta_get(key) or 0)
        except ValueError:
            not_before = 0.0
        if wait > 0 or now() < not_before:
            return True  # not yet
        try:
            asked_at = float(asked.get("at") or 0)
        except (TypeError, ValueError):
            asked_at = 0.0
        if asked.get("state") == "queued" and now() < asked_at + WAKE_GRACE:
            return True  # accepted: the connector sends its prompt when the conversation is free
        ident = str(asked["id"]) if asked.get("state") == "unknown" and asked.get("id") else "vigil-" + secrets.token_hex(8)
        # Kept before the call: the connector's prompt can arrive before its answer does,
        # and a daemon that stops in between must repeat this id, not make another.
        self._wake_record(scene.place, ident, "unknown")
        try:
            result = await call_tool(self.http, server, tool, {"id": ident, "note": WAKE_NOTE}, timeout=5.0)
            if result.get("isError"):
                raise WakeRefused(refusal_words(result))
        except Exception as error:
            if self._wake_asked(scene.place).get("id") != ident:
                return True  # its prompt has already come: the answer no longer matters
            # Only a refusal's own words are kept. A transport error can name the
            # connector's address, and the record is in the state database.
            http = re.search(r"HTTP (\d)\d\d", str(error))
            reason = str(error) if isinstance(error, WakeRefused) else f"the connector answered {http.group(0)}" if http else f"the connector could not be reached ({type(error).__name__})"
            if await self._wake_tool_gone(scene, server, tool):
                return False
            # A refusal and a 4xx are answers: nothing was queued. Anything else may
            # have reached the connector, so the next request repeats this id.
            known = isinstance(error, WakeRefused) or bool(http and http.group(1) == "4")
            self._wake_record(scene.place, ident, "refused" if known else "unknown")
            count = self._wake_refused(scene.place, reason)
            log.info("wake not asked for %s (%d in a row): %s", scene.place, count, reason)
            if http and http.group(0) in ("HTTP 401", "HTTP 403"):
                self._forget_wake(scene.place)  # the connector's credential changed; the next turn there brings the new one
            # Look again after a while, and less often while the answer stays the same.
            self.store.meta_set(key, str(now() + min(1800, 300 * 2 ** min(count - 1, 6))))
            return False
        if self._wake_asked(scene.place).get("id") == ident:  # unless its prompt has already come
            self._wake_record(scene.place, ident, "queued")
        self._wake_refused(scene.place, None)
        return True

    def _wake_refused(self, place: str, reason: str | None) -> int:
        """Keep the record that a place's connector will not set a wake-up, or with no
        reason drop it. Returns how many times in a row it has not."""
        key = WAKE_REFUSED + place
        kept = self.store.meta_get(key)
        if reason is None:
            if kept is not None:
                self.store.run("DELETE FROM meta WHERE key=?", (key,))
            return 0
        try:
            record = json.loads(kept or "{}")
        except ValueError:
            record = {}
        record = {"since": record.get("since") or now(), "count": int(record.get("count") or 0) + 1, "last": now(), "reason": reason}
        self.store.meta_set(key, json.dumps(record))
        return record["count"]

    async def tend_wakes(self) -> None:
        """Between turns: ask a place for a turn when something is owed there and no
        wake-up is set. It covers what the end of a turn cannot: a restart, a reminder
        the connector dropped (a Stop cancels it), work that finished in the background.
        Only places whose tool can be called from outside a turn are tended."""
        for place, saved in list(self._saved_wakes().items()):
            sid = str(saved.get("session") or "")
            if sid in self.turns:
                continue  # a turn is running there, and its end sets what is needed
            scene = Scene(session=sid, surface=str(saved.get("surface") or ""), place=place, place_label=str(saved.get("label") or ""), audience="group")
            await self._arm_wake(sid, scene, None, outside=True, tending=True)

    async def _wake_elsewhere(self) -> str:
        """After the owner sent work to another place: ask that place for a turn, and
        say in the tool's answer whether it will give one."""
        target, self.wake_target = getattr(self, "wake_target", None), None
        if target is None:
            return ""
        if await self._arm_wake(target[0], target[1], None, outside=True):
            return " That place will give you a turn for it when it is due, without waiting for someone to speak there."
        return " That place could not be asked for a turn just now, so you will see it on the next message there after it is due."

    # ---- tools ---------------------------------------------------------------------
    async def github_call(self, token: str, name: str, args: dict[str, Any]) -> Any:
        sid = self.tokens.get(token)
        scene = self.scenes.get(sid or "")
        if not sid or scene is None or self.github is None:
            raise ToolError("There is no active turn for this session.")
        return await self.github.call(scene, name, args)

    async def call(self, token: str, name: str, args: dict[str, Any]) -> Any:
        sid = self.tokens.get(token)
        scene = self.scenes.get(sid or "")
        if not sid or scene is None:
            raise ToolError("There is no active turn for this session.")
        memory = self.memory
        if name == "mind_recall":
            hits = memory.search(scene, str(args.get("query") or ""), int(args.get("limit") or 6))
            if not hits:
                # Asked about a test workspace, a live agent guessed at reasons for finding nothing.
                apart = " What you did in places your owner marked as practice (a test workspace, a scratch world) is kept apart on purpose: you recall it there, or when your owner asks you in private." if memory.practice_kept_apart(scene) and scene.audience != "owner" else ""
                return "Nothing in your notebook or journal matches, within what this audience may know." + apart
            lines = []
            for hit in hits:
                if hit["kind"] == "note":
                    lines.append(f"note #{hit['id']} [{hit['subject']}] ({brief.ago(hit['ts'])}): {hit['text']}")
                else:
                    origin = f" (another shared space; {hit['with']} was there, others here may not have seen it)" if hit.get("elsewhere") and scene.audience != "owner" else ""
                    lines.append(f"journal, {brief.ago(hit['ts'])}, {hit['where']}{origin}: {hit['who'] or 'someone'} said \"{hit['heard']}\" / you said \"{hit['said']}\"")
            return "\n".join(lines)
        if name == "mind_note":
            subject, text = str(args.get("subject") or "").strip(), str(args.get("note") or "").strip()
            if not subject or not text:
                raise ToolError("Give both a subject and a note.")
            target = scene
            place = str(args.get("place") or "")
            visibility = str(args.get("visibility") or "here")
            if place and scene.surface == "background" and scene.audience == "owner":
                target = self._scene_for_place(sid, place)
            elif scene.place == DREAM_PLACE and visibility == "here":
                # A live agent's nightly pass saved its summary of a game world as "here"
                # with no place. It was kept where only the owner's private sessions read
                # it, so the agent could not recall its own summary in that world.
                known = f" Tonight's entries came from: {', '.join(self.dream_places)}." if self.dream_places else ""
                raise ToolError(
                    'In this pass "here" needs the place the note belongs to: add place, with the "place" value of the entries it came from.'
                    f'{known} Saved without it, the note could be read only in private sessions with your owner; if that is what you mean, use visibility "owner".')
            note_id = memory.note_add(target, subject, text, visibility)
            return f"Saved as note #{note_id}."
        if name == "mind_forget":
            return "Retired." if memory.note_archive(scene, int(args.get("note_id") or 0)) else "No such note is readable here."
        if name == "mind_people":
            wanted = str(args.get("name") or "")
            return self._people(scene, "" if wanted.lower() in ("here", "everyone", "all") else wanted)
        if name == "mind_link_identity":
            who = str(args.get("person") or "")
            person = scene.actor_person if who.lower() in ("speaker", "me", "") else memory.find_person(who)
            if not person:
                raise ToolError(f"You do not know anyone called {who}.")
            return memory.link(scene, person, str(args.get("surface") or ""), str(args.get("handle") or ""), str(args.get("evidence") or ""))["note"]
        if name == "mind_follow_up":
            return self._follow_up(scene, args) + await self._wake_elsewhere()
        if name == "mind_agenda":
            return self._agenda(scene, str(args.get("show") or "all"))
        if name == "mind_cancel":
            ident = int(args.get("id") or 0)
            if args.get("kind") == "task":
                row = self.store.one("SELECT state, place FROM tasks WHERE id=?", (ident,))
                if not row or row["state"] not in ("queued", "running") or (scene.audience != "owner" and row["place"] != scene.place):
                    return "No such pending task here."
                self.store.run("UPDATE tasks SET state='cancelled', finished=? WHERE id=?", (now(), ident))
                await self.home.cancel(ident)
                return "Cancelled."
            return "Cancelled." if memory.thought_cancel(scene, ident) else "No such pending follow-up here."
        if name == "mind_elsewhere":
            rows = memory.elsewhere(scene, int(args.get("limit") or 6))
            if not rows:
                return "Nothing recent elsewhere that this audience may know."
            return "\n".join(f"{brief.ago(r['ts'])}, {r['place_label'] or r['surface']}, {r['actor_name'] or 'someone'}: \"{clip(r['heard'], 200)}\"" + (f" / you: \"{clip(r['said'], 200)}\"" if r["said"] else "") for r in rows)
        if name == "mind_delegate":
            goal = str(args.get("goal") or "").strip()
            if len(goal) < 12:
                raise ToolError("Describe the goal so it stands on its own.")
            runtime = str(args.get("runtime") or "") or self.config.background_runtime
            if runtime not in self.config.runtime_names():
                raise ToolError(f"No runtime called {runtime}. Configured: {', '.join(self.config.runtime_names())}.")
            if scene.surface == "background":
                raise ToolError("A background session cannot delegate further.")
            task_id = self.store.run(
                "INSERT INTO tasks(ts,goal,runtime,state,origin_session,place,place_label,person,audience) VALUES(?,?,?,?,?,?,?,?,?)",
                (now(), goal[:8000], runtime, "queued", sid, scene.place, scene.place_label, scene.actor_person, scene.audience),
            )
            self.daemon.kick()
            return f"Delegated as task #{task_id}. The result will come back to you in a later brief for this conversation."
        raise ToolError(f"Unknown tool {name}")

    def _scene_for_place(self, sid: str, place: str) -> Scene:
        row = self.store.one("SELECT surface, place_label, audience FROM journal WHERE place=? ORDER BY ts DESC LIMIT 1", (place,))
        if not row:
            raise ToolError(f"No journal entries came from place {place}.")
        return Scene(session=sid, surface=row["surface"] or "direct", place=place, place_label=row["place_label"] or "", audience=row["audience"] or "group")

    def _people(self, scene: Scene, name: str) -> str:
        memory = self.memory
        ids = [memory.find_person(name)] if name else list(dict.fromkeys(([scene.actor_person] if scene.actor_person else []) + list(scene.people.values())))
        lines = []
        for person_id in ids:
            who = memory.person(person_id)
            if not who or not memory.known_here(scene, who):
                continue
            kind = "your owner" if who["is_owner"] else ("an agent" if who["is_agent"] else "a person")
            if scene.audience == "owner":
                places = ", ".join(f"{i['surface']}:{i['handle']}" for i in who["identities"])
            else:
                places = ", ".join(sorted({i["surface"].split(":")[0] for i in who["identities"]}))
            notes = memory.notes_about(scene, who["id"], 3)
            lines.append(f"{who['name']} — {kind}; known on: {places}" + ("".join(f"\n  - {clip(n['text'], 240)}" for n in notes)))
        return "\n".join(lines) if lines else (f"You do not know anyone called {name}." if name else "Nobody identified in this conversation yet.")

    def _follow_up(self, scene: Scene, args: dict[str, Any]) -> str:
        about = str(args.get("about") or "").strip()
        if not about:
            raise ToolError("Say what to bring up.")
        due = now()
        if args.get("at"):
            try:
                moment = datetime.datetime.fromisoformat(str(args["at"]).replace("Z", "+00:00"))
                if moment.tzinfo is None:
                    moment = moment.replace(tzinfo=brief.local_now(self.config).tzinfo)
                due = moment.timestamp()
            except ValueError:
                raise ToolError("'at' must be an ISO time such as 2026-11-16T09:00.") from None
        elif args.get("in_minutes") is not None:
            due = now() + float(args["in_minutes"]) * 60
        every = int(float(args["repeat_every_minutes"]) * 60) if args.get("repeat_every_minutes") else None
        where = str(args.get("where") or "").strip()
        if where:
            return self._follow_up_elsewhere(scene, where, about, due, every)
        scope = str(args.get("scope") or "")
        if scope not in ("here", "person", "owner"):
            # Unless told otherwise: stay in this conversation when it can bring the
            # thought back by itself or others are listening; otherwise follow the person.
            scope = "here" if scene.surface == "slack" or self._wake_tool(scene.session, scene) or not scene.private else "person"
        person = None
        if scope == "person":
            who = str(args.get("who") or "")
            person = self.memory.find_person(who) if who else scene.actor_person
            if not person:
                raise ToolError(f"You do not know anyone called {who}.")
        thought_id = self.memory.thought_add(scene, "followup", about, due, scope, person, every)
        when = brief.local_now(self.config).fromtimestamp(due, brief.local_now(self.config).tzinfo).strftime("%a %H:%M")
        how = "It will come back to you here" if scope == "here" else ("It will come back the next time you talk with them" if scope == "person" else "It will come back in your next private session with your owner")
        note = ""
        if scope == "here" and scene.surface != "slack" and not self._wake_tool(scene.session, scene):
            note = " This place cannot wake you on its own, so you will see it on the next message here after it is due."
        elif scope == "person" and not args.get("scope"):
            note = " This place cannot wake you on its own, so it follows the person instead."
        return f"Held as follow-up #{thought_id}, due {when}. {how}.{note}"

    def _follow_up_elsewhere(self, scene: Scene, where: str, about: str, due: float, every: int | None) -> str:
        """Hold a thought for another place: something the owner asked for in private
        that has to be done somewhere else. It waits in that place as the agent's own
        note. Only the owner can send the agent across places, so only an owner-private
        conversation can set one; anything else would carry one room's talk into another."""
        if scene.audience != "owner":
            raise ToolError("Only in a private conversation with your owner can you set something for another place. Here, use scope 'here' or 'person'.")
        length = len(" ".join(about.split()))
        if length > OWNER_WORK_CHARS:
            # Refused here, where it can be rewritten, so that the place never shows a cut-off task.
            raise ToolError(
                f"That is {length} characters, and the brief in another place shows at most {OWNER_WORK_CHARS} of something set for it. "
                "Write it shorter: what to do there and what your owner wants to hear back. Leave out background you will find in that place.")
        words = [word for word in re.findall(r"\w+", where.casefold()) if word]
        rows = self.store.q(
            "SELECT place, place_label, MAX(ts) AS ts FROM journal WHERE place!=? AND COALESCE(place_label,'')!='' AND COALESCE(mode,'')!='overheard' "
            "GROUP BY place ORDER BY ts DESC LIMIT 60", (scene.place,))
        matches = [row for row in rows if words and all(word in row["place_label"].casefold() for word in words)]
        if len(matches) != 1:
            names = "; ".join(row["place_label"] for row in (matches or rows)[:8]) or "none yet"
            problem = f"More than one of your places matches '{where}'" if matches else f"None of your places is called '{where}'"
            raise ToolError(f"{problem}. Your places: {names}. Give enough of one name to pick it.")
        target = self._scene_for_place(scene.session, matches[0]["place"])
        thought_id = self.memory.thought_add(target, "followup", about, due, "here", None, every, {"sent_by_owner": True})
        when = brief.local_now(self.config).fromtimestamp(due, brief.local_now(self.config).tzinfo).strftime("%a %H:%M")
        note = "" if target.surface == "slack" else " That place cannot wake you on its own, so you will see it on the next message there after it is due."
        spec = WAKE_TOOLS.get(target.surface)
        if spec and spec[2]:
            # A place whose tool can be called from outside it is asked for a turn now,
            # through the session the agent last had there. `call` does the asking once
            # this answer is written, and adds whether it worked.
            last = self.store.one("SELECT session FROM journal WHERE place=? AND COALESCE(session,'')!='' ORDER BY ts DESC LIMIT 1", (target.place,))
            there = Scene(session=last["session"] if last else "", surface=target.surface, place=target.place, place_label=target.place_label, audience="group")
            if last and self._wake_tool(there.session, there):
                self.wake_target, note = (there.session, there), ""
        stays = "" if every else " It stays in your brief there until you close it with mind_cancel."
        return f"Held as follow-up #{thought_id}, due {when}. It will come back to you in {target.place_label}, the next time you are active there.{stays}{note}"

    def _agenda(self, scene: Scene, show: str = "all") -> str:
        lines = []
        for thought in self.memory.thoughts_pending(None if scene.audience == "owner" else scene.place) if show != "tasks" else []:
            if scene.audience != "owner" and thought["scope"] == "owner":
                continue
            due = brief.local_now(self.config).fromtimestamp(thought["due"], brief.local_now(self.config).tzinfo).strftime("%a %m-%d %H:%M")
            lines.append(f"follow-up #{thought['id']} due {due} ({thought['scope']}): {clip(thought['text'], 200)}")
        where = "" if scene.audience == "owner" else " AND place=?"
        args = () if scene.audience == "owner" else (scene.place,)
        for task in self.store.q(f"SELECT id, goal, state, ts FROM tasks WHERE state IN ('queued','running'){where} ORDER BY ts", args) if show != "follow_ups" else []:
            lines.append(f"task #{task['id']} {task['state']}, started {brief.ago(task['ts'])}: {clip(task['goal'], 200)}")
        return "\n".join(lines) if lines else "Nothing pending."

    # ---- background ----------------------------------------------------------------
    async def run_queued_tasks(self) -> None:
        if self._task_lock.locked():
            return
        async with self._task_lock:
            while True:
                row = self.store.one("SELECT * FROM tasks WHERE state='queued' ORDER BY ts LIMIT 1")
                if not row:
                    return
                await self._run_task(dict(row))

    async def _run_task(self, task: dict[str, Any]) -> None:
        task_id = task["id"]
        self.store.run("UPDATE tasks SET state='running', started=? WHERE id=?", (now(), task_id))
        scene = Scene(session=f"task-{task_id}", surface="background", place=task["place"] or "", place_label=task["place_label"] or "",
                      audience=task["audience"] or "group", actor_person=task["person"], mode="direct")
        model = self.daemon.engine_model()
        text = f"{prompts.unattended(model)}\nYou delegated this to yourself from {scene.place_label or 'a conversation'}. Goal:\n{task['goal']}\n\n{prompts.REPORT}"
        try:
            report, stop = await self.home.run(scene, text, task["runtime"] or self.config.background_runtime, task_id=task_id, check_finished=task["goal"])
            state = "done" if stop == "end_turn" else "stopped"
            error = None
        except asyncio.CancelledError:
            raise
        except Exception as failure:
            report, state, error = "", "failed", f"{type(failure).__name__}: {failure}"[:300]
            log.exception("task %s failed", task_id)
        current = self.store.one("SELECT state FROM tasks WHERE id=?", (task_id,))
        if current and current["state"] == "cancelled":
            return
        self.store.run("UPDATE tasks SET state=?, result=?, error=?, finished=? WHERE id=?", (state, report[:20000], error, now(), task_id))
        outcome = report.strip() or (f"It did not finish: {error}" if error else "It ended without a report.")
        self.memory.thought_add(scene, "task", f'Task #{task_id} ("{clip(task["goal"], 140)}") {"finished" if state == "done" else "ended early"}. Report: {outcome[:3000]}',
                                now(), "here", task["person"], data={"task": task_id})

    async def dream(self) -> dict[str, Any]:
        """Consolidate recent journal entries into notes. A decision model first picks
        the entries worth the main model's attention, in one batched request."""
        rows = self.store.q("SELECT * FROM journal WHERE consolidated=0 AND COALESCE(mode,'')!='overheard' AND ts>? AND (heard!='' OR said!='') ORDER BY ts LIMIT 96", (now() - 3 * 86400,))
        if not rows:
            return {"entries": 0, "kept": 0}
        kept = []
        # Practice stays in practice: nothing from a rehearsal place becomes a lasting note.
        scored = [r for r in rows if not self.memory.is_rehearsal(r["place"])]
        for start in range(0, len(scored), 24):
            chunk = scored[start:start + 24]
            state = {"entries": [{"index": i, "who": r["actor_name"], "heard": clip(r["heard"], 260), "agent_said": clip(r["said"], 260)} for i, r in enumerate(chunk)]}
            answers = await self.decider.ask("salience", state, decisions.salience_questions(len(chunk)), timeout=8.0) if self.decider.available else None
            for i, row in enumerate(chunk):
                if answers:
                    answer = answers.get(f"e{i}")
                    score = float(answer.value or 0) if answer and not answer.refused else 0.5
                else:
                    score = 0.6 if len(row["heard"] or "") + len(row["said"] or "") > 240 else 0.2
                self.store.run("UPDATE journal SET salience=? WHERE id=?", (score, row["id"]))
                if score >= 0.5 and row["audience"] != "dm":
                    kept.append(row)
        if kept:
            entries = [{"place": r["place"], "where": r["place_label"], "visibility": "owner" if r["audience"] == "owner" else "here",
                        "when": datetime.datetime.fromtimestamp(r["ts"]).strftime("%Y-%m-%d %H:%M"), "who": r["actor_name"],
                        "heard": clip(r["heard"], 900), "you_said": clip(r["said"], 900)} for r in kept[:40]]
            scene = Scene(session="dream", surface="background", place=DREAM_PLACE, place_label="your consolidation pass", audience="owner", actor_person="owner")
            self.dream_places = list(dict.fromkeys(entry["place"] for entry in entries if entry["visibility"] == "here" and entry["place"]))
            try:
                await self.home.run(scene, prompts.DREAM + "\n" + json.dumps(entries, ensure_ascii=False, indent=1), self.config.background_runtime)
            finally:
                self.dream_places = []
        self.store.run(f"UPDATE journal SET consolidated=1 WHERE id IN ({','.join('?' * len(rows))})", [r["id"] for r in rows])
        self.store.meta_set("last_dream", datetime.date.today().isoformat())
        return {"entries": len(rows), "kept": len(kept)}


class Home:
    """The daemon's own runtime sessions, for work that belongs to no conversation
    turn: delegated tasks and consolidation. Nobody is present to approve anything,
    so only the mind's own tools are approved here; the runtime's workspace mode
    governs its local tools."""

    def __init__(self, mind: Mind):
        self.mind = mind
        self.procs: dict[str, RuntimeProc] = {}
        self.turns: dict[str, Turn] = {}
        self.active: dict[int, tuple[RuntimeProc, str]] = {}

    async def stop(self) -> None:
        for proc in list(self.procs.values()):
            await proc.stop()
        self.procs.clear()

    async def _runtime(self, name: str) -> RuntimeProc:
        proc = self.procs.get(name)
        if proc and proc.alive:
            return proc
        spec = self.mind.config.runtime(name)
        proc = RuntimeProc(self.mind.config, spec, self._handler, f"home-{name}")
        await proc.start()
        from . import __version__
        proc.initialize_result = await proc.request("initialize", {"protocolVersion": 1, "clientInfo": {"name": "vigil", "version": __version__}, "clientCapabilities": {}})
        self.procs[name] = proc
        return proc

    async def _handler(self, method: str, params: Any, is_notification: bool) -> Any:
        if is_notification:
            if method == "session/update" and isinstance(params, dict):
                turn = self.turns.get(params.get("sessionId") or "")
                update = params.get("update") or {}
                if turn is not None:
                    if update.get("sessionUpdate") == "agent_message_chunk":
                        turn.chunk(update)
                    elif update.get("sessionUpdate") in ("tool_call", "tool_call_update"):
                        turn.tool(update)
            return None
        if method == "session/request_permission" and isinstance(params, dict):
            decided = self.mind.own_permission(params)
            return decided if decided is not None else {"outcome": {"outcome": "cancelled"}}
        from acp.exceptions import RequestError
        raise RequestError.method_not_found(method)

    async def run(self, scene: Scene, text: str, runtime: str, task_id: int | None = None, check_finished: str | None = None) -> tuple[str, str]:
        proc = await self._runtime(runtime)
        token = secrets.token_urlsafe(24)
        params: dict[str, Any] = {"cwd": proc.spec.cwd, "mcpServers": []}
        if self.mind.supports_http_mcp(proc):
            params["mcpServers"] = [self.mind.descriptor(token)]
            self.mind.tokens[token] = ""  # valid during tool discovery, before the session has an id
        try:
            created = await proc.request("session/new", params)
        except BaseException:
            self.mind.tokens.pop(token, None)
            raise
        sid = created["sessionId"]
        scene.session = sid
        self.mind.tokens[token] = sid
        self.mind.scenes[sid] = scene
        if task_id is not None:
            self.active[task_id] = (proc, sid)
            self.mind.store.run("UPDATE tasks SET session=? WHERE id=?", (sid, task_id))
        modes = [m.get("id") for m in ((created.get("modes") or {}).get("availableModes") or []) if isinstance(m, dict)]
        wanted = self.mind.config.mind("background_mode", "workspace")
        if wanted in modes:
            try:
                await proc.request("session/set_mode", {"sessionId": sid, "modeId": wanted})
            except Exception as error:
                log.info("background mode not applied: %s", error)
        report, stop = "", ""
        try:
            message = text
            for attempt in range(3):
                turn = self.turns[sid] = Turn()
                result = await asyncio.wait_for(proc.request("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": message}]}),
                                                float(self.mind.config.mind("background_turn_seconds", 3600)))
                stop = (result or {}).get("stopReason") or ""
                report = turn.final() or report
                if stop != "end_turn" or not check_finished or attempt == 2 or not self.mind.decider.available:
                    break
                # An end of turn is a report, not proof the goal was met. Ask a separate
                # judge, and continue at most twice so a stuck run still ends.
                answers = await self.mind.decider.ask("finished", {"goal": clip(check_finished, 1500), "final_report": clip(report, 3000)}, [decisions.FINISHED], timeout=6.0)
                verdict = answers.get("finished") if answers else None
                if not verdict or verdict.refused or verdict.value != "continue" or verdict.confidence < 0.6:
                    break
                if task_id is not None:
                    self.mind.store.run("UPDATE tasks SET continuations=continuations+1 WHERE id=?", (task_id,))
                message = "Your report shows work you can still do on your own. Continue with it now. If something blocks you, say exactly what."
            return report, stop
        finally:
            self.turns.pop(sid, None)
            self.mind.tokens.pop(token, None)
            self.mind.scenes.pop(sid, None)
            if task_id is not None:
                self.active.pop(task_id, None)
            try:
                await proc.request("session/close", {"sessionId": sid})
            except Exception:
                pass

    async def cancel(self, task_id: int) -> None:
        entry = self.active.get(task_id)
        if entry:
            proc, sid = entry
            try:
                await proc.notify("session/cancel", {"sessionId": sid})
            except Exception:
                pass
