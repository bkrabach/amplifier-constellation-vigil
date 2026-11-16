"""A scripted ACP agent standing in for a runtime in tests. It records what it was
sent and behaves according to keywords in the prompt, so tests can check exactly what
the conductor relays and in what order."""

from __future__ import annotations

import asyncio
import json
import os
import sys

from acp.connection import Connection

STATE = os.environ.get("FAKE_RUNTIME_STATE", "")
sessions: dict[str, dict] = {}
conn: Connection


def _options(effort: str) -> list[dict]:
    return [{"id": "reasoning_effort", "category": "thought_level", "currentValue": effort,
             "options": [{"value": "low"}, {"value": "medium"}, {"value": "high"}]}]


def _save() -> None:
    if STATE:
        with open(STATE, "w") as handle:
            json.dump(sessions, handle)


def _load() -> None:
    if STATE and os.path.exists(STATE):
        with open(STATE) as handle:
            sessions.update(json.load(handle))


async def _update(sid: str, update: dict) -> None:
    await conn.send_notification("session/update", {"sessionId": sid, "update": update})


async def handler(method: str, params, is_notification: bool):
    if method == "initialize":
        return {"protocolVersion": 1, "agentInfo": {"name": "fake-runtime", "version": "0"},
                "agentCapabilities": {"loadSession": True, "mcpCapabilities": {"http": True, "sse": False}},
                "_meta": {"io.github.bkrabach.agent-runtime": {"history": {"profile": "agent-runtime.history/1", "storeId": "x"}, "clientStdioMcpAllowed": False},
                          "steering": {"supported": True}}}
    if method == "session/new":
        sid = f"s{len(sessions) + 1}"
        sessions[sid] = {"mcp": [s["name"] for s in params.get("mcpServers", [])], "prompts": [], "effort": "medium"}
        _save()
        return {"sessionId": sid, "configOptions": _options("medium")}
    if method == "session/load":
        sid = params["sessionId"]
        sessions[sid]["mcp"] = [s["name"] for s in params.get("mcpServers", [])]
        for blocks in sessions[sid]["prompts"]:
            for block in blocks:
                await _update(sid, {"sessionUpdate": "user_message_chunk", "content": {"type": "text", "text": block["text"]}})
            await _update(sid, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "earlier reply"}})
        return {"configOptions": _options(sessions[sid]["effort"])}
    if method == "session/set_config_option":
        sessions[params["sessionId"]]["effort"] = params["value"]
        _save()
        return {"configOptions": _options(params["value"])}
    if method == "session/prompt":
        sid = params["sessionId"]
        blocks = params["prompt"]
        sessions[sid]["prompts"].append(blocks)
        _save()
        text = " ".join(b.get("text", "") for b in blocks[:-1]) if len(blocks) > 1 else blocks[0].get("text", "")
        for index in range(5):
            await _update(sid, {"sessionUpdate": "agent_message_chunk", "messageId": "m1", "content": {"type": "text", "text": f"part{index} "}})
        if "slow-turn" in text:
            await asyncio.sleep(1.2)
        if "ask-mind" in text:
            answer = await conn.send_request("session/request_permission", {
                "sessionId": sid, "toolCall": {"toolCallId": "t1", "rawInput": {"server": "mind", "tool": "mind_note", "arguments": {}}},
                "options": [{"optionId": "yes", "kind": "allow_once", "name": "Allow"}, {"optionId": "no", "kind": "reject_once", "name": "Reject"}]})
            await _update(sid, {"sessionUpdate": "agent_message_chunk", "messageId": "m1", "content": {"type": "text", "text": f"mind={answer['outcome'].get('optionId')} "}})
        if "ask-other" in text:
            answer = await conn.send_request("session/request_permission", {
                "sessionId": sid, "toolCall": {"toolCallId": "t2", "rawInput": {"server": "minecraft", "tool": "dig", "arguments": {}}},
                "options": [{"optionId": "yes", "kind": "allow_once", "name": "Allow"}, {"optionId": "no", "kind": "reject_once", "name": "Reject"}]})
            await _update(sid, {"sessionUpdate": "agent_message_chunk", "messageId": "m1", "content": {"type": "text", "text": f"other={answer['outcome'].get('optionId')} "}})
        if "ask-dressed" in text:
            await _update(sid, {"sessionUpdate": "tool_call", "toolCallId": "t3", "title": "workspace run", "kind": "execute", "status": "pending",
                                "rawInput": {"command": "sh", "args": ["-c", "true"], "server": "minecraft", "tool": "observe"}})
            answer = await conn.send_request("session/request_permission", {
                "sessionId": sid, "toolCall": {"toolCallId": "t3", "title": "workspace run", "kind": "other", "status": "pending",
                                               "rawInput": {"command": "sh", "args": ["-c", "true"], "server": "minecraft", "tool": "observe"}},
                "options": [{"optionId": "yes", "kind": "allow_once", "name": "Allow"}, {"optionId": "no", "kind": "reject_once", "name": "Reject"}]})
            await _update(sid, {"sessionUpdate": "agent_message_chunk", "messageId": "m1", "content": {"type": "text", "text": f"dressed={answer['outcome'].get('outcome')} "}})
        if "pass-json" in text:
            await _update(sid, {"sessionUpdate": "agent_message_chunk", "messageId": "m2", "content": {"type": "text", "text": '{"decision":"pass","reason":"nothing to add"}'}})
        await _update(sid, {"sessionUpdate": "agent_message_chunk", "messageId": "m1", "content": {"type": "text", "text": "done"}})
        return {"stopReason": "end_turn"}
    if method == "session/cancel" or is_notification:
        return None
    if method == "_session/steering":
        return {"outcome": "injected"}
    return {}


async def main() -> None:
    global conn
    _load()
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=32 * 1024 * 1024)
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
    transport, protocol = await loop.connect_write_pipe(asyncio.streams.FlowControlMixin, sys.stdout)
    writer = asyncio.StreamWriter(transport, protocol, reader, loop)
    conn = Connection(handler, writer, reader, listening=False)
    await conn.main_loop()


if __name__ == "__main__":
    asyncio.run(main())
