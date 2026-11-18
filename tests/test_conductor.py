"""End to end: a client attached through the daemon to a scripted runtime."""

from __future__ import annotations

import asyncio
import json
import socket
import sys
import time
from pathlib import Path

import pytest
from acp.connection import Connection

from vigil.brief import CLOSE, OPEN
from vigil.config import Config
from vigil.daemon import Daemon

HERE = Path(__file__).parent


def free_port() -> int:
    """A loopback port nothing holds right now. A fixed number collides with other
    programs' outgoing connections on a busy machine."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def make_home(tmp_path: Path, port: int, extra: str = "") -> Config:
    home = tmp_path / "home"
    home.mkdir()
    (home / "vigil.toml").write_text(f"""
schema = 1
[agent]
name = "Tester"
timezone = "UTC"
workspace = "{home}/workspace"
[owner]
name = "Olive"
os_user = "olive"
identities = [{{ surface = "slack:T1", handle = "UOWNER" }}]
[server]
mcp_port = {port}
[runtime]
main = "fake"
[runtimes.fake]
command = ["{sys.executable}", "{HERE / 'fake_runtime.py'}"]
[runtimes.fake.env]
FAKE_RUNTIME_STATE = "{home}/fake-state.json"
[mind]
dream_at = "off"
""" + extra)
    return Config.load(home)


class Client:
    def __init__(self) -> None:
        self.updates: list[dict] = []
        self.order: list[str] = []
        self.permissions: list[dict] = []
        self.conn: Connection | None = None
        self.writer = None

    async def handler(self, method, params, is_notification):
        if method == "session/update":
            self.updates.append(params["update"])
            self.order.append("update")
            return None
        if method == "session/request_permission":
            self.permissions.append(params)
            self.order.append("permission")
            return {"outcome": {"outcome": "selected", "optionId": "no"}}
        return None

    async def attach(self, config: Config) -> None:
        reader, writer = await asyncio.open_unix_connection(str(config.socket_path), limit=32 * 1024 * 1024)
        writer.write(b'{"vigil":"attach","pid":1}\n')
        self.writer = writer
        self.conn = Connection(self.handler, writer, reader)
        self.init = await self.conn.send_request("initialize", {"protocolVersion": 1, "clientCapabilities": {}})

    async def close(self) -> None:
        await self.conn.close()

    def text(self) -> str:
        return "".join(u["content"]["text"] for u in self.updates if u.get("sessionUpdate") == "agent_message_chunk")


@pytest.fixture
def run(tmp_path):
    def runner(scenario, extra=""):
        async def body():
            config = make_home(tmp_path, free_port(), extra)
            daemon = Daemon(config)
            task = asyncio.create_task(daemon.run())
            for _ in range(100):
                if config.socket_path.exists():
                    break
                await asyncio.sleep(0.02)
            try:
                return await scenario(config, daemon)
            finally:
                daemon._stop.set()
                await task
        return asyncio.run(body())
    return runner


def slack(channel: str, user: str, text: str, extra: dict | None = None, mode: str = "direct") -> str:
    data = {"conversation": {"surface": "slack", "workspace": "T1", "channel": channel, "thread": ""}, "your_slack_user_id": "UBOT",
            "trigger": {"sender": user, "text": text, "bot": False},
            "participants_from_slack": [{"id": user, "name": "N-" + user, "is_bot": False}], **(extra or {})}
    lead = "\nRead the room." if mode == "ambient" else ""
    return f"Slack conversation delivery. x{lead}\nLatest conversation projection:\n{json.dumps(data)}"


def test_relay_identity_brief_and_order(run):
    async def scenario(config, daemon):
        client = Client()
        await client.attach(config)
        assert client.init["agentInfo"] == {"name": "vigil", "title": "Tester", "version": client.init["agentInfo"]["version"]}
        meta = client.init["_meta"]
        assert "history" not in meta["io.github.bkrabach.agent-runtime"]
        assert meta["steering"] == {"supported": True}  # negotiated extensions pass through
        assert meta["dev.vigil"]["runtimeAgentInfo"]["name"] == "fake-runtime"
        new = await client.conn.send_request("session/new", {"cwd": config.workspace, "mcpServers": [{"type": "http", "name": "slack_conversations", "url": "http://127.0.0.1:1/x", "headers": []}]})
        sid = new["sessionId"]
        result = await client.conn.send_request("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": slack("C1", "U2", "hello there team")}]})
        assert result["stopReason"] == "end_turn"
        # every chunk reached the client, in order, before the prompt returned
        assert client.text() == "part0 part1 part2 part3 part4 done"
        state = json.loads((config.home / "fake-state.json").read_text())[sid]
        assert state["mcp"] == ["slack_conversations", "mind"]
        blocks = state["prompts"][0]
        assert len(blocks) == 2 and blocks[0]["text"].startswith("Slack conversation delivery.")
        assert blocks[1]["text"].startswith(OPEN) and blocks[1]["text"].rstrip().endswith(CLOSE)
        assert blocks[1]["_meta"]["com.microsoft.amplifier.context-snapshot"]["key"] == "vigil:presence-brief"
        assert "a group" in blocks[1]["text"] and "N-U2" in blocks[1]["text"]
        row = daemon.store.one("SELECT * FROM journal")
        assert row["heard"] == "hello there team" and row["said"] == "part0 part1 part2 part3 part4 done" and row["audience"] == "group"
        await client.close()
    run(scenario)


def test_a_runtime_that_never_opens_the_tools_is_told_so(run):
    async def scenario(config, daemon):
        from vigil.mind import TOOLS_MISSING

        client = Client()
        await client.attach(config)
        briefs = {}
        for name in ("silent", "opened"):
            sid = (await client.conn.send_request("session/new", {"cwd": config.workspace, "mcpServers": []}))["sessionId"]
            if name == "opened":  # a working runtime lists the tools before its first reply
                token = next(t for t, s in daemon.mind.tokens.items() if s == sid)
                assert daemon.mind.valid_token(token)
            for text in ("hello", "please remember that I like tea"):
                await client.conn.send_request("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": slack("C1", "U2", text)}]})
            state = json.loads((config.home / "fake-state.json").read_text())[sid]
            briefs[name] = [blocks[-1]["text"] for blocks in state["prompts"]]
        # The scripted runtime accepts the server and never calls it. One turn is given the
        # benefit of the doubt; after that the model is told, so it does not hunt for a notebook.
        assert TOOLS_MISSING not in briefs["silent"][0] and TOOLS_MISSING in briefs["silent"][1]
        assert all(TOOLS_MISSING not in text for text in briefs["opened"])
        assert daemon.store.one("SELECT count(*) AS n FROM sessions WHERE turns>=1 AND tools_seen IS NULL")["n"] == 1
        await client.close()
    run(scenario)


def test_a_restart_waits_for_a_running_turn(run):
    async def scenario(config, daemon):
        client = Client()
        await client.attach(config)
        sid = (await client.conn.send_request("session/new", {"cwd": config.workspace, "mcpServers": []}))["sessionId"]
        turn = asyncio.create_task(client.conn.send_request("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": slack("C1", "U2", "slow-turn please")}]}))
        await asyncio.sleep(0.3)
        assert daemon.mind.turns  # the turn is in flight
        daemon._stop.set()  # what a stop signal does
        result = await asyncio.wait_for(turn, 10)
        # the turn finished and reached the client before the daemon closed the connection
        assert result["stopReason"] == "end_turn" and client.text().endswith("done")
        assert daemon.store.one("SELECT status FROM events ORDER BY id DESC LIMIT 1")["status"] == "done"
    run(scenario)


def test_permissions_own_tools_only(run):
    async def scenario(config, daemon):
        client = Client()
        await client.attach(config)
        sid = (await client.conn.send_request("session/new", {"cwd": config.workspace, "mcpServers": []}))["sessionId"]
        await client.conn.send_request("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": "ask-mind ask-other"}]})
        # the mind's own tool was approved without the client; the other request went to the client
        assert "mind=yes" in client.text() and "other=no" in client.text()
        assert len(client.permissions) == 1 and client.permissions[0]["toolCall"]["rawInput"]["server"] == "minecraft"
        # the permission request arrived after the chunks that preceded it
        assert client.order.index("permission") >= 5
        await client.close()
    run(scenario)


def test_a_request_dressed_as_a_connector_tool_never_reaches_the_client(run):
    async def scenario(config, daemon):
        client = Client()
        await client.attach(config)
        sid = (await client.conn.send_request("session/new", {"cwd": config.workspace, "mcpServers": []}))["sessionId"]
        await client.conn.send_request("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": "ask-dressed ask-other"}]})
        # A command whose own arguments say server=minecraft, tool=observe is cancelled here.
        # The genuine envelope for another server's tool still goes to the client to decide.
        assert "dressed=cancelled" in client.text() and "other=no" in client.text()
        assert [p["toolCall"]["toolCallId"] for p in client.permissions] == ["t2"]
        await client.close()
    run(scenario)


def test_reload_hides_brief_and_pass_is_silence(run):
    async def scenario(config, daemon):
        first = Client()
        await first.attach(config)
        sid = (await first.conn.send_request("session/new", {"cwd": config.workspace, "mcpServers": []}))["sessionId"]
        await first.conn.send_request("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": slack("C1", "U2", "pass-json please", mode="ambient")}]})
        await first.close()
        assert daemon.store.one("SELECT said, mode FROM journal")["said"] == ""  # a pass is recorded as silence
        second = Client()  # a new process attaching, as Slack does for every turn
        await second.attach(config)
        await second.conn.send_request("session/load", {"sessionId": sid, "cwd": config.workspace, "mcpServers": []})
        replayed = [u["content"]["text"] for u in second.updates if u["sessionUpdate"] == "user_message_chunk"]
        assert len(replayed) == 1 and replayed[0].startswith("Slack conversation delivery.")
        assert not any(OPEN in text or "working memory" in text for text in replayed)
        await second.close()
    run(scenario)


def test_owner_private_memory_stays_out_of_groups(run):
    async def scenario(config, daemon):
        from vigil import surfaces
        owner = surfaces.read("a", [{"type": "text", "text": slack("D9", "UOWNER", "secret plan")}])
        daemon.memory.settle(owner)
        assert owner.audience == "owner"
        daemon.memory.note_add(owner, "plan", "The launch is on the 3rd.", "here")
        daemon.memory.note_add(owner, "person:Olive", "Olive likes short answers.", "anywhere")
        client = Client()
        await client.attach(config)
        sid = (await client.conn.send_request("session/new", {"cwd": config.workspace, "mcpServers": []}))["sessionId"]
        await client.conn.send_request("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": slack("C1", "U2", "when is the launch? do you remember?")}]})
        brief = json.loads((config.home / "fake-state.json").read_text())[sid]["prompts"][0][-1]["text"]
        assert "launch is on the 3rd" not in brief
        token = next(t for t, s in daemon.mind.tokens.items() if s == sid)
        daemon.mind.scenes[sid] = surfaces.read(sid, [{"type": "text", "text": slack("C1", "U2", "x")}])
        daemon.memory.settle(daemon.mind.scenes[sid])
        found = await daemon.mind.call(token, "mind_recall", {"query": "launch short answers"})
        assert "short answers" in found and "3rd" not in found
        await client.close()
    run(scenario)


def test_quiet_wake_costs_no_model_call(run):
    async def scenario(config, daemon):
        client = Client()
        await client.attach(config)
        sid = (await client.conn.send_request("session/new", {"cwd": config.workspace, "mcpServers": []}))["sessionId"]
        wake = {"accepted_work_continuation": {"goal": "[vigil:wake:abc123ef01] Return to this conversation when something is due."}}
        result = await client.conn.send_request("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": slack("D9", "UOWNER", "remind me", wake)}]})
        assert result == {"stopReason": "end_turn", "_meta": {"dev.vigil": {"wake": "quiet"}}}
        assert client.updates == []
        assert json.loads((config.home / "fake-state.json").read_text())[sid]["prompts"] == []  # the runtime never ran
        # once something is due, the same wake-up reaches the model with the thought in its brief
        scene = daemon.mind.scenes[sid]
        daemon.memory.thought_add(scene, "followup", "Ask whether the deploy finished.", 0)
        await client.conn.send_request("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": slack("D9", "UOWNER", "remind me", wake)}]})
        brief = json.loads((config.home / "fake-state.json").read_text())[sid]["prompts"][0][-1]["text"]
        assert "Ask whether the deploy finished." in brief and "Deliver it now" in brief
        assert daemon.store.one("SELECT state FROM thoughts")["state"] == "delivered"
        await client.close()
    run(scenario)


def test_spoken_game_request_goes_straight_to_the_game(run):
    """A voice remark about the game is answered by the agent's presence in the game:
    its body receives the caller's exact words, and the call session's model never runs."""
    from aiohttp import web

    seen: dict = {}
    bridge_port = free_port()

    async def task(request):
        seen["task"] = await request.json()
        seen["auth"] = request.headers.get("Authorization")
        return web.json_response({"queued": True, "request_id": "q1", "source_request_id": seen["task"]["source"]["request_id"]})

    async def outcomes(request):
        seen["query"] = await request.json()
        return web.json_response({"version": 1, "outcomes": [{"delivery": "admitted", "source_replies": [{"id": "r1", "text": "Coming over with the torches now."}]}]})

    async def scenario(config, daemon):
        config.write_secret("mc-caller", "t" * 40)
        app = web.Application()
        app.add_routes([web.post("/task", task), web.post("/source-outcomes", outcomes)])
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", bridge_port).start()
        try:
            client = Client()
            await client.attach(config)
            sid = (await client.conn.send_request("session/new", {"cwd": config.workspace, "mcpServers": []}))["sessionId"]
            call = ("The live voice conversation delegated help at 900ms. You are the same agent.\n\nThis new request's delegation_id is d1.\n\n"
                    "Current human source (exact):\ncan you bring me some torches, I am down in the cave\n\nRecent conversation, for references: none")
            meta = {"com.microsoft.amplifier.conversations": {"conversationId": "call-1", "author": "olive", "messageId": "m"}}
            result = await client.conn.send_request("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": call}], "_meta": meta})
            assert result == {"stopReason": "end_turn", "_meta": {"dev.vigil": {"routed": "minecraft"}}}
            assert client.text() == "Coming over with the torches now."
            assert seen["task"]["text"] == "can you bring me some torches, I am down in the cave"  # the caller's words, unchanged
            assert seen["task"]["source"]["actor"] == {"id": "olive", "label": "Olive"} and seen["auth"] == "Bearer " + "t" * 40
            assert json.loads((config.home / "fake-state.json").read_text())[sid]["prompts"] == []
            # a remark that is not about the game takes the ordinary path
            other = call.replace("can you bring me some torches, I am down in the cave", "what is on my calendar tomorrow")
            await client.conn.send_request("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": other}], "_meta": meta})
            assert len(json.loads((config.home / "fake-state.json").read_text())[sid]["prompts"]) == 1
            await client.close()
        finally:
            await runner.cleanup()

    run(scenario, extra=f'''
[bridges.minecraft]
url = "http://127.0.0.1:{bridge_port}"
secret = "mc-caller"
wait_seconds = 5
''')


def test_spoken_game_request_says_so_when_the_game_presence_is_down(run):
    """Nothing listens where the game presence should be. The call session answers, and
    its brief says the words were not passed on, so the caller is not told it is under way."""
    async def scenario(config, daemon):
        config.write_secret("mc-caller", "t" * 40)
        client = Client()
        await client.attach(config)
        sid = (await client.conn.send_request("session/new", {"cwd": config.workspace, "mcpServers": []}))["sessionId"]
        call = ("The live voice conversation delegated help at 900ms. You are the same agent.\n\nThis new request's delegation_id is d1.\n\n"
                "Current human source (exact):\ncan you bring me some torches, I am down in the cave\n\nRecent conversation, for references: none")
        meta = {"com.microsoft.amplifier.conversations": {"conversationId": "call-1", "author": "olive", "messageId": "m"}}
        result = await client.conn.send_request("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": call}], "_meta": meta})
        assert result["stopReason"] == "end_turn" and "routed" not in json.dumps(result)
        prompts = json.loads((config.home / "fake-state.json").read_text())[sid]["prompts"]
        assert len(prompts) == 1
        brief = prompts[0][-1]["text"]
        assert "could not be reached just now, so nothing was passed to it" in brief and "the Minecraft world" in brief
        await client.close()

    run(scenario, extra=f'''
[bridges.minecraft]
url = "http://127.0.0.1:{free_port()}"
secret = "mc-caller"
wait_seconds = 5
''')


# ---- coming back to a game conversation ------------------------------------------------

class GameTools:
    """The game connector's tool server as far as Vigil uses it: it lists its tools and
    keeps one reminder. `offer=False` is a connector from before `follow_up` existed."""

    def __init__(self, offer: bool = True) -> None:
        self.offer, self.refuse, self.calls, self.listed = offer, False, [], 0
        self.port = free_port()
        self.server = {"type": "http", "name": "minecraft", "url": f"http://127.0.0.1:{self.port}/mcp",
                       "headers": [{"name": "Authorization", "value": "Bearer game-token"}]}

    async def handle(self, request):
        from aiohttp import web

        body = await request.json()
        assert request.headers.get("Authorization") == "Bearer game-token"
        method = body.get("method")

        def result(value):
            return web.json_response({"jsonrpc": "2.0", "id": body.get("id"), "result": value})

        if method == "initialize":
            return result({"protocolVersion": body["params"]["protocolVersion"], "capabilities": {"tools": {}}, "serverInfo": {"name": "minecraft", "version": "0"}})
        if method == "notifications/initialized":
            return web.Response(status=202)
        if method == "tools/list":
            self.listed += 1
            return result({"tools": [{"name": name, "inputSchema": {"type": "object"}} for name in ["chat", "tell"] + (["follow_up"] if self.offer else [])]})
        self.calls.append(body["params"])
        if self.refuse:
            return result({"content": [{"type": "text", "text": "Follow-up unavailable: explicit_stop"}], "isError": True})
        return result({"content": [{"type": "text", "text": json.dumps({"id": "r1", "status": "scheduled"})}]})

    async def start(self):
        from aiohttp import web

        app = web.Application()
        app.add_routes([web.post("/mcp", self.handle)])
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        await web.TCPSite(self.runner, "127.0.0.1", self.port).start()

    async def stop(self):
        await self.runner.cleanup()


def game(messages: list[dict], world: dict | None = None) -> list[dict]:
    data = {"messages": messages, "world": {"username": "Tester", "players": ["Tester", "Wren"], **(world or {})}}
    return [{"type": "text", "text": "Current adapter permission policy for this turn:\n{}"}, {"type": "text", "text": "play.\n" + json.dumps(data)}]


def chat(number: int, text: str) -> list[dict]:
    return game([{"id": f"c{number}", "username": "Wren", "message": text, "source": "human"}])


def reminder(goal: str, world: dict | None = None) -> list[dict]:
    """The turn the connector starts when the agent's reminder comes due."""
    return game([{"id": "f1", "source": "follow_up", "message": goal, "reply_scope": "follow_up:f1"}], {"follow_up": True, "conversation_only": True, **(world or {})})


