"""The agent's read access to GitHub: who it acts as, which repositories it may read,
and where a private one may be shown. GitHub is a local stand-in here; `vigil github`
and a real turn are how the real service is checked."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

import aiohttp
import pytest
from aiohttp import web

from vigil import engine, github
from vigil.config import Config, ensure_layout
from vigil.mcp import ToolError
from vigil.store import Store
from vigil.surfaces import Scene

sys.path.insert(0, str(Path(__file__).parent))
from test_conductor import Client, free_port, make_home, run, slack  # noqa: E402,F401  (run is a fixture)

TOKEN = "ghx_not_a_real_token_0123456789"
ROOM = Scene(session="s-room", surface="slack", place="slack:T1:C1", place_label="general", audience="group")
OWNER = Scene(session="s-owner", surface="slack", place="slack:T1:D1", place_label="owner", audience="owner")


class Hub:
    """As much of GitHub's API as the tools use. It keeps every request's path and
    credential, so a test can say what was asked and as whom."""

    def __init__(self) -> None:
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.seen: list[tuple[str, str]] = []
        self.accept = {TOKEN}
        self.reject_next = 0
        self.private = {"acme/secret"}
        self.files = {"README.md": "# Readme\n"}
        self.comments = [{"user": {"login": "wren"}, "created_at": "2026-01-01T00:00:00Z", "body": "first"}]
        self.exchanges: list[str] = []
        self.queries: list[str] = []

    async def handle(self, request: web.Request) -> web.Response:
        path, auth = request.path, request.headers.get("Authorization", "")
        self.seen.append((path, auth))
        if path.startswith("/app/installations/"):
            self.exchanges.append(auth[7:])
            return web.json_response({"token": TOKEN, "expires_at": "2099-01-01T00:00:00Z"}, status=201)
        if self.reject_next:
            self.reject_next -= 1
            return web.Response(status=401)
        if auth[7:] not in self.accept:
            return web.Response(status=401)
        parts = path.strip("/").split("/")
        if path == "/user":
            return web.json_response({"login": "olive"})
        if path == "/installation/repositories":
            return web.json_response({"total_count": 2})
        if path == "/search/issues":
            # Like GitHub: an issue comes back when its repository is named in the query.
            self.queries.append(request.query["q"])
            named = {word[5:] for word in request.query["q"].split() if word.startswith("repo:")}
            return web.json_response({"items": [item for item in [
                {"repository_url": f"{self.url}/repos/acme/open", "number": 1, "title": "public one", "state": "open", "user": {"login": "wren"}, "html_url": "u1"},
                {"repository_url": f"{self.url}/repos/acme/secret", "number": 2, "title": "private one", "state": "open", "user": {"login": "wren"}, "html_url": "u2"},
                {"repository_url": f"{self.url}/repos/other/thing", "number": 3, "title": "not listed", "state": "open", "user": {"login": "wren"}, "html_url": "u3"}]
                if item["repository_url"].split("/repos/")[1] in named]})
        if path == "/search/repositories":
            # An owner with more repositories than one answer holds: only a search by
            # name finds the ones that matter.
            self.queries.append(request.query["q"])
            if "op in:name" not in request.query["q"] and "se in:name" not in request.query["q"] and " in:name" in request.query["q"]:
                return web.json_response({"items": []})
            return web.json_response({"items": [{"full_name": "acme/Open", "private": False}, {"full_name": "acme/secret", "private": True}, {"full_name": "acme/unrelated", "private": False}]})
        if parts[0] == "repos" and len(parts) >= 3:
            repo, rest = f"{parts[1]}/{parts[2]}", parts[3:]
            if parts[1] != "acme":
                return web.Response(status=404)
            if not rest:
                return web.json_response({"full_name": repo, "private": repo in self.private})
            if rest[0] == "issues" and len(rest) == 2:
                return web.json_response({"number": int(rest[1]), "title": "An issue", "state": "open", "user": {"login": "wren"}, "body": "the body", "comments": len(self.comments), "html_url": "u"})
            if rest[0] == "issues" and rest[-1] == "comments":
                page = int(request.query.get("page", 1))
                return web.json_response(self.comments[(page - 1) * 30:page * 30])
            if rest[0] == "contents":
                name = "/".join(rest[1:])
                if name not in self.files:
                    return web.Response(status=404)
                return web.json_response({"type": "file", "size": len(self.files[name]), "encoding": "base64", "content": base64.b64encode(self.files[name].encode()).decode()})
        return web.Response(status=404)

    async def __aenter__(self) -> Hub:
        app = web.Application()
        app.add_routes([web.route("*", "/{tail:.*}", self.handle)])
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        await web.TCPSite(self.runner, "127.0.0.1", self.port).start()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.runner.cleanup()

    def asked(self, fragment: str) -> list[str]:
        return [path for path, _ in self.seen if fragment in path]


def table(hub: Hub, **more) -> str:
    values = {"enabled": True, "identity": "token", "api": hub.url, "repos": ["acme/*"], **more}
    return "\n[github]\n" + "\n".join(f"{key} = {json.dumps(value)}" for key, value in values.items()) + "\n"


def client(tmp_path: Path, hub: Hub, http: aiohttp.ClientSession, **more) -> tuple[github.GitHub, Config, Store]:
    config = make_home(tmp_path, free_port(), table(hub, **more))
    ensure_layout(config.home)
    if more.get("identity", "token") == "token":
        config.write_secret("github-token", TOKEN)
    store = Store(config.db_path)
    return github.GitHub(config, store, lambda: http), config, store


def go(body):
    async def wrapped():
        async with Hub() as hub, aiohttp.ClientSession() as http:
            return await body(hub, http)
    return asyncio.run(wrapped())


async def refused(coroutine) -> str:
    with pytest.raises(ToolError) as caught:
        await coroutine
    return str(caught.value)


# ---- the three rules ------------------------------------------------------------------

def test_a_repository_the_owner_did_not_list_is_refused_without_asking_github(tmp_path):
    async def body(hub, http):
        hubgit, _, store = client(tmp_path, hub, http)
        said = await refused(hubgit.call(OWNER, "github_issue", {"repo": "other/thing", "number": 3}))
        assert "not one of the repositories" in said and hub.seen == []
        assert "owner/name" in await refused(hubgit.call(OWNER, "github_file", {"repo": "acme/open/../../other", "path": "x"}))
        assert hub.seen == []
        assert [row["status"] for row in store.q("SELECT status FROM events WHERE kind='github'")] == ["refused", "refused"]
    go(body)


def test_a_private_repository_is_read_with_the_owner_and_never_in_a_shared_room(tmp_path):
    async def body(hub, http):
        hubgit, _, _ = client(tmp_path, hub, http)
        said = await refused(hubgit.call(ROOM, "github_issue", {"repo": "acme/secret", "number": 2}))
        assert "private" in said and "Nothing from it was fetched" in said
        assert hub.asked("/issues") == [], "only the repository's visibility may be asked before the refusal"
        read = json.loads(await hubgit.call(OWNER, "github_issue", {"repo": "acme/secret", "number": 2}))
        assert read["title"] == "An issue" and read["comments"][0]["body"] == "first"
        assert json.loads(await hubgit.call(ROOM, "github_issue", {"repo": "acme/open", "number": 1}))["body"] == "the body"
    go(body)


def test_a_search_in_a_shared_room_leaves_out_private_and_unlisted_repositories(tmp_path):
    async def body(hub, http):
        hubgit, _, _ = client(tmp_path, hub, http)
        room = json.loads(await hubgit.call(ROOM, "github_search", {"query": "one"}))
        assert [hit["repo"] for hit in room["results"]] == ["acme/open"] and "left out" in room["note"]
        assert "private one" not in json.dumps(room) and "not listed" not in json.dumps(room)
        assert hub.queries[-1] == "one repo:acme/open repo:acme/unrelated", "GitHub is asked only about what this room may read"
        owner = json.loads(await hubgit.call(OWNER, "github_search", {"query": "one"}))
        assert [hit["repo"] for hit in owner["results"]] == ["acme/open", "acme/secret"] and "note" not in owner
        listed = json.loads(await hubgit.call(ROOM, "github_list", {"what": "repositories"}))
        assert listed["repositories"] == [{"repo": "acme/open", "private": False}, {"repo": "acme/unrelated", "private": False}]
    go(body)


def test_a_starred_name_is_looked_up_by_the_part_before_the_star(tmp_path):
    async def body(hub, http):
        hubgit, _, _ = client(tmp_path, hub, http, repos=["acme/op*", "acme/secret"])
        listed = json.loads(await hubgit.call(OWNER, "github_list", {"what": "repositories"}))
        assert listed["repositories"] == [{"repo": "acme/secret", "private": True}, {"repo": "acme/open", "private": False}]
        assert hub.queries == ["user:acme fork:true op in:name"], "asked once, by name, and kept"
        await hubgit.call(OWNER, "github_search", {"query": "one"})
        assert hub.queries[-1] == "one repo:acme/secret repo:acme/open" and len(hub.asked("/search/repositories")) == 1
        assert "not one of the repositories" in await refused(hubgit.call(OWNER, "github_issue", {"repo": "acme/unrelated", "number": 1}))
    go(body)


def test_the_owner_can_name_a_place_for_private_repositories_or_forbid_them_everywhere(tmp_path):
    async def body(hub, http):
        hubgit, _, _ = client(tmp_path / "a", hub, http, private_places=["slack:T1:C1"])
        assert json.loads(await hubgit.call(ROOM, "github_issue", {"repo": "acme/secret", "number": 2}))["title"] == "An issue"
        elsewhere = Scene(session="s", surface="slack", place="slack:T1:C2", audience="group")
        assert "private" in await refused(hubgit.call(elsewhere, "github_issue", {"repo": "acme/secret", "number": 2}))
        never, _, _ = client(tmp_path / "b", hub, http, private_repos="never")
        assert "private" in await refused(never.call(OWNER, "github_issue", {"repo": "acme/secret", "number": 2}))
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    go(body)


# ---- who the agent is on GitHub ---------------------------------------------------------

def test_the_gh_identity_asks_the_cli_and_the_token_goes_nowhere_else(tmp_path, caplog):
    counter = tmp_path / "gh-calls"
    fake = tmp_path / "gh"
    fake.write_text(f"#!/bin/sh\n[ \"$1 $2 $3 $4\" = \"auth token --hostname github.com\" ] || exit 2\necho x >> {counter}\necho {TOKEN}\n")
    fake.chmod(0o700)

    async def body(hub, http):
        hubgit, _, store = client(tmp_path, hub, http, identity="gh", gh=str(fake))
        with caplog.at_level(logging.DEBUG):
            first = await hubgit.call(ROOM, "github_issue", {"repo": "acme/open", "number": 1})
            second = await hubgit.call(ROOM, "github_file", {"repo": "acme/open", "path": "README.md"})
            assert counter.read_text().count("x") == 1, "the CLI is asked once and the answer kept for a while"
            hub.reject_next = 1  # the token was replaced: `gh auth refresh`, a new login
            third = await hubgit.call(ROOM, "github_issue", {"repo": "acme/open", "number": 1})
            assert counter.read_text().count("x") == 2 and json.loads(third)["title"] == "An issue"
        assert {auth for _, auth in hub.seen} == {"Bearer " + TOKEN}
        kept = json.dumps([dict(row) for row in store.q("SELECT * FROM events")]) + first + second + third + caplog.text
        assert TOKEN not in kept
        assert all(TOKEN not in path.read_text(errors="replace") for path in tmp_path.rglob("*") if path.is_file() and path != fake)
    go(body)


def test_an_identity_that_cannot_be_used_says_so_without_saying_why_to_the_room(tmp_path):
    async def body(hub, http):
        hubgit, config, _ = client(tmp_path, hub, http, identity="gh", gh=str(tmp_path / "no-such-gh"))
        said = await refused(hubgit.call(ROOM, "github_issue", {"repo": "acme/open", "number": 1}))
        assert "identity could not be used" in said and str(tmp_path) not in said
        assert any("no gh command" in problem for problem in github.problems(config))
    go(body)


def test_the_token_identity_reads_a_secret_only_its_owner_can_read(tmp_path):
    async def body(hub, http):
        hubgit, config, _ = client(tmp_path, hub, http)
        assert json.loads(await hubgit.call(ROOM, "github_issue", {"repo": "acme/open", "number": 1}))["title"] == "An issue"
        assert github.problems(config) == []
        os.chmod(config.secrets_dir / "github-token", 0o644)
        hubgit.identity._until = 0
        assert "identity could not be used" in await refused(hubgit.call(ROOM, "github_issue", {"repo": "acme/open", "number": 1}))
        assert any("readable by others" in problem for problem in github.problems(config))
    go(body)


@pytest.mark.skipif(not shutil.which("openssl"), reason="needs openssl to make and check a signature")
def test_the_app_identity_signs_a_request_that_verifies_and_trades_it_for_a_token(tmp_path):
    key, public = tmp_path / "app.pem", tmp_path / "app.pub"
    subprocess.run(["openssl", "genrsa", "-out", str(key), "2048"], check=True, capture_output=True)
    subprocess.run(["openssl", "rsa", "-in", str(key), "-pubout", "-out", str(public)], check=True, capture_output=True)

    async def body(hub, http):
        hubgit, config, _ = client(tmp_path, hub, http, identity="app", app_id=4242, installation_id=77)
        shutil.copy(key, config.secrets_dir / "github-app.pem")
        os.chmod(config.secrets_dir / "github-app.pem", 0o600)
        assert github.problems(config) == []
        for _ in range(2):
            assert json.loads(await hubgit.call(ROOM, "github_issue", {"repo": "acme/open", "number": 1}))["title"] == "An issue"
        assert len(hub.exchanges) == 1 and hub.asked("/app/installations/77/access_tokens"), "one exchange serves both calls"
        head, claims, signature = hub.exchanges[0].split(".")

        def decode(part: str) -> bytes:
            return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))

        assert json.loads(decode(head)) == {"alg": "RS256", "typ": "JWT"}
        payload = json.loads(decode(claims))
        assert payload["iss"] == "4242" and 0 < payload["exp"] - payload["iat"] <= 600
        (tmp_path / "sig").write_bytes(decode(signature))
        (tmp_path / "msg").write_bytes(f"{head}.{claims}".encode())
        check = subprocess.run(["openssl", "dgst", "-sha256", "-verify", str(public), "-signature", str(tmp_path / "sig"), str(tmp_path / "msg")], capture_output=True, text=True)
        assert check.returncode == 0, check.stdout + check.stderr
        report = await hubgit.check()
        assert report["accepted"] and "installation" in report["acts_as"] and TOKEN not in json.dumps(report)
    go(body)


def test_the_owner_can_see_who_the_agent_is_and_never_the_credential(tmp_path):
    async def body(hub, http):
        hubgit, _, _ = client(tmp_path, hub, http)
        report = await hubgit.check()
        assert report == {"identity": "token", "api": hub.url, "repositories": ["acme/*"], "acts_as": "olive", "accepted": True}
        hub.accept = set()
        hubgit.identity._until = 0
        report = await hubgit.check()
        assert report["accepted"] is False and "did not accept" in report["problem"] and TOKEN not in json.dumps(report)
    go(body)


def test_a_table_that_cannot_work_is_explained(tmp_path):
    (tmp_path / "vigil.toml").write_text('[agent]\nname="P"\n[github]\nenabled = true\nidentity = "pat"\nprivate_repos = "anywhere"\n')
    found = " ".join(github.problems(Config.load(tmp_path)))
    assert "must be one of gh, token, app" in found and "repos must list" in found and "private_repos must be" in found
    (tmp_path / "vigil.toml").write_text('[agent]\nname="P"\n')
    assert github.problems(Config.load(tmp_path)) == [] and not github.enabled(Config.load(tmp_path))


# ---- what the engine is given -----------------------------------------------------------

def test_a_long_file_comes_in_parts_that_the_engine_passes_on_whole(tmp_path):
    async def body(hub, http):
        hubgit, _, _ = client(tmp_path, hub, http)
        original = [f"line {n} " + "é" * 40 for n in range(3000)]
        hub.files["big.txt"] = "\n".join(original)
        got, start, calls = [], 1, 0
        while start:
            text = await hubgit.call(ROOM, "github_file", {"repo": "acme/open", "path": "big.txt", "start_line": start})
            assert len(text.encode()) <= github.LIMIT
            part = json.loads(text)
            got += part["text"].split("\n")
            start, calls = part.get("next_start_line"), calls + 1
        assert got == original and calls > 3
    go(body)


def test_many_long_comments_are_cut_at_a_whole_comment_and_say_so(tmp_path):
    async def body(hub, http):
        hubgit, _, _ = client(tmp_path, hub, http)
        hub.comments = [{"user": {"login": "wren"}, "created_at": "t", "body": f"{n} " + "word " * 700} for n in range(30)]
        text = await hubgit.call(ROOM, "github_issue", {"repo": "acme/open", "number": 1})
        result = json.loads(text)
        assert len(text.encode()) <= github.LIMIT and 0 < len(result["comments"]) < 30 and "did not fit" in result["cut"]
        assert all(comment["body"].startswith(f"{n} ") for n, comment in enumerate(result["comments"]))
    go(body)


def test_the_tool_definitions_are_fixed_text_that_configuration_cannot_change():
    assert [tool["name"] for tool in github.TOOLS] == ["github_search", "github_issue", "github_pull_request", "github_file", "github_list"]
    for tool in github.TOOLS:
        assert tool["inputSchema"]["required"] and tool["annotations"]["readOnlyHint"] is True
        assert tool["inputSchema"]["additionalProperties"] is False
    # A session's tool list is part of what the engine keeps its compacted history for.
    assert hashlib.sha256(json.dumps(github.TOOLS, sort_keys=True).encode()).hexdigest() == DEFINITIONS


#: Changing a definition is allowed, and is a decision: on a live agent every long
#: conversation resends its history once. Update this with the change, on purpose.
DEFINITIONS = "9dac6da6cab8470f62d87e8cc4ef545ab76623aeb37e5abca33319578ba3d953"


def test_the_engine_trusts_the_endpoint_only_while_github_is_on(tmp_path, monkeypatch):
    held = {"provider": {"kind": "mock", "model": "m"}, "limits": {}, "allowed_mcp_mutations": [], "trusted_mcp_readonly_urls": ["https://connector.example/context/mcp"]}
    written = []

    def fake_run(config, *args, stdin=None):
        if args[0] == "configure":
            written.append(json.loads(Path(args[-1]).read_text()))
        return ""

    monkeypatch.setattr(engine, "inspect", lambda config: {"revision": "r1", "config": json.loads(json.dumps(held))})
    monkeypatch.setattr(engine, "_run", fake_run)
    base = '[agent]\nname="P"\n[owner]\nname="Olive"\nos_user="olive"\n'

    def trusted(extra: str) -> list[str]:
        (tmp_path / "vigil.toml").write_text(base + extra)
        config = Config.load(tmp_path)
        engine.apply_settings(config)
        held["trusted_mcp_readonly_urls"] = written[-1]["trusted_mcp_readonly_urls"]
        return written[-1]["trusted_mcp_readonly_urls"], github.url(config), config.mind_url

    urls, hub, mind = trusted('[github]\nenabled = true\nrepos = ["acme/*"]\n')
    assert urls == ["https://connector.example/context/mcp", hub, mind]
    urls, hub, mind = trusted('[github]\nenabled = true\nrepos = ["acme/*"]\n')
    assert urls.count(hub) == 1, "applying twice does not list it twice"
    urls, hub, mind = trusted("")
    assert urls == ["https://connector.example/context/mcp", mind]
    assert not any(entry.startswith(hub + "#") for entry in written[-1]["allowed_mcp_mutations"]), "nothing on GitHub is pre-approved for writing"


# ---- in a running agent ------------------------------------------------------------------

def test_a_session_gets_the_tools_after_the_minds_and_a_game_session_does_not(run):  # noqa: F811
    async def scenario(config, daemon):
        user = Client()
        await user.attach(config)
        chat = {"type": "http", "name": "slack-context", "url": "http://127.0.0.1:9/mcp", "headers": []}
        forged = {"type": "http", "name": "github", "url": "http://127.0.0.1:9/elsewhere", "headers": []}
        room = (await user.conn.send_request("session/new", {"cwd": config.workspace, "mcpServers": [chat, forged]}))["sessionId"]
        game = (await user.conn.send_request("session/new", {"cwd": config.workspace, "mcpServers": [{"type": "http", "name": "minecraft", "url": "http://127.0.0.1:9/mcp", "headers": []}]}))["sessionId"]
        state = json.loads((config.home / "fake-state.json").read_text())
        assert state[room]["mcp"] == ["slack-context", "mind", "github"], "a client cannot supply its own server under that name"
        assert state[game]["mcp"] == ["minecraft", "mind"]

        token = next(token for token, session in daemon.mind.tokens.items() if session == room)
        async with aiohttp.ClientSession() as http:
            async def post(payload, bearer=token):
                async with http.post(github.url(config), json=payload, headers={"Authorization": "Bearer " + bearer}) as response:
                    return response.status, (await response.json() if response.status == 200 else None)

            assert (await post({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, bearer="guess"))[0] == 401
            status, listed = await post({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
            assert [tool["name"] for tool in listed["result"]["tools"]] == [tool["name"] for tool in github.TOOLS]
            _, early = await post({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "github_issue", "arguments": {"repo": "other/thing", "number": 1}}})
            assert early["result"]["isError"] and "no active turn" in early["result"]["content"][0]["text"]
            await user.conn.send_request("session/prompt", {"sessionId": room, "prompt": [{"type": "text", "text": slack("C1", "U2", "hello")}]})
            _, later = await post({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "github_issue", "arguments": {"repo": "other/thing", "number": 1}}})
            assert later["result"]["isError"] and "not one of the repositories" in later["result"]["content"][0]["text"]
        await user.close()

    run(scenario, '\n[github]\nenabled = true\nidentity = "token"\nrepos = ["acme/*"]\n')


def test_an_agent_without_the_table_has_no_such_server(run):  # noqa: F811
    async def scenario(config, daemon):
        user = Client()
        await user.attach(config)
        sid = (await user.conn.send_request("session/new", {"cwd": config.workspace, "mcpServers": []}))["sessionId"]
        assert json.loads((config.home / "fake-state.json").read_text())[sid]["mcp"] == ["mind"] and daemon.mind.github is None
        async with aiohttp.ClientSession() as http, http.post(github.url(config), json={}) as response:
            assert response.status in (404, 405)
        await user.close()

    run(scenario)
