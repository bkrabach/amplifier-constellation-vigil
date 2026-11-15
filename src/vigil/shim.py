"""`vigil acp`: the thin stdio bridge an Agent Instance launches as its backend.

Every experience starts a short-lived ACP process (Slack and Conversations do it per
turn). The agent itself must not live in that process, so this bridge only joins its
stdin/stdout to the always-on daemon's socket. It imports nothing heavy.

If the daemon cannot be reached, the bridge runs the main runtime directly. The agent
then still answers, without its mind, instead of going dark.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time


def _log(home: str, text: str) -> None:
    try:
        os.makedirs(os.path.join(home, "logs"), mode=0o700, exist_ok=True)
        with open(os.path.join(home, "logs", "shim.log"), "a") as handle:
            handle.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} pid={os.getpid()} {text}\n")
    except OSError:
        pass


def _fallback(home: str, reason: str) -> None:
    from .config import Config

    _log(home, f"daemon unavailable ({reason}); running the main runtime directly")
    config = Config.load(home)
    spec = config.runtime(config.main_runtime)
    env = config.runtime_env(spec)
    try:
        os.chdir(spec.cwd)
    except OSError:
        pass
    os.execvpe(spec.command[0], spec.command, env)


def _connect(path: str) -> socket.socket:
    last: OSError | None = None
    for delay in (0.0, 0.15, 0.4):  # ride out a daemon restart
        if delay:
            time.sleep(delay)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.connect(path)
            return sock
        except OSError as error:
            sock.close()
            last = error
    raise last or OSError("connect failed")


def main(home: str) -> None:
    home = os.path.abspath(os.path.expanduser(home))
    try:
        sock = _connect(os.path.join(home, "run", "vigil.sock"))
    except OSError as error:
        _fallback(home, str(error))
        return
    hello = {"vigil": "attach", "pid": os.getpid(), "cwd": os.getcwd()}
    sock.sendall((json.dumps(hello) + "\n").encode())

    def upstream() -> None:
        try:
            while True:
                data = os.read(0, 65536)
                if not data:
                    break
                sock.sendall(data)
        except OSError:
            pass
        try:
            sock.shutdown(socket.SHUT_WR)
        except OSError:
            pass

    threading.Thread(target=upstream, daemon=True).start()
    try:
        while True:
            data = sock.recv(65536)
            if not data:
                break
            view = memoryview(data)
            while view:
                written = os.write(1, view)
                view = view[written:]
    except OSError:
        pass
    sys.stdout.flush()
    os._exit(0)
