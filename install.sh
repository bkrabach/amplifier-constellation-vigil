#!/usr/bin/env bash
# Install Vigil for one agent, as the OS user who owns that agent. Never as root.
#
# Creates a private home with its own Python environment and runtime, and a systemd
# user service that keeps the daemon awake. The checkout is not needed afterwards.
set -euo pipefail
umask 077

SOURCE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
NAME=""
VIGIL_HOME=""
OWNER_NAME=$(id -un)
TIMEZONE=$(cat /etc/timezone 2>/dev/null || echo UTC)
WORKSPACE=""
PORT=47821
IDENTITIES=()
RUNTIME_BINARY=""
ALLOW_UNPINNED=0
SERVICE=1

usage() {
  cat <<'USAGE'
Install Vigil for one agent.

  bash install.sh --name NAME [options]

  --name NAME             The agent's name (also names the home and the service).
  --home PATH             Vigil home (default ~/.local/share/vigil/<name>).
  --owner-name NAME       What the agent calls its owner (default: your login).
  --timezone TZ           IANA timezone for the agent's sense of time.
  --workspace PATH        The runtime's working folder. For an Agent Instance, pass
                          the Instance's own workspace folder.
  --mcp-port PORT         Loopback port for the mind tools (default 47821).
  --identity SURFACE=ID   An account that is the owner, e.g. slack:T0123=U0456. Repeatable.
  --runtime-binary PATH   Install this agent-runtime instead of downloading the pinned one.
  --allow-unpinned        Accept a runtime binary that is not the pinned release.
  --no-service            Do not install or start the user service.
USAGE
}

while (($#)); do
  case $1 in
    --name) NAME=$2; shift 2 ;;
    --home) VIGIL_HOME=$2; shift 2 ;;
    --owner-name) OWNER_NAME=$2; shift 2 ;;
    --timezone) TIMEZONE=$2; shift 2 ;;
    --workspace) WORKSPACE=$2; shift 2 ;;
    --mcp-port) PORT=$2; shift 2 ;;
    --identity) IDENTITIES+=(--identity "$2"); shift 2 ;;
    --runtime-binary) RUNTIME_BINARY=$2; shift 2 ;;
    --allow-unpinned) ALLOW_UNPINNED=1; shift ;;
    --no-service) SERVICE=0; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

[[ -n $NAME ]] || { usage >&2; exit 2; }
[[ $(id -u) -ne 0 ]] || { echo "Run as the agent's owner, not root." >&2; exit 1; }
SLUG=$(printf '%s' "$NAME" | tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9' '-' | sed 's/-\+/-/g; s/^-//; s/-$//')
VIGIL_HOME=${VIGIL_HOME:-$HOME/.local/share/vigil/$SLUG}
mkdir -p "$VIGIL_HOME"
chmod 700 "$VIGIL_HOME"

if command -v uv >/dev/null; then
  uv venv -q --python 3.12 "$VIGIL_HOME/venv" --allow-existing
  uv pip install -q --python "$VIGIL_HOME/venv/bin/python" --reinstall-package amplifier-constellation-vigil "$SOURCE"
else
  python3 -m venv "$VIGIL_HOME/venv"
  "$VIGIL_HOME/venv/bin/pip" install -q --upgrade "$SOURCE"
fi
VIGIL="$VIGIL_HOME/venv/bin/vigil"

if [[ ! -f $VIGIL_HOME/vigil.toml ]]; then
  init=(--name "$NAME" --owner-name "$OWNER_NAME" --os-user "$(id -un)" --timezone "$TIMEZONE" --mcp-port "$PORT")
  [[ -z $WORKSPACE ]] || init+=(--workspace "$WORKSPACE")
  "$VIGIL" --home "$VIGIL_HOME" init "${init[@]}" "${IDENTITIES[@]}"
fi

engine=(engine install)
[[ -z $RUNTIME_BINARY ]] || engine+=(--source "$RUNTIME_BINARY")
(( !ALLOW_UNPINNED )) || engine+=(--allow-unpinned)
"$VIGIL" --home "$VIGIL_HOME" "${engine[@]}"

UNIT="vigil-$SLUG.service"
if ((SERVICE)); then
  mkdir -p "$HOME/.config/systemd/user"
  cat > "$HOME/.config/systemd/user/$UNIT" <<EOF
[Unit]
Description=Vigil always-on mind for $NAME
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$VIGIL_HOME
ExecStart="$VIGIL" --home "$VIGIL_HOME" serve
Restart=always
RestartSec=3
TimeoutStopSec=20
KillMode=control-group
UMask=0077

[Install]
WantedBy=default.target
EOF
  systemctl --user daemon-reload
  systemctl --user enable "$UNIT" >/dev/null
fi

cat <<EOF

Vigil is installed for $NAME.
  home:     $VIGIL_HOME
  service:  $UNIT (enabled; start it after the engine is configured)

Next:
  1. Store the model credential:   $VIGIL --home "$VIGIL_HOME" secret model-key --from-env ANTHROPIC_API_KEY
  2. Choose provider and model:    $VIGIL --home "$VIGIL_HOME" engine init -- --provider anthropic --model claude-opus-5-5 --key-env AGENT_MODEL_API_KEY --reasoning-effort medium
  3. Start:                        systemctl --user start $UNIT
  4. Point the Agent Instance at:  command "$VIGIL", args ["acp", "--home", "$VIGIL_HOME"]
EOF
