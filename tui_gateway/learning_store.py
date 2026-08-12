"""
Learning store: gateway-persisted courses, flashcard decks, learner
progress, and quiz attempts — the durable backend for a native client's
Learning surface, replacing client-local JSON blobs.

Storage:
  ~/.hermes/learning/courses/index.json   current state of every course
  ~/.hermes/learning/decks/index.json     current state of every deck
  ~/.hermes/learning/progress.json        per-course step progress +
                                          per-deck SRS state (client-written,
                                          server-folded)
  ~/.hermes/learning/attempts.jsonl       append-only quiz attempt log

Surface (see server.py):
  learning.course.set/get/list/delete, learning.module.set/delete,
  learning.step.set/delete, learning.deck.set/get/list/delete,
  learning.card.set/delete, learning.progress.record,
  learning.review.record, learning.attempt.record/list — plus a
  `learning.changed` gateway event on every mutation so clients stream
  updates without polling.

Design invariants:

* **Granular mutation.** Modules, steps, and cards are addressable by
  stable server ids so a writer updates ONE sub-entity — never resending
  the parent document. Every mutation is one lock-guarded read-modify-write
  bumping the parent's monotonic ``rev`` once; clients rev-guard on that
  single integer.
* **Content vs learner state.** Courses/decks are rev'd documents any
  writer maintains. Progress and SRS state live in a separate file with NO
  rev: writes are *events the server folds* with commutative rules
  (``best_score = max``, ``attempts += 1``, first ``completed_at`` wins;
  SM-2 recomputed server-side, stale reviews dropped by timestamp), so two
  devices racing cannot lose data.
* **Attempts are immutable.** A finished quiz is an append-only JSONL line,
  mirroring the artifact invocation ledger — naturally conflict-free.
"""

import json
import os
import re
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from hermes_constants import get_hermes_home

MAX_COURSES = 100
MAX_DECKS = 100
MAX_COURSE_BYTES = 512 * 1024
MAX_CARDS_PER_DECK = 500
MAX_ATTEMPTS_LISTED = 200
_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$")

_lock = threading.Lock()


def _learning_dir() -> Path:
    return Path(get_hermes_home()) / "learning"


def _courses_file() -> Path:
    return _learning_dir() / "courses" / "index.json"


def _decks_file() -> Path:
    return _learning_dir() / "decks" / "index.json"


def _progress_file() -> Path:
    return _learning_dir() / "progress.json"


def _attempts_file() -> Path:
    return _learning_dir() / "attempts.jsonl"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_json(path: Path, default):
    if not path.exists():
        return default
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, type(default)) else default
    except (json.JSONDecodeError, OSError):
        return default


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        os.replace(tmp, str(path))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _mint_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _validate_id(value: str, what: str) -> str:
    value = (value or "").strip()
    if not _ID_RE.match(value):
        raise ValueError(
            f"{what} must be 1-128 chars of [a-zA-Z0-9._-], starting alphanumeric"
        )
    return value


def _check_course_size(course: dict) -> None:
    encoded = json.dumps(course, ensure_ascii=False).encode("utf-8", errors="replace")
    if len(encoded) > MAX_COURSE_BYTES:
        raise ValueError(
            f"course exceeds {MAX_COURSE_BYTES} bytes — split content into a "
            "second course rather than growing this one"
        )


def _insert_at(items: list, entry: dict, position: Optional[int]) -> None:
    if position is None or position >= len(items):
        items.append(entry)
    else:
        items.insert(max(0, int(position)), entry)


# ── Courses ──────────────────────────────────────────────────────────────


def _course_summary(course: dict) -> dict:
    """List-view shape: everything but module bodies, plus rollup counts."""
    modules = course.get("modules") or []
    step_count = sum(len(m.get("steps") or []) for m in modules)
    summary = {k: v for k, v in course.items() if k != "modules"}
    summary["module_count"] = len(modules)
    summary["step_count"] = step_count
    return summary


