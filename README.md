# Vigil

Vigil is an always-on mind for an [Amplifier Constellation](https://github.com/microsoft/amplifier-constellation) Agent Instance.

A Constellation agent already has a dependable outward identity: one Instance that Slack, Minecraft, Companion, Conversations and Observatory all attach to. Inside, each conversation is a separate session, and the engine wakes only when a message arrives. Vigil is an engine that stays awake between those messages. It gives the agent one memory across every place, a sense of who it is talking to and who else can hear, promises that survive the end of a session, and background work of its own.

To the Instance, Vigil is an ordinary ACP agent. Nothing in any experience changes.

```
Slack · Minecraft · Companion · Conversations · Observatory · any ACP client
                         │  ACP (unchanged)
                 Agent Instance            identity, tokens, attention, context sources
                         │  backend command:  vigil acp      (a thin bridge, started per connection)
                         ▼
                 vigil serve               the always-on daemon
   ┌──────────────┬───────────────────────┬───────────────────────────┐
   │ conductor    │ mind                  │ scheduler                 │
   │ relays ACP   │ scene → decisions →   │ follow-ups, delegated     │
   │ both ways    │ presence brief        │ work, consolidation       │
   └──────┬───────┴───────────┬───────────┴─────────────┬─────────────┘
          │ ACP               │ HTTP MCP (mind_* tools) │
          ▼                   ▼                         ▼
   runtime: the Constellation native engine by default, or any other ACP agent
```

## What it adds

- **One entity everywhere.** Every turn starts with a short *presence brief*: where the agent is, who is speaking, what it knows about them, what it was doing elsewhere, and what it promised to bring up. The agent in a Companion call knows what it said in Slack an hour ago.
- **Audience-scoped memory.** A memory is shown only to an audience that could already have known it. Owner-private notes stay out of group channels. A private message from someone else never leaves that conversation. See [the design](docs/DESIGN.md#audiences).
- **People, not handles.** The same human has a different id in each Slack workspace, in Minecraft and in Conversations. Vigil links them, and only on evidence a surface can vouch for.
- **Follow-through.** `mind_follow_up` holds a thought until it is due and then delivers it where it can be heard: by waking the Slack conversation, or the next time that person talks to the agent anywhere suitable.
- **Background work.** `mind_delegate` runs a longer task in a session of the agent's own, checks that it really finished, and brings the report back to the conversation that asked.
- **Reflexes from typed decision models.** Small, fast, non-generative models decide how hard to think, whether memory is worth reading, which journal entries deserve consolidation and whether unattended work is done. A wake-up with nothing to deliver costs no model call at all.
- **Every provider the native engine supports.** The default runtime *is* the Constellation native engine, so OpenAI, Azure OpenAI, Anthropic, Gemini, Ollama, vLLM, Chat Completions, LiteLLM and GitHub Copilot all work, and new ones arrive with an engine update. Standing instructions are adjusted per model family, following each vendor's prompting guidance.
- **Any ACP agent as a runtime.** Codex, Claude Agent, Gemini CLI or anything else that speaks ACP can be the main runtime or take delegated work.
- **Degrades instead of going dark.** If the daemon is down, `vigil acp` runs the runtime directly. The agent still answers, without its mind, until the daemon returns.

## Install

Run as the OS user who owns the agent.

```bash
bash install.sh --name Polaris --owner-name Ada \
  --workspace "$HOME/.local/share/agent-instance/polaris/workspace" \
  --identity slack:T0123456=U0ABCDEF
```

This creates `~/.local/share/vigil/polaris` with its own Python environment and a pinned, checksummed runtime, and a `vigil-polaris` user service. The checkout is not needed afterwards. Then:

```bash
VIGIL=~/.local/share/vigil/polaris/venv/bin/vigil
H=~/.local/share/vigil/polaris

# 1. Credential and model. Everything after `--` goes to `agent-runtime init` unchanged,
#    so any provider is configured the way the engine's own documentation describes.
$VIGIL --home $H secret model-key --from-env ANTHROPIC_API_KEY
$VIGIL --home $H engine init -- --provider anthropic --model claude-opus-5-5 \
    --key-env AGENT_MODEL_API_KEY --reasoning-effort medium

# 2. Start.
systemctl --user start vigil-polaris
$VIGIL --home $H status
```

### Host it in an Agent Instance

Install an Instance with `--backend none`, then set its backend:

```json
"backend": {
  "command": "/home/you/.local/share/vigil/polaris/venv/bin/vigil",
  "args": ["acp", "--home", "/home/you/.local/share/vigil/polaris"],
  "env": {}, "secret_env": {},
  "working_directory": "${AGENT_HOME}/workspace",
  "allowed_working_roots": ["${AGENT_HOME}/workspace"]
}
```

Vigil's `agent.workspace` must be that same Instance workspace, because experiences open their sessions there. Connect Slack, Minecraft, Conversations, Companion and Observatory to the Instance exactly as for any other agent.

### Switch provider or model

```bash
$VIGIL --home $H secret model-key --from-env OPENAI_API_KEY
$VIGIL --home $H engine init -- --provider openai --model gpt-6.1-sol --key-env AGENT_MODEL_API_KEY
systemctl --user restart vigil-polaris
```

`engine init` rewrites the standing instructions for the new model family. Existing sessions stay bound to the provider they started on; new sessions use the new one. People can also switch among sibling models and effort levels per session from any client that shows ACP session options.

### Decision backends

Add any number to `vigil.toml`; they are tried in order, and Vigil falls back to plain rules if none answers.

```toml
[[decisions.backends]]
name = "jev"
provider = "system_one"            # TypeSafe Jev, Microsoft Decision, H2O Lightning, local SystemOne
endpoint = "https://api.typesafe.ai/v1/systemone"
model = "jev-latest"
secret = "typesafe-key"

[[decisions.backends]]
name = "openai"
provider = "openai_decisions"      # the OpenAI Decisions API
endpoint = "https://api.openai.com/v1/decisions"
model = "gpt-6-luna"
secret = "openai-key"
```

### Other runtimes

```toml
[runtimes.codex]
command = ["npx", "-y", "@agentclientprotocol/codex-acp@2.6.0"]

[runtime]
main = "native"        # the runtime conversations use
background = "codex"   # the runtime delegated work uses
```

The agent can also choose per task: `mind_delegate` takes an optional `runtime`. A runtime gets the mind tools if it accepts HTTP MCP servers at session setup.

## Operate

| Command | What it shows or does |
| --- | --- |
| `vigil status` | Uptime, attached connections, turns, memory size, follow-ups, tasks, decision latency. |
| `vigil memory notes` / `journal` / `agenda` / `decisions` | What the agent wrote down, what happened where, what it is holding, and what the decision models answered. |
| `vigil people` | Who the agent knows and which accounts are linked. `vigil people confirm --surface S --handle H` accepts a pending identity claim. |
| `vigil dream` | Run the consolidation pass now. It runs nightly at `mind.dream_at`. |
| `vigil engine show` / `apply` | Inspect the engine settings, or re-apply Vigil's settings after an edit. |

Standing instructions from the owner go in `memory/instructions.md`. They are part of every brief.

Logs are in `logs/` under the home: `vigil.log` for the daemon, `shim.log` for bridge fallbacks, and one file per runtime.

## Limits to know about

- **Experiences decide when the agent may speak.** No Constellation experience lets an agent post on its own. Vigil reaches a quiet Slack conversation by chaining the connector's follow-up tool (at most an hour per hop, each hop free of model calls). In other places a held thought is delivered the next time that person speaks to the agent.
- **A group audience never sees detail from another room.** It sees one-line hints that the agent and the person now speaking have talked elsewhere, and only if their accounts are linked.
- **Runtime 0.15.7 and Anthropic.** That release fails a turn when a tool is called with no arguments. Vigil's own tools always take one. For tools from experiences, either run a runtime that includes the fix in `ops/patches/`, or use another provider.
- **Session options.** Per-turn effort selection applies to runtimes that expose a `thought_level` session option, which the native engine does.

## Develop

```bash
uv venv && uv pip install -e . pytest ruff
.venv/bin/python -m pytest          # unit tests and an end-to-end conductor test against a scripted runtime
.venv/bin/python tests/drive.py --home HOME "hello"                      # act as an experience
.venv/bin/python tests/drive.py --home HOME --slack C123:U999:Dana "hi"  # a Slack-shaped turn
```

See [docs/DESIGN.md](docs/DESIGN.md) for how it works and why.
