"""Durable state: the event inbox, people, notes, journal, held thoughts, tasks and
the decision log. One SQLite file per agent, written only by the daemon.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any
from collections.abc import Iterable

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);

-- Event inbox. Every trigger (a prompt, a due timer, a finished task) is a row first,
-- so a restarted daemon can see what was in flight.
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY, ts REAL NOT NULL, kind TEXT NOT NULL, session TEXT, place TEXT,
  status TEXT NOT NULL, dedup TEXT UNIQUE, data TEXT, started REAL, finished REAL, result TEXT);
CREATE INDEX IF NOT EXISTS events_status ON events(status, ts);

CREATE TABLE IF NOT EXISTS sessions(
  id TEXT PRIMARY KEY, created REAL, updated REAL, runtime TEXT, surface TEXT, place TEXT,
  place_label TEXT, thread TEXT, audience TEXT, turns INTEGER DEFAULT 0, effort TEXT);

CREATE TABLE IF NOT EXISTS people(
  id TEXT PRIMARY KEY, name TEXT NOT NULL, is_owner INTEGER DEFAULT 0, created REAL, updated REAL);
CREATE TABLE IF NOT EXISTS identities(
  surface TEXT NOT NULL, handle TEXT NOT NULL, person TEXT NOT NULL, display TEXT,
  verified INTEGER DEFAULT 0, evidence TEXT, is_agent INTEGER DEFAULT 0, created REAL,
  PRIMARY KEY(surface, handle));
CREATE INDEX IF NOT EXISTS identities_person ON identities(person);

-- Notes: what the agent chose to keep. visibility: here | anywhere | owner.
CREATE TABLE IF NOT EXISTS notes(
  id INTEGER PRIMARY KEY, ts REAL NOT NULL, subject TEXT NOT NULL, text TEXT NOT NULL,
  visibility TEXT NOT NULL, place TEXT, audience TEXT, person TEXT, source_session TEXT,
  archived INTEGER DEFAULT 0);
CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(subject, text);

-- Journal: one row per turn in any place. This is the searchable transcript layer.
CREATE TABLE IF NOT EXISTS journal(
  id INTEGER PRIMARY KEY, ts REAL NOT NULL, session TEXT, surface TEXT, place TEXT,
  place_label TEXT, thread TEXT, audience TEXT, actor TEXT, actor_name TEXT, people TEXT,
  mode TEXT, heard TEXT, said TEXT, did TEXT, stop TEXT, salience REAL, consolidated INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS journal_place ON journal(place, ts);
CREATE INDEX IF NOT EXISTS journal_actor ON journal(actor, ts);
CREATE VIRTUAL TABLE IF NOT EXISTS journal_fts USING fts5(heard, said);

-- Held thoughts and follow-ups: things to bring up with someone, somewhere, later.
CREATE TABLE IF NOT EXISTS thoughts(
  id INTEGER PRIMARY KEY, ts REAL NOT NULL, kind TEXT NOT NULL, text TEXT NOT NULL,
  person TEXT, place TEXT, place_label TEXT, audience TEXT, scope TEXT NOT NULL DEFAULT 'here',
  due REAL NOT NULL, state TEXT NOT NULL, origin_session TEXT, delivered REAL,
  delivered_session TEXT, every_seconds INTEGER, data TEXT);
CREATE INDEX IF NOT EXISTS thoughts_state ON thoughts(state, due);

-- Background work delegated to a runtime outside any conversation turn.
CREATE TABLE IF NOT EXISTS tasks(
  id INTEGER PRIMARY KEY, ts REAL NOT NULL, goal TEXT NOT NULL, runtime TEXT, state TEXT NOT NULL,
  origin_session TEXT, place TEXT, place_label TEXT, person TEXT, audience TEXT, session TEXT,
  result TEXT, started REAL, finished REAL, continuations INTEGER DEFAULT 0, error TEXT);

CREATE TABLE IF NOT EXISTS decisions(
  id INTEGER PRIMARY KEY, ts REAL NOT NULL, purpose TEXT, backend TEXT, ms INTEGER,
  ok INTEGER, answers TEXT, error TEXT);
"""


