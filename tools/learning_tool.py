#!/usr/bin/env python3
"""
Learning Tool — gateway-persisted courses and flashcard decks the agent
builds and maintains PIECE BY PIECE.

The store is shared with the gateway RPCs (tui_gateway.learning_store), so
chat turns, cron jobs, and the HermesNative app all see the same state,
and connected clients stream every change live via `learning.changed`.

The contract this tool exists to enforce: NO GIANT NESTED JSON. A course
is built incrementally (course_create, then module_set per module, then
step_set per lesson/quiz) and updated surgically (step_set one step by its
id). The tool never accepts a whole-course document, so there is nothing
to resend.

Content actions:
  course_create/list/get/delete, module_set/delete, step_set/delete,
  deck_create/list/get/delete, card_set (batched), card_delete

Learner-state actions (READ-ONLY — progress is written by the user's
client, never by the agent):
  progress_get {course_id}   -> per-step completion/scores
  stats_get                  -> rollups across courses and decks
"""

import json
import logging

logger = logging.getLogger(__name__)


def learning_tool(
    action: str,
    course_id: str = "",
    module_id: str = "",
    step_id: str = "",
    deck_id: str = "",
    card_id: str = "",
    id: str = "",
    title: str = "",
    summary: str = "",
    overview: str = "",
    topic: str = "",
    step_type: str = "",
    markdown: str = "",
    questions: str = "",
    cards: str = "",
    append_questions: bool = False,
    position: int = -1,
    session_id: str = "",
) -> str:
    """Execute a learning action against the shared store.

    Returns a JSON STRING — the tool registry's result contract
    (_normalize_handler_result) accepts only str or the multimodal
    envelope; raw dicts are rejected as tool_result_contract errors.
    """
    return json.dumps(
        _learning_tool_impl(
            action, course_id=course_id, module_id=module_id, step_id=step_id,
            deck_id=deck_id, card_id=card_id, id=id, title=title,
            summary=summary, overview=overview, topic=topic,
            step_type=step_type, markdown=markdown, questions=questions,
            cards=cards, append_questions=append_questions,
            position=position, session_id=session_id,
        ),
        ensure_ascii=False,
        default=str,
    )


def _parse_json_list(raw: str, what: str):
    """Nested payloads arrive as JSON strings (tool params are scalars).
    A present-but-invalid string is an error, not a silent drop."""
    if not raw.strip():
        return None
    try:
        parsed = json.loads(raw)
    except ValueError:
        raise ValueError(f"{what} must be a JSON array")
    if not isinstance(parsed, list):
        raise ValueError(f"{what} must be a JSON array")
    return parsed


