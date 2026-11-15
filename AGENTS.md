# Working on Vigil

This codebase is yours: what it is for and how it is built are your call, and you will keep building on it.

Vigil is an always-on engine for a Constellation Agent Instance. Read [README.md](README.md) for what it does and [docs/DESIGN.md](docs/DESIGN.md) for why. Shared promises live in the [Constellation umbrella](https://github.com/microsoft/amplifier-constellation); apply them here without copying them.

## Map

| File | Responsibility |
| --- | --- |
| `src/vigil/shim.py` | `vigil acp`: the stdio bridge. Standard library only; it must start fast. |
| `src/vigil/conductor.py` | ACP relay between a client connection and a runtime process. |
| `src/vigil/mind.py` | A turn, the mind tools, wake-ups, background work, consolidation. |
| `src/vigil/surfaces.py` | One reader per experience: prompt → `Scene`. |
| `src/vigil/memory.py` | People, notes, journal, held thoughts, and the audience rule. |
| `src/vigil/brief.py` | The presence brief and the replay filter. |
| `src/vigil/decisions.py` | Decision backends and the questions Vigil asks. |
| `src/vigil/prompts.py` | Standing instructions and per-model overlays. |
| `src/vigil/engine.py` | Installing and configuring the native runtime. |
| `src/vigil/daemon.py`, `cli.py`, `config.py`, `store.py`, `mcp.py` | Wiring, commands, configuration, SQLite, MCP over HTTP. |

## Rules that are easy to break

- **Relay before the first `await`.** The SDK starts one task per incoming message and enqueues outgoing messages in a task's first step. A conductor handler that awaits anything before relaying can reorder a stream. `conductor.py` explains this at the top.
- **Every memory read goes through an audience filter.** A new query that returns notes, journal rows or thoughts must take a `Scene` and filter by it. Add a test in `tests/test_units.py` that shows a group cannot see what it should not.
- **A reader must never raise.** An unfamiliar prompt yields a restrictive Scene, not an error.
- **Keep the mind tool list stable.** Tools are part of a session's cached prefix. Add tools rarely, and give each at least one required argument.
- **Use declared interfaces only.** Do not read another component's files or secrets. Call an experience's tool through the descriptor it gave the session.
- **Do not add a second social gate.** Whether to speak in a group is the Instance's attention decision.

## Checks

```bash
.venv/bin/ruff check src
.venv/bin/python -m pytest
```

`tests/test_conductor.py` runs the real daemon against `tests/fake_runtime.py` and checks ordering, permissions, replay and the quiet wake-up. `tests/drive.py` talks to a live daemon as an experience would, including through an Instance with `--command`. A passing suite shows mechanics; judge model behaviour with real turns.

When a surface changes its prompt format, update its reader and its test together. When you learn something that would have saved you time, put it in this file.
