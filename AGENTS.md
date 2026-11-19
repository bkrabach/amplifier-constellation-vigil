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
| `src/vigil/github.py` | The agent's read access to GitHub: identity, the repository list, the audience rule, five tools. |
| `src/vigil/daemon.py`, `cli.py`, `config.py`, `store.py`, `mcp.py` | Wiring, commands, configuration, SQLite, MCP over HTTP. |

## Rules that are easy to break

- **Relay before the first `await`.** The SDK starts one task per incoming message and enqueues outgoing messages in a task's first step. A conductor handler that awaits anything before relaying can reorder a stream. `conductor.py` explains this at the top.
- **Every memory read goes through an audience filter.** A new query that returns notes, journal rows or thoughts must take a `Scene` and filter by it. Add a test in `tests/test_units.py` that shows a group cannot see what it should not.
- **Only the owner sends the agent across places.** `mind_follow_up` with `where` puts a note in another place's brief. It is refused outside an owner-private conversation, because from anywhere else it would carry one room's talk into another past the audience filter. Keep that check, and keep its test. A one-time request set this way stays pending and in that place's brief until the agent cancels it, so anything that treats a due thought as "waiting to be delivered" (a wake-up, the quiet wake-up check) must leave out the ones `owner_work_seen` accepts, or a Slack conversation would wake itself in a loop.
- **The nightly pass is in no place.** Its scene is owner-private, so a note saved there as "here" is readable only in the owner's private sessions. A note about a room needs that room's place key (`mind_note`'s `place`), and the pass refuses "here" without it. A live agent wrote its summary of a game world this way three nights running and could not recall it in that world. It happened only once the notebook already held notes: a throwaway home with an empty notebook does not reproduce it, so check consolidation changes on a copy of real records.
- **Unasked recall belongs to the room.** `Memory.search(asked=False)` puts this place first and takes a row from another place only on two shared subject words. Keep it that way: with notes listed before journal rows and one common word enough, a question in the game recalled four Slack notes and none of the game.
- **A wake-up must not be able to repeat itself.** Vigil brings the agent back to a conversation through the connector's follow-up tool (`WAKE_TOOLS` in `mind.py`). Three things keep that from looping: owner work the agent has seen is no reason to wake; a wake-up turn the agent ends without trying to speak settles what it surfaced; and a wake-up turn that left something undelivered is followed no sooner than five minutes later. A connector's refusal (`isError`) is never recorded as a wake-up set. Between turns, `tend_wakes` asks again only once a reminder is fifteen minutes overdue: asking sooner replaces one the connector is still waiting to admit. Check a connector's tool against its real server, on the Node version it runs: `tests/test_conductor.py` uses a stand-in.
- **Holding wake-ups is an operator control, not a setting.** `vigil wake --hold` writes the `wake-hold` list in the state database, and `_wake_tool` reads it on every decision. Keep every path that sets a wake-up going through `_wake_tool`, or a hold will leak. It does not cover the model calling the connector's own reminder tool.
- **A connector's credential stays in `secrets/`.** The tool-server descriptors Vigil keeps for waking a place (`secrets/wake-servers.json`, mode 0600) carry the connector's token. Never put a descriptor, its headers or its URL with credentials into `state.sqlite3`, the journal, an event, a log line or an error message: the database is copied for backups and for tests.
- **GitHub's three rules live in `GitHub.may_read` and `GitHub.repositories`.** Every tool reaches a repository through one of them: it is on the owner's list, and a private one is read only where `private_ok(scene, repo)` says, which is decided per repository so that opening a place to one project does not open it to every private repository the list happens to match. The table is re-read when `vigil.toml` changes (`GitHub.refresh`), so read settings through `self.table`, never through the `Config` the daemon started with. A new tool that builds its own address skips both. The tool definitions are fixed text with a pinned hash in `tests/test_github.py`: nothing from configuration goes into a description, because that would change every session's tool list. The credential is fetched and used inside `github.py` and goes nowhere else: not into an error, a log line, an event or a tool result. There is no write tool. Before adding one, decide who may cause a write (a stranger's text in an issue must not), and show with a test that a group conversation cannot.
- **A reader must never raise.** An unfamiliar prompt yields a restrictive Scene, not an error.
- **Keep the mind tool list stable.** Tools are part of a session's cached prefix. Add tools rarely, and give each at least one required argument.
- **Automatic approval must not trust model text.** `_own_tool` in `mind.py` decides which permission requests Vigil approves by itself. A title is display text and raw arguments are often the model's own. Loosen that check only with a test in `tests/test_units.py` showing a forged request is still refused. An identity the engine states (`com.microsoft.amplifier.tool`) may only withdraw an approval: metadata is not proof because it is called metadata.
- **Changing the pinned engine.** Add the release's hashes to `RELEASES` in `engine.py` from its `SHA256SUMS` and per-package manifest, and check the unpacked binary against them yourself. On a live agent, update the engine before a connector that depends on what the newer engine sends: a newer client with an older engine behind Vigil can refuse the agent's legitimate tools.
- **Use declared interfaces only.** Do not read another component's files or secrets. Call an experience's tool through the descriptor it gave the session.
- **Do not add a second social gate.** Whether to speak in a group is the Instance's attention decision.

