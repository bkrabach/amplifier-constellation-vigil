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
import datetime
import json
import logging
import re
import secrets
from typing import Any

import aiohttp

from . import bridges, brief, decisions, prompts, surfaces
from .conductor import VIGIL_META, RuntimeProc
from .mcp import ToolError, call_tool
from .memory import Memory, clip
from .store import now
from .surfaces import Scene

log = logging.getLogger("vigil.mind")

MIND_SERVER = "mind"
MAINTENANCE_META = "com.microsoft.amplifier.maintenance"
MESSAGE_META = "com.microsoft.amplifier.message"
EFFORTS = ("low", "medium", "high")
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
        "description": "Hold a thought to return to later. Give 'in_minutes' or an ISO 'at' time. Scope 'here' brings it back in this conversation, 'person' the next time you talk with that person in a suitable place, 'owner' the next time you talk privately with your owner. Leave scope out to let your core choose the one most likely to reach them. Use it whenever you promise to check, remind or get back to someone.",
        "inputSchema": {"type": "object", "properties": {"about": {"type": "string", "description": "What to bring up, written so it makes sense later without this conversation."}, "in_minutes": {"type": "number", "minimum": 0}, "at": {"type": "string"}, "scope": {"type": "string", "enum": ["here", "person", "owner"]}, "who": {"type": "string", "description": "For scope 'person': their name. Defaults to whoever is speaking."}, "repeat_every_minutes": {"type": "number", "minimum": 5}}, "required": ["about"], "additionalProperties": False},
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
_TOOL_TITLE = re.compile(r"^(?:mcp__)?(?:(?P<server>[A-Za-z0-9-]+)(?:__|[./:]))?(?P<tool>mind_[a-z_]+)\b")


def _names_mind_tool(title: str) -> bool:
    """Runtimes label a tool call differently: `mind_note`, `mcp__mind__mind_note`,
    `mind.mind_note`, `mind/mind_note`. Accept those, and nothing that names
    another server."""
    match = _TOOL_TITLE.match(title.strip())
    return bool(match) and match.group("tool") in _MIND_TOOLS and match.group("server") in (None, MIND_SERVER)


def spoken(text: str) -> str:
    """What was actually said aloud, given the participation conventions experiences use."""
    text = _FOLLOW_UP_LINE.sub("", text or "").strip()
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


