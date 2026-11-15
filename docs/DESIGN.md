# Design

This page explains how Vigil works and where each choice came from. It is for someone changing Vigil or deciding whether to trust it.

## The problem

A Constellation agent is one installed individual, but every experience reaches it through a short-lived process. Slack and Conversations start a new ACP process for every turn. Minecraft keeps one open. Companion opens a new private room for every call. Each conversation is a separate engine session, and nothing runs between messages. The agent therefore cannot remember across places, cannot come back to something on its own, and has no notion of who is listening beyond what one prompt says.

The ecosystem leaves this open on purpose. The Agent Instance contract promises a stable outward identity and says the inside is free. The native engine "does not synthesize a global memory". Instance background work "is not a recurring job planner" and delivers nothing to an experience. Vigil fills that inside space and keeps every outward promise as it found it.

## Shape

Vigil is two processes.

`vigil acp` is the command an Instance launches as its backend, once per client connection. It only joins its standard input and output to a socket. It starts in tens of milliseconds and holds no state, so it suits experiences that attach per turn.

`vigil serve` is the daemon. It owns all state and has three parts.

**The conductor** makes Vigil look like one ACP agent while a runtime does the reasoning. For each attached connection it starts a runtime process and relays raw JSON in both directions. Raw relay matters: experiences negotiate optional extensions with the engine (steering, source delivery, speech completion, context snapshots, session options), and all of them keep working because Vigil passes through what it does not understand. Framing, request ids, correlation and cancellation come from the official ACP SDK's `Connection`, as the family's engine-compatibility agreement asks. The conductor intercepts five things: `initialize` (to present its own identity), session setup (to add the mind's tools), `session/prompt` (the mind's turn), session updates (to observe what was said), and permission requests for its own tools.

**The mind** is what happens around a prompt. It is described below.

**The scheduler** polls for what is due: queued background tasks and the nightly consolidation. Timers do not start work themselves; the loop turns a due item into work and sleeps again. Sleeping costs nothing.

This follows the five primitives in Anthropic's *Building always-on agents*: an event inbox (the `events` table: every prompt is recorded before it runs and marked interrupted, never replayed, if the daemon dies), timers (the `thoughts` table and the scheduler), memory, permissions, and an outbox (held thoughts with a delivery rule).

## A turn

1. **Read the scene.** `surfaces.py` turns the prompt into a `Scene`: surface, place, thread, audience, who is speaking, who is present, what was said. Each experience words its prompt differently, so there is one small reader per experience. A prompt from an experience Vigil has never seen still produces a Scene, with the most restrictive audience, so a new adapter works immediately and gets sharper when a reader is added.
2. **Settle people and audience.** Surface handles are resolved to people. A one-to-one conversation with a configured owner identity becomes owner-private.
3. **Short-circuit if nothing is owed.** If this turn is Vigil's own wake-up and nothing is due, Vigil re-arms the wake-up and returns without calling the runtime.
4. **Decide by reflex.** Rules first: a greeting needs low effort and no recall. Otherwise one request to a decision model asks two typed questions at once: how much reasoning the message needs, and whether answering depends on earlier context. The effort answer sets the runtime's `thought_level` session option for this turn. The recall answer decides whether to search memory.
5. **Write the presence brief** and append it as the last prompt block.
6. **Let the runtime work.** Updates stream back to the experience unchanged.
7. **Record.** The exchange goes into the journal, delivered thoughts are marked, and if something is still owed in this Slack conversation the wake-up is armed.

## The presence brief

The brief is a few lines of text, composed fresh each turn from memory the current audience may know: the time, the place and audience, the speaker, notes about the people present, what the agent did recently elsewhere, thoughts that are due, and an index of the notebook.

Three details matter.

It is appended *after* the person's content. The native engine derives a session's title from the first text it sees, and clients replay stored prompts when a session is loaded. Placing the brief last keeps titles and replays about what was said. Vigil also removes its brief from replayed history.

It carries a context-snapshot key (`com.microsoft.amplifier.context-snapshot`). Runtimes that support that profile keep only the newest brief in the model's context, so a long conversation does not accumulate one brief per turn.