def test_the_game_gives_the_agent_a_turn_for_what_it_holds_there(run):
    """A live agent held work for a game world and had no turn there for a day: the game
    starts a turn only for chat, and Vigil could ask only Slack to bring it back."""
    async def scenario(config, daemon):
        tools = GameTools()
        await tools.start()
        client = Client()
        await client.attach(config)
        state = config.home / "fake-state.json"
        sid = (await client.conn.send_request("session/new", {"cwd": config.workspace, "mcpServers": [tools.server]}))["sessionId"]

        async def prompt(blocks):
            return await client.conn.send_request("session/prompt", {"sessionId": sid, "prompt": blocks})

        def model_turns():
            return len(json.loads(state.read_text())[sid]["prompts"])

        await prompt(chat(1, "hello there"))
        assert tools.calls == [] and tools.listed == 1  # nothing is held, so nothing is set
        scene = daemon.mind.scenes[sid]
        # Something held for this place, due in ten minutes: the end of the next turn sets the game's reminder.
        daemon.memory.thought_add(scene, "followup", "Ask Wren whether the door is wired.", time.time() + 600)
        await prompt(chat(2, "anyone seen the iron?"))
        assert [call["name"] for call in tools.calls] == ["follow_up"]
        first = tools.calls[0]["arguments"]
        assert 595 <= first["after_seconds"] <= 605 and "[vigil:wake:" in first["goal"]
        await prompt(chat(3, "found some"))
        assert len(tools.calls) == 1 and tools.listed == 1  # one reminder is enough, and the connector is asked for its tools once

        # It comes due with nothing ready yet: answered without the model, and set again.
        turns = model_turns()
        assert await prompt(reminder(first["goal"])) == {"stopReason": "end_turn", "_meta": {"dev.vigil": {"wake": "quiet"}}}
        assert model_turns() == turns and len(tools.calls) == 2

        # Now it is due. The model gets the turn with the thought in its brief, and ends it without a word.
        daemon.store.run("UPDATE thoughts SET due=0")
        await prompt(reminder(tools.calls[1]["arguments"]["goal"], {"note": "stay-silent"}))
        brief = json.loads(state.read_text())[sid]["prompts"][-1][-1]["text"]
        assert "Ask Wren whether the door is wired." in brief and "Deliver it now" in brief and "chat or tell" in brief
        assert client.updates[-1]["content"]["text"] == "done"  # the silent turn sent nothing
        # It chose not to raise it. Held open, the thought would wake the conversation again in seconds.
        assert daemon.store.one("SELECT state FROM thoughts")["state"] == "delivered"
        assert len(tools.calls) == 2

        # A refusal from the connector is not a reminder: it is asked again at the next turn's end.
        tools.refuse = True
        daemon.memory.thought_add(scene, "followup", "Ask about the roof.", time.time() + 600)
        await prompt(chat(4, "roof next?"))
        assert len(tools.calls) == 3 and float(daemon.store.meta_get(f"wake:{scene.place}") or 0) == 0
        tools.refuse = False
        await prompt(chat(5, "ok"))
        assert len(tools.calls) == 4 and float(daemon.store.meta_get(f"wake:{scene.place}")) > 0

        # The agent sets a reminder of its own with the game's tool. The connector keeps one,
        # so setting ours would replace it and lose its goal: none is set until it has come due.
        daemon.memory.thought_add(scene, "followup", "Ask about the windows.", time.time() + 120)
        await prompt(game([{"id": "c6", "username": "Wren", "message": "remind yourself", "source": "human"}], {"note": "set-own-reminder"}))
        await prompt(chat(7, "and again"))
        assert len(tools.calls) == 4
        await prompt(reminder("check on the door"))  # its own reminder: an ordinary turn, after which ours is set
        assert len(tools.calls) == 5 and 100 <= tools.calls[4]["arguments"]["after_seconds"] <= 125

        # A private turn sets none: the game's reminder belongs to the public conversation.
        daemon.store.meta_set(f"wake:{scene.place}", "0")
        await prompt(game([{"id": "w1", "username": "Wren", "message": "psst", "source": "human", "whisper": True}]))
        assert len(tools.calls) == 5
        await client.close()
        await tools.stop()
    run(scenario)


