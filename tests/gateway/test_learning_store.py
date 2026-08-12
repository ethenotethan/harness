"""Learning store: granular course/deck mutation, server-folded progress
and SM-2 state, and the append-only attempt log — against a temp
HERMES_HOME."""

import json

import pytest


@pytest.fixture()
def learning_home(tmp_path, monkeypatch):
    # get_hermes_home() reads HERMES_HOME live — no cache to reset.
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    return tmp_path


QUESTION = {"q": "What is 2+2?", "options": ["A) 3", "B) 4", "C) 5", "D) 6"],
            "correct": "B", "explanation": "arithmetic"}


def _build_course(store):
    course = store.set_course(title="Pointer Lock 101", summary="Mouse capture",
                              updated_by="test")
    module = store.set_module(course["id"], title="Basics", overview="The API")
    module_id = module["module"]["id"]
    lesson = store.set_step(course["id"], module_id, title="Intro",
                            step_type="lesson", markdown="# Locks\nBody.")
    quiz = store.set_step(course["id"], module_id, title="Check",
                          step_type="quiz", questions=[QUESTION])
    return course["id"], module_id, lesson["step"]["id"], quiz["step"]["id"]


# ── Courses: granular mutation ───────────────────────────────────────────


def test_course_builds_incrementally_and_rev_is_monotonic(learning_home):
    from tui_gateway import learning_store as store

    course_id, module_id, lesson_id, quiz_id = _build_course(store)
    course = store.get_course(course_id)
    assert course["rev"] == 4  # create + module + 2 steps
    assert [m["id"] for m in course["modules"]] == [module_id]
    assert [s["id"] for s in course["modules"][0]["steps"]] == [lesson_id, quiz_id]
    assert course["modules"][0]["steps"][0]["markdown"].startswith("# Locks")


def test_course_set_updates_shell_without_touching_modules(learning_home):
    from tui_gateway import learning_store as store

    course_id, *_ = _build_course(store)
    store.set_course(course_id=course_id, title="Pointer Lock 201")
    course = store.get_course(course_id)
    assert course["title"] == "Pointer Lock 201"
    assert len(course["modules"]) == 1  # shell update never clobbers content


def test_step_update_in_place_preserves_position(learning_home):
    from tui_gateway import learning_store as store

    course_id, module_id, lesson_id, quiz_id = _build_course(store)
    store.set_step(course_id, module_id, step_id=lesson_id,
                   markdown="# Locks v2\nRewritten.")
    course = store.get_course(course_id)
    steps = course["modules"][0]["steps"]
    assert [s["id"] for s in steps] == [lesson_id, quiz_id]  # order unchanged
    assert steps[0]["markdown"].startswith("# Locks v2")


def test_append_questions_extends_without_resend(learning_home):
    from tui_gateway import learning_store as store

    course_id, module_id, _, quiz_id = _build_course(store)
    extra = {"q": "Esc does what?", "options": ["A) locks", "B) releases", "C) hides", "D) nothing"],
             "correct": "B"}
    store.set_step(course_id, module_id, step_id=quiz_id,
                   questions=[extra], append_questions=True)
    quiz = store.get_course(course_id)["modules"][0]["steps"][1]
    assert len(quiz["questions"]) == 2
    assert quiz["questions"][0]["q"] == QUESTION["q"]  # original survived


def test_position_inserts_rather_than_appends(learning_home):
    from tui_gateway import learning_store as store

    course_id, module_id, lesson_id, quiz_id = _build_course(store)
    inserted = store.set_step(course_id, module_id, title="Remedial",
                              step_type="lesson", markdown="Again.", position=1)
    steps = store.get_course(course_id)["modules"][0]["steps"]
    assert [s["id"] for s in steps] == [lesson_id, inserted["step"]["id"], quiz_id]


def test_type_confusion_is_rejected(learning_home):
    from tui_gateway import learning_store as store

    course_id, module_id, lesson_id, quiz_id = _build_course(store)
    with pytest.raises(ValueError):
        store.set_step(course_id, module_id, step_id=lesson_id, questions=[QUESTION])
    with pytest.raises(ValueError):
        store.set_step(course_id, module_id, step_id=quiz_id, markdown="nope")


def test_missing_parents_raise_lookup(learning_home):
    from tui_gateway import learning_store as store

    with pytest.raises(LookupError):
        store.set_module("ghost", title="x")
    course_id, *_ = _build_course(store)
    with pytest.raises(LookupError):
        store.set_step(course_id, "ghost-module", title="x",
                       step_type="lesson", markdown="y")