It tells the model that it is the agent's own working memory, that nobody in the conversation wrote it, and that it is context, not a request. Prompting guidance for current models warns that text arriving next to a user's message can be read as injected instructions; saying plainly what the block is avoids that.

## Audiences

The agent remembers the way a colleague on several teams does. Identity is continuous; what may surface in a scene depends on where a memory came from and who is present now.

Evidence for the audience comes from two places. The strongest is the binding an Instance verifies with the source provider and places in its conversation orientation (`owner`, provider, conversation, actor). The fallback is what the experience's prompt says: a Slack channel id beginning with `D`, a Conversations room with one person, a Minecraft whisper.

| Where a memory came from | Where it may surface |
| --- | --- |
| A private exchange with one person (direct message, whisper, call) | Only with that person, in private. The owner does not see other people's private exchanges. |
| A shared space (a channel, a room, the game world) | That space; any owner-private scene; and any other scene where someone present was part of it. |
| A note the agent marked `anywhere` | Anywhere. |

The third column of the middle row is the important one. If Maya is in two channels, what was decided in the first can surface in the second even when someone else there asks, because Maya could have said it herself. With nobody in common, nothing crosses. Whether to *say* it is then the agent's judgment, and the standing guidance tells it to mention where something came from and to ask before repeating something from a small room in a much larger one. Tool results obey the same rule, so the model cannot retrieve what the brief would not have shown.

Places the owner marks as rehearsal (`mind.rehearsal_places`) sit outside this table. Without that mark, a test workspace leaks into real work through the one person who is in both: the owner. With it, what was learned in a rehearsal place surfaces only in rehearsal places, a follow-up promised there is delivered there, nothing from it is consolidated, and only the owner, asking in private, can look it up.

`mind.cross_space` selects the policy: `overlap` (above, the default), `hints` (other shared spaces appear only as one-line reminders for the person speaking), or `strict` (nothing crosses between places).

In a scripted day across two channels, a thread, direct messages, a game world and a voice call (`tests/etiquette/journey.py`), the agent carried a decision to the sub-team that needed it, recognised a linked teammate in the game, turned away a stranger, and kept a confidence when its owner asked directly.

## People

The same human has a different handle on every surface. A new handle becomes a new person; names alone never merge two people. A link takes effect only when something can vouch for it: the owner's configuration, an owner-private session, or the person asserting it from a surface with a stronger sign-in than the one being claimed (an OS login outranks a Slack account, which outranks an offline-mode Minecraft name). Any other claim is stored for the owner to confirm with `vigil people confirm` and unlocks nothing until then.

## Coming back

No Constellation experience lets an agent post unprompted, and Vigil does not go around them. It has three sanctioned ways to deliver a held thought.

- **Slack wake chain.** The Slack connector gives a running turn a `slack_follow_up` tool that schedules one continuation, at most an hour ahead. When a turn ends with something still owed in that conversation, Vigil calls that tool itself with a marked goal. When the continuation arrives and nothing is due yet, Vigil answers it without a model call and schedules the next hop. When the thought is due, the model writes the message in its own voice. If the model scheduled its own follow-up in the turn, Vigil leaves it alone.
- **Next contact.** Elsewhere, the thought appears in the brief the next time it can be heard: in the same place, or, for a thought that follows a person, wherever the agent next talks with them in an audience no wider than where it was created. A follow-up set in a private place that cannot wake the agent follows the person by default.
- **Reports from background work** are held thoughts too, so they use the same two paths.

## Decisions

A typed decision model takes some state and a set of closed questions and returns a probability for each option in one pass. It generates no text, so its answer can steer control flow without ever becoming an instruction. Browser Use's Jev-based agent showed the patterns worth copying: ask several typed questions in one request, never let a model's output become an action directly, verify "done" independently, and replay a known path with no model at all. Vigil's uses:

| Question | When | Effect |
| --- | --- | --- |
| How much reasoning does this message need? | Each turn | Sets the effort for the turn. |
| Does answering depend on earlier context? | Each turn, same request | Decides whether to search memory. |
| Is this journal entry worth remembering for weeks? | Nightly, up to 24 entries per request | Only the entries that pass reach the main model for consolidation. |
| Is the goal met, is work left, or is it blocked? | After an unattended run ends a turn | Continues the run, at most twice. |

