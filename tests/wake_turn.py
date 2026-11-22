"""What does a real model do in a wake-up turn for a follow-up that is work?

    python tests/wake_turn.py LABEL ENGINE_BINARY KEYREF [RUNS [PLAN_FILE [AHEAD_FILE]]]

A throwaway Vigil home on the real native engine, a stand-in for the game connector's
tool server (observe, find, move, gather, chat, tell, wake) and a client that plays the
connector: it approves the game's and the mind's tools, and sends the connector's wake
prompt when Vigil's scheduler asks for one. The agent holds a plan for a search in the
world; the stand-in has what it looks for about 130 blocks away in every direction.

Prints one JSON line per run: the tools the model called in the wake-up turn, where it
ended, and what it holds afterwards, with the minutes until each is due. LABEL is copied
into each line, to tell two versions of a wording apart: check out one, run, check out
the other, run again. PLAN_FILE holds another follow-up to play in place of the search,
for instance one that waits for somebody's answer. AHEAD_FILE holds a second follow-up
for the same place that is not due for two hours, for instance an earlier plan the due
one has overtaken.
KEYREF names the model credential as `tests/runtime_matrix.py` does (an environment
variable, or `file:PATH#NAME`). Each run makes a handful of real model requests.

A stand-in world shows what a wording does to a fresh session, not what a live agent
with a long history will do. Both wordings of the wake line tried in 0.1.33 acted here
in every run; on the live agent the same model had just postponed a search twice.
"""

from __future__ import annotations

import asyncio
import json
import math
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import runtime_matrix as rm  # noqa: E402
from aiohttp import web  # noqa: E402

PLAN = ("Red mushrooms for the cook's stew: none within 48 blocks of the shop (checked this morning). Eight more stews are still needed; the barrel is at (-506,64,-553). "
        "Next: travel in bounded steps (about 60 blocks at a time) in one direction, running a red_mushroom find at each stop. "
        "Bring back what you find to the barrel and leave some to spread. The door is still waiting on its builder's design; leave it alone.")
AHEAD = ""
START = (-484, 64, -537)
PATCHES = [(-484 + dx, 64, -537 + dz) for dx, dz in ((-130, 0), (130, 0), (0, -130), (0, 130), (-128, 3), (127, -2), (2, -127), (-3, 128))]
OP = {"operation_id": {"type": "string", "description": "Durable ID for this intent. Use operation_status after uncertainty."}}
TOOLS = {
    "wake": ("Request one immediate agent wake on this saved game session. The agent owns timing. Reuse the same ID after an unknown outcome.",
             {"id": {"type": "string"}, "note": {"type": "string"}}, ["id", "note"]),
    "observe": ("Read the current position, visible players, nearby terrain, inventory and public conversation.", {"detail": {"type": "string", "enum": ["summary", "full"]}}, []),
    "inventory": ("Refresh inventory from the server, including after crafting or a held-item discrepancy.", {}, []),
    "find": ("Find up to twelve loaded blocks of an exact type near you.", {"block": {"type": "string"}, "radius": {"type": "integer", "minimum": 1, "maximum": 48}}, ["block"]),
    "move": ("Move to exact feet height at the destination FEET cell. reached verifies the chosen target only.",
             {"x": {"type": "integer"}, "y": {"type": "integer"}, "z": {"type": "integer"}, **OP}, ["x", "y", "z", "operation_id"]),
    "gather": ("Mine only explicitly selected targets, in order, up to count. Targets must be within 24 blocks at the start. Returns exact removed coordinates and inventory deltas.",
               {"block": {"type": "string"}, "count": {"type": "integer"}, "targets": {"type": "array", "items": {"type": "object"}}, **OP}, ["block", "targets", "operation_id"]),
    "chat": ("Speak in public game chat.", {"text": {"type": "string"}, **OP}, ["text", "operation_id"]),
    "tell": ("Address a connected player in public game chat.", {"player": {"type": "string"}, "text": {"type": "string"}, **OP}, ["player", "text", "operation_id"]),
}