class Turn:
    """What the runtime produced during one prompt, as seen passing through."""

    def __init__(self) -> None:
        self.messages: dict[str, dict[str, str]] = {}
        self.order: list[str] = []
        self.tools: list[str] = []
        self.own_follow_up = False
        self.cancelled = False

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
        if "slack_follow_up" in name or "slack follow up" in name.lower():
            self.own_follow_up = True

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
        caps = ((runtime.initialize_result or {}).get("agentCapabilities") or {}).get("mcpCapabilities") or {}
        return caps.get("http") is True

    async def session_setup(self, link: Any, method: str, params: dict[str, Any]) -> Any:
        client_servers = [s for s in params.get("mcpServers") or [] if isinstance(s, dict) and s.get("name") != MIND_SERVER]
        token = secrets.token_urlsafe(24)
        sid = params.get("sessionId")
        if self.supports_http_mcp(link.runtime):
            params["mcpServers"] = client_servers + [self.descriptor(token)]
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
        return result

    def link_closed(self, link: Any) -> None:
        for sid in link.sessions:
            self.servers.pop(sid, None)
            self.turns.pop(sid, None)
        dead = [token for token, sid in self.tokens.items() if sid in link.sessions]

        def drop() -> None:
            for token in dead:
                self.tokens.pop(token, None)

        # A tool call can still be in flight when a client hangs up; keep its token briefly.
        asyncio.get_running_loop().call_later(120, drop)

    def valid_token(self, token: str) -> bool:
        return token in self.tokens

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
        raw = call.get("rawInput")
        if not (isinstance(raw, dict) and raw.get("server") == MIND_SERVER) and not _names_mind_tool(str(call.get("title") or "")):
            return None
        options = params.get("options") or []
        chosen = next((o for o in options if o.get("kind") == "allow_once"), None) or next((o for o in options if str(o.get("kind", "")).startswith("allow")), None)
        if not chosen:
            return None
        return {"outcome": {"outcome": "selected", "optionId": chosen.get("optionId")}}

    def note_cancel(self, link: Any, params: dict[str, Any]) -> None:
        turn = self.turns.get(params.get("sessionId") or "")
        if turn:
            turn.cancelled = True

    def note_steering(self, link: Any, params: dict[str, Any]) -> None:
        sid = params.get("sessionId") or ""
        scene = self.scenes.get(sid)
        if scene is None:
            return
        texts = [b.get("text", "") for b in params.get("prompt") or [] if isinstance(b, dict) and b.get("type") == "text"]
        if texts:
            scene.heard = (scene.heard + "\n" + "\n".join(texts))[-6000:]

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
        self.memory.settle(scene)
        self._label(scene)
        self.scenes[sid] = scene
        turn = self.turns[sid] = Turn()
        event = self.store.event_begin("prompt", sid, scene.place, {"surface": scene.surface, "mode": scene.mode, "audience": scene.audience})
        try:
            if scene.mode == "continuation" and scene.wake and not self.memory.thoughts_due(scene):
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
                extras["recall"] = self.memory.search(scene, scene.heard, 4)
            if scene.wake:
                extras["lines"] = ["This turn exists because something you were holding is due. Deliver it now."]
            text, surfaced = brief.compose(scene, self.memory, self.config, extras)
            params["prompt"] = blocks + [brief.block(text)]
            if effort:
                await self._set_effort(link, sid, effort)
            if turn.cancelled:
                self.store.event_finish(event, "cancelled")
                return {"stopReason": "cancelled"}

            result = await link.runtime.request("session/prompt", params)

            stop = (result or {}).get("stopReason") or ""
            said = spoken(turn.final())
            if surfaced and said and stop == "end_turn":
                self.memory.thoughts_delivered(surfaced, sid)
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
        if result["state"] == "working":
            extras.setdefault("lines", []).append(
                f"You already passed the caller's words to your own presence in {self.bridge.label}, and it is acting on them now. It has not replied yet. "
                "Tell the caller in a few words that you are on it. Do not send the request again.")
        return None

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
        soonest: float | None = None
        for thought in self.memory.thoughts_pending(scene.place):
            wait = max(0.0, thought["due"] - now())
            soonest = wait if soonest is None else min(soonest, wait)
        running = self.store.one("SELECT count(*) AS n FROM tasks WHERE place=? AND state IN ('queued','running')", (scene.place,))
        if running and running["n"]:
            soonest = 45.0 if soonest is None else min(soonest, 45.0)
        return soonest

    async def _arm_wake(self, sid: str, scene: Scene, turn: Turn | None) -> None:
        """Ask the experience to give this conversation another turn when something
        held for it comes due. Only Slack offers this, through its follow-up tool, and
        only for up to an hour at a time, so longer waits are a chain of quiet hops."""
        if scene.surface != "slack" or not self.config.mind("followup_chain", True):
            return
        if turn is not None and turn.own_follow_up:
            return
        server = next((s for s in self.servers.get(sid) or [] if str(s.get("name", "")).startswith("slack") and s.get("url")), None)
        wait = self._next_wake(scene)
        if server is None or wait is None or wait > float(self.config.mind("followup_chain_horizon_hours", 24)) * 3600:
            return
        after = int(min(3600, max(5, wait + 2)))
        goal = f"[vigil:wake:{secrets.token_hex(5)}] Return to this conversation when something you are holding for it is due. Stay quiet if nothing is ready."
        try:
            await call_tool(self.http, server, "slack_follow_up", {"after_seconds": after, "goal": goal}, timeout=5.0)
        except Exception as error:
            log.info("wake not armed for %s: %s", scene.place, error)

    # ---- tools ---------------------------------------------------------------------
    async def call(self, token: str, name: str, args: dict[str, Any]) -> Any:
        sid = self.tokens.get(token)
        scene = self.scenes.get(sid or "")
        if not sid or scene is None:
            raise ToolError("There is no active turn for this session.")
        memory = self.memory
        if name == "mind_recall":
            hits = memory.search(scene, str(args.get("query") or ""), int(args.get("limit") or 6))
            if not hits:
                return "Nothing in your notebook or journal matches, within what this audience may know."
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
            if place and scene.surface == "background" and scene.audience == "owner":
                target = self._scene_for_place(sid, place)
            note_id = memory.note_add(target, subject, text, str(args.get("visibility") or "here"))
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
            return self._follow_up(scene, args)
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
            if not who:
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
        scope = str(args.get("scope") or "")
        if scope not in ("here", "person", "owner"):
            # Unless told otherwise: stay in this conversation when it can bring the
            # thought back by itself or others are listening; otherwise follow the person.
            scope = "here" if scene.surface == "slack" or not scene.private else "person"
        person = None
        if scope == "person":
            who = str(args.get("who") or "")
            person = self.memory.find_person(who) if who else scene.actor_person
            if not person:
                raise ToolError(f"You do not know anyone called {who}.")
        every = int(float(args["repeat_every_minutes"]) * 60) if args.get("repeat_every_minutes") else None
        thought_id = self.memory.thought_add(scene, "followup", about, due, scope, person, every)
        when = brief.local_now(self.config).fromtimestamp(due, brief.local_now(self.config).tzinfo).strftime("%a %H:%M")
        how = "It will come back to you here" if scope == "here" else ("It will come back the next time you talk with them" if scope == "person" else "It will come back in your next private session with your owner")
        note = ""
        if scope == "here" and scene.surface != "slack":
            note = " This place cannot wake you on its own, so you will see it on the next message here after it is due."
        elif scope == "person" and not args.get("scope"):
            note = " This place cannot wake you on its own, so it follows the person instead."
        return f"Held as follow-up #{thought_id}, due {when}. {how}.{note}"

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
        rows = self.store.q("SELECT * FROM journal WHERE consolidated=0 AND ts>? AND (heard!='' OR said!='') ORDER BY ts LIMIT 96", (now() - 3 * 86400,))
        if not rows:
            return {"entries": 0, "kept": 0}
        kept = []
        for start in range(0, len(rows), 24):
            chunk = rows[start:start + 24]
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
            scene = Scene(session="dream", surface="background", place="background:dream", place_label="your consolidation pass", audience="owner", actor_person="owner")
            await self.home.run(scene, prompts.DREAM + "\n" + json.dumps(entries, ensure_ascii=False, indent=1), self.config.background_runtime)
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