def set_course(
    course_id: Optional[str] = None,
    title: Optional[str] = None,
    summary: Optional[str] = None,
    source_session_id: Optional[str] = None,
    updated_by: str = "",
) -> dict:
    """Create or update a course SHELL (title/summary only). Modules and
    steps are managed exclusively through their own granular setters —
    this function never touches them, so an agent refreshing a title
    cannot clobber course content."""
    with _lock:
        courses = _read_json(_courses_file(), {})
        if course_id:
            course_id = _validate_id(course_id, "course id")
        else:
            course_id = _mint_id("crs")
        existing = courses.get(course_id)
        if existing is None:
            if len(courses) >= MAX_COURSES:
                raise ValueError(f"course cap reached ({MAX_COURSES})")
            if not (title or "").strip():
                raise ValueError("title required to create a course")
            course = {
                "id": course_id,
                "title": title.strip(),
                "summary": (summary or "").strip(),
                "modules": [],
                "rev": 1,
                "created_at": _now_iso(),
                "updated_at": _now_iso(),
                "updated_by": updated_by or "",
            }
            if source_session_id:
                course["source_session_id"] = str(source_session_id)
        else:
            course = existing
            if title is not None and title.strip():
                course["title"] = title.strip()
            if summary is not None:
                course["summary"] = summary.strip()
            if source_session_id:
                course["source_session_id"] = str(source_session_id)
            course["rev"] = int(course.get("rev", 0)) + 1
            course["updated_at"] = _now_iso()
            course["updated_by"] = updated_by or ""
        courses[course_id] = course
        _write_json(_courses_file(), courses)
        return _course_summary(course)


def get_course(course_id: str) -> Optional[dict]:
    with _lock:
        return _read_json(_courses_file(), {}).get((course_id or "").strip())


def list_courses() -> list[dict]:
    """All courses WITHOUT module bodies, newest-updated first."""
    with _lock:
        courses = _read_json(_courses_file(), {})
    summaries = [_course_summary(c) for c in courses.values()]
    summaries.sort(key=lambda c: c.get("updated_at", ""), reverse=True)
    return summaries


def delete_course(course_id: str) -> bool:
    """Remove a course and its folded progress. Attempts stay — they are a
    historical record of something the learner did, not course content."""
    course_id = (course_id or "").strip()
    with _lock:
        courses = _read_json(_courses_file(), {})
        if course_id not in courses:
            return False
        del courses[course_id]
        _write_json(_courses_file(), courses)
        progress = _read_json(_progress_file(), {})
        if course_id in (progress.get("courses") or {}):
            del progress["courses"][course_id]
            _write_json(_progress_file(), progress)
        return True


def _find_module(course: dict, module_id: str) -> Optional[dict]:
    for module in course.get("modules") or []:
        if module.get("id") == module_id:
            return module
    return None


def _touch(course: dict, updated_by: str) -> None:
    course["rev"] = int(course.get("rev", 0)) + 1
    course["updated_at"] = _now_iso()
    course["updated_by"] = updated_by or ""


def set_module(
    course_id: str,
    module_id: Optional[str] = None,
    title: Optional[str] = None,
    overview: Optional[str] = None,
    position: Optional[int] = None,
    updated_by: str = "",
) -> dict:
    """Upsert one module. Omitted id mints one (returned); an existing id
    updates in place preserving position unless ``position`` moves it."""
    with _lock:
        courses = _read_json(_courses_file(), {})
        course = courses.get((course_id or "").strip())
        if course is None:
            raise LookupError(f"course {course_id!r} not found")
        modules = course.setdefault("modules", [])
        if module_id:
            module_id = _validate_id(module_id, "module id")
        else:
            module_id = _mint_id("m")
        module = _find_module(course, module_id)
        if module is None:
            if not (title or "").strip():
                raise ValueError("title required to create a module")
            module = {
                "id": module_id,
                "title": title.strip(),
                "overview": (overview or "").strip(),
                "steps": [],
            }
            _insert_at(modules, module, position)
        else:
            if title is not None and title.strip():
                module["title"] = title.strip()
            if overview is not None:
                module["overview"] = overview.strip()
            if position is not None:
                modules.remove(module)
                _insert_at(modules, module, position)
        _check_course_size(course)
        _touch(course, updated_by)
        _write_json(_courses_file(), courses)
        return {"module": {"id": module_id}, "rev": course["rev"]}


