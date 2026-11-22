"""Print the presence brief a turn in one place would get, from a COPY of a home.

    python tests/show_brief.py COPY PLACE [--mode wake] [--due ID ...] [--every-line] [--src DIR]

COPY is a directory holding a copy of a home's ``vigil.toml`` and ``state.sqlite3``
(take the database with sqlite3's ``.backup``). The script writes to it, so it refuses
a home that has a daemon's socket. ``--due`` makes held follow-ups due in the copy.
The scene is built from the home's records, not from a prompt, so what only a reader
knows has to be said: ``--every-line`` for a place whose connector starts a turn for
every public line and shows nobody the reply (the game connector since its PR 162).
``--src`` reads Vigil from another checkout's ``src``, to compare two versions on the
same records without a model call. The output is the agent's private memory: keep it
out of anything published.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("copy", type=Path)
    parser.add_argument("place")
    parser.add_argument("--mode", default="direct", help="direct, ambient, continuation or wake")
    parser.add_argument("--due", type=int, nargs="*", default=[], help="ids of held follow-ups to make due in the copy")
    parser.add_argument("--every-line", action="store_true", help="the place's connector starts a turn for every public line")
    parser.add_argument("--src", type=Path, help="another checkout's src directory")
    args = parser.parse_args()
    if any((args.copy / "run").glob("*.sock")):
        print("this home has a daemon's socket: work on a copy", file=sys.stderr)
        return 2
    if args.src:
        sys.path.insert(0, str(args.src))
    import vigil
    from vigil.brief import compose
    from vigil.config import Config
    from vigil.memory import Memory
    from vigil.store import Store
    from vigil.surfaces import Scene

    config = Config.load(args.copy)
    memory = Memory(Store(args.copy / "state.sqlite3"), config)
    row = memory.store.one("SELECT id, surface, place_label, audience FROM sessions WHERE place=? ORDER BY updated DESC LIMIT 1", (args.place,))
    if row is None:
        print(f"no session of this home is in {args.place}", file=sys.stderr)
        return 2
    for ident in args.due:
        memory.store.run("UPDATE thoughts SET due=? WHERE id=? AND state='pending'", (time.time() - 1, ident))
    scene = Scene(session=row["id"], surface=row["surface"], place=args.place, place_label=row["place_label"] or "", audience=row["audience"] or "unknown",
                  mode=args.mode, wake="check" if args.mode == "wake" else None, reminder=args.mode == "wake")
    if args.every_line:
        scene.speech_by_tool = scene.every_line = True
    memory.settle(scene)
    text, surfaced = compose(scene, memory, config)
    print(f"# vigil {vigil.__version__} from {Path(vigil.__file__).parent}; follow-ups shown as due: {surfaced}")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