def _learning_tool_impl(
    action: str,
    course_id: str = "",
    module_id: str = "",
    step_id: str = "",
    deck_id: str = "",
    card_id: str = "",
    id: str = "",
    title: str = "",
    summary: str = "",
    overview: str = "",
    topic: str = "",
    step_type: str = "",
    markdown: str = "",
    questions: str = "",
    cards: str = "",
    append_questions: bool = False,
    position: int = -1,
    session_id: str = "",
) -> dict:
    from tui_gateway import learning_store

    action = (action or "").strip().lower()
    updated_by = f"agent:{session_id}" if session_id else "agent"
    pos = None if position is None or int(position) < 0 else int(position)

    try:
        # ── Courses ──
        if action == "course_list":
            return {"success": True, "courses": learning_store.list_courses()}

        if action == "course_get":
            course = learning_store.get_course(course_id or id)
            if course is None:
                return {"success": False, "error": f"course not found: {(course_id or id)!r}"}
            return {"success": True, "course": course}

        if action == "course_create":
            stored = learning_store.set_course(
                course_id=id or None, title=title, summary=summary,
                source_session_id=session_id or None, updated_by=updated_by,
            )
            _emit_changed("course", stored)
            return {"success": True, "course": stored}

        if action == "course_update":
            stored = learning_store.set_course(
                course_id=course_id or id, title=title or None,
                summary=summary or None, updated_by=updated_by,
            )
            _emit_changed("course", stored)
            return {"success": True, "course": stored}

        if action == "course_delete":
            target = course_id or id
            if not learning_store.delete_course(target):
                return {"success": False, "error": f"course not found: {target!r}"}
            _emit_changed("course", {"id": target, "deleted": True})
            return {"success": True, "deleted": target}

        # ── Modules ──
        if action == "module_set":
            result = learning_store.set_module(
                course_id=course_id, module_id=module_id or id or None,
                title=title or None, overview=overview or None,
                position=pos, updated_by=updated_by,
            )
            _emit_changed("course", {"id": course_id, "rev": result["rev"]})
            return {"success": True, **result}

        if action == "module_delete":
            result = learning_store.delete_module(
                course_id=course_id, module_id=module_id or id, updated_by=updated_by
            )
            _emit_changed("course", {"id": course_id, "rev": result["rev"]})
            return {"success": True, **result}

        # ── Steps ──
        if action == "step_set":
            parsed_questions = _parse_json_list(questions, "questions")
            result = learning_store.set_step(
                course_id=course_id, module_id=module_id,
                step_id=step_id or id or None, title=title or None,
                step_type=step_type or None, markdown=markdown or None,
                questions=parsed_questions,
                append_questions=bool(append_questions),
                position=pos, updated_by=updated_by,
            )
            _emit_changed("course", {"id": course_id, "rev": result["rev"]})
            return {"success": True, **result}

        if action == "step_delete":
            result = learning_store.delete_step(
                course_id=course_id, module_id=module_id,
                step_id=step_id or id, updated_by=updated_by,
            )
            _emit_changed("course", {"id": course_id, "rev": result["rev"]})
            return {"success": True, **result}

        # ── Decks ──
        if action == "deck_list":
            return {"success": True, "decks": learning_store.list_decks()}

        if action == "deck_get":
            deck = learning_store.get_deck(deck_id or id)
            if deck is None:
                return {"success": False, "error": f"deck not found: {(deck_id or id)!r}"}
            return {"success": True, "deck": deck}

        if action == "deck_create":
            stored = learning_store.set_deck(
                deck_id=id or None, topic=topic, updated_by=updated_by
            )
            _emit_changed("deck", stored)
            return {"success": True, "deck": stored}

        if action == "deck_delete":
            target = deck_id or id
            if not learning_store.delete_deck(target):
                return {"success": False, "error": f"deck not found: {target!r}"}
            _emit_changed("deck", {"id": target, "deleted": True})
            return {"success": True, "deleted": target}

        if action == "card_set":
            parsed_cards = _parse_json_list(cards, "cards")
            if not parsed_cards:
                return {"success": False, "error": "cards must be a non-empty JSON array"}
            result = learning_store.set_cards(
                deck_id=deck_id, cards=parsed_cards, updated_by=updated_by
            )
            _emit_changed("deck", {"id": deck_id, "rev": result["rev"]})
            return {"success": True, **result}

        if action == "card_delete":
            result = learning_store.delete_card(
                deck_id=deck_id, card_id=card_id or id, updated_by=updated_by
            )
            _emit_changed("deck", {"id": deck_id, "rev": result["rev"]})
            return {"success": True, **result}

        # ── Learner state (read-only) ──
        if action == "progress_get":
            return {"success": True, "progress": learning_store.get_progress(course_id or id)}

        if action == "stats_get":
            return {"success": True, "stats": learning_store.learning_stats()}

        return {"success": False, "error": f"unknown action {action!r}"}
    except LookupError as exc:
        return {"success": False, "error": str(exc)}
    except ValueError as exc:
        return {"success": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 — tool results must not raise
        logger.exception("learning tool failed")
        return {"success": False, "error": str(exc)}


def _emit_changed(entity: str, payload: dict) -> None:
    """Best-effort learning.changed emission — tool calls should update
    connected clients live, but a headless context (no gateway loop) must
    not fail the write."""
    try:
        from tui_gateway.server import _emit

        event = {"entity": entity}
        for key in ("id", "rev", "updated_at", "updated_by", "deleted"):
            if key in payload:
                event[key] = payload[key]
        _emit("learning.changed", "", event)
    except Exception:  # noqa: BLE001
        pass


# =============================================================================
# OpenAI Function-Calling Schema
# =============================================================================

LEARNING_SCHEMA = {
    "name": "learning",
    "description": (
        "Build and maintain the user's LEARNING LIBRARY: courses (modules of "
        "lessons and quizzes) and flashcard decks, persisted on the gateway "
        "and streamed live to the user's client. Use this whenever the user "
        "wants to be TAUGHT a subject (\"teach me X\", \"build me a course on "
        "X\") or wants flashcards.\n\n"
        "BUILD INCREMENTALLY — never as one blob: `course_create` (returns "
        "the course id), then `module_set` per module (returns the module "
        "id), then `step_set` per lesson or quiz. To change ONE step later, "
        "`course_get` first, then `step_set` that step by its id — NEVER "
        "rebuild the course. `append_questions: true` extends a quiz without "
        "resending its existing questions. Lesson steps carry a `markdown` "
        "body (a few hundred words: explain, then a concrete example); quiz "
        "steps carry `questions` as a JSON array of {q, options: [\"A) …\", "
        "\"B) …\", \"C) …\", \"D) …\"], correct: \"A\", explanation}. A good "
        "course: 3-5 modules, each 2-4 lessons then one quiz over that "
        "module's material.\n\n"
        "Decks: `deck_create` then `card_set` with a JSON array of {front, "
        "back, category?} — batched, so send all cards for a topic in one "
        "call; update a card by including its id.\n\n"
        "The user's progress (completions, quiz scores, spaced-repetition "
        "schedule) is recorded by their client — you can READ it via "
        "`progress_get`/`stats_get` to adapt: a module with low quiz scores "
        "deserves a remedial lesson (`step_set` a new lesson into that "
        "module); mastered material deserves harder questions "
        "(`append_questions`). You cannot write progress."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "course_create", "course_update", "course_get", "course_list",
                    "course_delete", "module_set", "module_delete",
                    "step_set", "step_delete",
                    "deck_create", "deck_get", "deck_list", "deck_delete",
                    "card_set", "card_delete",
                    "progress_get", "stats_get",
                ],
            },
            "course_id": {"type": "string", "description": "Course id (from course_create/course_list)"},
            "module_id": {"type": "string", "description": "Module id (from module_set)"},
            "step_id": {"type": "string", "description": "Step id (from step_set / course_get)"},
            "deck_id": {"type": "string", "description": "Deck id (from deck_create/deck_list)"},
            "card_id": {"type": "string", "description": "Card id (for card_delete)"},
            "id": {"type": "string", "description": "Explicit id for creation (optional — omit to mint one)"},
            "title": {"type": "string", "description": "Course/module/step title"},
            "summary": {"type": "string", "description": "One-paragraph course summary"},
            "overview": {"type": "string", "description": "One-sentence module framing"},
            "topic": {"type": "string", "description": "Deck topic"},
            "step_type": {"type": "string", "enum": ["lesson", "quiz"], "description": "Required when creating a step"},
            "markdown": {"type": "string", "description": "Lesson body (markdown)"},
            "questions": {
                "type": "string",
                "description": "JSON array of {q, options[], correct, explanation} for quiz steps",
            },
            "cards": {
                "type": "string",
                "description": "JSON array of {front, back, category?, id?} for card_set",
            },
            "append_questions": {
                "type": "boolean",
                "description": "step_set on a quiz: extend the question list instead of replacing it",
            },
            "position": {
                "type": "integer",
                "description": "Insert index for module_set/step_set (omit or -1 to append)",
            },
        },
        "required": ["action"],
    },
}


# --- Registry ---
from tools.registry import registry

registry.register(
    name="learning",
    toolset="learning",
    schema=LEARNING_SCHEMA,
    handler=lambda args, **kw: learning_tool(
        action=args.get("action", ""),
        course_id=args.get("course_id", ""),
        module_id=args.get("module_id", ""),
        step_id=args.get("step_id", ""),
        deck_id=args.get("deck_id", ""),
        card_id=args.get("card_id", ""),
        id=args.get("id", ""),
        title=args.get("title", ""),
        summary=args.get("summary", ""),
        overview=args.get("overview", ""),
        topic=args.get("topic", ""),
        step_type=args.get("step_type", ""),
        markdown=args.get("markdown", ""),
        questions=args.get("questions", ""),
        cards=args.get("cards", ""),
        append_questions=bool(args.get("append_questions", False)),
        position=int(args.get("position", -1) or -1),
        session_id=str(kw.get("session_id", "") or ""),
    ),
    emoji="🎓",
)