def delete_module(course_id: str, module_id: str, updated_by: str = "") -> dict:
    with _lock:
        courses = _read_json(_courses_file(), {})
        course = courses.get((course_id or "").strip())
        if course is None:
            raise LookupError(f"course {course_id!r} not found")
        modules = course.get("modules") or []
        module = _find_module(course, (module_id or "").strip())
        if module is None:
            raise LookupError(f"module {module_id!r} not found")
        modules.remove(module)
        _touch(course, updated_by)
        _write_json(_courses_file(), courses)
        return {"rev": course["rev"]}


def _normalize_questions(questions: list) -> list[dict]:
    """Validate + normalize quiz questions, minting ids for new ones.
    A question is {q, options[], correct, explanation?} — the shape the
    curriculum envelope already used, so agents don't relearn it."""
    normalized = []
    for raw in questions:
        if not isinstance(raw, dict):
            raise ValueError("each question must be an object")
        prompt = str(raw.get("q") or raw.get("question") or "").strip()
        options = raw.get("options")
        correct = str(raw.get("correct") or "").strip()
        if not prompt or not isinstance(options, list) or not options or not correct:
            raise ValueError("question requires q, options[], and correct")
        normalized.append(
            {
                "id": str(raw.get("id") or _mint_id("q")),
                "q": prompt,
                "options": [str(o) for o in options],
                "correct": correct,
                "explanation": str(raw.get("explanation") or ""),
            }
        )
    return normalized


def set_step(
    course_id: str,
    module_id: str,
    step_id: Optional[str] = None,
    title: Optional[str] = None,
    step_type: Optional[str] = None,
    markdown: Optional[str] = None,
    questions: Optional[list] = None,
    append_questions: bool = False,
    position: Optional[int] = None,
    updated_by: str = "",
) -> dict:
    """Upsert one step (lesson or quiz) inside a module.

    ``append_questions=True`` EXTENDS an existing quiz's question list
    instead of replacing it — the "add three harder questions" case with
    no resend of the existing ones. Progress stays keyed to the step id,
    so updating a step in place preserves the learner's record; replacing
    a step under a new id deliberately starts fresh.
    """
    with _lock:
        courses = _read_json(_courses_file(), {})
        course = courses.get((course_id or "").strip())
        if course is None:
            raise LookupError(f"course {course_id!r} not found")
        module = _find_module(course, (module_id or "").strip())
        if module is None:
            raise LookupError(f"module {module_id!r} not found")
        steps = module.setdefault("steps", [])
        if step_id:
            step_id = _validate_id(step_id, "step id")
        else:
            step_id = _mint_id("s")
        step = next((s for s in steps if s.get("id") == step_id), None)

        if step is None:
            if not (title or "").strip():
                raise ValueError("title required to create a step")
            kind = (step_type or "").strip().lower()
            if kind == "lesson":
                if not (markdown or "").strip():
                    raise ValueError("lesson step requires markdown")
                step = {"id": step_id, "title": title.strip(), "type": "lesson",
                        "markdown": markdown}
            elif kind == "quiz":
                normalized = _normalize_questions(questions or [])
                if not normalized:
                    raise ValueError("quiz step requires at least one question")
                step = {"id": step_id, "title": title.strip(), "type": "quiz",
                        "questions": normalized}
            else:
                raise ValueError("step type must be 'lesson' or 'quiz'")
            _insert_at(steps, step, position)
        else:
            if title is not None and title.strip():
                step["title"] = title.strip()
            if step.get("type") == "lesson":
                if markdown is not None:
                    if not markdown.strip():
                        raise ValueError("lesson markdown cannot be emptied")
                    step["markdown"] = markdown
                if questions is not None:
                    raise ValueError("cannot put questions on a lesson step")
            else:
                if markdown is not None:
                    raise ValueError("cannot put markdown on a quiz step")
                if questions is not None:
                    normalized = _normalize_questions(questions)
                    if append_questions:
                        step["questions"] = (step.get("questions") or []) + normalized
                    else:
                        if not normalized:
                            raise ValueError("quiz step requires at least one question")
                        step["questions"] = normalized
            if position is not None:
                steps.remove(step)
                _insert_at(steps, step, position)
        _check_course_size(course)
        _touch(course, updated_by)
        _write_json(_courses_file(), courses)
        return {"step": {"id": step_id}, "rev": course["rev"]}


