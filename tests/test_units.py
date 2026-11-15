"""Scene reading, audience rules, identity links, decision wire formats, replay filter."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vigil import decisions, surfaces
from vigil.brief import CLOSE, OPEN, ReplayFilter, compose
from vigil.config import Config, DecisionBackend
from vigil.memory import Memory
from vigil.mind import spoken
from vigil.store import Store
from vigil.surfaces import Scene, Who


def instance_block(current: dict | None) -> dict:
    data = {"current": current, "orientation": {}} if current else {"sources": []}
    text = ("[Configured agent instance context]\nName: Polaris\nPurpose: p\nInstructions:\nx\nProfiles:\n\n[Collaboration guidance]\ng\n[/Collaboration guidance]\n"
            "\n[Conversation orientation]\nYou are the same individual across experiences. Use context_read for detail.\n" + json.dumps(data)
            + "\n[/Conversation orientation]\n[/Configured agent instance context]")
    return {"type": "text", "text": text, "_meta": {"io.github.bkrabach.agent-instance": {"context": True}}}


def slack_block(channel="C1", sender="U2", text="hi", **extra) -> dict:
    data = {"conversation": {"surface": "slack", "workspace": "T1", "channel": channel, "thread": "17.1"}, "your_slack_user_id": "UBOT",
            "trigger": {"sender": sender, "text": text, "bot": False},
            "participants_from_slack": [{"id": sender, "name": "Dana", "is_bot": False}, {"id": "UBOT", "name": "Polaris", "is_bot": True}, {"id": "UB2", "name": "Maker", "is_bot": True}], **extra}
    return {"type": "text", "text": "Slack conversation delivery. x\nYou have been addressed directly.\nLatest conversation projection:\n" + json.dumps(data)}


@pytest.fixture
def memory(tmp_path: Path) -> Memory:
    (tmp_path / "vigil.toml").write_text('[agent]\nname="P"\n[owner]\nname="Olive"\nos_user="olive"\nidentities=[{surface="slack:T1",handle="UOWNER"}]\n')
    config = Config.load(tmp_path)
    return Memory(Store(tmp_path / "s.sqlite3"), config)


# ---- scenes ------------------------------------------------------------------------

def test_slack_group_scene():
    scene = surfaces.read("s", [instance_block(None) | {}, slack_block()])
    assert (scene.surface, scene.place, scene.thread, scene.mode) == ("slack", "slack:T1:C1", "17.1", "direct")
    assert scene.actor == Who("slack:T1", "U2", "Dana", False)
    assert [w.handle for w in scene.participants] == ["U2", "UB2"] and scene.participants[1].is_agent
    assert scene.heard == "hi" and scene.identity_name == "Polaris"


def test_verified_binding_decides_place_and_audience():
    binding = {"provider_id": "slack:abc", "conversation_id": "C1", "thread_id": "", "owner": False, "actor_id": "U2"}
    scene = surfaces.read("s", [instance_block(binding), slack_block(channel="D5")])
    assert scene.place == "slack:abc|C1" and scene.audience == "dm"
    owner = surfaces.read("s", [instance_block(binding | {"owner": True}), slack_block()])
    assert owner.audience == "owner"  # the provider-verified binding outranks the adapter's wording


def test_owner_client_without_grant_is_owner_audience():
    scene = surfaces.read("s", [instance_block(None), {"type": "text", "text": "status?"}])
    assert scene.audience == "owner" and scene.heard == "status?" and scene.surface == "direct"


def test_unknown_experience_is_restrictive():
    scene = surfaces.read("s9", [{"type": "text", "text": "hello from somewhere new"}])
    assert scene.audience == "unknown" and scene.place == "direct" and scene.heard.startswith("hello")


def test_continuation_and_wake_marker():
    block = slack_block(accepted_work_continuation={"goal": "[vigil:wake:0123abcd] come back"})
    scene = surfaces.read("s", [block])
    assert scene.mode == "continuation" and scene.wake == "0123abcd"
    theirs = surfaces.read("s", [slack_block(accepted_work_continuation={"goal": "check the build"})])
    assert theirs.mode == "continuation" and theirs.wake is None


def test_conversations_room_and_companion_call():
    meta = {"com.microsoft.amplifier.conversations": {"conversationId": "r1", "author": "olive", "messageId": "m"}}
    room = ('You are participating as Polaris (connection polaris). This is a group conversation. Decide.\nRoom participants: '
            '{"people":["olive","sam"],"agents":["polaris","maker"]}. Human speaker: olive. Trigger message: m.\nOnly completed.\n[{"id":"m","text":"thoughts?"}]')
    scene = surfaces.read("s", [{"type": "text", "text": room}], meta)
    assert (scene.surface, scene.place, scene.audience, scene.mode) == ("conversations", "conversations:r1", "group", "ambient")
    assert scene.actor.handle == "olive" and sum(w.is_agent for w in scene.participants) == 2
    call = "The live voice conversation delegated help at 12ms. You are the same agent.\n\nThis new request's delegation_id is d1.\n\nCurrent human source (exact):\nwhat's on my plate today\n\nRecent conversation, for references: none"
    voice = surfaces.read("s", [{"type": "text", "text": call}], meta)
    assert (voice.surface, voice.place, voice.mode, voice.audience) == ("companion", "companion:olive", "voice", "dm")
    assert voice.heard == "what's on my plate today"
    private = surfaces.read("s", [{"type": "text", "text": "just us"}], meta)
    assert private.audience == "dm" and private.heard == "just us"


def test_minecraft_scene():
    world = {"messages": [{"id": 1, "username": "Paradox921", "message": "Polaris come build with me", "whisper": False, "source": "human"}],
             "world": {"username": "Polaris", "players": ["Paradox921", "Maker", "Polaris"]}}
    blocks = [{"type": "text", "text": "Current adapter permission policy for this turn:\n{}"},
              {"type": "text", "text": "You are a survival player.\nCONVERSATION-ONLY TURN: reply.\n" + json.dumps(world)}]
    scene = surfaces.read("s", blocks)
    assert (scene.surface, scene.audience, scene.self_handle) == ("minecraft", "group", "Polaris")
    assert scene.actor == Who("minecraft", "Paradox921", "Paradox921", False) and scene.heard == "Polaris come build with me"
    assert [w.handle for w in scene.participants] == ["Paradox921", "Maker"]


# ---- audiences ---------------------------------------------------------------------

def scene_for(memory: Memory, place: str, audience: str, who: Who | None) -> Scene:
    scene = Scene(session="s-" + place, surface=place.split(":")[0], place=place, place_label=place, audience=audience, actor=who,
                  participants=[who] if who else [])
    memory.settle(scene)
    return scene


def test_notes_respect_audience(memory: Memory):
    owner = scene_for(memory, "slack:T1:D1", "dm", Who("slack:T1", "UOWNER", "Olive"))
    assert owner.audience == "owner"  # a DM with a configured owner identity
    memory.note_add(owner, "budget", "Budget is 40k.", "here")
    memory.note_add(owner, "person:Olive", "Olive likes short answers.", "anywhere")
    group = scene_for(memory, "slack:T1:C1", "group", Who("slack:T1", "U2", "Dana"))
    memory.note_add(group, "standup", "Standup moved to 10.", "here")
    other = scene_for(memory, "slack:T1:C2", "group", Who("slack:T1", "U3", "Eli"))
    text = lambda scene, q: " ".join(h.get("text", "") for h in memory.search(scene, q))  # noqa: E731
    assert "40k" in text(owner, "budget") and "Standup" in text(owner, "standup")
    assert "40k" not in text(group, "budget") and "short answers" in text(group, "short answers")
    assert "Standup" in text(group, "standup") and "Standup" not in text(other, "standup")


def test_private_message_with_someone_else_never_leaves(memory: Memory):
    dm = scene_for(memory, "slack:T1:D7", "dm", Who("slack:T1", "U2", "Dana"))
    dm.heard = "between us, I am interviewing elsewhere"
    memory.journal_add(dm, "Understood, I will keep that private.", "", "end_turn")
    memory.note_add(dm, "person:Dana", "Dana is interviewing elsewhere.", "here")
    owner = scene_for(memory, "slack:T1:D1", "dm", Who("slack:T1", "UOWNER", "Olive"))
    assert memory.search(owner, "interviewing") == [] and memory.elsewhere(owner) == []
    assert memory.notes_about(owner, dm.actor_person) == []
    assert len(memory.search(dm, "interviewing")) == 2


def test_cross_place_hint_only_for_the_same_linked_person(memory: Memory):
    slack = scene_for(memory, "slack:T1:C1", "group", Who("slack:T1", "U2", "Dana"))
    slack.heard = "can you draft the release notes"
    memory.journal_add(slack, "On it.", "", "end_turn")
    game = scene_for(memory, "minecraft:world", "group", Who("minecraft", "DanaCraft", "DanaCraft"))
    assert memory.elsewhere(game) == []  # an unlinked name proves nothing
    result = memory.link(game, game.actor_person, "slack:T1", "U2", "says she is Dana on Slack")
    assert result["linked"] is False and memory.elsewhere(game) == []  # a claim from the weaker surface waits
    result = memory.link(slack, slack.actor_person, "minecraft", "DanaCraft", "said her Minecraft name in Slack")
    assert result["linked"] is True
    game = scene_for(memory, "minecraft:world", "group", Who("minecraft", "DanaCraft", "DanaCraft"))
    hints = memory.elsewhere(game)
    assert len(hints) == 1 and "release notes" in hints[0]["heard"] and hints[0]["said"] == ""
    stranger = scene_for(memory, "minecraft:world", "group", Who("minecraft", "Rando", "Rando"))
    assert memory.elsewhere(stranger) == []


def test_thought_scopes(memory: Memory):
    here = scene_for(memory, "slack:T1:C1", "group", Who("slack:T1", "U2", "Dana"))
    memory.thought_add(here, "followup", "check the deploy", 0, "here")
    memory.thought_add(here, "followup", "ask Dana about the offsite", 0, "person")
    owner = scene_for(memory, "slack:T1:D1", "dm", Who("slack:T1", "UOWNER", "Olive"))
    memory.thought_add(owner, "followup", "weekly summary", 0, "owner")
    elsewhere = scene_for(memory, "slack:T1:C9", "group", Who("slack:T1", "U2", "Dana"))
    stranger = scene_for(memory, "slack:T1:C9", "group", Who("slack:T1", "U5", "Eli"))
    texts = lambda scene: sorted(t["text"] for t in memory.thoughts_due(scene))  # noqa: E731
    assert texts(here) == ["ask Dana about the offsite", "check the deploy"]
    assert texts(elsewhere) == ["ask Dana about the offsite"] and texts(stranger) == []
    assert texts(owner) == ["weekly summary"]
    ids = [t["id"] for t in memory.thoughts_due(here)]
    memory.thoughts_delivered(ids, "s")
    assert memory.thoughts_due(here) == [] and memory.thoughts_due(elsewhere) == []


def test_brief_names_place_people_and_due_thoughts(memory: Memory):
    scene = scene_for(memory, "slack:T1:C1", "group", Who("slack:T1", "U2", "Dana"))
    memory.note_add(scene, "person:Dana", "Dana runs the release train.", "here")
    memory.thought_add(scene, "followup", "check the deploy", 0, "here")
    text, surfaced = compose(scene, memory, memory.config)
    assert text.startswith(OPEN) and text.endswith(CLOSE) and len(surfaced) == 1
    for expected in ("a group", "Speaking: Dana", "Dana runs the release train.", "check the deploy", "Notebook: 1 notes"):
        assert expected in text


# ---- small pieces ------------------------------------------------------------------

def test_spoken():
    assert spoken('{"decision":"pass","reason":"x"}') == "" and spoken("[[PASS]]") == ""
    assert spoken('{"decision":"speak","text":"Ship it."}') == "Ship it."
    assert spoken('Done.\nSLACK_FOLLOW_UP {"after_seconds":60,"goal":"x"}') == "Done."


def test_replay_filter_handles_split_chunks():
    f = ReplayFilter()
    assert f.apply("s", f"hello\n{OPEN}\nsecret\n{CLOSE}") == "hello\n"
    assert f.apply("s", f"{OPEN}\nsecret") is None and f.apply("s", "more secret") is None
    assert f.apply("s", f"end{CLOSE}after") == "after"
    assert f.apply("s", "plain") == "plain"


def test_decision_wire_and_normalize():
    qs = decisions.triage_questions()
    jev = DecisionBackend("jev", "system_one", "https://x/v1/systemone", "jev-latest")
    wire = decisions._wire(jev, {"m": 1}, qs)
    assert wire["questions"]["effort"]["type"] == "choice" and set(wire["questions"]["effort"]["criteria"]) == {"low", "medium", "high"}
    assert wire["questions"]["recall"] == {"type": "noul", "instructions": qs[1].instructions} and wire["state"] == {"m": 1}
    body = {"answers": {"effort": {"type": "choice", "choice": "high", "probabilities": {"low": 0.1, "medium": 0.2, "high": 0.7}}, "recall": {"type": "noul", "noul": 0.9}}}
    answers = decisions._normalize(jev, qs, body)
    assert answers["effort"].value == "high" and answers["effort"].confidence == 0.7 and answers["recall"].value == 0.9
    oai = DecisionBackend("openai", "openai_decisions", "https://x/v1/decisions", "gpt-6-luna")
    wire = decisions._wire(oai, {"m": 1}, qs)
    assert wire["input"][0]["content"][0] == {"type": "input_json", "data": {"m": 1}}
    assert wire["questions"][0]["choices"][0] == {"value": "low", "description": qs[0].choices["low"]}
    body = {"answers": [{"name": "recall", "type": "predicate", "probability": 0.2},
                        {"name": "effort", "type": "choice", "choice": "low", "probabilities": [{"value": "low", "probability": 0.8}, {"value": "medium", "probability": 0.2}, {"value": "high", "probability": 0.0}]}]}
    answers = decisions._normalize(oai, qs, body)
    assert answers["effort"].value == "low" and answers["recall"].confidence == 0.8
    with pytest.raises(ValueError):
        decisions._normalize(jev, qs, {"answers": {"effort": {"type": "choice", "choice": "extreme"}, "recall": {"type": "noul", "noul": 0.5}}})
