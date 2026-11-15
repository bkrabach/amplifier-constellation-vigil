"""The ACP conductor.

To an Agent Instance, Vigil is an ordinary ACP agent. Behind that, each attached
connection gets its own runtime process, and the conductor relays between the two.
Messages pass through as raw JSON so every extension an experience negotiates with
the runtime (steering, source delivery, speech completion, context snapshots) keeps
working without Vigil knowing about it. Only a few calls are intercepted, where the
mind has something to add.

Framing, request ids, correlation and cancellation come from the official ACP SDK's
`Connection`. Ordering matters: the SDK starts one task per incoming message in
arrival order, and enqueues an outgoing message synchronously in that task's first
step. A handler therefore keeps the peer's order as long as it relays before its
first `await`. Handlers here are written to that rule.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import time
from typing import Any

from acp.connection import Connection
from acp.exceptions import RequestError

from . import __version__
from .config import Config, RuntimeSpec

log = logging.getLogger("vigil.conductor")

STREAM_LIMIT = 32 * 1024 * 1024
SESSION_SETUP = ("session/new", "session/load", "session/resume")
RUNTIME_META = "io.github.bkrabach.agent-runtime"
VIGIL_META = "dev.vigil"
AUTH_REQUIRED = -32000


class RuntimeProc:
    """One runtime child process. Vigil is its ACP client."""

    def __init__(self, config: Config, spec: RuntimeSpec, handler: Any, label: str):
        self.config = config
        self.spec = spec
        self.handler = handler
        self.label = label
        self.proc: asyncio.subprocess.Process | None = None
        self.conn: Connection | None = None
        self.initialize_result: dict[str, Any] | None = None
        self.authenticated = False

    async def start(self) -> None:
        env = self.config.runtime_env(self.spec)
        stderr = open(self.config.log_dir / f"runtime-{self.spec.name}.log", "ab")
        try:
            self.proc = await asyncio.create_subprocess_exec(
                *self.spec.command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=stderr,
                env=env,
                cwd=self.spec.cwd if os.path.isdir(self.spec.cwd) else None,
                start_new_session=True,
                limit=STREAM_LIMIT,
            )
        finally:
            stderr.close()
        assert self.proc.stdin and self.proc.stdout
        self.conn = Connection(self.handler, self.proc.stdin, self.proc.stdout)

    async def request(self, method: str, params: Any = None) -> Any:
        if not self.conn:
            raise ConnectionError("runtime is not running")
        try:
            return await self.conn.send_request(method, params)
        except RequestError as error:
            # A runtime whose credential Vigil supplies may still want the ACP
            # authenticate step. Do it once with the configured method, then retry.
            if error.code != AUTH_REQUIRED or not self.spec.auth_method or self.authenticated or method == "authenticate":
                raise
            self.authenticated = True
            await self.conn.send_request("authenticate", {"methodId": self.spec.auth_method})
            return await self.conn.send_request(method, params)

    async def notify(self, method: str, params: Any = None) -> None:
        if self.conn:
            await self.conn.send_notification(method, params)

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    async def stop(self) -> None:
        proc, conn = self.proc, self.conn
        self.proc = self.conn = None
        if conn:
            try:
                await conn.close()
            except Exception:
                pass
        if proc and proc.returncode is None:
            try:
                await asyncio.wait_for(proc.wait(), 2.0)
            except TimeoutError:
                for sig, grace in ((signal.SIGTERM, 2.0), (signal.SIGKILL, 2.0)):
                    try:
                        os.killpg(proc.pid, sig)
                    except ProcessLookupError:
                        break
                    try:
                        await asyncio.wait_for(proc.wait(), grace)
                        break
                    except TimeoutError:
                        continue


def patch_initialize(result: dict[str, Any], config: Config, runtime_name: str) -> dict[str, Any]:
    """Present Vigil's identity while keeping the runtime's negotiated capabilities."""
    result = dict(result or {})
    backend_info = result.get("agentInfo")
    meta = dict(result.get("_meta") or {})
    runtime_meta = meta.get(RUNTIME_META)
    if isinstance(runtime_meta, dict):
        # The history profile lets an Instance read a native engine's store by running
        # that engine's own binary. Vigil is not that binary, so it does not claim it.
        runtime_meta = {k: v for k, v in runtime_meta.items() if k != "history"}
        meta[RUNTIME_META] = runtime_meta
    meta[VIGIL_META] = {"version": __version__, "runtime": runtime_name, "runtimeAgentInfo": backend_info, "mind": True}
    result["_meta"] = meta
    result["agentInfo"] = {"name": "vigil", "title": config.agent_name, "version": __version__}
    return result


class FrontLink:
    """One attached client connection (an Instance backend slot) and its runtime."""

    counter = 0

    def __init__(self, daemon: Any, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, hello: dict[str, Any]):
        FrontLink.counter += 1
        self.id = FrontLink.counter
        self.daemon = daemon
        self.config: Config = daemon.config
        self.mind = daemon.mind
        self.hello = hello
        self.opened = time.time()
        self.writer = writer
        self.front = Connection(self.on_front, writer, reader, listening=False)
        spec = self.config.runtime(self.config.main_runtime)
        self.runtime = RuntimeProc(self.config, spec, self.on_runtime, f"link{self.id}")
        self.sessions: set[str] = set()
        self.closed = False

    async def run(self) -> None:
        try:
            await self.runtime.start()
            watcher = asyncio.create_task(self._watch_runtime())
            try:
                await self.front.main_loop()
            finally:
                watcher.cancel()
        except Exception:
            log.exception("link %s failed", self.id)
        finally:
            await self.close()

    async def _watch_runtime(self) -> None:
        proc = self.runtime.proc
        if proc:
            await proc.wait()
            if not self.closed:
                log.warning("link %s: runtime exited with %s", self.id, proc.returncode)
                self.writer.close()

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.mind.link_closed(self)
        await self.runtime.stop()
        try:
            await self.front.close()
        except Exception:
            pass
        try:
            self.writer.close()
        except Exception:
            pass

    # ---- client -> runtime ---------------------------------------------------------
    async def on_front(self, method: str, params: Any, is_notification: bool) -> Any:
        if is_notification:
            if method == "session/cancel":
                self.mind.note_cancel(self, params or {})
            await self.runtime.notify(method, params)
            return None
        if method == "initialize":
            result = await self.runtime.request(method, params)
            self.runtime.initialize_result = result
            return patch_initialize(result, self.config, self.runtime.spec.name)
        if method in SESSION_SETUP:
            return await self.mind.session_setup(self, method, dict(params or {}))
        if method == "session/prompt":
            return await self.mind.prompt(self, dict(params or {}))
        if method == "_session/steering":
            self.mind.note_steering(self, params or {})
        return await self.runtime.request(method, params)

    # ---- runtime -> client ---------------------------------------------------------
    async def on_runtime(self, method: str, params: Any, is_notification: bool) -> Any:
        if is_notification:
            if method == "session/update" and isinstance(params, dict):
                params = self.mind.observe_update(self, params)
                if params is None:
                    return None
            await self.front.send_notification(method, params)
            return None
        if method == "session/request_permission" and isinstance(params, dict):
            decided = self.mind.own_permission(params)
            if decided is not None:
                return decided
        try:
            return await self.front.send_request(method, params)
        except ConnectionError:
            raise RequestError.internal_error({"details": "client disconnected"}) from None
