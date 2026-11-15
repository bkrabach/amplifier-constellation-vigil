"""End to end: a client attached through the daemon to a scripted runtime."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest
from acp.connection import Connection

from vigil.brief import CLOSE, OPEN
from vigil.config import Config
from vigil.daemon import Daemon

HERE = Path(__file__).parent


def make_home(tmp_path: Path, port: int) -> Config:
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
""")
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
def run(tmp_path, unused_tcp_port_factory=None):
    def runner(scenario, port=47930):
        async def body():
            config = make_home(tmp_path, port)
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
