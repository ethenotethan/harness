"""Learning-surface JSON-RPC handlers (learning.* — courses, decks,
progress, attempts), moved from the pre-split server.py tail during the
upstream-rebase conflict resolution of PR #5.

Handler bodies are unchanged from the original commit; they are rebound onto
server.py's globals at install time — see method_ctx.py.
"""

from .method_ctx import HandlerRegistry

_registry = HandlerRegistry()
method = _registry.method


# ── Learning surface ─────────────────────────────────────────────────────
# Gateway-persisted courses, decks, learner progress, and quiz attempts —
# the durable backend for a native client's Learning page, replacing
# client-local JSON blobs. Content mutations are GRANULAR (one module /
# step / card batch per call) so agents never resend a parent document;
# learner state (progress/SRS) is written by the client and folded
# server-side with commutative rules. Every mutation emits
# `learning.changed` (metadata only; clients refetch).
# Error family: 5230-5249.


def _learning_changed(entity: str, payload: dict) -> None:
    event = {"entity": entity}
    for key in ("id", "rev", "updated_at", "updated_by", "deleted"):
        if key in payload:
            event[key] = payload[key]
    _emit("learning.changed", "", event)


@method("learning.course.set")
def _(rid, params: dict) -> dict:
    """Create or update a course SHELL (title/summary). Modules and steps
    are managed only through learning.module.set / learning.step.set, so a
    title refresh can never clobber content."""
    try:
        from tui_gateway import learning_store

        stored = learning_store.set_course(
            course_id=str(params.get("id", "")) or None,
            title=params.get("title"),
            summary=params.get("summary"),
            source_session_id=params.get("source_session_id"),
            updated_by=str(params.get("updated_by", "")),
        )
        _learning_changed("course", stored)
        return _ok(rid, {"course": stored})
    except ValueError as e:
        return _err(rid, 4001, str(e))
    except Exception as e:
        logger.exception("learning.course.set failed")
        return _err(rid, 5230, str(e))


@method("learning.course.get")
def _(rid, params: dict) -> dict:
    """One course with full module/step bodies, plus its folded progress."""
    try:
        from tui_gateway import learning_store

        course_id = str(params.get("id", ""))
        course = learning_store.get_course(course_id)
        if course is None:
            return _err(rid, 4004, "course not found")
        return _ok(rid, {
            "course": course,
            "progress": learning_store.get_progress(course_id),
        })
    except Exception as e:
        logger.exception("learning.course.get failed")
        return _err(rid, 5231, str(e))


@method("learning.course.list")
def _(rid, params: dict) -> dict:
    """All courses without module bodies, newest-updated first."""
    try:
        from tui_gateway import learning_store

        return _ok(rid, {"courses": learning_store.list_courses()})
    except Exception as e:
        logger.exception("learning.course.list failed")
        return _err(rid, 5232, str(e))


@method("learning.course.delete")
def _(rid, params: dict) -> dict:
    try:
        from tui_gateway import learning_store

        course_id = str(params.get("id", ""))
        if not learning_store.delete_course(course_id):
            return _err(rid, 4004, "course not found")
        _learning_changed("course", {"id": course_id, "deleted": True})
        return _ok(rid, {"deleted": course_id})
    except Exception as e:
        logger.exception("learning.course.delete failed")
        return _err(rid, 5233, str(e))


@method("learning.module.set")
def _(rid, params: dict) -> dict:
    """Upsert one module. Omitted id mints one (returned)."""
    try:
        from tui_gateway import learning_store

        position = params.get("position")
        result = learning_store.set_module(
            course_id=str(params.get("course_id", "")),
            module_id=str(params.get("id", "")) or None,
            title=params.get("title"),
            overview=params.get("overview"),
            position=int(position) if position is not None else None,
            updated_by=str(params.get("updated_by", "")),
        )
        _learning_changed("course", {"id": str(params.get("course_id", "")), "rev": result["rev"]})
        return _ok(rid, result)
    except LookupError as e:
        return _err(rid, 4004, str(e))
    except (TypeError, ValueError) as e:
        return _err(rid, 4001, str(e))
    except Exception as e:
        logger.exception("learning.module.set failed")
        return _err(rid, 5234, str(e))


