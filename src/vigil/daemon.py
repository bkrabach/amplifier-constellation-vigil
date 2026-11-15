"""`vigil serve`: the always-on process.

It owns the state, listens on a private socket for ACP attachments, serves the mind
tools over loopback HTTP, and runs the scheduler. Experiences come and go; this
process is what stays awake.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import logging.handlers
import os
import signal
import time

from aiohttp import web

from . import __version__, engine
from .brief import local_now
from .conductor import STREAM_LIMIT, FrontLink
from .config import Config, ensure_layout
from .decisions import Decider
from .mcp import McpServer
from .memory import Memory
from .mind import MIND_INSTRUCTIONS, TOOLS, Mind
from .store import Store, now

log = logging.getLogger("vigil")


class Daemon:
    def __init__(self, config: Config):
        self.config = config
        ensure_layout(config.home)
        self.store = Store(config.db_path)
        self.memory = Memory(self.store, config)
        self.decider = Decider(config, self.store)
        self.mind = Mind(self)
        self.links: dict[int, FrontLink] = {}
        self.started = time.time()
        self._wake = asyncio.Event()
        self._stop = asyncio.Event()
        self._model: str | None = None

    def engine_model(self) -> str:
        if self._model is None:
            self._model = engine.model(self.config) or ""
        return self._model

    def kick(self) -> None:
        self._wake.set()

    # ---- ACP attachments -----------------------------------------------------------
    async def _attach(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = await asyncio.wait_for(reader.readline(), 5)
            hello = json.loads(line)
            if not isinstance(hello, dict) or hello.get("vigil") != "attach":
                raise ValueError("not an attach")
        except Exception:
            writer.close()
            return
        link = FrontLink(self, reader, writer, hello)
        self.links[link.id] = link
        try:
            await link.run()
        finally:
            self.links.pop(link.id, None)

    # ---- control API ---------------------------------------------------------------
    def _authorized(self, request: web.Request) -> bool:
        expected = self.config.secret("control-token")
        header = request.headers.get("Authorization", "")
        return bool(expected) and header == f"Bearer {expected}"

    async def _status(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        store = self.store
        day = now() - 86400
        decided = store.one("SELECT count(*) AS n, sum(ok) AS ok, avg(ms) AS ms FROM decisions WHERE ts>?", (day,))
        by_purpose = {r["purpose"]: r["n"] for r in store.q("SELECT purpose, count(*) AS n FROM decisions WHERE ts>? GROUP BY purpose", (day,))}
        places = [dict(r) for r in store.q("SELECT surface, place_label, audience, count(*) AS turns, max(ts) AS last FROM journal WHERE ts>? GROUP BY place ORDER BY last DESC LIMIT 12", (now() - 7 * 86400,))]
        return web.json_response({
            "version": __version__,
            "agent": self.config.agent_name,
            "uptime_seconds": int(time.time() - self.started),
            "runtime": {"main": self.config.main_runtime, "background": self.config.background_runtime, "configured": self.config.runtime_names(), "model": self.engine_model()},
            "attached": [{"link": link.id, "sessions": sorted(link.sessions), "seconds": int(time.time() - link.opened)} for link in self.links.values()],
            "turns_24h": store.one("SELECT count(*) AS n FROM events WHERE kind='prompt' AND ts>?", (day,))["n"],
            "quiet_wakes_24h": store.one("SELECT count(*) AS n FROM events WHERE kind='prompt' AND ts>? AND result LIKE '%quiet%'", (day,))["n"],
            "places": places,
            "people": store.one("SELECT count(*) AS n FROM people")["n"],
            "notes": store.one("SELECT count(*) AS n FROM notes WHERE archived=0")["n"],
            "journal": store.one("SELECT count(*) AS n FROM journal")["n"],
            "pending_follow_ups": store.one("SELECT count(*) AS n FROM thoughts WHERE state='pending'")["n"],
            "tasks": {r["state"]: r["n"] for r in store.q("SELECT state, count(*) AS n FROM tasks GROUP BY state")},
            "decisions_24h": {"count": decided["n"], "ok": decided["ok"] or 0, "avg_ms": round(decided["ms"] or 0), "by_purpose": by_purpose,
                              "backends": [b.name for b in self.decider.backends]},
            "last_dream": store.meta_get("last_dream"),
        })

    async def _dream(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        return web.json_response(await self.mind.dream())

    # ---- scheduler -----------------------------------------------------------------
    async def _scheduler(self) -> None:
        """Timers do not start runs themselves. This loop polls for what is due, turns
        it into work, and goes back to sleep. Sleeping costs no tokens."""
        while not self._stop.is_set():
            try:
                await self.mind.run_queued_tasks()
                await self._maybe_dream()
                if int(time.time()) % 3600 < 30:
                    self.store.prune()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("scheduler pass failed")
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), 20)
            except TimeoutError:
                pass

    async def _maybe_dream(self) -> None:
        at = str(self.config.mind("dream_at", "03:30"))
        if at in ("", "off"):
            return
        local = local_now(self.config)
        try:
            hour, minute = (int(part) for part in at.split(":"))
        except ValueError:
            return
        today = datetime.date.today().isoformat()
        if self.store.meta_get("last_dream") == today or (local.hour, local.minute) < (hour, minute):
            return
        if self.links:  # wait for a quiet moment
            return
        result = await self.mind.dream()
        self.store.meta_set("last_dream", today)
        if result["entries"]:
            log.info("consolidation: %s", result)

    # ---- lifecycle -----------------------------------------------------------------
    async def run(self) -> None:
        interrupted = self.store.recover_events()
        if interrupted:
            log.warning("%s turn(s) were in flight when the daemon last stopped; they are marked interrupted, not replayed", interrupted)
        self.store.run("UPDATE tasks SET state='queued' WHERE state='running'")
        if not self.config.secret("control-token"):
            import secrets as pysecrets
            self.config.write_secret("control-token", pysecrets.token_urlsafe(32))

        app = web.Application(client_max_size=4 * 1024 * 1024)
        mcp = McpServer(TOOLS, self.mind.valid_token, self.mind.call, MIND_INSTRUCTIONS)
        app.add_routes(mcp.routes())
        app.add_routes([web.get("/control/status", self._status), web.post("/control/dream", self._dream)])
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", self.config.mcp_port).start()

        path = self.config.socket_path
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if path.exists():
            path.unlink()
        server = await asyncio.start_unix_server(self._attach, path=str(path), limit=STREAM_LIMIT)
        os.chmod(path, 0o600)

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self._stop.set)
        scheduler = asyncio.create_task(self._scheduler())
        log.info("vigil %s awake for %s: socket %s, mind %s, runtime %s (%s)", __version__, self.config.agent_name, path,
                 self.config.mind_url, self.config.main_runtime, self.engine_model() or "model unknown")
        try:
            await self._stop.wait()
        finally:
            log.info("stopping")
            scheduler.cancel()
            server.close()
            for link in list(self.links.values()):
                await link.close()
            await self.mind.close()
            await self.decider.close()
            await runner.cleanup()
            try:
                path.unlink()
            except OSError:
                pass
            self.store.close()


def serve(home: str) -> None:
    config = Config.load(home)
    ensure_layout(config.home)
    handler = logging.handlers.RotatingFileHandler(config.log_dir / "vigil.log", maxBytes=8 * 1024 * 1024, backupCount=3)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=[handler, logging.StreamHandler()])
    asyncio.run(Daemon(config).run())