def delete_step(course_id: str, module_id: str, step_id: str, updated_by: str = "") -> dict:
    with _lock:
        courses = _read_json(_courses_file(), {})
        course = courses.get((course_id or "").strip())
        if course is None:
            raise LookupError(f"course {course_id!r} not found")
        module = _find_module(course, (module_id or "").strip())
        if module is None:
            raise LookupError(f"module {module_id!r} not found")
        steps = module.get("steps") or []
        step = next((s for s in steps if s.get("id") == (step_id or "").strip()), None)
        if step is None:
            raise LookupError(f"step {step_id!r} not found")
        steps.remove(step)
        _touch(course, updated_by)
        _write_json(_courses_file(), courses)
        return {"rev": course["rev"]}


# ── Decks ────────────────────────────────────────────────────────────────


def _deck_summary(deck: dict) -> dict:
    summary = {k: v for k, v in deck.items() if k != "cards"}
    summary["card_count"] = len(deck.get("cards") or [])
    return summary


def set_deck(
    deck_id: Optional[str] = None,
    topic: Optional[str] = None,
    updated_by: str = "",
) -> dict:
    with _lock:
        decks = _read_json(_decks_file(), {})
        if deck_id:
            deck_id = _validate_id(deck_id, "deck id")
        else:
            deck_id = _mint_id("dk")
        existing = decks.get(deck_id)
        if existing is None:
            if len(decks) >= MAX_DECKS:
                raise ValueError(f"deck cap reached ({MAX_DECKS})")
            if not (topic or "").strip():
                raise ValueError("topic required to create a deck")
            deck = {
                "id": deck_id,
                "topic": topic.strip(),
                "cards": [],
                "rev": 1,
                "created_at": _now_iso(),
                "updated_at": _now_iso(),
                "updated_by": updated_by or "",
            }
        else:
            deck = existing
            if topic is not None and topic.strip():
                deck["topic"] = topic.strip()
            deck["rev"] = int(deck.get("rev", 0)) + 1
            deck["updated_at"] = _now_iso()
            deck["updated_by"] = updated_by or ""
        decks[deck_id] = deck
        _write_json(_decks_file(), decks)
        return _deck_summary(deck)


def get_deck(deck_id: str) -> Optional[dict]:
    with _lock:
        return _read_json(_decks_file(), {}).get((deck_id or "").strip())


def list_decks() -> list[dict]:
    with _lock:
        decks = _read_json(_decks_file(), {})
    summaries = [_deck_summary(d) for d in decks.values()]
    summaries.sort(key=lambda d: d.get("updated_at", ""), reverse=True)
    return summaries


def delete_deck(deck_id: str) -> bool:
    deck_id = (deck_id or "").strip()
    with _lock:
        decks = _read_json(_decks_file(), {})
        if deck_id not in decks:
            return False
        del decks[deck_id]
        _write_json(_decks_file(), decks)
        progress = _read_json(_progress_file(), {})
        if deck_id in (progress.get("srs") or {}):
            del progress["srs"][deck_id]
            _write_json(_progress_file(), progress)
        return True