def test_delete_course_drops_its_progress_but_not_attempts(learning_home):
    from tui_gateway import learning_store as store

    course_id, _, lesson_id, _ = _build_course(store)
    store.record_progress(course_id, lesson_id, "lesson_read")
    store.record_attempt({"topic": "check", "score": 4, "total": 5,
                          "course_id": course_id})
    assert store.delete_course(course_id)
    assert store.get_progress(course_id) == {}
    assert len(store.list_attempts()) == 1  # history survives content deletion


def test_course_size_cap_rejects_with_guidance(learning_home):
    from tui_gateway import learning_store as store

    course_id, module_id, *_ = _build_course(store)
    with pytest.raises(ValueError, match="split content"):
        store.set_step(course_id, module_id, title="Huge",
                       step_type="lesson", markdown="x" * (513 * 1024))


# ── Decks ────────────────────────────────────────────────────────────────


def test_card_set_batches_upserts_by_id(learning_home):
    from tui_gateway import learning_store as store

    deck = store.set_deck(topic="Swift")
    first = store.set_cards(deck["id"], [
        {"front": "What is @MainActor?", "back": "Main-thread isolation"},
        {"front": "What is Sendable?", "back": "Cross-actor safety"},
    ])
    assert len(first["card_ids"]) == 2
    assert first["rev"] == 2  # one bump for the batch

    # Update one card in place by id, add one new.
    updated = store.set_cards(deck["id"], [
        {"id": first["card_ids"][0], "back": "Main-actor isolation"},
        {"front": "What is a worktree?", "back": "Isolated checkout"},
    ])
    cards = store.get_deck(deck["id"])["cards"]
    assert len(cards) == 3
    assert cards[0]["back"] == "Main-actor isolation"
    assert updated["rev"] == 3


def test_card_delete_drops_its_srs_state(learning_home):
    from tui_gateway import learning_store as store

    deck = store.set_deck(topic="Swift")
    ids = store.set_cards(deck["id"], [{"front": "f", "back": "b"}])["card_ids"]
    store.record_review(deck["id"], ids[0], quality=5)
    assert ids[0] in store.get_srs(deck["id"])
    store.delete_card(deck["id"], ids[0])
    assert ids[0] not in store.get_srs(deck["id"])


# ── Progress folding ─────────────────────────────────────────────────────


def test_progress_fold_is_commutative(learning_home):
    from tui_gateway import learning_store as store

    course_id, _, _, quiz_id = _build_course(store)
    # Device A reports 60 (fail, no `at`), device B reports 90 (pass, stamps at).
    a = dict(course_id=course_id, step_id=quiz_id, kind="quiz_attempt", score_percent=60)
    b = dict(course_id=course_id, step_id=quiz_id, kind="quiz_attempt",
             score_percent=90, at="2026-08-13T10:00:00+00:00")

    store.record_progress(**a)
    forward = store.record_progress(**b)

    # Reset and replay in the opposite order.
    store.delete_course(course_id)
    course_id2, _, _, quiz_id2 = _build_course(store)
    b2 = {**b, "course_id": course_id2, "step_id": quiz_id2}
    a2 = {**a, "course_id": course_id2, "step_id": quiz_id2}
    store.record_progress(**b2)
    reverse = store.record_progress(**a2)

    assert forward["best_score_percent"] == reverse["best_score_percent"] == 90
    assert forward["attempts"] == reverse["attempts"] == 2
    # completed_at: first PASSING stamp wins in both orders.
    assert forward["completed_at"] == "2026-08-13T10:00:00+00:00"
    assert reverse["completed_at"] == "2026-08-13T10:00:00+00:00"


def test_lesson_read_completes_and_is_idempotent_on_timestamp(learning_home):
    from tui_gateway import learning_store as store

    course_id, _, lesson_id, _ = _build_course(store)
    first = store.record_progress(course_id, lesson_id, "lesson_read",
                                  at="2026-08-13T09:00:00+00:00")
    again = store.record_progress(course_id, lesson_id, "lesson_read",
                                  at="2026-08-14T09:00:00+00:00")
    assert first["completed_at"] == again["completed_at"]  # first stamp stands
    assert again["attempts"] == 2


# ── SM-2 folding ─────────────────────────────────────────────────────────