class Game:
    """The connector's tool server, as far as this check needs it."""

    def __init__(self) -> None:
        self.port = rm.free_port()
        self.position = list(START)
        self.inventory: dict[str, int] = {}
        self.calls: list[tuple[str, dict]] = []
        self.woken: asyncio.Queue[str] = asyncio.Queue()
        self.server = {"type": "http", "name": "minecraft", "url": f"http://127.0.0.1:{self.port}/mcp", "headers": [{"name": "Authorization", "value": "Bearer game-token"}]}

    def call(self, name: str, a: dict):
        if name == "wake":
            self.woken.put_nowait(a["id"])
            return {"queued": True, "id": a["id"]}
        x, y, z = self.position
        if name == "observe":
            return {"username": "Tester", "ready": True, "busy": False, "stopped": False, "position": {"x": x + 0.5, "y": y, "z": z + 0.5},
                    "feet_block": {"x": x, "y": y, "z": z}, "health": 20, "food": 20, "time": 6200, "inventory": [{"name": k, "count": v} for k, v in self.inventory.items()],
                    "players": [], "terrain": "level grass and oak forest, walkable in every direction", "recent_public_chat": []}
        if name == "inventory":
            return {"inventory": [{"name": k, "count": v} for k, v in self.inventory.items()]}
        if name == "find":
            radius = min(48, int(a.get("radius") or 24))
            hits = [p for p in PATCHES if a.get("block") == "red_mushroom" and math.dist(p, self.position) <= radius]
            return [{"x": p[0], "y": p[1], "z": p[2]} for p in hits[:12]]
        if name == "move":
            self.position = [int(a["x"]), int(a["y"]), int(a["z"])]
            return {"reached": True, "feet_block": {"x": a["x"], "y": a["y"], "z": a["z"]}}
        if name == "gather":
            taken = []
            for target in a.get("targets") or []:
                spot = (target.get("x"), target.get("y"), target.get("z"))
                if spot in PATCHES and math.dist(spot, self.position) <= 24:
                    PATCHES.remove(spot)
                    taken.append(target)
            self.inventory["red_mushroom"] = self.inventory.get("red_mushroom", 0) + len(taken)
            return {"removed": taken, "inventory_delta": {"red_mushroom": len(taken)}}
        return {"transport_submitted": True}

    async def handle(self, request):
        body = await request.json()
        if request.headers.get("Authorization") != "Bearer game-token":
            return web.Response(status=401)
        method, rid = body.get("method"), body.get("id")
        if method == "initialize":
            return web.json_response({"jsonrpc": "2.0", "id": rid, "result": {"protocolVersion": body["params"]["protocolVersion"], "capabilities": {"tools": {}}, "serverInfo": {"name": "minecraft-player", "version": "0.2.0"}}})
        if method == "notifications/initialized":
            return web.Response(status=202)
        if method == "tools/list":
            tools = [{"name": name, "description": text, "inputSchema": {"type": "object", "properties": props, "required": need}} for name, (text, props, need) in TOOLS.items()]
            return web.json_response({"jsonrpc": "2.0", "id": rid, "result": {"tools": tools}})
        if method == "tools/call":
            name, args = body["params"]["name"], body["params"].get("arguments") or {}
            self.calls.append((name, args))
            try:
                return web.json_response({"jsonrpc": "2.0", "id": rid, "result": {"content": [{"type": "text", "text": json.dumps(self.call(name, args))}]}})
            except Exception as error:
                return web.json_response({"jsonrpc": "2.0", "id": rid, "result": {"isError": True, "content": [{"type": "text", "text": json.dumps({"error": str(error)})}]}})
        return web.json_response({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "Method not found"}})

    async def start(self):
        app = web.Application()
        app.add_routes([web.post("/mcp", self.handle)])
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        await web.TCPSite(self.runner, "127.0.0.1", self.port).start()


class Connector(rm.Client):
    """The client side: approves the game's and the mind's tools, refuses the rest."""

    async def _handler(self, method, params, is_notification):
        if method == "session/request_permission":
            call = params.get("toolCall") or {}
            seen = json.dumps([call.get("title"), call.get("rawInput"), call.get("_meta")])
            wanted = "allow_once" if ("minecraft" in seen or "mind_" in seen) else "reject_once"
            if wanted == "reject_once":
                self.refused += 1
            chosen = next((o for o in params.get("options") or [] if o.get("kind") == wanted), None)
            return {"outcome": {"outcome": "selected", "optionId": chosen["optionId"]}} if chosen else {"outcome": {"outcome": "cancelled"}}
        return await super()._handler(method, params, is_notification)


def permissions(sid: str) -> dict:
    return {"type": "text", "text": "Current Minecraft tool permissions:\n" + json.dumps({
        "policy_revision": "0" * 64, "session_id": sid, "active": True, "server": "minecraft", "allow_once_tools": sorted(set(TOOLS) - {"wake"}),
        "scope": "same connected ACP session and registered Minecraft tool; disabled during replay and maintenance"})}