## Product page and ecosystem map

The product page (`site/`, published at <https://bkrabach.github.io/amplifier-constellation-vigil/>) and the README's "Where this fits" block follow the [Constellation map guide](https://github.com/microsoft/amplifier-constellation/blob/main/docs/ecosystem-maps.md). `docs/ecosystem-map.json` is the one record of Vigil's role, parts and relationships; `docs/ecosystem-map.svg` and `docs/product/` are generated from it with that repository's renderer, and `site/build.py` fills the page's "Where it fits" facts from it. When a role, interface, part or dependency changes, edit the record, regenerate, and update the page and the README together; [site/README.md](site/README.md) has the commands. Map what the source does, keep limits beside promises, and never infer compatibility from a map. Vigil is listed in the Constellation registry (`docs/ecosystem/registry.json` in `microsoft/amplifier-constellation`) as an independent project, and that entry must stay identical to `docs/ecosystem-map.json`. When the record changes, send the same record there in a pull request; its owners decide whether to merge it. The purpose line is what the project directory shows beside the name, so it keeps the statement that Vigil is independent. Vigil is not in `family.toml`, which locates Microsoft components.

## Checks

```bash
.venv/bin/ruff check src
.venv/bin/python -m pytest
```

`tests/test_github.py` checks the GitHub tools against a stand-in for GitHub's API; `vigil github` and a real turn are the check against the real service. `tests/test_conductor.py` runs the real daemon against `tests/fake_runtime.py` and checks ordering, permissions, replay and the quiet wake-up. `tests/drive.py` talks to a live daemon as an experience would, including through an Instance with `--command`. A passing suite shows mechanics; judge model behaviour with real turns.

A check "through Vigil" has to confirm that the daemon answered. A home whose path leaves no room for the socket (107 bytes on Linux) makes the daemon refuse to start, and `vigil acp` then runs the engine alone: a file-attachment check passed that way without Vigil in it. `_meta["dev.vigil"].mind` in the `initialize` reply is the daemon's mark, and `tests/runtime_matrix.py` now refuses to go on without it. Keep throwaway homes on a short path.

## Redeploying a live agent

A restart waits up to `server.drain_seconds` (45 by default) for running turns to finish, and `vigil status` shows how many are running. A turn longer than that is cut off. For a game connection that means unsettled effects, and the connector then pauses the agent's work there until someone reviews it. Look before you restart, and do not redeploy in the middle of a session you are testing.

The wait only works if the stop signal reaches the daemon alone. The service unit therefore has `KillMode=mixed`: with systemd's `control-group`, the engine processes get the signal at the same moment and every running turn dies at once, whatever the daemon is waiting for. A unit written before 0.1.15 has `control-group`; change it and run `systemctl --user daemon-reload` (no restart needed).

Make the restart depend on the check, in one command: `vigil --home HOME status --wait-idle 120 && systemctl --user restart vigil-NAME`. It waits up to that long for no turn to be running and exits 3 if one still is. A deploy script that printed the status and restarted on the next line cut a turn in a live conversation: reading the number is not a gate. `vigil status --json` gives `running_turns` and `attached` for a script that has to decide more. Nothing holds new turns back, so the check and the restart are not atomic. A turn that arrives while the daemon is down waits for it: the bridge (`shim.py`) tries the socket for up to twelve seconds before it runs the runtime directly. Before 0.1.20 it gave up after half a second, and on a live agent a Slack turn that arrived 3 seconds into a 3.6-second restart was answered by the engine alone: no memory, no journal entry, and a tool list without the mind's tools, which cost that conversation a compaction. `logs/shim.log` records each wait of a second or more and each turn run without the daemon, so read it after a restart.

A change to the standing instructions, or a connector update that changes its tools, costs each long conversation its compacted history. The native engine keeps a compacted window only for the exact instructions and tool list it was made with, so the next request in every long session carries the engine's bounded copy of the whole history. On a live agent one prompt clause and one connector update did this to a game session: each quiet turn then sent about 180,000 tokens until a compaction succeeded again. Change prompts deliberately, and afterwards read `model.started` and `context.*` in the engine's `history.sqlite3` events for the long sessions.

To see which engine a live agent really runs, without a session or a model call, attach as a client would and negotiate only: start `vigil acp --home HOME`, send ACP `initialize`, and hash `/proc/<pid>/exe` of the `agent-runtime` child the daemon started for that attachment. `_meta["dev.vigil"].runtimeAgentInfo.version` in the reply is the engine's own version. No engine process stays up while the agent is idle.

## When a runtime misbehaves

Do not guess. Run it through `tests/runtime_matrix.py --only NAME --keep` and read `logs/vigil.log` in the kept home: the trace shows whether the runtime opened the mind's tool server and what it asked permission for. A runtime that never calls `tools/list` has no mind tools, whatever it advertised. The matrix refuses every permission request that is not for a mind tool; keep it that way, because a model without its tools will otherwise explore the machine.

A turn that ends with `Provider requested a tool that was not offered` means the model asked for a tool by a name the session did not list, usually a mind tool's short name (`mind_note`) where the engine listed `mcp_1_mind_note`. The native engine fails the whole turn on that in every version up to 0.15.8; from 0.15.9 it answers that one call with an error and the model can ask again under a listed name. Its history keeps what the model asked for: read the `function_call` items of the session in `engine/home/history.sqlite3` (`sessions.items`) before suspecting anything else. The standing instructions tell the model to use listed names; with that line, six of six note-saving turns used the listed name on a live agent where eight of eleven had not.

A turn that ends with `Context service rejected request (HTTP 400, invalid_request); history unchanged` is the native engine's compaction being refused. Up to 0.15.10 this happened when the model stayed silent (a reply with reasoning and no text) while another input was already waiting: the engine asked for a summary of a window that ended on reasoning, and the provider refused it. The waiting input was lost and the session stayed large. Engine 0.15.11, pinned here from Vigil 0.1.5, leaves that trailing reasoning out of the summary request ([Runtime PR 54](https://github.com/microsoft/amplifier-constellation-runtime/pull/54)). On an older engine such a session recovers the next time the agent uses a tool in it, because a compaction after a tool result is accepted. The engine's events show which case you have: every `context.compaction.failed` there follows a `model.finished`, every `context.compacted` follows a `tool.finished` or a `turn.started`. With 0.15.11 a `context.compacted` can follow a `model.finished` too.

Engine 0.15.10 added an approval-policy profile: when the client negotiates it at `initialize` (Instance 0.22.9 or later), the engine describes each permission request so the Instance can remember an approval. Vigil relays `initialize`, so the profile switches on by itself once the Instance in front of it negotiates. That path has not been exercised through Vigil: when the Instance is updated past that version, run a turn that needs a permission for a tool that is not Vigil's own and read the request in `logs/vigil.log`.

The engine's token threshold (`compact_threshold_tokens`) only applies from the second model request of a turn. A conversation of one-request turns, such as listening to a room, compacts only when its window passes three quarters of the engine's byte limit, so it can run far above the threshold. Do not assume a quiet session is small: check `counted_model_context_bytes` in its `model.started` events.

## Testing against a live agent

Rehearsals leave memories. Before driving a deployed agent with scripted people, list the test places under `mind.rehearsal_places` in its `vigil.toml` so nothing learned there reaches real work. Afterwards, `vigil memory forget --place <prefix> --people` shows what a clean-up would remove and `--apply` removes it. Do not edit the SQLite file by hand: the search indexes go stale.

## Reviewing a voice call

Vigil hears a call only at its hand-offs. Between them the Companion's voice answers by itself, and nothing said after the last hand-off reaches the agent. On a live call the caller said which game player they were, the voice acknowledged it, and the agent never heard it. So read the Companion's own records beside the journal. In the Companion's home, `state.sqlite3` holds `companion_delegations` (`request_text` is what the caller said for each hand-off) and `companion_events` (every speech fragment and hand-off with its time, and `playback` rows where the caller cut the voice off).

The reader's `heard` for a hand-off must equal that `request_text` with line breaks as spaces. Check it on real prompts when the Companion changes: it once began sending the caller's words as a list of timed fragments, and the journal, the unasked recall and the words passed to the game all carried that JSON until the reader was taught the shape.

The caller's wait is from their last word to the voice's first. On that call it was 12 and 19 seconds. About five seconds of each passed before the prompt reached Vigil: the Companion waits about two seconds for the transcript, and the request takes about three more to arrive. The rest is the model, so a large session or a turn with several tool calls is what the caller hears as silence.

## Reviewing a game conversation

The game server's log is the record of what was said and when. Read it beside the journal, the connector's log and the engine's events for the session, because the connector decides what becomes a turn:

- It starts a turn for some chat lines and holds the rest (`participation_offer_applied` with `action: hold` in its log). A held line reaches the agent only inside a later prompt, in `world.conversation.messages` (the last 16 public lines) or, for a person's words that authorise activity, in `world.task_capabilities.delegation_sources`.
- A line said while a turn is running arrives as an update, and the connector starts no turn for it afterwards (`Source input already dispatched` in its log). On a live server the owner's question to the room arrived only this way.
- The message that starts a turn and the delegation sources name the speaker and words `username` and `message`; the room's conversation and the updates use `author` and `text`. The ids are the same everywhere.

So a person's question may never be a turn of its own. The reader keeps all three kinds as overheard rows under whoever said them; check that on real prompts when the connector changes. The reader for updates once expected the wrong pair of names and kept nothing, and the journal held one of the owner's three messages.

What the agent said in the game is what `chat` and `tell` delivered. The connector posts the text that ends a turn only when a person spoke to the agent and it tried no speech tool; after another agent's message or its own activity that text goes nowhere (`final_reply_withheld` in the connector's log). A `chat` call that names the message it replies to is refused once anyone else has spoken (`Reply source changed or was not exposed as pending public input`), and each refusal costs the person waiting another model request.