# Pinned parity vectors — MUST match Portal's SRSEngineTests. A divergence
# means the client's optimistic state and the server's truth drift apart.
SM2_VECTORS = [
    # (qualities..., expected interval_days, repetitions, ease ± 0.001)
    ([5], 1.0, 1, 2.6),
    ([5, 5], 6.0, 2, 2.7),
    ([5, 5, 5], 16.0, 3, 2.8),          # round(6 * 2.7) = 16
    ([5, 5, 0], 1.0, 0, 1.9),           # failure resets reps+interval; 2.7 - 0.8
    ([2], 1.0, 0, 2.18),                # sub-3 quality never grows reps
]


@pytest.mark.parametrize("qualities,interval,reps,ease", SM2_VECTORS)
def test_sm2_parity_vectors(learning_home, qualities, interval, reps, ease):
    from tui_gateway import learning_store as store

    deck = store.set_deck(topic="v")
    card_id = store.set_cards(deck["id"], [{"front": "f", "back": "b"}])["card_ids"][0]
    state = None
    for i, quality in enumerate(qualities):
        result = store.record_review(
            deck["id"], card_id, quality,
            reviewed_at=f"2026-08-{13 + i:02d}T10:00:00+00:00")
        state = result["state"]
    assert state["interval_days"] == interval
    assert state["repetitions"] == reps
    assert abs(state["ease_factor"] - ease) < 0.001


def test_stale_review_is_dropped(learning_home):
    from tui_gateway import learning_store as store

    deck = store.set_deck(topic="v")
    card_id = store.set_cards(deck["id"], [{"front": "f", "back": "b"}])["card_ids"][0]
    store.record_review(deck["id"], card_id, 5, reviewed_at="2026-08-13T10:00:00+00:00")
    late = store.record_review(deck["id"], card_id, 0, reviewed_at="2026-08-12T10:00:00+00:00")
    assert late["applied"] is False
    assert store.get_srs(deck["id"])[card_id]["last_quality"] == 5


def test_bootstrap_state_accepted_only_on_first_contact(learning_home):
    from tui_gateway import learning_store as store

    deck = store.set_deck(topic="v")
    card_id = store.set_cards(deck["id"], [{"front": "f", "back": "b"}])["card_ids"][0]
    imported = {"interval_days": 30.0, "ease_factor": 2.9, "repetitions": 6,
                "next_review_date": "2026-09-01T00:00:00+00:00",
                "last_reviewed_at": "2026-08-01T00:00:00+00:00",
                "last_quality": 5, "review_count": 12}
    first = store.record_review(deck["id"], card_id, 5,
                                reviewed_at="2026-08-01T00:00:00+00:00", state=imported)
    assert first["state"]["review_count"] == 12  # import honored verbatim

    # A second import attempt cannot overwrite live state.
    hijack = store.record_review(deck["id"], card_id, 0,
                                 reviewed_at="2026-08-20T00:00:00+00:00",
                                 state={"interval_days": 999.0})
    assert hijack["state"]["interval_days"] != 999.0


# ── Attempts ─────────────────────────────────────────────────────────────


def test_attempts_append_only_newest_first(learning_home):
    from tui_gateway import learning_store as store

    store.record_attempt({"topic": "first", "score": 3, "total": 5,
                          "completed_at": "2026-08-13T09:00:00+00:00"})
    store.record_attempt({"topic": "second", "score": 5, "total": 5,
                          "completed_at": "2026-08-13T10:00:00+00:00"})
    attempts = store.list_attempts()
    assert [a["topic"] for a in attempts] == ["second", "first"]
    assert all(a["id"].startswith("att-") for a in attempts)


def test_attempt_requires_topic(learning_home):
    from tui_gateway import learning_store as store

    with pytest.raises(ValueError):
        store.record_attempt({"score": 1, "total": 2})


# ── Stats (agent's read-only view) ───────────────────────────────────────


def test_stats_rolls_up_progress(learning_home):
    from tui_gateway import learning_store as store

    course_id, _, lesson_id, quiz_id = _build_course(store)
    store.record_progress(course_id, lesson_id, "lesson_read")
    store.record_progress(course_id, quiz_id, "quiz_attempt", score_percent=80,
                          at="2026-08-13T10:00:00+00:00")
    stats = store.learning_stats()
    course_stats = stats["courses"][0]
    assert course_stats["total_steps"] == 2
    assert course_stats["completed_steps"] == 2
    assert course_stats["average_quiz_score"] == 80
