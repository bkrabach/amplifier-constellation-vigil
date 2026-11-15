"""Minimal MCP over Streamable HTTP: the server that exposes the mind tools to a
runtime, and a small client for calling a tool an experience handed to the session.

Both sides speak plain JSON-RPC over POST. The server answers with JSON bodies and
keeps no per-connection state, so any runtime process may call it at any time. A
bearer token ties each call to one session, and through it to that session's audience.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any
from collections.abc import Awaitable, Callable

import aiohttp
from aiohttp import web

from . import __version__

log = logging.getLogger("vigil.mcp")
TRACE = os.environ.get("VIGIL_TRACE", "") not in ("", "0")

PROTOCOLS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")

#: (token, tool name, arguments) -> result text. Raises ToolError for a failed call.
ToolCaller = Callable[[str, str, dict[str, Any]], Awaitable[Any]]


class ToolError(Exception):
    pass


def _rpc(request_id: Any, result: Any = None, error: tuple[int, str] | None = None) -> dict[str, Any]:
    if error:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": error[0], "message": error[1]}}
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


class McpServer:
    def __init__(self, tools: list[dict[str, Any]], authorize: Callable[[str], bool], call: ToolCaller, instructions: str = ""):
        self.tools = tools
        self.authorize = authorize
        self.call = call
        self.instructions = instructions

    def routes(self) -> list[web.RouteDef]:
        return [web.post("/mcp", self.handle), web.get("/mcp", self.no_stream), web.delete("/mcp", self.no_stream)]

    async def no_stream(self, request: web.Request) -> web.Response:
        if TRACE:
            log.info("trace mcp %s /mcp -> 405", request.method)
        return web.Response(status=405, headers={"Allow": "POST"})

    async def handle(self, request: web.Request) -> web.Response:
        header = request.headers.get("Authorization", "")
        token = header[7:].strip() if header.lower().startswith("bearer ") else ""
        if not token or not self.authorize(token):
            if TRACE:
                log.info("trace mcp POST /mcp -> 401 (%s)", "no bearer token" if not token else "unknown token")
            return web.json_response(_rpc(None, error=(-32001, "Unknown or expired session token")), status=401)
        try:
            body = await request.json()
        except (ValueError, aiohttp.ContentTypeError):
            return web.json_response(_rpc(None, error=(-32700, "Parse error")), status=400)
        batch = body if isinstance(body, list) else [body]
        if TRACE:
            log.info("trace mcp POST /mcp %s", [f"{m.get('method')}:{(m.get('params') or {}).get('name', '')}" if isinstance(m, dict) else "?" for m in batch])
        replies = [reply for message in batch if (reply := await self._one(token, message)) is not None]
        if not replies:
            return web.Response(status=202)
        return web.json_response(replies if isinstance(body, list) else replies[0])

    async def _one(self, token: str, message: Any) -> dict[str, Any] | None:
        if not isinstance(message, dict) or "method" not in message:
            return None
        method, params, request_id = message["method"], message.get("params") or {}, message.get("id")
        if "id" not in message:
            return None  # a notification
        if method == "initialize":
            wanted = params.get("protocolVersion")
            return _rpc(request_id, {
                "protocolVersion": wanted if wanted in PROTOCOLS else PROTOCOLS[1],
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "mind", "title": "Vigil mind", "version": __version__},
                "instructions": self.instructions,
            })
        if method == "ping":
            return _rpc(request_id, {})
        if method == "tools/list":
            return _rpc(request_id, {"tools": self.tools})
        if method == "tools/call":
            name = params.get("name")
            if not any(tool["name"] == name for tool in self.tools):
                return _rpc(request_id, error=(-32602, f"Unknown tool: {name}"))
            arguments = params.get("arguments") or {}
            try:
                result = await self.call(token, name, arguments if isinstance(arguments, dict) else {})
                text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
                return _rpc(request_id, {"content": [{"type": "text", "text": text}], "isError": False})
            except ToolError as error:
                return _rpc(request_id, {"content": [{"type": "text", "text": str(error)}], "isError": True})
            except Exception as error:  # never leak a traceback into a model's context
                return _rpc(request_id, {"content": [{"type": "text", "text": f"The tool failed: {type(error).__name__}"}], "isError": True})
        return _rpc(request_id, error=(-32601, f"Method not found: {method}"))


async def call_tool(http: aiohttp.ClientSession, server: dict[str, Any], name: str, arguments: dict[str, Any], timeout: float = 6.0) -> dict[str, Any]:
    """Call one tool on an HTTP MCP server described the way ACP describes it in
    `mcpServers` (`{type:"http", url, headers:[{name,value}]}`)."""
    headers = {h["name"]: h["value"] for h in server.get("headers") or [] if isinstance(h, dict) and h.get("name")}
    headers.update({"Content-Type": "application/json", "Accept": "application/json, text/event-stream"})
    limit = aiohttp.ClientTimeout(total=timeout)

    async def post(payload: dict[str, Any]) -> Any:
        async with http.post(server["url"], json=payload, headers=headers, timeout=limit, allow_redirects=False) as response:
            session_id = response.headers.get("Mcp-Session-Id")
            if session_id:
                headers["Mcp-Session-Id"] = session_id
            if response.status == 202:
                return None
            text = await response.text()
            if response.status >= 300:
                raise RuntimeError(f"HTTP {response.status}")
            if "text/event-stream" in response.headers.get("Content-Type", ""):
                for line in reversed(text.splitlines()):
                    if line.startswith("data:"):
                        return json.loads(line[5:].strip())
                raise RuntimeError("empty event stream")
            return json.loads(text)

    init = await post({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": PROTOCOLS[1], "capabilities": {}, "clientInfo": {"name": "vigil", "version": __version__}}})
    if not isinstance(init, dict) or "result" not in init:
        raise RuntimeError("initialize failed")
    await post({"jsonrpc": "2.0", "method": "notifications/initialized"})
    reply = await post({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": name, "arguments": arguments}})
    if not isinstance(reply, dict) or "result" not in reply:
        raise RuntimeError(str((reply or {}).get("error") or "call failed")[:200])
    return reply["result"]
