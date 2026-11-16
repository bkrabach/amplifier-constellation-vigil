"""Command line for Vigil. `vigil acp` is the hot path and imports nothing heavy."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_HOME = os.environ.get("VIGIL_HOME") or os.path.expanduser("~/.local/share/vigil/default")

TEMPLATE = """\
schema = 1

[agent]
name = {name}
timezone = {timezone}
# The runtime's working folder. When an Agent Instance hosts Vigil this must be the
# Instance's own workspace, because experiences open sessions there.
workspace = {workspace}

[owner]
name = {owner_name}
os_user = {os_user}
# Accounts that are the owner, one per surface. Slack ids differ per workspace.
identities = [{identities}]

[server]
mcp_port = {port}

[runtime]
main = "native"
background = "native"

# The default runtime: the Constellation native engine. Every provider and model it
# supports is available; choose with `vigil engine init -- --provider ... --model ...`.
[runtimes.native]
kind = "native"
command = ["${{VIGIL_HOME}}/engine/bin/agent-runtime", "--home", "${{VIGIL_HOME}}/engine/home", "acp"]

[runtimes.native.secret_env]
AGENT_MODEL_API_KEY = "model-key"

# Any other ACP agent can be a runtime, as the main one or for delegated work:
# [runtimes.codex]
# command = ["npx", "-y", "@agentclientprotocol/codex-acp@2.6.0"]
# [runtimes.claude]
# command = ["npx", "-y", "@agentclientprotocol/claude-agent-acp@0.107.0"]

# Typed decision backends, tried in order. Omit the section to run on rules alone.
# [[decisions.backends]]
# name = "jev"
# provider = "system_one"
# endpoint = "https://api.typesafe.ai/v1/systemone"
# model = "jev-latest"
# secret = "typesafe-key"

[mind]
effort_routing = "sticky"
cross_group_hints = true
followup_chain = true
followup_chain_horizon_hours = 24
dream_at = "03:30"