Each game prompt is about 50,000 characters: the connector's observation is 36,000 to 42,000 of them and the Instance's context 9,000, against under 3,000 for the brief. Every chat line that starts a turn therefore adds about 22,000 tokens. With provider compaction the engine keeps each of those whole until it next compacts, so a busy room compacts every third or fourth turn at a 100,000-token threshold, and each compaction holds the turn for 10 to 14 seconds. `model.started` (`counted_model_context_bytes`) and `context.*` in the engine's events show it.

When the owner is in the game and the agent seems to know nothing of what they asked for elsewhere, look at what it is holding (`vigil status` gives the count; the `thoughts` table has the text and scope). A follow-up scoped to a person or to the owner waits for a private conversation, and public game chat is not one. From 0.1.7 the agent can set such a thing for the place it concerns, and from 0.1.8 it stays in that place's brief until the agent closes it. `tests/drive.py` with `--command` gives the owner-private turn that asks the agent to move one; check `vigil status` first, because that turn also delivers anything else held for the owner.

## When the agent says it cannot do something

Find which layer lacks it before changing anything, because each has a different owner. On a live agent four things came up in one Slack thread:

- **It could not open a link.** The engine's provider tools were empty (`provider.native.tools` in the engine's `runtime.json`, shown by `vigil engine show`). That is Vigil's to set: `[engine] native_tools` in the home, then `vigil engine apply`. It changes the tool list, so each long conversation loses its compacted history once. The agent's commands have no network (`command_network`), and should not be given one for this.
- **It could not read a large PDF.** The Slack connector refused the file ("Attachment exceeds the configured size limit"): its `media.max_file_bytes` cannot be set above 2 MiB. Past the connector, the engine carries at most 16 MiB of base64 per file and 20 MiB per prompt. The agent told people the limit was a setting on its host that could be raised to 50 MB; neither part was true, and it had no way to know.
- **It could not start a direct message.** No connector tool starts a conversation; the agent speaks where it was addressed, and `mind_follow_up` for a person waits until they next talk to it.
- **It did not link to things in Slack.** The connector posts replies as standard Markdown, so `[words](address)` works, and its context tools return an address for each conversation and message. Nothing in the prompt says either.