def now() -> float:
    return time.time()


class Store:
    def __init__(self, path: Path):
        self.path = path
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        if "tools_seen" not in {row["name"] for row in self.q("PRAGMA table_info(sessions)")}:
            self.run("ALTER TABLE sessions ADD COLUMN tools_seen REAL")
        if not self.meta_get("tools_seen_backfilled"):
            # Sessions from before this was recorded are unknown, not broken.
            self.run("UPDATE sessions SET tools_seen=COALESCE(tools_seen, updated, created) WHERE turns>=1")
            self.meta_set("tools_seen_backfilled", "1")

    def close(self) -> None:
        self.db.close()

    # ---- helpers -------------------------------------------------------------------
    def q(self, sql: str, args: Iterable[Any] = ()) -> list[sqlite3.Row]:
        return self.db.execute(sql, tuple(args)).fetchall()

    def one(self, sql: str, args: Iterable[Any] = ()) -> sqlite3.Row | None:
        return self.db.execute(sql, tuple(args)).fetchone()

    def run(self, sql: str, args: Iterable[Any] = ()) -> int:
        cursor = self.db.execute(sql, tuple(args))
        return cursor.lastrowid if cursor.lastrowid is not None else 0

    def meta_get(self, key: str, default: str | None = None) -> str | None:
        row = self.one("SELECT value FROM meta WHERE key=?", (key,))
        return row["value"] if row else default

    def meta_set(self, key: str, value: str) -> None:
        self.run("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    # ---- inbox ---------------------------------------------------------------------
    def event_begin(self, kind: str, session: str | None, place: str | None, data: dict, dedup: str | None = None) -> int | None:
        """Record a trigger as running. Returns None when the same source event was
        already recorded (the dedup key is the source's own identifier)."""
        try:
            return self.run(
                "INSERT INTO events(ts,kind,session,place,status,dedup,data,started) VALUES(?,?,?,?,?,?,?,?)",
                (now(), kind, session, place, "running", dedup, json.dumps(data), now()),
            )
        except sqlite3.IntegrityError:
            return None

    def event_finish(self, event_id: int | None, status: str, result: dict | None = None) -> None:
        if event_id is None:
            return
        self.run(
            "UPDATE events SET status=?, finished=?, result=? WHERE id=?",
            (status, now(), json.dumps(result or {}), event_id),
        )

    def recover_events(self) -> int:
        """Called at start: anything still 'running' was cut off by a restart. Mark it;
        never replay a prompt whose effects are unknown."""
        cursor = self.db.execute("UPDATE events SET status='interrupted', finished=? WHERE status='running'", (now(),))
        return cursor.rowcount

    def prune(self, keep_days: int = 30) -> None:
        cutoff = now() - keep_days * 86400
        self.run("DELETE FROM events WHERE ts<? AND status!='running'", (cutoff,))
        self.run("DELETE FROM decisions WHERE ts<?", (cutoff,))

    # ---- sessions ------------------------------------------------------------------
    def session_touch(self, sid: str, **fields: Any) -> None:
        row = self.one("SELECT id FROM sessions WHERE id=?", (sid,))
        if not row:
            self.run("INSERT INTO sessions(id,created,updated) VALUES(?,?,?)", (sid, now(), now()))
        if fields:
            assignments = ", ".join(f"{key}=?" for key in fields)
            self.run(f"UPDATE sessions SET {assignments}, updated=? WHERE id=?", (*fields.values(), now(), sid))

    def session(self, sid: str) -> sqlite3.Row | None:
        return self.one("SELECT * FROM sessions WHERE id=?", (sid,))

    # ---- decisions -----------------------------------------------------------------
    def log_decision(self, purpose: str, backend: str, ms: int, ok: bool, answers: Any, error: str | None) -> None:
        self.run(
            "INSERT INTO decisions(ts,purpose,backend,ms,ok,answers,error) VALUES(?,?,?,?,?,?,?)",
            (now(), purpose, backend, ms, int(ok), json.dumps(answers) if answers is not None else None, error),
        )