def set_cards(deck_id: str, cards: list, updated_by: str = "") -> dict:
    """Batched card upsert-by-id: one rev bump for N cards. Cards without
    an id are minted one; cards with a known id update in place."""
    if not isinstance(cards, list) or not cards:
        raise ValueError("cards must be a non-empty list")
    with _lock:
        decks = _read_json(_decks_file(), {})
        deck = decks.get((deck_id or "").strip())
        if deck is None:
            raise LookupError(f"deck {deck_id!r} not found")
        stored = deck.setdefault("cards", [])
        by_id = {c.get("id"): c for c in stored}
        card_ids = []
        for raw in cards:
            if not isinstance(raw, dict):
                raise ValueError("each card must be an object")
            front = str(raw.get("front") or "").strip()
            back = str(raw.get("back") or "").strip()
            card_id = str(raw.get("id") or "").strip() or _mint_id("c")
            existing = by_id.get(card_id)
            if existing is None:
                if not front or not back:
                    raise ValueError("card requires front and back")
                card = {"id": card_id, "front": front, "back": back}
                if raw.get("category"):
                    card["category"] = str(raw["category"])
                stored.append(card)
                by_id[card_id] = card
            else:
                if front:
                    existing["front"] = front
                if back:
                    existing["back"] = back
                if raw.get("category"):
                    existing["category"] = str(raw["category"])
            card_ids.append(card_id)
        if len(stored) > MAX_CARDS_PER_DECK:
            raise ValueError(f"deck card cap reached ({MAX_CARDS_PER_DECK})")
        deck["rev"] = int(deck.get("rev", 0)) + 1
        deck["updated_at"] = _now_iso()
        deck["updated_by"] = updated_by or ""
        _write_json(_decks_file(), decks)
        return {"card_ids": card_ids, "rev": deck["rev"]}


def delete_card(deck_id: str, card_id: str, updated_by: str = "") -> dict:
    with _lock:
        decks = _read_json(_decks_file(), {})
        deck = decks.get((deck_id or "").strip())
        if deck is None:
            raise LookupError(f"deck {deck_id!r} not found")
        cards = deck.get("cards") or []
        card = next((c for c in cards if c.get("id") == (card_id or "").strip()), None)
        if card is None:
            raise LookupError(f"card {card_id!r} not found")
        cards.remove(card)
        deck["rev"] = int(deck.get("rev", 0)) + 1
        deck["updated_at"] = _now_iso()
        deck["updated_by"] = updated_by or ""
        _write_json(_decks_file(), decks)
        progress = _read_json(_progress_file(), {})
        srs = (progress.get("srs") or {}).get(deck["id"]) or {}
        if card["id"] in srs:
            del srs[card["id"]]
            _write_json(_progress_file(), progress)
        return {"rev": deck["rev"]}


# ── Learner state: progress + SRS (server-folded, no rev) ────────────────


def get_progress(course_id: str) -> dict:
    """Folded per-step progress for one course: {step_id: StepProgress}."""
    with _lock:
        progress = _read_json(_progress_file(), {})
    return (progress.get("courses") or {}).get((course_id or "").strip()) or {}


def get_srs(deck_id: str) -> dict:
    """Folded per-card SRS state for one deck: {card_id: SRSState}."""
    with _lock:
        progress = _read_json(_progress_file(), {})
    return (progress.get("srs") or {}).get((deck_id or "").strip()) or {}