async def one(label: str, engine: str, keyref: str, base: Path, number: int) -> dict:
    home = base / f"r{number}"
    entry = {"name": "wake", "engine": ["--provider", "anthropic", "--model", "claude-opus-5-5", "--key-env", "AGENT_MODEL_API_KEY", "--reasoning-effort", "medium"],
             "secret_env": {"AGENT_MODEL_API_KEY": keyref}}
    game = Game()
    await game.start()
    daemon = None
    out: dict = {"arm": label, "run": number}
    try:
        rm.write_home(home, entry, rm.free_port(), engine)
        await rm.sh(rm.VIGIL, "--home", str(home), "engine", "install", "--source", engine, "--allow-unpinned")
        await rm.sh(rm.VIGIL, "--home", str(home), "engine", "init", "--", *entry["engine"])
        log = open(home / "daemon.out", "wb")
        daemon = await asyncio.create_subprocess_exec(rm.VIGIL, "--home", str(home), "serve", stdout=log, stderr=log)
        for _ in range(100):
            if (home / "run" / "vigil.sock").exists():
                break
            await asyncio.sleep(0.1)
        client = Connector(home)
        await client.attach()
        sid = (await asyncio.wait_for(client.conn.send_request("session/new", {"cwd": str(home / "workspace"), "mcpServers": [game.server]}), 120))["sessionId"]
        line = {"id": "m1", "source": "human", "username": "Wren", "message": "back at the stove, still short on red mushrooms for the stew", "time": "2026-11-22T17:30:00.000Z", "server_attribution": None}
        await asyncio.wait_for(client.conn.send_request("session/prompt", {"sessionId": sid, "prompt": [
            permissions(sid), {"type": "text", "text": "Public Minecraft chat. To speak in the game use a Minecraft chat tool.\n" + json.dumps(line)}]}), 240)
        first = [name for name, _ in game.calls]
        game.calls.clear()
        game.position = list(START)
        db = sqlite3.connect(home / "state.sqlite3")
        place, place_label = db.execute("SELECT place, place_label FROM sessions WHERE id=?", (sid,)).fetchone()
        db.execute("INSERT INTO thoughts(ts, kind, text, place, place_label, audience, scope, due, state, origin_session, data) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                   (time.time() - 7200, "followup", PLAN, place, place_label, "group", "here", time.time() - 5, "pending", sid, "{}"))
        if AHEAD:
            db.execute("INSERT INTO thoughts(ts, kind, text, place, place_label, audience, scope, due, state, origin_session, data) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                       (time.time() - 10800, "followup", AHEAD, place, place_label, "group", "here", time.time() + 7200, "pending", sid, "{}"))
        db.commit()
        db.close()
        ident = await asyncio.wait_for(game.woken.get(), 90)  # Vigil's scheduler asks within twenty seconds
        game.calls.clear()
        request = {"id": ident, "source": "agent_wake", "note": "Something you are holding for this conversation is due.", "time": "2026-11-22T20:35:19.000Z"}
        started = time.time()
        result = await asyncio.wait_for(client.conn.send_request("session/prompt", {"sessionId": sid, "prompt": [
            permissions(sid), {"type": "text", "text": "Agent-requested wake. Nobody spoke in the game.\n" + json.dumps(request)}]}), 600)
        db = sqlite3.connect(home / "state.sqlite3")
        thoughts = db.execute("SELECT state, round((due - ?) / 60), substr(text,1,90) FROM thoughts ORDER BY id", (time.time(),)).fetchall()
        event = db.execute("SELECT result FROM events WHERE kind='prompt' ORDER BY id DESC LIMIT 1").fetchone()
        db.close()
        names = [name for name, _ in game.calls]
        out.update({"first_turn_tools": first, "wake_turn_tools": names, "moves": names.count("move"), "finds": names.count("find"), "gathers": names.count("gather"),
                    "speech": names.count("chat") + names.count("tell"), "ended_at": game.position, "mushrooms": game.inventory.get("red_mushroom", 0),
                    "seconds": round(time.time() - started), "stop": (result or {}).get("stopReason"), "thoughts": thoughts, "event": event[0] if event else None,
                    "refused": client.refused})
        await client.close()
    except Exception as error:
        out["error"] = f"{type(error).__name__}: {str(error)[:200]}"
    finally:
        if daemon and daemon.returncode is None:
            daemon.terminate()
            try:
                await asyncio.wait_for(daemon.wait(), 25)
            except TimeoutError:
                daemon.kill()
        await game.runner.cleanup()
        shutil.rmtree(home, ignore_errors=True)
    return out


async def main() -> None:
    label, engine, keyref = sys.argv[1], sys.argv[2], sys.argv[3]
    runs = int(sys.argv[4]) if len(sys.argv) > 4 else 1
    global PLAN, AHEAD
    if len(sys.argv) > 5:
        PLAN = Path(sys.argv[5]).read_text().strip()
    if len(sys.argv) > 6:
        AHEAD = Path(sys.argv[6]).read_text().strip()
    base = Path(tempfile.mkdtemp(prefix="wb-"))
    try:
        for number in range(1, runs + 1):
            global PATCHES
            PATCHES = [(-484 + dx, 64, -537 + dz) for dx, dz in ((-130, 0), (130, 0), (0, -130), (0, 130), (-128, 3), (127, -2), (2, -127), (-3, 128))]
            print(json.dumps(await one(label, engine, keyref, base, number)), flush=True)
    finally:
        shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
