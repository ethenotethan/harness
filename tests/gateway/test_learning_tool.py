"""Learning agent tool: incremental course/deck building through the
action-enum surface, JSON-string payload parsing, and the read-only
learner-state view."""

import json

import pytest

from tools.learning_tool import learning_tool


@pytest.fixture()
def learning_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    return tmp_path


def call(action, **kwargs):
    return json.loads(learning_tool(action=action, **kwargs))


QUESTIONS = json.dumps([
    {"q": "What is 2+2?", "options": ["A) 3", "B) 4", "C) 5", "D) 6"], "correct": "B"},
])


def test_incremental_course_build(learning_home):
    created = call("course_create", title="Git Internals", summary="Plumbing up",
                   session_id="sess1")
    assert created["success"]
    course_id = created["course"]["id"]
    assert created["course"]["updated_by"] == "agent:sess1"

    module = call("module_set", course_id=course_id, title="Objects")
    assert module["success"]
    module_id = module["module"]["id"]

    lesson = call("step_set", course_id=course_id, module_id=module_id,
                  title="Blobs", step_type="lesson", markdown="# Blobs\nContent.")
    assert lesson["success"]

    quiz = call("step_set", course_id=course_id, module_id=module_id,
                title="Check", step_type="quiz", questions=QUESTIONS)
    assert quiz["success"]

    fetched = call("course_get", course_id=course_id)
    steps = fetched["course"]["modules"][0]["steps"]
    assert [s["type"] for s in steps] == ["lesson", "quiz"]


def test_surgical_step_update_by_id(learning_home):
    course_id = call("course_create", title="T")["course"]["id"]
    module_id = call("module_set", course_id=course_id, title="M")["module"]["id"]
    step_id = call("step_set", course_id=course_id, module_id=module_id,
                   title="L", step_type="lesson", markdown="v1")["step"]["id"]

    updated = call("step_set", course_id=course_id, module_id=module_id,
                   step_id=step_id, markdown="v2")
    assert updated["success"]
    course = call("course_get", course_id=course_id)["course"]
    assert course["modules"][0]["steps"][0]["markdown"] == "v2"
    assert len(course["modules"][0]["steps"]) == 1  # updated, not duplicated


def test_invalid_questions_json_is_an_error_not_a_drop(learning_home):
    course_id = call("course_create", title="T")["course"]["id"]
    module_id = call("module_set", course_id=course_id, title="M")["module"]["id"]
    result = call("step_set", course_id=course_id, module_id=module_id,
                  title="Q", step_type="quiz", questions="{not json")
    assert not result["success"]
    assert "JSON array" in result["error"]


def test_deck_and_batched_cards(learning_home):
    deck = call("deck_create", topic="Kanji")
    assert deck["success"]
    deck_id = deck["deck"]["id"]

    cards = call("card_set", deck_id=deck_id, cards=json.dumps([
        {"front": "水", "back": "water"},
        {"front": "火", "back": "fire"},
    ]))
    assert cards["success"]
    assert len(cards["card_ids"]) == 2

    fetched = call("deck_get", deck_id=deck_id)
    assert fetched["deck"]["cards"][0]["front"] == "水"


def test_progress_is_readable_but_not_writable(learning_home):
    course_id = call("course_create", title="T")["course"]["id"]
    progress = call("progress_get", course_id=course_id)
    assert progress["success"]
    assert progress["progress"] == {}
    # There is no progress-writing action on the tool at all.
    denied = call("progress_record", course_id=course_id)
    assert not denied["success"]
    assert "unknown action" in denied["error"]


def test_stats_get(learning_home):
    call("course_create", title="T")
    stats = call("stats_get")
    assert stats["success"]
    assert stats["stats"]["courses"][0]["title"] == "T"


def test_unknown_targets_are_reported(learning_home):
    assert not call("course_get", course_id="ghost")["success"]
    assert not call("deck_get", deck_id="ghost")["success"]
    assert not call("module_set", course_id="ghost", title="x")["success"]
    assert not call("course_delete", course_id="ghost")["success"]
