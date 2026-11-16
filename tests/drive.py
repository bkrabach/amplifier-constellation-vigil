"""Manual harness: act as an experience and talk to Vigil through its ACP bridge.

    python tests/drive.py --home HOME "hello" "what did I just say?"
    python tests/drive.py --home HOME --slack C123:U999:Dana "hi"      # a Slack channel turn
    python tests/drive.py --home HOME --session ID --load "again"       # reload a saved session
    python tests/drive.py --home HOME --mcp game=http://127.0.0.1:9000/mcp "look around"   # with an experience's tools

Each run is a fresh process, the way Slack and Conversations attach per turn.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shlex
import sys
import time
import tomllib
from pathlib import Path

from acp.connection import Connection

VIGIL = str(Path(sys.executable).parent / "vigil")


def slack_prompt(spec: str, text: str, mode: str) -> str:
    channel, user, name = (spec.split(":") + ["", ""])[:3]
    team = "TTEST"
    data = {
        "conversation": {"surface": "slack", "workspace": team, "channel": channel, "thread": "", "context": "ctx-" + channel},
        "your_slack_user_id": "UBOT",
        "history": [],
        "trigger": {"team": team, "channel": channel, "thread": "", "ts": str(time.time()), "sender": user, "bot": False, "text": text},
        "job_id": "job-1",
        "participants_from_slack": [{"id": user, "name": name or user, "is_bot": False}, {"id": "UBOT", "name": "Polaris", "is_bot": True}],
        "orientation": {"conversation_kind": "direct" if channel.startswith("D") else "group"},
    }
    instruction = {
        "direct": "You have been addressed directly or in a one-to-one direct message. Respond to the request; reply with the final text to send here.",
        "ambient": 'Read the room. Contribute only when useful and timely. Return exactly one final JSON object: {"decision":"speak","text":"..."} or {"decision":"pass","reason":"..."}.',
    }[mode]
    return f"Slack conversation delivery. You remain the agent configured at this endpoint.\n{instruction}\nLatest conversation projection:\n{json.dumps(data)}"


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", required=True)
    parser.add_argument("--slack")
    parser.add_argument("--mode", default="direct")
    parser.add_argument("--session")
    parser.add_argument("--load", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--command", help="attach command to run instead of `vigil acp`, e.g. an Instance's `agent-instance acp --home ...`")
    parser.add_argument("--cwd", help="session working folder (default: the workspace in vigil.toml)")
    parser.add_argument("--mcp", action="append", default=[], metavar="NAME=URL", help="an HTTP tool server to hand the session, as an experience does; repeatable")
    parser.add_argument("texts", nargs="+")
    args = parser.parse_args()
    workspace = args.cwd or tomllib.loads((Path(args.home) / "vigil.toml").read_text())["agent"]["workspace"].replace("${VIGIL_HOME}", args.home)
    command = shlex.split(args.command) if args.command else [VIGIL, "acp", "--home", args.home]
    proc = await asyncio.create_subprocess_exec(*command, stdin=asyncio.subprocess.PIPE,
                                                stdout=asyncio.subprocess.PIPE, limit=32 * 1024 * 1024)
    replaying = False

    async def handler(method, params, is_notification):
        if method == "session/update":
            update = params["update"]
            kind = update.get("sessionUpdate")
            if replaying:
                if kind == "user_message_chunk":
                    print(f"   [replayed user] {update['content'].get('text', '')[:160]!r}")
                return None
            if kind == "agent_message_chunk" and update["content"].get("type") == "text":
                print(update["content"]["text"], end="", flush=True)
            elif kind == "tool_call" and not args.quiet:
                print(f"\n   [tool] {update.get('title')} {json.dumps(update.get('rawInput'))[:200]}", flush=True)
            elif kind == "config_option_update" and not args.quiet:
                current = {o["id"]: o.get("currentValue") for o in update.get("configOptions", [])}
                print(f"\n   [config] {current}", flush=True)
            return None
        if method == "session/request_permission":
            print(f"\n   [permission asked] {json.dumps(params.get('toolCall', {}).get('rawInput'))[:200]}", flush=True)
            option = next(o for o in params["options"] if o["kind"] == "allow_once")
            return {"outcome": {"outcome": "selected", "optionId": option["optionId"]}}
        return None

    servers = [{"type": "http", "name": name, "url": url, "headers": []} for name, _, url in (spec.partition("=") for spec in args.mcp)]
    conn = Connection(handler, proc.stdin, proc.stdout)
    started = time.time()
    init = await conn.send_request("initialize", {"protocolVersion": 1, "clientInfo": {"name": "drive", "version": "0"}, "clientCapabilities": {}})
    if not args.quiet:
        print(f"[initialize {time.time() - started:.2f}s] agentInfo={init['agentInfo']} mcp={init['agentCapabilities'].get('mcpCapabilities')} meta={sorted((init.get('_meta') or {}).keys())}")
    if args.session:
        replaying = True
        method = "session/load" if args.load else "session/resume"
        result = await conn.send_request(method, {"sessionId": args.session, "cwd": workspace, "mcpServers": servers})
        replaying = False
        sid = args.session
    else:
        result = await conn.send_request("session/new", {"cwd": workspace, "mcpServers": servers})
        sid = result["sessionId"]
    if not args.quiet:
        options = {o["id"]: (o.get("currentValue"), o.get("category")) for o in result.get("configOptions") or []}
        print(f"[session {sid}] options={options}")
    for text in args.texts:
        body = slack_prompt(args.slack, text, args.mode) if args.slack else text
        print(f"\n>>> {text}")
        began = time.time()
        reply = await conn.send_request("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": body}]})
        print(f"\n<<< stop={reply.get('stopReason')} in {time.time() - began:.1f}s meta={json.dumps((reply.get('_meta') or {}).get('dev.vigil'))}")
    proc.stdin.close()
    await asyncio.wait_for(proc.wait(), 10)
    print(f"[session {sid}]")


if __name__ == "__main__":
    asyncio.run(main())
