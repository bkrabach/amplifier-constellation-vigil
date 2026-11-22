"""Bridges: reaching one of the agent's own presences from another.

The agent can be in a voice call and in a game world at the same time. Those are two
sessions, and by default a spoken request about the game would be reasoned about in
the call session, handed to the game session through tools, reasoned about again
there, and reported back: two model turns and several hops before anything moves.

A bridge lets the mind hand the caller's own words straight to the presence that can
act on them, through that presence's declared interface for outside callers, and
speak its reply. One model turn, in the session that already holds the game.

Only the Minecraft adapter's installation-caller interface is implemented: `POST
/task` with the unchanged human text and its source, then `POST /source-outcomes`
for the reply recorded for that source.
"""

from __future__ import annotations

import asyncio
import datetime
import time
import uuid
from typing import Any

import aiohttp

from .config import Config


class MinecraftBridge:
    def __init__(self, config: Config, table: dict[str, Any]):
        self.config = config
        self.url = str(table["url"]).rstrip("/")
        self.secret = str(table.get("secret", ""))
        self.label = str(table.get("label", "the Minecraft world"))
        self.voice = bool(table.get("voice", True))
        self.wait = float(table.get("wait_seconds", 15))

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.config.secret(self.secret) or ''}", "Content-Type": "application/json"}

    async def ask(self, http: aiohttp.ClientSession, text: str, actor_id: str, actor_label: str, call_id: str) -> dict[str, Any]:
        """Submit the caller's words and wait briefly for the reply recorded for them.

        Returns {"state": ..., "text": str}. "replied" carries what the game session
        said to this caller; it is not proof that physical work in the world finished.
        Without a reply the state is what the connector last said of the words:
        "working" (a turn has them), "waiting" (still queued: nothing has taken them
        up), "ended" (a turn had them and ended without a reply for the caller),
        "passed" (accepted, and nothing more is known), "unknown" (sent, and the
        connector's answer was lost) or "unavailable" (refused or not reached).
        """
        source = {
            "version": 1, "request_id": str(uuid.uuid4()), "call_id": call_id or "call", "intent_id": str(uuid.uuid4()), "revision": "1",
            "occurred_at": datetime.datetime.now(datetime.UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "actor": {"id": actor_id or "caller", "label": actor_label or actor_id or "caller"},
            "audience": {"kind": "private", "experience": "voice-client", "id": call_id or "call"},
        }
        limit = aiohttp.ClientTimeout(total=8)
        try:
            async with http.post(f"{self.url}/task", json={"text": text, "source": source}, headers=self._headers(), timeout=limit) as response:
                if response.status >= 300:
                    return {"state": "unavailable", "text": ""}
                queued = await response.json()
        except aiohttp.ClientConnectorError:
            return {"state": "unavailable", "text": ""}  # nothing was sent
        except (TimeoutError, aiohttp.ClientError, ValueError):
            return {"state": "unknown", "text": ""}  # sent, and the answer was lost
        # From here the words are with the connector. Whatever goes wrong while waiting
        # for a reply, they were passed on, and the caller must not be told otherwise.
        request_id = queued.get("request_id") if isinstance(queued, dict) else None
        if not isinstance(request_id, str) or not request_id:
            return {"state": "passed", "text": ""}
        query = {"version": 1, "requests": [{"request_id": request_id, "source": source}]}
        delivery = ""
        deadline = time.monotonic() + self.wait
        while time.monotonic() < deadline:
            await asyncio.sleep(0.7)
            try:
                async with http.post(f"{self.url}/source-outcomes", json=query, headers=self._headers(), timeout=limit) as response:
                    if response.status >= 300:
                        continue
                    outcome = ((await response.json()).get("outcomes") or [{}])[0]
                if not isinstance(outcome, dict):
                    continue
            except (TimeoutError, aiohttp.ClientError, ValueError):
                continue
            replies = [r.get("text", "") for r in outcome.get("source_replies") or [] if isinstance(r, dict) and r.get("text")]
            if replies:
                return {"state": "replied", "text": "\n".join(replies)}
            delivery = "terminal" if outcome.get("terminal_at") else str(outcome.get("delivery") or delivery)
            if delivery in ("terminal", "interrupted"):
                break
        return {"state": {"admitted": "working", "queued": "waiting", "terminal": "ended", "interrupted": "ended"}.get(delivery, "passed"), "text": ""}


def load(config: Config) -> MinecraftBridge | None:
    table = (config.get("bridges", {}) or {}).get("minecraft")
    return MinecraftBridge(config, table) if isinstance(table, dict) and table.get("url") else None
