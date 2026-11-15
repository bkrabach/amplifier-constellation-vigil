"""Typed decisions: fast, non-generative answers to closed questions.

A decision model takes some state and a set of typed questions and scores only the
supplied options in one pass. It writes no text, so its answer can steer control flow
without ever becoming a command. Vigil asks several questions per request and uses
the answers for reflexes around the main model: how hard to think, whether memory is
worth reading, which journal entries are worth consolidating, whether unattended work
is really finished.

Two wire formats cover every provider the Constellation runtime supports:

- ``system_one``: TypeSafe Jev, Microsoft Decision, H2O Lightning, local SystemOne
  servers (``POST .../v1/systemone``).
- ``openai_decisions``: the OpenAI Decisions API (``POST /v1/decisions``).

Answers are advisory. Every caller has a rule-based default for when no backend
answers in time, and no answer here ever grants a permission.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any

import aiohttp

from .config import Config, DecisionBackend
from .store import Store


@dataclass
class Question:
    name: str
    kind: str  # "predicate" | "choice" | "score"
    instructions: str
    choices: dict[str, str] | None = None  # value -> description
    levels: list[str] | None = None


@dataclass
class Answer:
    name: str
    kind: str
    value: Any = None  # probability | chosen value | score
    probabilities: dict[str, float] | None = None
    refused: bool = False

    @property
    def confidence(self) -> float:
        """Probability mass behind the reported answer."""
        if self.kind == "predicate":
            p = float(self.value or 0.0)
            return max(p, 1.0 - p)
        if self.kind == "choice" and self.probabilities and self.value in self.probabilities:
            return float(self.probabilities[self.value])
        return 0.0


def predicate(name: str, instructions: str) -> Question:
    return Question(name, "predicate", instructions)


def choice(name: str, instructions: str, choices: dict[str, str]) -> Question:
    return Question(name, "choice", instructions, choices=choices)


def _wire(backend: DecisionBackend, state: Any, questions: list[Question]) -> dict[str, Any]:
    if backend.provider == "openai_decisions":
        items = []
        for q in questions:
            if q.kind == "predicate":
                items.append({"type": "predicate", "name": q.name, "instructions": q.instructions})
            elif q.kind == "choice":
                items.append({"type": "choice", "name": q.name, "instructions": q.instructions,
                              "choices": [{"value": v, "description": d} for v, d in (q.choices or {}).items()]})
            else:
                items.append({"type": "score", "name": q.name, "instructions": q.instructions,
                              "levels": [{"label": level} for level in q.levels or []]})
        payload = state if isinstance(state, str) else [{"role": "user", "content": [{"type": "input_json", "data": state}]}]
        return {"model": backend.model, "input": payload, "questions": items}
    named: dict[str, Any] = {}
    for q in questions:
        if q.kind == "predicate":
            named[q.name] = {"type": "noul", "instructions": q.instructions}
        elif q.kind == "choice":
            named[q.name] = {"type": "choice", "instructions": q.instructions, "criteria": dict(q.choices or {})}
        else:
            named[q.name] = {"type": "score", "instructions": q.instructions, "criteria": list(q.levels or [])}
    return {"model": backend.model, "state": state, "questions": named}


def _probability(value: Any) -> float:
    p = float(value)
    if not 0.0 <= p <= 1.0:
        raise ValueError("probability out of range")
    return p


def _normalize(backend: DecisionBackend, questions: list[Question], body: dict[str, Any]) -> dict[str, Answer]:
    raw = body.get("answers")
    if backend.provider == "openai_decisions":
        if not isinstance(raw, list):
            raise ValueError("answers missing")
        by_name = {a.get("name"): a for a in raw if isinstance(a, dict)}
    else:
        if not isinstance(raw, dict):
            raise ValueError("answers missing")
        by_name = raw
    out: dict[str, Answer] = {}
    for q in questions:
        a = by_name.get(q.name)
        if not isinstance(a, dict):
            raise ValueError(f"answer for {q.name} missing")
        if a.get("type") == "refusal":
            out[q.name] = Answer(q.name, q.kind, refused=True)
            continue
        if q.kind == "predicate":
            key = "probability" if backend.provider == "openai_decisions" else "noul"
            out[q.name] = Answer(q.name, "predicate", _probability(a[key]))
            continue
        probs_raw = a.get("probabilities")
        probs: dict[str, float] = {}
        if isinstance(probs_raw, list):
            probs = {str(p["value"]): _probability(p["probability"]) for p in probs_raw}
        elif isinstance(probs_raw, dict):
            probs = {str(k): _probability(v) for k, v in probs_raw.items()}
        if q.kind == "choice":
            chosen = a.get("choice")
            if chosen not in (q.choices or {}):
                raise ValueError(f"{q.name}: unknown option")
            out[q.name] = Answer(q.name, "choice", chosen, probs or None)
        else:
            out[q.name] = Answer(q.name, "score", float(a["score"]), probs or None)
    return out


class Decider:
    """Asks the configured backends in order and returns the first complete answer."""

    def __init__(self, config: Config, store: Store | None = None):
        self.config = config
        self.store = store
        self.backends = config.decision_backends()
        self._http: aiohttp.ClientSession | None = None
        #: A backend that just failed is skipped for a short while so a provider outage
        #: costs one timeout, not one per turn.
        self._cooldown: dict[str, float] = {}

    @property
    def available(self) -> bool:
        return bool(self.backends)

    async def close(self) -> None:
        if self._http:
            await self._http.close()

    async def ask(self, purpose: str, state: Any, questions: list[Question], timeout: float | None = None) -> dict[str, Answer] | None:
        """Return answers by question name, or None when no backend could answer."""
        if not self.backends or not questions:
            return None
        if self._http is None:
            self._http = aiohttp.ClientSession()
        for backend in self.backends:
            if self._cooldown.get(backend.name, 0) > time.monotonic():
                continue
            started = time.monotonic()
            try:
                answers = await asyncio.wait_for(self._call(backend, state, questions), timeout or backend.timeout_seconds)
                elapsed = int((time.monotonic() - started) * 1000)
                self._log(purpose, backend.name, elapsed, True, answers, None)
                return answers
            except Exception as error:  # timeout, HTTP, shape: fall through to the next backend
                elapsed = int((time.monotonic() - started) * 1000)
                self._cooldown[backend.name] = time.monotonic() + 60
                self._log(purpose, backend.name, elapsed, False, None, f"{type(error).__name__}: {error}"[:300])
        return None

    async def _call(self, backend: DecisionBackend, state: Any, questions: list[Question]) -> dict[str, Answer]:
        headers = {"Content-Type": "application/json"}
        if backend.auth != "none":
            key = self.config.secret(backend.secret)
            if not key:
                raise RuntimeError(f"secret '{backend.secret}' is not set")
            if backend.auth == "api-key":
                headers["api-key"] = key
            else:
                headers["Authorization"] = f"Bearer {key}"
        assert self._http is not None
        async with self._http.post(backend.endpoint, json=_wire(backend, state, questions), headers=headers, allow_redirects=False) as response:
            text = await response.text()
            if response.status >= 300:
                raise RuntimeError(f"HTTP {response.status}: {text[:160]}")
            return _normalize(backend, questions, json.loads(text))

    def _log(self, purpose: str, backend: str, ms: int, ok: bool, answers: dict[str, Answer] | None, error: str | None) -> None:
        if not self.store:
            return
        compact = None
        if answers:
            compact = {name: {"v": a.value, "p": a.probabilities, "refused": a.refused} for name, a in answers.items()}
        self.store.log_decision(purpose, backend, ms, ok, compact, error)


# ---- the questions Vigil asks ----------------------------------------------------------

EFFORT = choice(
    "effort",
    "How much reasoning does the agent need to respond well to the latest message?",
    {
        "low": "A greeting, thanks, small talk, an acknowledgement, or a simple question answerable in a sentence or two.",
        "medium": "An ordinary question or request that needs some thought, a lookup, or a few tool calls.",
        "high": "A hard or open-ended task: building or debugging something, research, analysis, planning, or long multi-step work.",
    },
)
RECALL = predicate(
    "recall",
    "Responding well to the latest message depends on something the agent may have learned earlier and elsewhere: a past "
    "conversation, a decision, a commitment, a preference, or what someone was working on.",
)


def triage_questions() -> list[Question]:
    return [EFFORT, RECALL]


def salience_questions(count: int) -> list[Question]:
    return [
        predicate(
            f"e{index}",
            f"Entry {index} in the list contains something worth remembering for weeks: a durable fact about a person or project, "
            "a stated preference, a decision, a commitment, or a lesson. Small talk and one-off answers are not worth remembering.",
        )
        for index in range(count)
    ]


FINISHED = choice(
    "finished",
    "The agent was given a goal to complete on its own. Judge its final report against that goal.",
    {
        "done": "The report shows the goal was completed, or completed as far as is possible.",
        "continue": "Work the agent could still do on its own remains: it announced a next step, offered to continue, or listed remaining items.",
        "blocked": "The agent cannot proceed without a person: it needs a decision, an approval, access, or information only a person has.",
    },
)


GAME_TALK = choice(
    "route",
    "The agent is on a voice call and is also a player in a game world right now. Where does the caller's latest remark belong?",
    {
        "game": "It is about what is happening in the game world right now, or asks the agent to do, look at, build, fetch, follow or say something there.",
        "call": "Anything else: work, plans, other places, people, memory, or general conversation.",
    },
)