def record_progress(
    course_id: str,
    step_id: str,
    kind: str,
    score_percent: Optional[int] = None,
    at: Optional[str] = None,
) -> dict:
    """Fold one progress EVENT into the stored record. Every fold rule is
    order-insensitive (max / increment / first-stamp-wins), so two devices
    reporting the same study session in either order converge:

      attempts           += 1
      best_score_percent  = max(stored, incoming)
      completed_at        = stored ?? incoming   (first stamp wins)

    ``lesson_read`` events complete unconditionally; ``quiz_attempt``
    events complete only via the client's pass threshold — the client
    sends ``at`` only for attempts it considers passing. The server does
    not re-judge scores; it just folds.
    """
    kind = (kind or "").strip().lower()
    if kind not in ("lesson_read", "quiz_attempt"):
        raise ValueError("kind must be 'lesson_read' or 'quiz_attempt'")
    course_id = (course_id or "").strip()
    step_id = (step_id or "").strip()
    if not course_id or not step_id:
        raise ValueError("course_id and step_id required")
    stamp = (at or "").strip() or _now_iso()
    with _lock:
        progress = _read_json(_progress_file(), {})
        course_map = progress.setdefault("courses", {}).setdefault(course_id, {})
        record = course_map.get(step_id) or {
            "completed_at": None,
            "best_score_percent": None,
            "attempts": 0,
        }
        record["attempts"] = int(record.get("attempts", 0)) + 1
        if kind == "quiz_attempt" and score_percent is not None:
            score = max(0, min(100, int(score_percent)))
            prior = record.get("best_score_percent")
            record["best_score_percent"] = score if prior is None else max(int(prior), score)
        if record.get("completed_at") is None:
            if kind == "lesson_read":
                record["completed_at"] = stamp
            elif at:  # quiz: client stamps `at` only on a passing attempt
                record["completed_at"] = stamp
        course_map[step_id] = record
        _write_json(_progress_file(), progress)
        return record


# SM-2 constants — MUST stay bit-compatible with Portal's SRSEngine.swift.
# Shared test vectors are pinned in tests on both sides.
_SM2_MIN_EASE = 1.3
_SM2_DEFAULT_EASE = 2.5
_ONE_DAY_SECONDS = 86_400


def _sm2(state: Optional[dict], quality: int, reviewed_at: str) -> dict:
    """Port of Portal's pure ``SRSEngine.calculate`` (SuperMemo SM-2).

    ``interval_days`` mirrors Swift's day-denominated interval; the next
    review date is reviewed_at + interval days.
    """
    state = dict(state or {})
    interval = float(state.get("interval_days", 0.0))
    ease = float(state.get("ease_factor", _SM2_DEFAULT_EASE))
    repetitions = int(state.get("repetitions", 0))
    review_count = int(state.get("review_count", 0))

    review_count += 1
    if quality < 3:
        repetitions = 0
        interval = 1.0
    else:
        if repetitions == 0:
            interval = 1.0
        elif repetitions == 1:
            interval = 6.0
        else:
            interval = float(round(interval * ease))
        repetitions += 1

    delta = 0.1 - (5.0 - quality) * (0.08 + (5.0 - quality) * 0.02)
    ease = max(_SM2_MIN_EASE, ease + delta)

    try:
        base = datetime.fromisoformat(reviewed_at.replace("Z", "+00:00"))
    except ValueError:
        base = datetime.now(timezone.utc)
    next_review = base.timestamp() + interval * _ONE_DAY_SECONDS
    next_review_iso = datetime.fromtimestamp(next_review, timezone.utc).isoformat()

    return {
        "interval_days": interval,
        "ease_factor": ease,
        "repetitions": repetitions,
        "next_review_date": next_review_iso,
        "last_reviewed_at": reviewed_at,
        "last_quality": quality,
        "review_count": review_count,
    }