def test_work_the_owner_sends_to_the_game_gets_a_turn_there(run):
    async def scenario(config, daemon):
        tools, old = GameTools(), GameTools(offer=False)
        await tools.start()
        await old.start()
        client = Client()  # one client: the scripted runtime numbers sessions per process
        await client.attach(config)
        state = config.home / "fake-state.json"

        async def session(servers, blocks):
            sid = (await client.conn.send_request("session/new", {"cwd": config.workspace, "mcpServers": servers}))["sessionId"]
            await client.conn.send_request("session/prompt", {"sessionId": sid, "prompt": blocks})
            return sid

        game_sid = await session([tools.server], chat(1, "hello there"))
        owner_sid = await session([], [{"type": "text", "text": slack("D9", "UOWNER", "a job for you")}])
        token = next(token for token, sid in daemon.mind.tokens.items() if sid == owner_sid)

        said = await daemon.mind.call(token, "mind_follow_up", {"about": "Direct the door work and tell me how it went.", "where": "minecraft world"})
        assert "will give you a turn for it when it is due, without waiting for someone to speak there" in said and "cannot wake" not in said
        assert [call["name"] for call in tools.calls] == ["follow_up"] and tools.calls[0]["arguments"]["after_seconds"] == 5

        # The game starts that turn. The work is in its brief, and seeing it once does not set another reminder.
        await client.conn.send_request("session/prompt", {"sessionId": game_sid, "prompt": reminder(tools.calls[0]["arguments"]["goal"], {"note": "stay-silent"})})
        brief = json.loads(state.read_text())[game_sid]["prompts"][-1][-1]["text"]
        assert "Direct the door work and tell me how it went." in brief and "Your owner asked this of you in private" in brief
        work = daemon.store.one("SELECT state, delivered FROM thoughts")
        assert work["state"] == "pending" and work["delivered"]  # seen, and open until the agent closes it
        assert len(tools.calls) == 1

        # Sent again while the connector refuses (the conversation is stopped): the answer says so.
        tools.refuse = True
        said = await daemon.mind.call(token, "mind_follow_up", {"about": "Check the roof too.", "where": "minecraft world"})
        assert "could not be asked for a turn just now" in said and len(tools.calls) == 2

        # A connector from before the tool existed is asked for its tools, not for a reminder.
        other_sid = await session([old.server], chat(7, "hi"))
        scene = daemon.mind.scenes[other_sid]
        daemon.memory.thought_add(scene, "followup", "Ask about the iron.", time.time() + 600)
        await client.conn.send_request("session/prompt", {"sessionId": other_sid, "prompt": chat(8, "hello again")})
        assert old.listed == 1 and old.calls == []
        await client.close()
        await tools.stop()
        await old.stop()
    run(scenario)
