"""CCA Step 4 / Batch 4B — Conditional link validation.

Spec §6.4 + user clarifications 2026-05-07:

- A Practice can be linked to **at most one** Conditional Question.
- A Practice that is part of a saved Relation cannot have a
  PracticeConditional — the link goes on the Relation instead
  (Path A: `RelationConditional` table introduced in this batch).
- A Relation can be linked to **at most one** Conditional Question.
- When `create_relation` runs, refuse if any incoming Practice has
  an existing PracticeConditional — the SE must explicitly clear
  the link before moving the practice into a relation.

Pure-function checkers operate on already-loaded ORM rows so they
can be unit-tested without the DB. The async router-side wrappers
load the rows + delegate.

Stable error codes:
- `practice_in_relation_use_relation_link`
- `practice_already_in_conditional`
- `relation_already_in_conditional`
- `practice_has_independent_conditional` (cross-check at relation
  create time; the relation-validation service in `relation_validation.py`
  consumes this rule)
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


class ConditionalValidationError(Exception):
    """Raised when a conditional-link validation rule fails. `code`
    is the stable identifier the route layer maps to a 422."""

    def __init__(self, code: str, message: str, extra: Optional[dict] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra or {}


def assert_practice_can_be_linked_to_conditional(
    *,
    practice_id: str,
    practice_relation_id: Optional[str],
    target_question_id: str,
    existing_question_id_for_practice: Optional[str],
) -> None:
    """Run both rules at link-time for `link_practice_conditional`:

    1. Practice must NOT be in a saved Relation. If it is, the
       conditional link goes on the Relation (use the
       `link_relation_conditional` endpoint instead).
    2. Practice must not already be linked to ANOTHER conditional
       question. If `existing_question_id_for_practice` matches
       `target_question_id`, the call is a duplicate (idempotent
       no-op handled by the caller); if different, refuse.
    """
    if practice_relation_id is not None:
        raise ConditionalValidationError(
            "practice_in_relation_use_relation_link",
            "This Practice is part of a saved Relation. The "
            "conditional link must go on the Relation itself — use "
            "the link_relation_conditional endpoint instead.",
            extra={
                "practice_id": practice_id,
                "relation_id": practice_relation_id,
            },
        )
    if (
        existing_question_id_for_practice is not None
        and existing_question_id_for_practice != target_question_id
    ):
        raise ConditionalValidationError(
            "practice_already_in_conditional",
            "This Practice is already linked to another Conditional "
            "Question. A practice may be linked to at most one "
            "conditional question.",
            extra={
                "practice_id": practice_id,
                "current_question_id": existing_question_id_for_practice,
                "target_question_id": target_question_id,
            },
        )


def assert_relation_can_be_linked_to_conditional(
    *,
    relation_id: str,
    target_question_id: str,
    existing_question_id_for_relation: Optional[str],
) -> None:
    """Same idea for relation-side links: a Relation can be linked
    to at most one Conditional Question. Same-question repeats are
    idempotent (the caller decides); different-question is refused."""
    if (
        existing_question_id_for_relation is not None
        and existing_question_id_for_relation != target_question_id
    ):
        raise ConditionalValidationError(
            "relation_already_in_conditional",
            "This Relation is already linked to another Conditional "
            "Question. A relation may be linked to at most one "
            "conditional question.",
            extra={
                "relation_id": relation_id,
                "current_question_id": existing_question_id_for_relation,
                "target_question_id": target_question_id,
            },
        )


def assert_practices_have_no_independent_conditional(
    *,
    practices_with_conditional: list[dict],
) -> None:
    """Cross-check at `create_relation` time. Each entry in
    `practices_with_conditional` is a dict with at least
    `practice_id` and `question_id`. If the list is non-empty, the
    relation save is refused.

    Reasoning: when a Practice is independent, its conditional link
    is stored on PracticeConditional. Once the practice joins a
    Relation, that link must move to the Relation (RelationConditional)
    or be discarded. To avoid silent data shifts, the SE must clear
    the practice-side link explicitly before adding the practice to
    a relation, then re-link via the relation if desired.
    """
    if practices_with_conditional:
        raise ConditionalValidationError(
            "practice_has_independent_conditional",
            "At least one Practice has an existing Conditional link. "
            "Clear the practice-side conditional link first, then "
            "add the Practice to the Relation, then link the Relation "
            "to the conditional if needed.",
            extra={"practices": practices_with_conditional},
        )