@method("learning.module.delete")
def _(rid, params: dict) -> dict:
    try:
        from tui_gateway import learning_store

        result = learning_store.delete_module(
            course_id=str(params.get("course_id", "")),
            module_id=str(params.get("id", "")),
            updated_by=str(params.get("updated_by", "")),
        )
        _learning_changed("course", {"id": str(params.get("course_id", "")), "rev": result["rev"]})
        return _ok(rid, result)
    except LookupError as e:
        return _err(rid, 4004, str(e))
    except Exception as e:
        logger.exception("learning.module.delete failed")
        return _err(rid, 5235, str(e))


@method("learning.step.set")
def _(rid, params: dict) -> dict:
    """Upsert one lesson/quiz step. `append_questions` extends a quiz's
    question list without resending existing questions."""
    try:
        from tui_gateway import learning_store

        position = params.get("position")
        questions = params.get("questions")
        if questions is not None and not isinstance(questions, list):
            return _err(rid, 4001, "questions must be a list")
        result = learning_store.set_step(
            course_id=str(params.get("course_id", "")),
            module_id=str(params.get("module_id", "")),
            step_id=str(params.get("id", "")) or None,
            title=params.get("title"),
            step_type=params.get("type"),
            markdown=params.get("markdown"),
            questions=questions,
            append_questions=bool(params.get("append_questions", False)),
            position=int(position) if position is not None else None,
            updated_by=str(params.get("updated_by", "")),
        )
        _learning_changed("course", {"id": str(params.get("course_id", "")), "rev": result["rev"]})
        return _ok(rid, result)
    except LookupError as e:
        return _err(rid, 4004, str(e))
    except (TypeError, ValueError) as e:
        return _err(rid, 4001, str(e))
    except Exception as e:
        logger.exception("learning.step.set failed")
        return _err(rid, 5236, str(e))


@method("learning.step.delete")
def _(rid, params: dict) -> dict:
    try:
        from tui_gateway import learning_store

        result = learning_store.delete_step(
            course_id=str(params.get("course_id", "")),
            module_id=str(params.get("module_id", "")),
            step_id=str(params.get("id", "")),
            updated_by=str(params.get("updated_by", "")),
        )
        _learning_changed("course", {"id": str(params.get("course_id", "")), "rev": result["rev"]})
        return _ok(rid, result)
    except LookupError as e:
        return _err(rid, 4004, str(e))
    except Exception as e:
        logger.exception("learning.step.delete failed")
        return _err(rid, 5237, str(e))


@method("learning.deck.set")
def _(rid, params: dict) -> dict:
    try:
        from tui_gateway import learning_store

        stored = learning_store.set_deck(
            deck_id=str(params.get("id", "")) or None,
            topic=params.get("topic"),
            updated_by=str(params.get("updated_by", "")),
        )
        _learning_changed("deck", stored)
        return _ok(rid, {"deck": stored})
    except ValueError as e:
        return _err(rid, 4001, str(e))
    except Exception as e:
        logger.exception("learning.deck.set failed")
        return _err(rid, 5238, str(e))


@method("learning.deck.get")
def _(rid, params: dict) -> dict:
    """One deck with cards, plus its folded SRS state."""
    try:
        from tui_gateway import learning_store

        deck_id = str(params.get("id", ""))
        deck = learning_store.get_deck(deck_id)
        if deck is None:
            return _err(rid, 4004, "deck not found")
        return _ok(rid, {"deck": deck, "srs": learning_store.get_srs(deck_id)})
    except Exception as e:
        logger.exception("learning.deck.get failed")
        return _err(rid, 5239, str(e))


@method("learning.deck.list")
def _(rid, params: dict) -> dict:
    try:
        from tui_gateway import learning_store

        return _ok(rid, {"decks": learning_store.list_decks()})
    except Exception as e:
        logger.exception("learning.deck.list failed")
        return _err(rid, 5240, str(e))


@method("learning.deck.delete")
def _(rid, params: dict) -> dict:
    try:
        from tui_gateway import learning_store

        deck_id = str(params.get("id", ""))
        if not learning_store.delete_deck(deck_id):
            return _err(rid, 4004, "deck not found")
        _learning_changed("deck", {"id": deck_id, "deleted": True})
        return _ok(rid, {"deleted": deck_id})
    except Exception as e:
        logger.exception("learning.deck.delete failed")
        return _err(rid, 5241, str(e))