The last three belong to the connector. Report them to its owners with what you measured; do not teach Vigil a connector's limits in a prompt, where they go stale.

## Files in a conversation

Vigil sets no limit of its own on files. It relays every prompt part and every tool-call update unchanged, and reads only text parts for its own memory. So nothing from an attached file enters the journal or recall: after a compaction, or in another conversation, the agent has a file's content only if it wrote a note or can read the file again with a tool.

The limits are the engine's (`src/media.rs` and `src/engine.rs` in the Runtime repository), and they are the first place to look when a turn with files fails:

- **A prompt has at most 32 parts, and Vigil's brief is one of them.** The connector and the Instance have 31.
- **A prompt has at most 256 KiB of text**, counting every text part and every attached text file. Over that the engine refuses the whole prompt, so the turn fails, not only the file. A busy Slack thread already sent 170,000 to 190,000 bytes of text per prompt before any file.
- **A file sent as bytes is read only if it is labelled `text/*`, JSON or XML and is valid UTF-8.** Any other label gives the model a size and a hash.
- **A tool result is cut at `limits.tool_output_bytes`** (65,536 unless the home's engine settings say otherwise), so a tool that reads documents has to return them in pages.
- **From engine 0.15.14 the agent can attach a workspace document to its reply** (`workspace_attach_file`: Markdown, text, CSV or JSON, up to 200,000 bytes). The engine asks no permission for it, and Vigil's audience rule covers memory, not workspace files.

When measuring a live agent's prompts, count sizes and hashes; never copy message text out of its home.

## Linking one of the owner's accounts

Add the account to `[owner] identities` in `vigil.toml` and restart. The restart makes the account the owner's and, when the earlier person was known only through it, moves that person's history to the owner; `vigil people` shows the result. Before 0.1.4 the restart moved the account and left the history on a stranger with the old name.

Decide first how far the place's sign-in can be trusted, because the link is more than a name. A one-to-one conversation with an owner account is owner-private, so in a game a whisper from that player name opens the owner's private memory. Public chat stays a group. Check the server, not the connector's log: the game connector's `signature_verified` flag says whether a chat message carried a verified signature, not whether the player signed in with a real account. What matters is where players enter, whether that entrance verifies the account, and whether the server's allow-list holds account ids or ids derived from the name. On one live server, people entered through a proxy that verified accounts, agents through a local proxy that did not, and the allow-list held the owner's account id, so the unverified entrance could not present the owner's name. When the owner later joined, the server's log showed it: the account's id came from the server's authenticator, and the owner's chat lines carried no `[Not Secure]` mark while every agent's did.

Game accounts share one namespace, `minecraft`, across every world the agent joins. A linked name is the owner in all of them, so link a game name only when every world the agent joins verifies sign-in.

When a surface changes its prompt format, update its reader and its test together. When you learn something that would have saved you time, put it in this file.