# Friendly names for places, by place key or key prefix.
[places]
"""


def _q(value: str) -> str:
    return json.dumps(value)


def cmd_init(args: argparse.Namespace) -> None:
    from .config import CONFIG_FILE, ensure_layout, socket_path_problem

    home = Path(args.home).expanduser().resolve()
    if problem := socket_path_problem(home):
        sys.exit(problem)
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    ensure_layout(home)
    target = home / CONFIG_FILE
    if target.exists() and not args.force:
        sys.exit(f"{target} already exists. Use --force to rewrite it.")
    identities = []
    for item in args.identity or []:
        surface, _, handle = item.partition("=")
        if not handle:
            sys.exit(f"--identity takes surface=handle, got {item}")
        identities.append(f"{{ surface = {_q(surface)}, handle = {_q(handle)} }}")
    workspace = args.workspace or "${VIGIL_HOME}/workspace"
    target.write_text(TEMPLATE.format(
        name=_q(args.name), timezone=_q(args.timezone), workspace=_q(workspace), owner_name=_q(args.owner_name),
        os_user=_q(args.os_user), identities=", ".join(identities), port=args.mcp_port))
    os.chmod(target, 0o600)
    print(f"Created {target}")


def cmd_secret(args: argparse.Namespace) -> None:
    from .config import Config

    config = Config.load(args.home)
    if args.from_env:
        value = os.environ.get(args.from_env, "")
        if not value:
            sys.exit(f"{args.from_env} is not set in this environment.")
    elif args.file:
        value = Path(args.file).read_text()
    else:
        value = sys.stdin.read()
    if not value.strip():
        sys.exit("No value supplied.")
    config.write_secret(args.name, value)
    print(f"Stored secret {args.name}.")


def cmd_engine(args: argparse.Namespace) -> None:
    from . import engine
    from .config import Config

    config = Config.load(args.home)
    if args.action == "install":
        print(f"Installed {engine.install(config, args.version or engine.DEFAULT_VERSION, args.source, args.allow_unpinned)}")
    elif args.action == "init":
        engine.init(config, args.rest)
        settings = engine.apply_settings(config)
        print(f"Engine ready: {settings['provider']['kind']} / {settings['provider']['model']}")
    elif args.action == "apply":
        settings = engine.apply_settings(config)
        print(f"Applied: {settings['provider']['kind']} / {settings['provider']['model']}")
    else:
        document = engine.inspect(config)
        document["config"]["instructions"] = f"<{len(document['config']['instructions'])} chars>"
        print(json.dumps(document["config"], indent=2))


def _control(args: argparse.Namespace, path: str, method: str = "GET") -> dict:
    from .config import Config

    config = Config.load(args.home)
    request = urllib.request.Request(f"http://127.0.0.1:{config.mcp_port}{path}", method=method,
                                     headers={"Authorization": f"Bearer {config.secret('control-token') or ''}"})
    import time

    # A daemon that was just (re)started needs a moment before it listens.
    for attempt in range(8):
        try:
            with urllib.request.urlopen(request, timeout=900) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            sys.exit(f"The daemon refused the request: HTTP {error.code}")
        except urllib.error.URLError as error:
            refused = isinstance(getattr(error, "reason", None), ConnectionRefusedError)
            if not refused or attempt == 7:
                sys.exit(f"The daemon is not answering on port {config.mcp_port}: {error.reason if hasattr(error, 'reason') else error}")
            time.sleep(0.5)
    raise AssertionError("unreachable")


def cmd_status(args: argparse.Namespace) -> None:
    status = _control(args, "/control/status")
    if args.json:
        print(json.dumps(status, indent=2))
        return
    runtime = status["runtime"]
    decisions = status["decisions_24h"]
    print(f"{status['agent']} — vigil {status['version']}, awake {status['uptime_seconds'] // 60} min")
    print(f"runtime: {runtime['main']} ({runtime['model'] or 'model unknown'}); configured: {', '.join(runtime['configured'])}")
    print(f"attached now: {len(status['attached'])} ({status.get('running_turns', 0)} turn(s) running); turns in 24h: {status['turns_24h']} (quiet wake-ups: {status['quiet_wakes_24h']})")
    print(f"memory: {status['people']} people, {status['notes']} notes, {status['journal']} journal entries; last consolidation: {status['last_dream'] or 'never'}")
    print(f"holding: {status['pending_follow_ups']} follow-up(s); tasks: {status['tasks'] or 'none'}")
    if status.get("sessions_without_tools_24h"):
        print(f"warning: {status['sessions_without_tools_24h']} conversation(s) in 24h where the engine never opened the memory tools (see logs/vigil.log)")
    print(f"decisions in 24h: {decisions['count']} ({decisions['ok']} answered, avg {decisions['avg_ms']} ms) via {', '.join(decisions['backends']) or 'rules only'}")
    for place in status["places"]:
        print(f"  {place['place_label'] or place['surface']:<40} {place['audience']:<8} {place['turns']} turn(s)")


def cmd_people(args: argparse.Namespace) -> None:
    from .config import Config
    from .memory import Memory
    from .store import Store

    config = Config.load(args.home)
    store = Store(config.db_path)
    memory = Memory(store, config)
    if args.action == "confirm":
        row = store.one("SELECT claimed_person FROM identities WHERE surface=? AND handle=?", (args.surface, args.handle))
        if not row or not row["claimed_person"]:
            sys.exit("No pending claim for that identity.")
        old = store.one("SELECT person FROM identities WHERE surface=? AND handle=?", (args.surface, args.handle))["person"]
        store.run("UPDATE identities SET person=?, claimed_person=NULL, verified=1, evidence='confirmed by the owner' WHERE surface=? AND handle=?",
                  (row["claimed_person"], args.surface, args.handle))
        memory.merge(old, row["claimed_person"])
        print("Linked.")
        return
    if args.action == "link":
        person = memory.find_person(args.person)
        if not person:
            sys.exit(f"Nobody called {args.person}.")
        old = store.one("SELECT person FROM identities WHERE surface=? AND handle=?", (args.surface, args.handle))
        memory._bind(args.surface, args.handle, person, "", True, "linked by the owner", False)
        if old and old["person"] != person:
            memory.merge(old["person"], person)
        print("Linked.")
        return
    for row in store.q("SELECT * FROM people ORDER BY is_owner DESC, name"):
        who = memory.person(row["id"])
        kind = "owner" if who["is_owner"] else ("agent" if who["is_agent"] else "person")
        print(f"{who['name']} [{kind}]")
        for identity in who["identities"]:
            print(f"    {identity['surface']}:{identity['handle']}" + (f"  ({identity['display']})" if identity["display"] else ""))
    for row in store.q("SELECT surface, handle, claimed_person, evidence FROM identities WHERE claimed_person IS NOT NULL"):
        print(f"claim awaiting confirmation: {row['surface']}:{row['handle']} is {memory.name_of(row['claimed_person'])} — {row['evidence']}")


def cmd_memory(args: argparse.Namespace) -> None:
    from .config import Config
    from .store import Store

    config = Config.load(args.home)
    store = Store(config.db_path)
    if args.action == "notes":
        for row in store.q("SELECT id, subject, text, visibility, audience FROM notes WHERE archived=0 ORDER BY ts DESC LIMIT ?", (args.limit,)):
            print(f"#{row['id']} [{row['subject']}] ({row['visibility']}, from {row['audience']}): {row['text']}")
    elif args.action == "journal":
        import datetime
        for row in store.q("SELECT ts, place_label, audience, actor_name, heard, said FROM journal ORDER BY ts DESC LIMIT ?", (args.limit,)):
            stamp = datetime.datetime.fromtimestamp(row["ts"]).strftime("%m-%d %H:%M")
            print(f"{stamp} {row['place_label']} ({row['audience']}) {row['actor_name']}: {row['heard'][:140]!r} -> {row['said'][:140]!r}")
    elif args.action == "agenda":
        import datetime
        for row in store.q("SELECT id, kind, scope, due, place_label, text FROM thoughts WHERE state='pending' ORDER BY due"):
            due = datetime.datetime.fromtimestamp(row["due"]).strftime("%m-%d %H:%M")
            print(f"#{row['id']} {row['kind']} ({row['scope']}) due {due} in {row['place_label']}: {row['text'][:160]}")
    elif args.action == "decisions":
        for row in store.q("SELECT ts, purpose, backend, ms, ok, answers, error FROM decisions ORDER BY ts DESC LIMIT ?", (args.limit,)):
            print(f"{row['purpose']:<9} {row['backend']:<10} {row['ms']:>5} ms {'ok ' if row['ok'] else 'ERR'} {row['answers'] or row['error']}")
    elif args.action == "places":
        from .memory import Memory

        memory = Memory(store, config)
        for row in store.q("SELECT place, max(place_label) AS label, count(*) AS n, max(ts) AS last FROM journal GROUP BY place ORDER BY last DESC"):
            mark = "  [rehearsal]" if memory.is_rehearsal(row["place"]) else ""
            print(f"{row['n']:>5} turn(s)  {row['place']}  ({row['label']}){mark}")
    elif args.action == "forget":
        from .memory import Memory

        if not args.place and not args.since:
            sys.exit("Say what to forget: --place KEY-PREFIX, --since WHEN, or both. `vigil memory places` lists place keys.")
        try:
            since = _when(args.since) if args.since else None
            until = _when(args.until) if args.until else None
        except ValueError as error:
            sys.exit(str(error))
        counts = Memory(store, config).forget(args.place, since, until, args.people, args.apply)
        summary = ", ".join(f"{count} {name}" for name, count in counts.items() if count) or "nothing"
        print(("Removed: " if args.apply else "Would remove: ") + summary + ("" if args.apply else ". Add --apply to do it."))


def _when(text: str) -> float:
    """A point in time: "90m", "2h" or "3d" ago, or a local "YYYY-MM-DD[ HH:MM]"."""
    import datetime
    import re
    import time

    text = text.strip()
    relative = re.fullmatch(r"(\d+)\s*([mhd])", text)
    if relative:
        return time.time() - int(relative.group(1)) * {"m": 60, "h": 3600, "d": 86400}[relative.group(2)]
    for pattern in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return datetime.datetime.strptime(text, pattern).timestamp()
        except ValueError:
            continue
    raise ValueError(f"Cannot read the time {text!r}. Use 90m, 2h, 3d, or YYYY-MM-DD HH:MM.")


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "acp":  # hot path: no argparse, no heavy imports
        home = DEFAULT_HOME
        if "--home" in sys.argv:
            home = sys.argv[sys.argv.index("--home") + 1]
        from .shim import main as shim

        shim(home)
        return

    parser = argparse.ArgumentParser(prog="vigil", description="An always-on mind for a Constellation Agent Instance.")
    parser.add_argument("--home", default=DEFAULT_HOME)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="create a Vigil home")
    p.add_argument("--name", required=True)
    p.add_argument("--owner-name", required=True)
    p.add_argument("--os-user", default=os.environ.get("USER", ""))
    p.add_argument("--timezone", default="UTC")
    p.add_argument("--workspace")
    p.add_argument("--mcp-port", type=int, default=47821)
    p.add_argument("--identity", action="append", help="surface=handle, repeatable")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("secret", help="store a named secret from stdin, a file or an environment variable")
    p.add_argument("name")
    p.add_argument("--file")
    p.add_argument("--from-env")
    p.set_defaults(func=cmd_secret)

    p = sub.add_parser("engine", help="install and configure the native runtime")
    p.add_argument("action", choices=["install", "init", "apply", "show"])
    p.add_argument("--version", help="a pinned runtime release; the default is the one this Vigil was tested with")
    p.add_argument("--source")
    p.add_argument("--allow-unpinned", action="store_true")
    p.set_defaults(func=cmd_engine, passthrough=True)

    p = sub.add_parser("serve", help="run the always-on daemon")
    p.set_defaults(func=lambda a: __import__("vigil.daemon", fromlist=["serve"]).serve(a.home))

    # `vigil acp ...` takes the fast path at the top of main(). This is `vigil --home H acp`.
    p = sub.add_parser("acp", help="ACP stdio bridge for an Agent Instance backend")
    p.set_defaults(func=lambda a: __import__("vigil.shim", fromlist=["main"]).main(a.home))

    p = sub.add_parser("status", help="what the daemon is doing")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("dream", help="run the consolidation pass now")
    p.set_defaults(func=lambda a: print(json.dumps(_control(a, "/control/dream", "POST"))))

    p = sub.add_parser("people", help="who the agent knows, and identity links")
    p.add_argument("action", nargs="?", default="list", choices=["list", "link", "confirm"])
    p.add_argument("--person")
    p.add_argument("--surface")
    p.add_argument("--handle")
    p.set_defaults(func=cmd_people)

    p = sub.add_parser("memory", help="inspect notes, journal, agenda, decisions and places; forget a practice run")
    p.add_argument("action", choices=["notes", "journal", "agenda", "decisions", "places", "forget"])
    p.add_argument("--limit", type=int, default=30)
    p.add_argument("--place", help="forget: place key prefix, as listed by `vigil memory places`")
    p.add_argument("--since", help="forget: from this time (90m, 2h, 3d, or YYYY-MM-DD HH:MM)")
    p.add_argument("--until", help="forget: up to this time")
    p.add_argument("--people", action="store_true", help="forget: also remove people left with no history (linked accounts are kept)")
    p.add_argument("--apply", action="store_true", help="forget: actually delete (the default only counts)")
    p.set_defaults(func=cmd_memory)

    # `vigil engine init -- <agent-runtime init arguments>` hands everything after
    # the separator to the engine untouched.
    argv = sys.argv[1:]
    rest: list[str] = []
    if "--" in argv:
        split = argv.index("--")
        argv, rest = argv[:split], argv[split + 1:]
    args = parser.parse_args(argv)
    if rest and not getattr(args, "passthrough", False):
        parser.error("unexpected arguments after --")
    args.rest = rest
    args.func(args)


if __name__ == "__main__":
    main()
