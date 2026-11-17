"""Provision and configure the default runtime: the Constellation native engine.

Vigil does not re-implement a model loop. Its default runtime is the same
`agent-runtime` binary an Instance installs, so every provider and model that engine
supports works here unchanged, and new ones arrive with an engine update. This module
downloads a pinned release, verifies it, and writes the engine settings Vigil cares
about (standing instructions for the bound model, limits, and trust in the mind's
own tools).
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
import tarfile
import tempfile
import urllib.request
from pathlib import Path
from typing import Any

from . import prompts
from .config import Config

REPOSITORY = "microsoft/amplifier-constellation-runtime"

#: Pinned releases. Hashes are the ones each release publishes in its `SHA256SUMS` and
#: per-package manifest. From 0.15.8 the engine states each tool's identity itself and
#: checks a built-in tool's arguments against its schema before asking permission.
#: From 0.15.9 a request for a tool name the session does not list gets an error for
#: that call, and the model can ask again, instead of the whole turn failing.
#: From 0.15.11 a session can compact after a reply with reasoning and no text; before
#: that, such a turn failed and the input waiting behind it was lost. 0.15.10 added an
#: approval-policy profile that applies only when the client negotiates it.
RELEASES: dict[str, dict[str, dict[str, str]]] = {
    "0.15.11": {
        "linux-aarch64": {
            "asset_sha256": "b0ad6b987ce3a75e75b2c5aadad49c9f295daf86c769f047e4aa4f4e8776275a",
            "binary_sha256": "31c1e4c98e3c4483a09d9be089424bd724e268206c29f08c686dc4c99ba611a7",
        },
        "linux-x86_64": {
            "asset_sha256": "aa3cc6f8ff5007369930ccc550bd63e77215b36ffbfdc6df02e816fdec3acb44",
            "binary_sha256": "a923d03d44662d150cababe9ebdec60c37e1d99de8d1316193640416039b221b",
        },
    },
    "0.15.9": {
        "linux-aarch64": {
            "asset_sha256": "ff7c2f4920d44a63bc0bb2a73fff10042a5183bc3631a79575f709c549c7c3bc",
            "binary_sha256": "b71033c94664515a6df1003f3dde4d3fb4b5135c43ee58b525d26edd41be4313",
        },
        "linux-x86_64": {
            "asset_sha256": "e255de44ab02ecb2877704294b63f10cda99bd05bc4b5fb5cd1a492f01685fd5",
            "binary_sha256": "a03edd7de9f841acff9ee274e998059ef1ef1c51b57d16afadaa7decdb1d6845",
        },
    },
    "0.15.8": {
        "linux-aarch64": {
            "asset_sha256": "da83f8d2a13d55da29a080edec746f19f7a9e5cafecfd37f9a63ee915857770e",
            "binary_sha256": "595983551adf056317f5a77cf7f2f9b2624c68dcfd3f5f75c8db951cebabfe5a",
        },
        "linux-x86_64": {
            "asset_sha256": "d224218238d727551691ae5c53f13d070b60a9ca5bd26d7df0b870c1f213b9e1",
            "binary_sha256": "00969373a7058cb3f20ae43bf7f8d32c00acefd3be60d1a984706d3b4a6f7c76",
        },
    },
    "0.15.7": {
        "linux-aarch64": {
            "asset_sha256": "2939784cbe34da57183d965cf16115ef244c51c763c54682b0469170b3c550b0",
            "binary_sha256": "b9fd71aaa1b5c88f624a9cc89bf3028a84cdf532a7586b93c76542421ddb28fb",
        },
        "linux-x86_64": {
            "asset_sha256": "814c0bd3009ca19958190ad55d9d7bea93300130226282fb47180c14b99ba8d4",
            "binary_sha256": "e20849084f3c161c51a73100128b10992356e9fb4be9d764709ece1e9c2d2eba",
        },
    },
}
DEFAULT_VERSION = "0.15.11"

#: Mind tools that change Vigil's own memory. The engine is told it may call them
#: without asking, since nothing outside Vigil is affected.
MUTATING_TOOLS = ("mind_note", "mind_forget", "mind_link_identity", "mind_follow_up", "mind_cancel", "mind_delegate")

#: Sibling models offered for per-session switching, by provider kind.
SIBLINGS = {
    "anthropic": ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5", "claude-fable-5-1"],
    "openai": ["gpt-6.1-sol", "gpt-6-astra", "gpt-6.1-terra", "gpt-6-luna"],
    "gemini": ["gemini-4-argon", "gemini-3.8-flash", "gemini-3.5-flash-lite"],
}


def platform_key() -> str:
    machine = platform.machine().lower()
    arch = {"arm64": "aarch64", "amd64": "x86_64"}.get(machine, machine)
    return f"{platform.system().lower()}-{arch}"


def binary_path(config: Config) -> Path:
    return config.home / "engine" / "bin" / "agent-runtime"


def engine_home(config: Config) -> Path:
    return config.home / "engine" / "home"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def install(config: Config, version: str = DEFAULT_VERSION, source: str | None = None, allow_unpinned: bool = False) -> Path:
    """Install the runtime binary. `source` may be a local binary to copy instead of
    downloading: a copy of the pinned release for hosts without access to GitHub, or,
    with `allow_unpinned`, a build of your own. What was installed is recorded in
    `engine/installation.json`."""
    target = binary_path(config)
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    pins = RELEASES.get(version, {}).get(platform_key())
    origin = "pinned release"
    if source:
        data = Path(source).read_bytes()
        if not pins or hashlib.sha256(data).hexdigest() != pins["binary_sha256"]:
            if not allow_unpinned:
                raise RuntimeError("The supplied binary is not the pinned release. Pass --allow-unpinned to install a build of your own.")
            origin = f"unpinned local build from {Path(source).resolve()}"
    else:
        if not pins:
            raise RuntimeError(f"No pinned runtime {version} for {platform_key()}. Pass --source with a local agent-runtime binary.")
        asset = f"agent-runtime-{version}-{platform_key()}.tar.gz"
        url = f"https://github.com/{REPOSITORY}/releases/download/v{version}/{asset}"
        with tempfile.TemporaryDirectory() as scratch:
            archive = Path(scratch) / asset
            with urllib.request.urlopen(url, timeout=120) as response, open(archive, "wb") as out:
                while chunk := response.read(1 << 20):
                    out.write(chunk)
            if _sha256(archive) != pins["asset_sha256"]:
                raise RuntimeError("Downloaded runtime archive failed its checksum.")
            with tarfile.open(archive) as tar:
                member = tar.getmember("agent-runtime")
                extracted = tar.extractfile(member)
                assert extracted is not None
                data = extracted.read()
            if hashlib.sha256(data).hexdigest() != pins["binary_sha256"]:
                raise RuntimeError("Runtime binary failed its checksum.")
    staged = target.with_suffix(".new")
    staged.write_bytes(data)
    os.chmod(staged, 0o755)
    os.replace(staged, target)
    record = {"version": version, "origin": origin, "binary_sha256": hashlib.sha256(data).hexdigest(), "platform": platform_key()}
    (target.parent.parent / "installation.json").write_text(json.dumps(record, indent=2) + "\n")
    return target


def _run(config: Config, *args: str, stdin: str | None = None) -> str:
    result = subprocess.run([str(binary_path(config)), "--home", str(engine_home(config)), *args],
                            input=stdin, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout).strip()[-600:])
    return result.stdout


def init(config: Config, provider_args: list[str]) -> None:
    """Create the engine home. `provider_args` are passed to `agent-runtime init`
    verbatim (--provider, --model, --endpoint, --auth, ...), so every provider the
    engine knows can be selected exactly as its own documentation describes."""
    home = engine_home(config)
    home.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    Path(config.workspace).mkdir(mode=0o700, parents=True, exist_ok=True)
    if (home / "runtime.json").exists():
        _run(config, "configure-provider", *provider_args)
    else:
        _run(config, "init", "--workspace", config.workspace, "--name", config.agent_name, *provider_args)


def inspect(config: Config) -> dict[str, Any]:
    return json.loads(_run(config, "config-inspect"))


def model(config: Config) -> str:
    try:
        return json.loads((engine_home(config) / "runtime.json").read_text())["provider"]["model"]
    except (OSError, KeyError, ValueError):
        return ""


def apply_settings(config: Config) -> dict[str, Any]:
    """Write Vigil's engine settings: standing instructions for the bound model,
    working limits, and the mind's tools as trusted. Idempotent."""
    document = inspect(config)
    settings = document["config"]
    provider = settings["provider"]
    bound = provider["model"]
    settings["name"] = config.agent_name
    settings["instructions"] = prompts.instructions(bound)
    overrides = config.get("engine", {}) or {}
    limits = settings["limits"]
    limits["max_model_calls"] = int(overrides.get("max_model_calls", 40))
    limits["max_tool_calls"] = int(overrides.get("max_tool_calls", 120))
    limits["max_output_tokens"] = int(overrides.get("max_output_tokens", 32000))
    for key in ("mode", "command_network", "command_boundary"):
        if key in overrides:
            settings[key] = overrides[key]
    url = config.mind_url
    kept = [entry for entry in settings.get("allowed_mcp_mutations", []) if not entry.startswith(url + "#")]
    # An owner can pre-approve named tools of an experience (an emoji reaction, say), as
    # "<exact MCP endpoint>#<tool>", so the agent is not stopped for approval each time.
    for extra in overrides.get("allowed_mcp_mutations", []) or []:
        if isinstance(extra, str) and "#" in extra and not extra.startswith(url + "#") and extra not in kept:
            kept.append(extra)
    settings["allowed_mcp_mutations"] = kept + [f"{url}#{tool}" for tool in MUTATING_TOOLS]
    trusted = [entry for entry in settings.get("trusted_mcp_readonly_urls", []) if entry != url]
    for extra in overrides.get("trusted_mcp_readonly_urls", []) or []:
        if extra not in trusted:
            trusted.append(extra)
    settings["trusted_mcp_readonly_urls"] = trusted + [url]
    # Let people show the agent things: photos on a call, images and PDFs in a channel.
    native = dict(provider.get("native") or {})
    if provider["kind"] != "mock":
        native.setdefault("image_input", True)
    if provider["kind"] in ("openai", "azure-openai", "anthropic", "gemini"):
        native.setdefault("pdf_input", True)
    # Tools the model provider runs itself (reading a web page, searching the web) are
    # a grant the owner makes by name. The list in vigil.toml is the whole grant: an
    # empty list takes it away. With no setting, whatever the engine holds is left alone.
    if "native_tools" in overrides:
        tools = overrides["native_tools"] or []
        if not isinstance(tools, list) or not all(isinstance(tool, dict) for tool in tools):
            raise RuntimeError("[engine] native_tools must be a list of tables, one per provider tool.")
        native["tools"] = [dict(tool) for tool in tools]
    if "native_betas" in overrides:
        native["anthropic_betas"] = [str(beta) for beta in overrides["native_betas"] or []]
    if native:
        provider["native"] = native
    efforts = ["low", "medium", "high"]
    if provider.get("reasoning_effort") in efforts:
        names = [bound] + [m for m in SIBLINGS.get(provider["kind"], []) if m != bound]
        settings["model_options"] = [
            {"model": name, "name": name, "reasoning_effort": provider["reasoning_effort"], "reasoning_efforts": efforts} for name in names
        ]
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
        json.dump(settings, handle)
        path = handle.name
    try:
        _run(config, "config-validate", path)
        _run(config, "configure", "--expected-revision", document["revision"], path)
    finally:
        os.unlink(path)
    return settings
