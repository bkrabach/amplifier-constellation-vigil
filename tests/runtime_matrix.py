"""Run the same short scenario against several runtimes and report what each supports.

    python tests/runtime_matrix.py matrix.json [--only NAME ...] [--keep]

For every entry it builds a throwaway Vigil home, starts a daemon with that runtime
as the main one, and plays an owner's Slack direct message through the ACP bridge:

  turn       the runtime answers at all
  brief      it knows who is speaking, which only the presence brief says
  mind tool  asked to remember something, it writes a note through the mind tools
  recall     a new session, in a new process, finds that note
  reload     a saved session loads again (when the runtime offers loadSession)

The matrix file is JSON (see `runtime_matrix.example.json`). Credentials are named,
never written: `secret_env` maps a variable the runtime expects to the name of an
environment variable in the caller's shell, or to `file:PATH#NAME` for a `KEY=value`
line in an env file. Nothing here prints a credential.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import socket
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

from acp.connection import Connection
from acp.exceptions import RequestError

VIGIL = str(Path(sys.executable).parent / "vigil")
OWNER = "Ada"
STEP_TIMEOUT = 240


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def resolve_secret(ref: str) -> str:
    if ref.startswith("file:"):
        path, _, key = ref[5:].partition("#")
        for line in Path(path).expanduser().read_text().splitlines():
            name, _, value = line.strip().partition("=")
            if name == key:
                return value.strip().strip('"').strip("'")
        return ""
    return os.environ.get(ref, "")


def slack_dm(text: str) -> str:
    data = {
        "conversation": {"surface": "slack", "workspace": "TMATRIX", "channel": "D1", "thread": ""},
        "your_slack_user_id": "UBOT",
        "trigger": {"sender": "UOWNER", "text": text, "bot": False},
        "participants_from_slack": [{"id": "UOWNER", "name": OWNER, "is_bot": False}],
        "orientation": {"conversation_kind": "direct"},
    }
    return ("Slack conversation delivery. You remain the agent configured at this endpoint.\n"
            "You have been addressed directly or in a one-to-one direct message. Respond to the request; reply with the final text to send here.\n"
            "Latest conversation projection:\n" + json.dumps(data))


class Client:
    def __init__(self, home: Path):
        self.home = home
        self.text = ""
        self.permissions: list[str] = []
        self.refused = 0
        self.proc = None
        self.conn: Connection | None = None
        self.init: dict = {}

    async def _handler(self, method, params, is_notification):
        if method == "session/update":
            update = params.get("update") or {}
            content = update.get("content") or {}
            if update.get("sessionUpdate") == "agent_message_chunk" and content.get("type") == "text":
                self.text += content.get("text") or ""
            return None
        if method == "session/request_permission":
            call = params.get("toolCall") or {}
            self.permissions.append(json.dumps({"title": call.get("title"), "kind": call.get("kind"), "rawInput": call.get("rawInput")})[:240])
            options = params.get("options") or []
            # Approve only a request that names one of the mind's own tools, so its shape gets
            # recorded. Anything else (a shell command, a file write) is refused: this harness
            # runs unattended and must not let a model wander the machine.
            wanted = "allow_once" if "mind_" in json.dumps([call.get("title"), call.get("rawInput")]) else "reject_once"
            chosen = next((o for o in options if o.get("kind") == wanted), None)
            if wanted == "reject_once":
                self.refused += 1
            return {"outcome": {"outcome": "selected", "optionId": chosen["optionId"]}} if chosen else {"outcome": {"outcome": "cancelled"}}
        if is_notification:
            return None
        raise RequestError.method_not_found(method)

    async def attach(self) -> None:
        self.proc = await asyncio.create_subprocess_exec(VIGIL, "acp", "--home", str(self.home), stdin=asyncio.subprocess.PIPE,
                                                         stdout=asyncio.subprocess.PIPE, limit=32 * 1024 * 1024)
        self.conn = Connection(self._handler, self.proc.stdin, self.proc.stdout)
        self.init = await asyncio.wait_for(self.conn.send_request("initialize", {"protocolVersion": 1, "clientInfo": {"name": "matrix", "version": "0"}, "clientCapabilities": {}}), 90)
        if not ((self.init.get("_meta") or {}).get("dev.vigil") or {}).get("mind"):
            # With no daemon the bridge runs the engine alone, and everything after this
            # would be a check of the engine, not of Vigil.
            raise RuntimeError(f"The Vigil daemon for {self.home} did not answer; `vigil acp` ran the engine alone. Is the home's path short enough for its socket?")

    async def prompt(self, sid: str, text: str) -> tuple[str, str]:
        self.text = ""
        result = await asyncio.wait_for(self.conn.send_request("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": slack_dm(text)}]}), STEP_TIMEOUT)
        return self.text, (result or {}).get("stopReason", "")

    async def close(self) -> None:
        try:
            await self.conn.close()
        except Exception:
            pass
        try:
            self.proc.stdin.close()
            await asyncio.wait_for(self.proc.wait(), 10)
        except Exception:
            self.proc.kill()


def write_home(home: Path, entry: dict, port: int, engine_binary: str | None) -> None:
    home.mkdir(parents=True)
    workspace = home / "workspace"
    lines = [
        "schema = 1", "[agent]", 'name = "Matrix"', 'timezone = "UTC"', f'workspace = "{workspace}"',
        "[owner]", f'name = "{OWNER}"', 'os_user = "matrix"', 'identities = [{ surface = "slack:TMATRIX", handle = "UOWNER" }]',
        "[server]", f"mcp_port = {port}", "[runtime]", 'main = "subject"', "[mind]", 'dream_at = "off"', "triage = false",
    ]
    secrets_dir = home / "secrets"
    secrets_dir.mkdir(mode=0o700)
    env = dict(entry.get("env") or {})
    secret_env = {}
    for variable, ref in (entry.get("secret_env") or {}).items():
        value = resolve_secret(ref)
        if not value:
            raise RuntimeError(f"credential for {variable} is not available")
        name = "cred-" + variable.lower()
        path = secrets_dir / name
        path.write_text(value + "\n")
        path.chmod(0o600)
        secret_env[variable] = name
    if entry.get("engine"):
        command = ["${VIGIL_HOME}/engine/bin/agent-runtime", "--home", "${VIGIL_HOME}/engine/home", "acp"]
        kind = "native"
    else:
        command, kind = entry["command"], "acp"
    lines += ["[runtimes.subject]", f'kind = "{kind}"', "command = " + json.dumps(command)]
    if entry.get("auth_method"):
        lines.append(f'auth_method = {json.dumps(entry["auth_method"])}')
    if entry.get("mind_tools") is False:
        lines.append("mind_tools = false")
    if env:
        lines += ["[runtimes.subject.env]"] + [f"{k} = {json.dumps(v)}" for k, v in env.items()]
    if secret_env:
        lines += ["[runtimes.subject.secret_env]"] + [f"{k} = {json.dumps(v)}" for k, v in secret_env.items()]
    (home / "vigil.toml").write_text("\n".join(lines) + "\n")
    # Some agents need a file in their own home before they will use a client's tools
    # (Gemini CLI opens MCP servers only in a folder it trusts). `runtime_files` maps a
    # path under the runtime's private home to its content; {workspace} is substituted.
    for relative, content in (entry.get("runtime_files") or {}).items():
        target = home / "runtime-home" / "subject" / relative
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        target.write_text(content.replace("{workspace}", str(workspace)))


async def sh(*args: str) -> None:
    proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    out, _ = await proc.communicate()
    if proc.returncode:
        raise RuntimeError(out.decode()[-300:])


async def run_one(entry: dict, base: Path, engine_binary: str | None) -> dict:
    name = entry["name"]
    home = base / name
    result = {"name": name, "turn": "-", "brief": "-", "mind tool": "-", "recall": "-", "reload": "-", "notes": ""}
    daemon = None
    try:
        write_home(home, entry, free_port(), engine_binary)
        if entry.get("engine"):
            if not engine_binary:
                raise RuntimeError("no engine binary given (--engine-binary)")
            await sh(VIGIL, "--home", str(home), "engine", "install", "--source", engine_binary, "--allow-unpinned")
            await sh(VIGIL, "--home", str(home), "engine", "init", "--", *entry["engine"])
        log = open(home / "daemon.out", "wb")
        daemon = await asyncio.create_subprocess_exec(VIGIL, "--home", str(home), "serve", stdout=log, stderr=log,
                                                      env={**os.environ, "VIGIL_TRACE": "1"})  # message shapes land in logs/vigil.log
        for _ in range(100):
            if (home / "run" / "vigil.sock").exists():
                break
            await asyncio.sleep(0.1)

        first = Client(home)
        await first.attach()
        caps = first.init.get("agentCapabilities") or {}
        inner = ((first.init.get("_meta") or {}).get("dev.vigil") or {}).get("runtimeAgentInfo") or {}
        http_mcp = (caps.get("mcpCapabilities") or {}).get("http") is True and entry.get("mind_tools") is not False
        result["notes"] = f"{inner.get('name', '?')} {inner.get('version', '')}".strip() + ("" if http_mcp else "; without the mind tools")
        sid = (await asyncio.wait_for(first.conn.send_request("session/new", {"cwd": str(home / "workspace"), "mcpServers": []}), 120))["sessionId"]
        text, stop = await first.prompt(sid, "In one short sentence: who am I to you, and what kind of place is this conversation?")
        result["turn"] = "ok" if text.strip() and stop == "end_turn" else f"stop={stop or 'none'}"
        result["brief"] = "ok" if OWNER.lower() in text.lower() or "owner" in text.lower() else "no"
        await first.prompt(sid, "Please write this in your notebook so you never have to ask again: my favourite test colour is teal. Then confirm in a few words.")
        db = sqlite3.connect(home / "state.sqlite3")
        saved = db.execute("SELECT count(*) FROM notes WHERE lower(text) LIKE '%teal%'").fetchone()[0]
        db.close()
        result["mind tool"] = "ok" if saved else ("n/a" if not http_mcp else "no")
        if first.permissions:
            result["notes"] += "; asked permission: " + first.permissions[0]
        if first.refused:
            result["notes"] += f"; {first.refused} other permission request(s) refused"
        await first.close()

        second = Client(home)
        await second.attach()
        sid2 = (await asyncio.wait_for(second.conn.send_request("session/new", {"cwd": str(home / "workspace"), "mcpServers": []}), 120))["sessionId"]
        text, _ = await second.prompt(sid2, "What is my favourite test colour? Answer with the one word.")
        result["recall"] = "ok" if "teal" in text.lower() else ("n/a" if not saved else "no")
        await second.close()

        if caps.get("loadSession"):
            third = Client(home)
            await third.attach()
            try:
                await asyncio.wait_for(third.conn.send_request("session/load", {"sessionId": sid, "cwd": str(home / "workspace"), "mcpServers": []}), 120)
                text, stop = await third.prompt(sid, "Thanks. Reply with just the word: loaded")
                result["reload"] = "ok" if text.strip() else f"stop={stop}"
            except Exception as error:
                result["reload"] = f"failed: {str(error)[:60]}"
            await third.close()
        else:
            result["reload"] = "n/a"
    except Exception as error:
        result["notes"] = (result["notes"] + "; " if result["notes"] else "") + f"{type(error).__name__}: {str(error)[:160]}"
    finally:
        if daemon and daemon.returncode is None:
            daemon.terminate()
            try:
                await asyncio.wait_for(daemon.wait(), 25)
            except TimeoutError:
                daemon.kill()
    return result


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("matrix")
    parser.add_argument("--only", nargs="*")
    parser.add_argument("--engine-binary", help="an agent-runtime binary for entries that use the native engine")
    parser.add_argument("--keep", action="store_true", help="keep the throwaway homes for inspection")
    parser.add_argument("--out", help="also write the table to this file")
    args = parser.parse_args()
    entries = [e for e in json.loads(Path(args.matrix).read_text())["runtimes"] if not args.only or e["name"] in args.only]
    base = Path(tempfile.mkdtemp(prefix="vigil-matrix-"))
    rows = []
    for entry in entries:
        started = time.time()
        row = await run_one(entry, base, args.engine_binary)
        row["seconds"] = int(time.time() - started)
        rows.append(row)
        print(f"{row['name']}: turn={row['turn']} brief={row['brief']} mind={row['mind tool']} recall={row['recall']} reload={row['reload']} ({row['seconds']}s) {row['notes']}", flush=True)
    columns = ["name", "turn", "brief", "mind tool", "recall", "reload", "seconds", "notes"]
    table = ["| " + " | ".join(columns) + " |", "| " + " | ".join("---" for _ in columns) + " |"]
    table += ["| " + " | ".join(str(row[c]).replace("|", "/") for c in columns) + " |" for row in rows]
    print("\n" + "\n".join(table))
    if args.out:
        Path(args.out).write_text("\n".join(table) + "\n")
    if args.keep:
        print(f"\nhomes kept in {base}")
    else:
        shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
