"""Pure-function tests for `conditional_validation.py`. Integration
coverage of the API surface lives in
`tests/test_phase_cca_step4_integration.py`.
"""
from __future__ import annotations

import pytest

from app.services.conditional_validation import (
    ConditionalValidationError,
    assert_practice_can_be_linked_to_conditional,
    assert_practices_have_no_independent_conditional,
    assert_relation_can_be_linked_to_conditional,
)


# ── assert_practice_can_be_linked_to_conditional ─────────────────────────────

def test_practice_independent_no_existing_link_passes():
    """Happy path: independent practice, no existing link."""
    assert_practice_can_be_linked_to_conditional(
        practice_id="P", practice_relation_id=None,
        target_question_id="Q", existing_question_id_for_practice=None,
    )


def test_practice_in_relation_must_use_relation_link():
    """Spec §6.4 + user clarification: if the practice is in a
    saved Relation, the conditional binds to the Relation."""
    with pytest.raises(ConditionalValidationError) as ei:
        assert_practice_can_be_linked_to_conditional(
            practice_id="P", practice_relation_id="REL-1",
            target_question_id="Q", existing_question_id_for_practice=None,
        )
    assert ei.value.code == "practice_in_relation_use_relation_link"
    assert ei.value.extra["relation_id"] == "REL-1"


def test_practice_already_linked_to_different_question_fails():
    """A practice can be linked to at most ONE conditional question."""
    with pytest.raises(ConditionalValidationError) as ei:
        assert_practice_can_be_linked_to_conditional(
            practice_id="P", practice_relation_id=None,
            target_question_id="Q-NEW",
            existing_question_id_for_practice="Q-OLD",
        )
    assert ei.value.code == "practice_already_in_conditional"


def test_practice_already_linked_to_same_question_passes():
    """Same `(practice_id, question_id)` is idempotent — caller
    decides whether to update the answer or skip. The validator
    doesn't fire."""
    assert_practice_can_be_linked_to_conditional(
        practice_id="P", practice_relation_id=None,
        target_question_id="Q", existing_question_id_for_practice="Q",
    )


# ── assert_relation_can_be_linked_to_conditional ─────────────────────────────

def test_relation_no_existing_link_passes():
    assert_relation_can_be_linked_to_conditional(
        relation_id="REL", target_question_id="Q",
        existing_question_id_for_relation=None,
    )


def test_relation_already_linked_to_different_question_fails():
    """One conditional per Relation, mirroring the practice rule."""
    with pytest.raises(ConditionalValidationError) as ei:
        assert_relation_can_be_linked_to_conditional(
            relation_id="REL", target_question_id="Q-NEW",
            existing_question_id_for_relation="Q-OLD",
        )
    assert ei.value.code == "relation_already_in_conditional"


def test_relation_already_linked_to_same_question_passes():
    assert_relation_can_be_linked_to_conditional(
        relation_id="REL", target_question_id="Q",
        existing_question_id_for_relation="Q",
    )


# ── assert_practices_have_no_independent_conditional ─────────────────────────

def test_no_independent_conditionals_passes():
    assert_practices_have_no_independent_conditional(practices_with_conditional=[])


def test_at_least_one_independent_conditional_fails():
    """Cross-check at relation-create time: surface the offending
    practice/question pairs so the SE can clear them first."""
    with pytest.raises(ConditionalValidationError) as ei:
        assert_practices_have_no_independent_conditional(
            practices_with_conditional=[
                {"practice_id": "P1", "question_id": "Q1"},
                {"practice_id": "P2", "question_id": "Q2"},
            ],
        )
    assert ei.value.code == "practice_has_independent_conditional"
    assert len(ei.value.extra["practices"]) == 2