@method("learning.card.set")
def _(rid, params: dict) -> dict:
    """Batched card upsert-by-id: one rev bump for N cards."""
    try:
        from tui_gateway import learning_store

        cards = params.get("cards")
        if not isinstance(cards, list):
            return _err(rid, 4001, "cards must be a list")
        result = learning_store.set_cards(
            deck_id=str(params.get("deck_id", "")),
            cards=cards,
            updated_by=str(params.get("updated_by", "")),
        )
        _learning_changed("deck", {"id": str(params.get("deck_id", "")), "rev": result["rev"]})
        return _ok(rid, result)
    except LookupError as e:
        return _err(rid, 4004, str(e))
    except ValueError as e:
        return _err(rid, 4001, str(e))
    except Exception as e:
        logger.exception("learning.card.set failed")
        return _err(rid, 5242, str(e))


@method("learning.card.delete")
def _(rid, params: dict) -> dict:
    try:
        from tui_gateway import learning_store

        result = learning_store.delete_card(
            deck_id=str(params.get("deck_id", "")),
            card_id=str(params.get("id", "")),
            updated_by=str(params.get("updated_by", "")),
        )
        _learning_changed("deck", {"id": str(params.get("deck_id", "")), "rev": result["rev"]})
        return _ok(rid, result)
    except LookupError as e:
        return _err(rid, 4004, str(e))
    except Exception as e:
        logger.exception("learning.card.delete failed")
        return _err(rid, 5243, str(e))


@method("learning.progress.record")
def _(rid, params: dict) -> dict:
    """Fold one learner progress event (client-written). Commutative folds
    — max score, incremented attempts, first completion stamp — so racing
    devices converge."""
    try:
        from tui_gateway import learning_store

        score = params.get("score_percent")
        record = learning_store.record_progress(
            course_id=str(params.get("course_id", "")),
            step_id=str(params.get("step_id", "")),
            kind=str(params.get("kind", "")),
            score_percent=int(score) if score is not None else None,
            at=params.get("at"),
        )
        _learning_changed("progress", {"id": str(params.get("course_id", ""))})
        return _ok(rid, {"progress": record})
    except (TypeError, ValueError) as e:
        return _err(rid, 4001, str(e))
    except Exception as e:
        logger.exception("learning.progress.record failed")
        return _err(rid, 5244, str(e))


@method("learning.review.record")
def _(rid, params: dict) -> dict:
    """Fold one SRS review (client-written). Server recomputes SM-2; a
    review older than the stored one is dropped (applied: false). `state`
    bootstraps a card's history on first contact only — migration path."""
    try:
        from tui_gateway import learning_store

        state = params.get("state")
        if state is not None and not isinstance(state, dict):
            return _err(rid, 4001, "state must be an object")
        result = learning_store.record_review(
            deck_id=str(params.get("deck_id", "")),
            card_id=str(params.get("card_id", "")),
            quality=int(params.get("quality", -1)),
            reviewed_at=params.get("reviewed_at"),
            state=state,
        )
        _learning_changed("progress", {"id": str(params.get("deck_id", ""))})
        return _ok(rid, result)
    except (TypeError, ValueError) as e:
        return _err(rid, 4001, str(e))
    except Exception as e:
        logger.exception("learning.review.record failed")
        return _err(rid, 5245, str(e))


@method("learning.attempt.record")
def _(rid, params: dict) -> dict:
    """Append one finished quiz session to the immutable attempt log."""
    try:
        from tui_gateway import learning_store

        attempt_id = learning_store.record_attempt(dict(params))
        _learning_changed("attempt", {"id": attempt_id})
        return _ok(rid, {"attempt_id": attempt_id})
    except (TypeError, ValueError) as e:
        return _err(rid, 4001, str(e))
    except Exception as e:
        logger.exception("learning.attempt.record failed")
        return _err(rid, 5246, str(e))


@method("learning.attempt.list")
def _(rid, params: dict) -> dict:
    """Newest-first quiz attempt history."""
    try:
        from tui_gateway import learning_store

        limit = params.get("limit", 50)
        return _ok(rid, {"attempts": learning_store.list_attempts(int(limit))})
    except (TypeError, ValueError):
        return _err(rid, 4001, "limit must be an integer")
    except Exception as e:
        logger.exception("learning.attempt.list failed")
        return _err(rid, 5247, str(e))


def register(server) -> None:
    """Bind this module's handlers onto ``server``'s globals and registry."""
    _registry.install(server)