Every decision is logged with its probabilities and latency (`vigil memory decisions`), which is the raw material for tuning thresholds. Each caller has a rule-based default, and a backend that fails is skipped for a minute, so an outage costs one timeout rather than one per turn. Group-chat admission ("should I speak?") is not here: an Instance already owns that with its attention profile, and running a second social gate would break the family's agreement.

## Voice and the game

A person on a Companion call who is also in the game with the agent wants it to react, not to relay. By default a spoken request would be reasoned about in the call session, handed to the game session through tools, reasoned about again there, and reported back. With a bridge configured, the mind asks a decision model one question about each spoken remark: is this about the game? If so, the caller's exact words go to the agent's own player through the adapter's interface for outside callers, attributed to the caller, and the player's reply is returned as the spoken answer. The call session's model does not run. The game session sees that the request came from a voice call and keeps its reply short. Measured on a live world: about 3 to 8 seconds from words to answer, with the routing decision taking under 100 ms.

## Background work and consolidation

`mind_delegate` queues a task. The daemon runs it in a session of its own, in workspace mode, with only the mind tools approved. An end of turn is treated as a report, not proof: a decision model judges the report against the goal, and Vigil continues the run at most twice, as Anthropic's guidance for unattended agents recommends. The report becomes a held thought for the conversation that asked.

The nightly pass ("dreaming") moves knowledge from the journal into notes. A decision model scores the day's entries in batches, and only the keepers go to the main model, which writes or corrects notes with the same tools it uses in conversation. Private exchanges with people other than the owner are never consolidated. The pass also tells the model to retire stale cautions, because rules that pile up in memory make current models needlessly hesitant.

## Prompts

The standing instructions describe the agent's situation and what its tools are for, in plain prose. They avoid rule lists and emphatic wording, which over-constrain current models.

Each model family gets a short overlay for its own measured behaviour, taken from that vendor's guide: persistence and clear reporting for Claude Fable and Opus; "finish, verify, don't add unrequested work" for Sonnet and Haiku, plus a line for Haiku about holding to its instructions; scope inference and standing permission inside the workspace for GPT-6 Astra; directness and a read/write risk split for Gemini. Unattended runs get a different addition from interactive ones, because stopping to check in is right when a person is present and wrong when nobody is. The overlay is chosen when the engine is configured, so the system prompt never changes mid-session and the prompt cache keeps matching.

Other guidance is applied in the harness instead of in text: effort is set through the engine's option instead of asking the model to think less; history is append-only; mid-turn input stays on the engine's steering channel; and the mind's tools are declared from the first request of a session.

## Failure and recovery

- **Daemon down:** the bridge runs the main runtime directly. Sessions keep working without the mind.
- **Daemon restart mid-turn:** the prompt's event row is marked interrupted. It is not replayed; the experience's own retry rules apply.
- **Runtime exits:** the client connection is closed, and the experience reconnects as it would for any engine.
- **Decision backend down:** rules take over.
- **Task interrupted by a restart:** it is queued again from the start, since its session is the daemon's own.

## Boundaries Vigil keeps

- It reads no other component's files and uses no other component's credentials. It calls an experience's tool only through the descriptor that experience handed to the session.
- It advertises only what the runtime negotiated, plus its own identity. It removes the native history profile, because an Instance uses that to run the engine binary against its store, and Vigil is not that binary.
- It approves permission requests only for its own tools. Everything else goes to the client, so each experience's approval policy stands. Because that approval is automatic, it never rests on text a model controls: Vigil accepts the native runtime's exact `{server, tool, arguments}` envelope, or a request whose title is the whole identifier of a mind tool, whose kind is the one harnesses give MCP tools, and whose raw arguments are all parameters of that tool. A shell command that begins with a tool's name, or another tool that carries `server: "mind"` among its arguments, is passed to the client like anything else.
- It adds no social gate of its own in front of prompts an Instance has admitted.
