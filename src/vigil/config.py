"""Vigil home layout and configuration.

One Vigil home belongs to one agent. It holds the configuration, the private
secrets, the state database and the runtime's own home. The module is
standard-library only so the ACP shim can import it cheaply.
"""

from __future__ import annotations

import os
import stat
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CONFIG_FILE = "vigil.toml"

#: Variables a spawned runtime inherits from the daemon. Everything else is dropped,
#: which matches how Instance and the adapters launch their ACP children.
PASS_ENV = ("PATH", "USER", "LANG", "LC_ALL", "TMPDIR", "TEMP", "TMP", "SSL_CERT_FILE", "SSL_CERT_DIR")


@dataclass
class RuntimeSpec:
    """An ACP agent Vigil can drive: the built-in native runtime or any other ACP command."""

    name: str
    command: list[str]
    cwd: str
    env: dict[str, str] = field(default_factory=dict)
    secret_env: dict[str, str] = field(default_factory=dict)
    #: "native" runtimes get engine-specific features (per-turn effort, context snapshots).
    kind: str = "acp"
    #: An ACP auth method the runtime accepts without a person, such as "api-key" when
    #: the key is supplied through `secret_env`. Used when the runtime asks to authenticate.
    auth_method: str = ""
    #: Set false for an agent that advertises HTTP MCP but refuses servers a client adds
    #: (some fail the whole conversation). Vigil then keeps its tools back and tells the
    #: model it has no notebook there; the presence brief still works.
    mind_tools: bool = True


@dataclass
class DecisionBackend:
    name: str
    provider: str  # "system_one" | "openai_decisions"
    endpoint: str
    model: str
    auth: str = "bearer"  # "bearer" | "api-key" | "none"
    secret: str = ""
    timeout_seconds: float = 4.0


class Config:
    def __init__(self, home: Path, raw: dict[str, Any]):
        self.home = home
        self.raw = raw

    # ---- loading -------------------------------------------------------------------
    @classmethod
    def load(cls, home: str | os.PathLike[str]) -> Config:
        home = Path(home).expanduser().resolve()
        with open(home / CONFIG_FILE, "rb") as handle:
            return cls(home, tomllib.load(handle))

    def expand(self, value: str) -> str:
        return value.replace("${VIGIL_HOME}", str(self.home))

    def get(self, path: str, default: Any = None) -> Any:
        node: Any = self.raw
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    # ---- paths ---------------------------------------------------------------------
    @property
    def socket_path(self) -> Path:
        return socket_path(self.home)

    @property
    def db_path(self) -> Path:
        return self.home / "state.sqlite3"

    @property
    def memory_dir(self) -> Path:
        return self.home / "memory"

    @property
    def log_dir(self) -> Path:
        return self.home / "logs"

    @property
    def secrets_dir(self) -> Path:
        return self.home / "secrets"

    @property
    def workspace(self) -> str:
        return self.expand(self.get("agent.workspace", "${VIGIL_HOME}/workspace"))

    # ---- identity ------------------------------------------------------------------
    @property
    def agent_name(self) -> str:
        return self.get("agent.name", "Agent")

    @property
    def timezone(self) -> str:
        return self.get("agent.timezone", "UTC")

    @property
    def owner(self) -> dict[str, Any]:
        return self.get("owner", {}) or {}

    # ---- server --------------------------------------------------------------------
    @property
    def mcp_port(self) -> int:
        return int(self.get("server.mcp_port", 47821))

    @property
    def mind_url(self) -> str:
        return f"http://127.0.0.1:{self.mcp_port}/mcp"

    # ---- secrets -------------------------------------------------------------------
    def secret(self, name: str) -> str | None:
        """Read a named secret. Files must be private to the owner, like every other
        Constellation component requires."""
        if not name or "/" in name or name.startswith("."):
            return None
        path = self.secrets_dir / name
        try:
            info = path.lstat()
        except OSError:
            return None
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            return None
        return path.read_text().strip() or None

    def write_secret(self, name: str, value: str) -> None:
        self.secrets_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = self.secrets_dir / name
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(value.strip() + "\n")
        os.chmod(path, 0o600)

    # ---- runtimes ------------------------------------------------------------------
    @property
    def main_runtime(self) -> str:
        return self.get("runtime.main", "native")

    @property
    def background_runtime(self) -> str:
        return self.get("runtime.background", self.main_runtime)

    def runtime_names(self) -> list[str]:
        return sorted((self.get("runtimes", {}) or {}).keys())

    def runtime(self, name: str) -> RuntimeSpec:
        table = (self.get("runtimes", {}) or {}).get(name)
        if not table:
            raise KeyError(f"Runtime '{name}' is not configured in {CONFIG_FILE}")
        command = [self.expand(part) for part in table.get("command", [])]
        if not command:
            raise ValueError(f"Runtime '{name}' has no command")
        return RuntimeSpec(
            name=name,
            command=command,
            cwd=self.expand(table.get("cwd", self.workspace)),
            env={k: self.expand(str(v)) for k, v in (table.get("env") or {}).items()},
            secret_env=dict(table.get("secret_env") or {}),
            kind=table.get("kind", "acp"),
            auth_method=str(table.get("auth_method", "")),
            mind_tools=table.get("mind_tools", True) is not False,
        )

    def runtime_env(self, spec: RuntimeSpec) -> dict[str, str]:
        env = {key: os.environ[key] for key in PASS_ENV if key in os.environ}
        env["HOME"] = str(self.home / "runtime-home" / spec.name)
        Path(env["HOME"]).mkdir(mode=0o700, parents=True, exist_ok=True)
        env.update(spec.env)
        for variable, secret_name in spec.secret_env.items():
            value = self.secret(secret_name)
            if value:
                env[variable] = value
        return env

    # ---- decisions -----------------------------------------------------------------
    def decision_backends(self) -> list[DecisionBackend]:
        out = []
        for table in self.get("decisions.backends", []) or []:
            out.append(
                DecisionBackend(
                    name=table["name"],
                    provider=table.get("provider", "system_one"),
                    endpoint=table["endpoint"],
                    model=table["model"],
                    auth=table.get("auth", "bearer"),
                    secret=table.get("secret", ""),
                    timeout_seconds=float(table.get("timeout_seconds", 4.0)),
                )
            )
        return out

    # ---- mind ----------------------------------------------------------------------
    def mind(self, key: str, default: Any) -> Any:
        return self.get(f"mind.{key}", default)


def ensure_layout(home: Path) -> None:
    for sub in ("run", "logs", "secrets", "memory", "workspace"):
        (home / sub).mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(home, 0o700)


#: A Unix socket's path has to fit the kernel's address structure, terminator included:
#: 108 bytes on Linux, 104 on macOS.
SOCKET_PATH_LIMIT = 103 if sys.platform == "darwin" else 107


def socket_path(home: Path) -> Path:
    return home / "run" / "vigil.sock"


def socket_path_problem(home: Path) -> str | None:
    """Why the daemon could not listen under this home, or None. Without the daemon the
    bridge starts the runtime alone, and the agent then has no memory tools, so a home
    that cannot work is refused when it is created and when the daemon starts."""
    path = socket_path(home)
    size = len(os.fsencode(str(path)))
    if size > SOCKET_PATH_LIMIT:
        return f"{path} is {size} bytes long; a Unix socket path can be at most {SOCKET_PATH_LIMIT}. Use a home with a shorter path."
    return None