def record_review(
    deck_id: str,
    card_id: str,
    quality: int,
    reviewed_at: Optional[str] = None,
    state: Optional[dict] = None,
) -> dict:
    """Fold one SRS review. The server recomputes SM-2 from its stored
    state — the client's optimistic copy runs the identical algorithm, so
    they agree without trusting the client's arithmetic.

    Ordering rule: a review older than the stored ``last_reviewed_at`` is
    DROPPED (``applied: false``) — newest-wins keeps a late-syncing device
    from rolling the schedule backwards.

    ``state`` is the bootstrap import path: accepted ONLY when no server
    state exists for the card, so a migrating client can carry years of
    local SM-2 history up without replaying every review. It can never
    overwrite live server state.
    """
    deck_id = (deck_id or "").strip()
    card_id = (card_id or "").strip()
    if not deck_id or not card_id:
        raise ValueError("deck_id and card_id required")
    quality = int(quality)
    if not 0 <= quality <= 5:
        raise ValueError("quality must be 0-5")
    stamp = (reviewed_at or "").strip() or _now_iso()
    with _lock:
        progress = _read_json(_progress_file(), {})
        deck_map = progress.setdefault("srs", {}).setdefault(deck_id, {})
        stored = deck_map.get(card_id)

        if stored is None and isinstance(state, dict) and state:
            deck_map[card_id] = dict(state)
            _write_json(_progress_file(), progress)
            return {"state": deck_map[card_id], "applied": True}

        last = (stored or {}).get("last_reviewed_at") or ""
        if last and stamp < last:
            return {"state": stored, "applied": False}

        folded = _sm2(stored, quality, stamp)
        deck_map[card_id] = folded
        _write_json(_progress_file(), progress)
        return {"state": folded, "applied": True}


# ── Attempts (append-only) ───────────────────────────────────────────────


def record_attempt(attempt: dict) -> str:
    """Append one finished quiz session to the JSONL log. Returns the
    minted attempt id. Attempts are immutable history — no update path."""
    if not isinstance(attempt, dict):
        raise ValueError("attempt must be an object")
    topic = str(attempt.get("topic") or "").strip()
    if not topic:
        raise ValueError("attempt requires a topic")
    entry = {
        "id": _mint_id("att"),
        "topic": topic,
        "score": int(attempt.get("score", 0)),
        "total": int(attempt.get("total", 0)),
        "completed_at": str(attempt.get("completed_at") or _now_iso()),
    }
    for optional in ("questions", "selected", "course_id", "step_id", "source_session_id"):
        if attempt.get(optional) is not None:
            entry[optional] = attempt[optional]
    with _lock:
        path = _attempts_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry["id"]


def list_attempts(limit: int = 50) -> list[dict]:
    """Newest-first attempt history."""
    limit = max(1, min(int(limit), MAX_ATTEMPTS_LISTED))
    with _lock:
        path = _attempts_file()
        if not path.exists():
            return []
        entries = []
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    entries.reverse()
    return entries[:limit]


# ── Agent-facing stats (read-only) ───────────────────────────────────────


def learning_stats() -> dict:
    """Progress rollups + attempt stats for the agent's read-only view —
    lets it adapt courses to weak spots. Progress WRITES stay client-only."""
    with _lock:
        courses = _read_json(_courses_file(), {})
        decks = _read_json(_decks_file(), {})
        progress = _read_json(_progress_file(), {})
    course_stats = []
    for cid, course in courses.items():
        steps = [s for m in (course.get("modules") or []) for s in (m.get("steps") or [])]
        recs = (progress.get("courses") or {}).get(cid) or {}
        completed = sum(1 for s in steps if (recs.get(s.get("id")) or {}).get("completed_at"))
        scores = [r["best_score_percent"] for r in recs.values()
                  if isinstance(r, dict) and r.get("best_score_percent") is not None]
        course_stats.append({
            "id": cid,
            "title": course.get("title", ""),
            "total_steps": len(steps),
            "completed_steps": completed,
            "average_quiz_score": (sum(scores) // len(scores)) if scores else None,
        })
    deck_stats = []
    for did, deck in decks.items():
        srs = (progress.get("srs") or {}).get(did) or {}
        deck_stats.append({
            "id": did,
            "topic": deck.get("topic", ""),
            "card_count": len(deck.get("cards") or []),
            "reviewed_cards": len(srs),
        })
    return {"courses": course_stats, "decks": deck_stats}
