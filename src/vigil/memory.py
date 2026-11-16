"""What the agent carries between conversations: people, notes, a journal of turns,
and thoughts it is holding for someone.

The agent remembers the way a colleague on several teams does. What decides whether
a memory may surface in a scene is where it came from and who is here now:

- **Private stays private.** An exchange with one person (a direct message, a
  whisper, a call) is never shown outside that relationship. The owner's private
  sessions see the owner's own private material, not other people's.
- **Shared spaces travel with the people in them.** Something said in a channel or
  in the game world may surface in another place when someone present here was
  part of it there. That person could have brought it up themselves, so the agent
  knowing it is not a leak; whether to say it is the agent's judgment, as it would
  be a colleague's. With nobody in common, it does not surface.
- **Owner-private scenes** see every shared space.

`mind.cross_space` selects the policy: "overlap" (the default, described above),
"hints" (other shared spaces appear only as one-line reminders for the person
speaking) or "strict" (nothing crosses between places).
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

from .config import Config
from .store import Store, now
from .surfaces import Scene, Who

#: How much a surface's own sign-in vouches for an identity. A claim made from a
#: stronger surface about a weaker one is accepted; the reverse waits for the owner.
TRUST = {"os": 3, "conversations": 3, "companion": 3, "slack": 2, "minecraft": 1, "direct": 0}

VISIBILITIES = ("here", "anywhere", "owner")
SCOPES = ("here", "person", "owner")


def _trust(surface: str) -> int:
    return TRUST.get(surface.split(":", 1)[0], 0)


def _fts(query: str) -> str:
    words = re.findall(r"[\w']{2,}", query.lower())[:10]
    return " OR ".join(f'"{word}"' for word in words)


def clip(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


class Memory:
    def __init__(self, store: Store, config: Config):
        self.store = store
        self.config = config
        self._migrate()
        self.seed_owner()

    def _migrate(self) -> None:
        columns = {row["name"] for row in self.store.q("PRAGMA table_info(identities)")}
        if "claimed_person" not in columns:
            self.store.run("ALTER TABLE identities ADD COLUMN claimed_person TEXT")
        if "people" not in {row["name"] for row in self.store.q("PRAGMA table_info(notes)")}:
            self.store.run("ALTER TABLE notes ADD COLUMN people TEXT")
        if "source_id" not in {row["name"] for row in self.store.q("PRAGMA table_info(journal)")}:
            self.store.run("ALTER TABLE journal ADD COLUMN source_id TEXT")
        self.store.run("CREATE INDEX IF NOT EXISTS journal_source ON journal(source_id)")

    # ---- who is here ---------------------------------------------------------------
    @property
    def policy(self) -> str:
        value = self.config.mind("cross_space", "overlap")
        if self.config.mind("cross_group_hints", True) is False:
            return "strict"
        return value if value in ("overlap", "hints", "strict") else "overlap"

    def _present(self, scene: Scene) -> list[str]:
        """The people in this scene whose presence elsewhere lets a shared memory
        surface here: everyone for "overlap", only the speaker for "hints"."""
        if self.policy == "strict":
            return []
        ids = [scene.actor_person] if scene.actor_person else []
        if self.policy == "overlap":
            ids += list(scene.people.values())
        return list(dict.fromkeys(i for i in ids if i))[:16]

    def _overlap(self, scene: Scene, column: str = "people") -> tuple[str, list[Any]]:
        """SQL for "a shared-space row that someone here was part of"."""
        present = self._present(scene)
        if not present:
            return "0", []
        clause = " OR ".join(f"{column} LIKE ?" for _ in present)
        return f"(audience='group' AND ({clause}))", [f'%"{person}"%' for person in present]

    def who_was_there(self, scene: Scene, people_json: str | None) -> str:
        try:
            there = set(json.loads(people_json or "[]"))
        except ValueError:
            return ""
        names = [self.name_of(p) for p in self._present(scene) if p in there]
        return ", ".join(names[:3])

    # ---- rehearsal places ----------------------------------------------------------
    def _rehearsal_prefixes(self) -> list[str]:
        value = self.config.mind("rehearsal_places", []) or []
        return [p for p in value if isinstance(p, str) and p]

    def is_rehearsal(self, place: str | None) -> bool:
        """A place the owner marked as practice: a test workspace, a scratch world."""
        return bool(place) and any(place.startswith(prefix) for prefix in self._rehearsal_prefixes())

    def _outside_rehearsal(self, scene: Scene, column: str = "place") -> tuple[str, list[Any]]:
        """SQL that hides what was learned in a rehearsal place from every scene outside
        one. Inside a rehearsal place everything follows the ordinary rules."""
        prefixes = self._rehearsal_prefixes()
        if not prefixes or self.is_rehearsal(scene.place):
            return "1", []
        clause = " AND ".join(f"substr(COALESCE({column},''),1,?)!=?" for _ in prefixes)
        return f"({clause})", [value for prefix in prefixes for value in (len(prefix), prefix)]

    def known_here(self, scene: Scene, who: dict[str, Any]) -> bool:
        """Someone met only in a rehearsal place is a stranger outside one."""
        if who.get("is_owner") or scene.audience == "owner" or self.is_rehearsal(scene.place):
            return True
        surfaces = [i["surface"] for i in who.get("identities", [])]
        return not surfaces or not all(self.is_rehearsal(surface) for surface in surfaces)

    def forget(self, place: str | None, since: float | None, until: float | None, people: bool, apply: bool) -> dict[str, int]:
        """Remove what was recorded in a place (by key prefix) and/or a time window.

        This is for clearing practice runs and mistakes. It deletes journal entries,
        notes, held thoughts, tasks, sessions and inbox rows. With ``people``, it also
        removes people who are then referenced by nothing, unless their accounts were
        linked across places.
        Without ``apply`` it only counts."""
        if not place and since is None:
            raise ValueError("give a place, a start time, or both")
        counts: dict[str, int] = {}
        for table, stamp in (("journal", "ts"), ("notes", "ts"), ("thoughts", "ts"), ("tasks", "ts"), ("events", "ts"), ("sessions", "created")):
            where, args = ["1"], []
            if place:
                where.append("substr(COALESCE(place,''),1,?)=?")
                args += [len(place), place]
            if since is not None:
                where.append(f"{stamp}>=?")
                args.append(since)
            if until is not None:
                where.append(f"{stamp}<=?")
                args.append(until)
            if table == "events":
                where.append("status!='running'")
            clause = " AND ".join(where)
            ids = [row[0] for row in self.store.q(f"SELECT {'rowid' if table == 'sessions' else 'id'} FROM {table} WHERE {clause}", args)]
            counts[table] = len(ids)
            if not apply or not ids:
                continue
            for start in range(0, len(ids), 400):
                chunk = ids[start:start + 400]
                marks = ",".join("?" * len(chunk))
                if table in ("journal", "notes"):
                    self.store.run(f"DELETE FROM {table}_fts WHERE rowid IN ({marks})", chunk)
                self.store.run(f"DELETE FROM {table} WHERE {'rowid' if table == 'sessions' else 'id'} IN ({marks})", chunk)
        counts["people"] = 0
        if people:
            referenced: set[str] = set()
            for row in self.store.q("SELECT actor, people FROM journal"):
                referenced.add(row["actor"] or "")
                try:
                    referenced.update(json.loads(row["people"] or "[]"))
                except ValueError:
                    pass
            for table in ("notes", "thoughts", "tasks"):
                referenced.update(row["person"] or "" for row in self.store.q(f"SELECT person FROM {table}"))
            for row in self.store.q("SELECT id FROM people WHERE is_owner=0"):
                if row["id"] in referenced:
                    continue
                surfaces = [r["surface"] for r in self.store.q("SELECT surface FROM identities WHERE person=?", (row["id"],))]
                if len(surfaces) > 1 and not (place and all(surface.startswith(place) for surface in surfaces)):
                    continue  # accounts linked across places are worth keeping even with no history
                counts["people"] += 1
                if apply:
                    self.store.run("DELETE FROM identities WHERE person=?", (row["id"],))
                    self.store.run("DELETE FROM people WHERE id=?", (row["id"],))
        return counts

    # ---- people --------------------------------------------------------------------
    def seed_owner(self) -> None:
        owner = self.config.owner
        if not owner:
            return
        if not self.store.one("SELECT id FROM people WHERE id='owner'"):
            self.store.run(
                "INSERT INTO people(id,name,is_owner,created,updated) VALUES('owner',?,1,?,?)",
                (owner.get("name") or owner.get("os_user") or "Owner", now(), now()),
            )
        elif owner.get("name"):
            self.store.run("UPDATE people SET name=? WHERE id='owner'", (owner["name"],))
        seeds = list(owner.get("identities") or [])
        if owner.get("os_user"):
            seeds += [{"surface": s, "handle": owner["os_user"]} for s in ("os", "conversations")]
        for seed in seeds:
            self._bind(seed["surface"], seed["handle"], "owner", seed.get("name") or "", True, "configured by the owner", False)

    def _bind(self, surface: str, handle: str, person: str, display: str, verified: bool, evidence: str, is_agent: bool) -> None:
        self.store.run(
            "INSERT INTO identities(surface,handle,person,display,verified,evidence,is_agent,created) VALUES(?,?,?,?,?,?,?,?) "
            "ON CONFLICT(surface,handle) DO UPDATE SET person=excluded.person, verified=excluded.verified, evidence=excluded.evidence, "
            "display=CASE WHEN excluded.display!='' THEN excluded.display ELSE identities.display END",
            (surface, handle, person, display, int(verified), evidence, int(is_agent), now()),
        )

    def resolve(self, who: Who) -> str:
        """Return the person behind a surface identity, creating a new person the first
        time a handle is seen. A new handle is never merged with someone by name alone."""
        row = self.store.one("SELECT person, display, is_agent FROM identities WHERE surface=? AND handle=?", (who.surface, who.handle))
        if row:
            if who.name and who.name != row["display"]:
                self.store.run("UPDATE identities SET display=? WHERE surface=? AND handle=?", (who.name, who.surface, who.handle))
            if who.is_agent and not row["is_agent"]:
                # Some places only say that a participant is an agent when it speaks.
                self.store.run("UPDATE identities SET is_agent=1 WHERE surface=? AND handle=?", (who.surface, who.handle))
            return row["person"]
        person = "p_" + uuid.uuid4().hex[:10]
        self.store.run(
            "INSERT INTO people(id,name,is_owner,created,updated) VALUES(?,?,0,?,?)",
            (person, who.name or who.handle, now(), now()),
        )
        self._bind(who.surface, who.handle, person, who.name, True, "first seen on this surface", who.is_agent)
        return person

    def person(self, person_id: str | None) -> dict[str, Any] | None:
        if not person_id:
            return None
        row = self.store.one("SELECT * FROM people WHERE id=?", (person_id,))
        if not row:
            return None
        identities = self.store.q("SELECT surface,handle,display,verified,is_agent FROM identities WHERE person=?", (person_id,))
        return {
            "id": row["id"],
            "name": row["name"],
            "is_owner": bool(row["is_owner"]),
            "is_agent": any(i["is_agent"] for i in identities),
            "identities": [dict(i) for i in identities],
        }

    def name_of(self, person_id: str | None) -> str:
        row = self.store.one("SELECT name FROM people WHERE id=?", (person_id,)) if person_id else None
        return row["name"] if row else "someone"

    def find_person(self, name: str) -> str | None:
        name = name.strip().lstrip("@")
        if not name:
            return None
        row = self.store.one("SELECT id FROM people WHERE lower(name)=lower(?) OR id=?", (name, name))
        if row:
            return row["id"]
        row = self.store.one("SELECT person FROM identities WHERE lower(display)=lower(?) OR handle=?", (name, name))
        return row["person"] if row else None

    def rename(self, person_id: str, name: str) -> None:
        self.store.run("UPDATE people SET name=?, updated=? WHERE id=?", (name.strip()[:80], now(), person_id))

    def link(self, scene: Scene, person_id: str, surface: str, handle: str, evidence: str) -> dict[str, Any]:
        """Record that `surface:handle` is the same human as `person_id`.

        The link takes effect only when the claim comes from somewhere that can vouch
        for it: an owner-private scene, or the person themselves speaking on a surface
        with a stronger sign-in than the one being claimed. Otherwise it is stored as
        a claim for the owner to confirm, and unlocks nothing.
        """
        target = self.store.one("SELECT person, is_agent FROM identities WHERE surface=? AND handle=?", (surface, handle))
        if target and target["person"] == person_id:
            return {"linked": True, "note": "already linked"}
        if target and target["is_agent"]:
            return {"linked": False, "note": "that identity belongs to an agent"}
        vouched = scene.audience == "owner" or (
            scene.actor_person == person_id and scene.actor is not None and _trust(scene.actor.surface) > _trust(surface)
        )
        if not vouched:
            if target:
                self.store.run("UPDATE identities SET claimed_person=?, evidence=? WHERE surface=? AND handle=?", (person_id, clip(evidence, 300), surface, handle))
            else:
                placeholder = "p_" + uuid.uuid4().hex[:10]
                self.store.run("INSERT INTO people(id,name,is_owner,created,updated) VALUES(?,?,0,?,?)", (placeholder, handle, now(), now()))
                self._bind(surface, handle, placeholder, "", True, "claimed, not yet seen", False)
                self.store.run("UPDATE identities SET claimed_person=?, evidence=? WHERE surface=? AND handle=?", (person_id, clip(evidence, 300), surface, handle))
            return {"linked": False, "note": "recorded as an unconfirmed claim; the owner can confirm it"}
        old = target["person"] if target else None
        self._bind(surface, handle, person_id, "", True, clip(evidence, 300), False)
        self.store.run("UPDATE identities SET claimed_person=NULL WHERE surface=? AND handle=?", (surface, handle))
        if old and old != person_id:
            self.merge(old, person_id)
        return {"linked": True, "note": "linked"}

    def merge(self, old: str, into: str) -> None:
        if old == into or old == "owner":
            return
        self.store.run("UPDATE identities SET person=? WHERE person=?", (into, old))
        for table, column in (("notes", "person"), ("journal", "actor"), ("thoughts", "person"), ("tasks", "person")):
            self.store.run(f"UPDATE {table} SET {column}=? WHERE {column}=?", (into, old))
        self.store.run("DELETE FROM people WHERE id=?", (old,))

    def settle(self, scene: Scene) -> None:
        """Resolve the scene's identities to people and settle the audience."""
        scene.people = {}
        for who in scene.participants:
            scene.people[f"{who.surface}|{who.handle}"] = self.resolve(who)
        if scene.actor:
            scene.actor_person = self.resolve(scene.actor)
            scene.people.setdefault(f"{scene.actor.surface}|{scene.actor.handle}", scene.actor_person)
        elif scene.binding and scene.binding.get("owner"):
            scene.actor_person = "owner"
        # A one-to-one conversation with a signed-in identity of the owner is owner-private
        # even when the experience has no context provider to say so.
        if scene.audience == "dm" and scene.actor_person == "owner":
            scene.audience = "owner"

    # ---- notes ---------------------------------------------------------------------
    def note_add(self, scene: Scene, subject: str, text: str, visibility: str) -> int:
        if visibility not in VISIBILITIES:
            visibility = "here"
        if scene.audience == "owner" and visibility == "here":
            visibility = "owner"
        person = None
        if subject.lower().startswith("person:"):
            person = self.find_person(subject.split(":", 1)[1])
        note_id = self.store.run(
            "INSERT INTO notes(ts,subject,text,visibility,place,audience,person,source_session,people) VALUES(?,?,?,?,?,?,?,?,?)",
            (now(), subject.strip()[:120], text.strip()[:4000], visibility, scene.place, scene.audience, person, scene.session,
             json.dumps(sorted(set(scene.people.values())))),
        )
        self.store.run("INSERT INTO notes_fts(rowid,subject,text) VALUES(?,?,?)", (note_id, subject, text))
        return note_id

    def note_archive(self, scene: Scene, note_id: int) -> bool:
        where, args = self._note_filter(scene)
        row = self.store.one(f"SELECT id FROM notes WHERE id=? AND {where}", (note_id, *args))
        if not row:
            return False
        self.store.run("UPDATE notes SET archived=1 WHERE id=?", (note_id,))
        self.store.run("DELETE FROM notes_fts WHERE rowid=?", (note_id,))
        return True

    def _note_filter(self, scene: Scene, asked: bool = False) -> tuple[str, list[Any]]:
        """``asked`` marks an explicit lookup. The owner, asking in private, may look up
        what happened in a rehearsal place; nothing from one surfaces unasked."""
        if scene.audience == "owner":
            guard, guard_args = ("1", []) if asked else self._outside_rehearsal(scene)
            return f"archived=0 AND (audience!='dm' OR place=?) AND {guard}", [scene.place, *guard_args]
        guard, guard_args = self._outside_rehearsal(scene)
        shared, args = self._overlap(scene)
        return (f"archived=0 AND {guard} AND (visibility='anywhere' OR (visibility='here' AND (place=? OR {shared})))",
                [*guard_args, scene.place, *args])

    def notes_about(self, scene: Scene, person_id: str, limit: int = 3) -> list[dict[str, Any]]:
        where, args = self._note_filter(scene)
        rows = self.store.q(f"SELECT id,subject,text,ts FROM notes WHERE person=? AND {where} ORDER BY ts DESC LIMIT ?", (person_id, *args, limit))
        return [dict(r) for r in rows]

    def note_subjects(self, scene: Scene, limit: int = 14) -> tuple[int, list[str]]:
        where, args = self._note_filter(scene)
        total = self.store.one(f"SELECT count(*) AS n FROM notes WHERE {where}", args)["n"]
        rows = self.store.q(f"SELECT subject, max(ts) AS t FROM notes WHERE {where} GROUP BY subject ORDER BY t DESC LIMIT ?", (*args, limit))
        return total, [r["subject"] for r in rows]

    # ---- journal -------------------------------------------------------------------
    def journal_add(self, scene: Scene, said: str, did: str, stop: str) -> int:
        row_id = self.store.run(
            "INSERT INTO journal(ts,session,surface,place,place_label,thread,audience,actor,actor_name,people,mode,heard,said,did,stop,source_id) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                now(), scene.session, scene.surface, scene.place, scene.place_label, scene.thread, scene.audience,
                scene.actor_person, self.name_of(scene.actor_person) if scene.actor_person else "",
                json.dumps(sorted(set(scene.people.values()))), scene.mode, scene.heard[:4000], said[:4000], did[:600], stop, scene.source_id,
            ),
        )
        self.store.run("INSERT INTO journal_fts(rowid,heard,said) VALUES(?,?,?)", (row_id, scene.heard[:4000], said[:4000]))
        return row_id

    def overhear(self, scene: Scene) -> int:
        """Keep what else was said in this room. An experience shows recent messages
        with each turn; the agent takes part in only some of them, and the rest would
        otherwise be visible in this session and unknown from every other place. Each
        message is kept once, under the audience rules of the room it was said in."""
        added = 0
        people = json.dumps(sorted(set(scene.people.values())))
        for source_id, who, text, at in scene.overheard:
            if self.store.one("SELECT 1 FROM journal WHERE source_id=?", (source_id,)):
                continue
            person = self.resolve(who)
            row_id = self.store.run(
                "INSERT INTO journal(ts,session,surface,place,place_label,thread,audience,actor,actor_name,people,mode,heard,said,did,stop,source_id) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (at if 0 < at <= now() else now(), scene.session, scene.surface, scene.place, scene.place_label, scene.thread, scene.audience,
                 person, self.name_of(person), people, "overheard", text, "", "", "", source_id),
            )
            self.store.run("INSERT INTO journal_fts(rowid,heard,said) VALUES(?,?,?)", (row_id, text, ""))
            added += 1
        return added

    def _journal_filter(self, scene: Scene, asked: bool = False) -> tuple[str, list[Any]]:
        if scene.audience == "owner":
            guard, guard_args = ("1", []) if asked else self._outside_rehearsal(scene)
            return f"(audience!='dm' OR place=?) AND {guard}", [scene.place, *guard_args]
        if self.policy != "overlap":
            return "place=?", [scene.place]
        guard, guard_args = self._outside_rehearsal(scene)
        shared, args = self._overlap(scene)
        return f"(place=? OR ({shared} AND {guard}))", [scene.place, *args, *guard_args]

    def last_here(self, scene: Scene) -> dict[str, Any] | None:
        row = self.store.one("SELECT ts, heard, said FROM journal WHERE place=? AND COALESCE(mode,'')!='overheard' ORDER BY ts DESC LIMIT 1", (scene.place,))
        return dict(row) if row else None

    def elsewhere(self, scene: Scene, limit: int = 5, since_hours: float = 72) -> list[dict[str, Any]]:
        """Recent turns in other places that this audience may know about."""
        cutoff = now() - since_hours * 3600
        guard, guard_args = self._outside_rehearsal(scene)
        if scene.audience == "owner":
            rows = self.store.q(
                "SELECT ts,surface,place_label,actor_name,heard,said,audience FROM journal "
                f"WHERE place!=? AND audience!='dm' AND COALESCE(mode,'')!='overheard' AND {guard} AND ts>? AND (said!='' OR heard!='') ORDER BY ts DESC LIMIT ?",
                (scene.place, *guard_args, cutoff, limit),
            )
            return [dict(r) for r in rows]
        shared, args = self._overlap(scene)
        if shared == "0":
            return []
        # With the "hints" policy other places show only what was heard, as a reminder.
        said = "said" if self.policy == "overlap" else "''"
        rows = self.store.q(
            f"SELECT ts,surface,place_label,actor_name,heard,{said} AS said,audience,people FROM journal "
            f"WHERE place!=? AND COALESCE(mode,'')!='overheard' AND {shared} AND {guard} AND ts>? AND (heard!='' OR said!='') ORDER BY ts DESC LIMIT ?",
            (scene.place, *args, *guard_args, cutoff, min(limit, 3)),
        )
        return [dict(r) | {"with": self.who_was_there(scene, r["people"])} for r in rows]

    def search(self, scene: Scene, query: str, limit: int = 6, asked: bool = True) -> list[dict[str, Any]]:
        """``asked`` is True for a lookup the agent made itself. The recall that the
        mind adds to a brief on its own passes False, so practice stays out of it."""
        match = _fts(query)
        if not match:
            return []
        hits: list[dict[str, Any]] = []
        where, args = self._note_filter(scene, asked=asked)
        for row in self.store.q(
            f"SELECT n.id,n.subject,n.text,n.ts,n.place,n.audience FROM notes_fts f JOIN notes n ON n.id=f.rowid "
            f"WHERE notes_fts MATCH ? AND {where} ORDER BY bm25(notes_fts) LIMIT ?",
            (match, *args, limit),
        ):
            hits.append({"kind": "note", "id": row["id"], "subject": row["subject"], "text": row["text"], "ts": row["ts"]})
        where, args = self._journal_filter(scene, asked=asked)
        for row in self.store.q(
            f"SELECT j.id,j.ts,j.place,j.place_label,j.actor_name,j.heard,j.said,j.people FROM journal_fts f JOIN journal j ON j.id=f.rowid "
            f"WHERE journal_fts MATCH ? AND {where} ORDER BY bm25(journal_fts) LIMIT ?",
            (match, *args, limit),
        ):
            hits.append({
                "kind": "journal", "id": row["id"], "ts": row["ts"], "where": row["place_label"], "who": row["actor_name"],
                "heard": clip(row["heard"], 500), "said": clip(row["said"], 500),
                "elsewhere": row["place"] != scene.place, "with": self.who_was_there(scene, row["people"]),
            })
        return hits

    # ---- thoughts ------------------------------------------------------------------
    def thought_add(self, scene: Scene, kind: str, text: str, due: float, scope: str = "here", person: str | None = None,
                    every_seconds: int | None = None, data: dict | None = None) -> int:
        if scope not in SCOPES:
            scope = "here"
        return self.store.run(
            "INSERT INTO thoughts(ts,kind,text,person,place,place_label,audience,scope,due,state,origin_session,every_seconds,data) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (now(), kind, text.strip()[:4000], person or scene.actor_person, scene.place, scene.place_label, scene.audience,
             scope, due, "pending", scene.session, every_seconds, json.dumps(data or {})),
        )

    def thoughts_due(self, scene: Scene, horizon: float = 0.0) -> list[dict[str, Any]]:
        """Thoughts that may be delivered in this scene now."""
        rows = self.store.q("SELECT * FROM thoughts WHERE state='pending' AND due<=? ORDER BY due LIMIT 40", (now() + horizon,))
        out = []
        practice_only = not self.is_rehearsal(scene.place)
        for row in rows:
            scope, origin = row["scope"], row["audience"]
            if practice_only and scope == "person" and self.is_rehearsal(row["place"]):
                continue  # a promise made in practice is kept in practice
            if scope == "here":
                ok = row["place"] == scene.place
            elif scope == "owner":
                ok = scene.audience == "owner"
            else:  # person: wherever I next talk with them, as long as that is no wider an audience
                ok = row["person"] == scene.actor_person and (scene.audience == "owner" or row["place"] == scene.place or (origin == "group" and scene.audience in ("group", "dm")) or (origin == "dm" and scene.audience == "dm"))
            if ok:
                out.append(dict(row))
        return out[:6]

    def thoughts_pending(self, place: str | None = None) -> list[dict[str, Any]]:
        if place:
            rows = self.store.q("SELECT * FROM thoughts WHERE state='pending' AND place=? ORDER BY due", (place,))
        else:
            rows = self.store.q("SELECT * FROM thoughts WHERE state='pending' ORDER BY due")
        return [dict(r) for r in rows]

    def thoughts_delivered(self, ids: list[int], session: str) -> None:
        for thought_id in ids:
            row = self.store.one("SELECT every_seconds, due FROM thoughts WHERE id=?", (thought_id,))
            if not row:
                continue
            if row["every_seconds"]:
                due = row["due"]
                while due <= now():
                    due += row["every_seconds"]
                self.store.run("UPDATE thoughts SET due=?, delivered=?, delivered_session=? WHERE id=?", (due, now(), session, thought_id))
            else:
                self.store.run("UPDATE thoughts SET state='delivered', delivered=?, delivered_session=? WHERE id=?", (now(), session, thought_id))

    def thought_cancel(self, scene: Scene | None, thought_id: int) -> bool:
        row = self.store.one("SELECT place, person, state FROM thoughts WHERE id=?", (thought_id,))
        if not row or row["state"] != "pending":
            return False
        if scene is not None and scene.audience != "owner" and row["place"] != scene.place and row["person"] != scene.actor_person:
            return False
        self.store.run("UPDATE thoughts SET state='cancelled' WHERE id=?", (thought_id,))
        return True
