"""Pure-function tests for `validate_relation_save` and its
sub-checks. Integration coverage of the API surface lives in
`tests/test_phase_cca_step4_integration.py`.
"""
from __future__ import annotations

import pytest

from app.services.relations import PracticeRef
from app.services.relation_validation import (
    RelationValidationFailed,
    build_structure_from_parts,
    validate_relation_save,
)


def _ref(pid: str, *, is_special: bool = False, common: str | None = None,
         l2: str | None = None):
    return PracticeRef(
        practice_id=pid,
        common_name_cosh_id=common or f"cn:{pid}",
        is_special_input=is_special,
        role="",
        l2_type=l2,
    )


def _meta(*, l0: str = "INPUT", l1: str = "PESTICIDE",
          timeline_id: str = "TL-1", relation_id: str | None = None) -> dict:
    return {
        "l0_type": l0, "l1_type": l1,
        "timeline_id": timeline_id, "relation_id": relation_id,
    }


# ── happy paths ──────────────────────────────────────────────────────────────

def test_pure_and_two_inputs_passes():
    """Pesticide + Fertilizer in one Part/Option (AND of two)."""
    structure = validate_relation_save(
        relation_type="AND",
        target_timeline_id="TL-1",
        parts=[[["A", "B"]]],
        practice_refs_by_id={
            "A": _ref("A"),
            "B": _ref("B", common="cn:B"),
        },
        practice_meta={
            "A": _meta(l1="PESTICIDE"),
            "B": _meta(l1="FERTILIZER"),
        },
    )
    assert len(structure.parts) == 1
    assert len(structure.parts[0].options) == 1
    assert len(structure.parts[0].options[0].practices) == 2
    # Roles populated
    assert structure.parts[0].options[0].practices[0].role == "PART_1__OPT_1__POS_1"
    assert structure.parts[0].options[0].practices[1].role == "PART_1__OPT_1__POS_2"


def test_pure_or_within_pesticides_passes():
    validate_relation_save(
        relation_type="OR",
        target_timeline_id="TL-1",
        parts=[[["A"], ["B"]]],
        practice_refs_by_id={"A": _ref("A"), "B": _ref("B")},
        practice_meta={"A": _meta(l1="PESTICIDE"), "B": _meta(l1="PESTICIDE")},
    )


def test_or_with_special_input_alongside_pesticide_passes():
    """Spec §6.4 + user clarification: Special Inputs (adjuvants)
    are an exception and may mix into either side of an OR."""
    validate_relation_save(
        relation_type="OR",
        target_timeline_id="TL-1",
        parts=[[["A"], ["S"]]],
        practice_refs_by_id={
            "A": _ref("A"),
            "S": _ref("S", is_special=True),
        },
        practice_meta={
            "A": _meta(l1="PESTICIDE"),
            "S": _meta(l1="ADJUVANT"),  # L1 is irrelevant when special
        },
    )


# ── AND restriction: input-only ──────────────────────────────────────────────

def test_and_with_non_input_fails():
    """User correction 2026-05-07: AND relations are between Input
    Practices only. Non-Inputs cannot participate."""
    with pytest.raises(RelationValidationFailed) as ei:
        validate_relation_save(
            relation_type="AND",
            target_timeline_id="TL-1",
            parts=[[["A", "X"]]],
            practice_refs_by_id={"A": _ref("A"), "X": _ref("X")},
            practice_meta={
                "A": _meta(l0="INPUT", l1="PESTICIDE"),
                "X": _meta(l0="NON_INPUT", l1="WATER_MGMT"),
            },
        )
    codes = {e.code for e in ei.value.errors}
    assert "relation_and_non_input" in codes


def test_and_with_instruction_fails():
    with pytest.raises(RelationValidationFailed) as ei:
        validate_relation_save(
            relation_type="AND",
            target_timeline_id="TL-1",
            parts=[[["A", "I"]]],
            practice_refs_by_id={"A": _ref("A"), "I": _ref("I")},
            practice_meta={
                "A": _meta(l0="INPUT", l1="PESTICIDE"),
                "I": _meta(l0="INSTRUCTION", l1=None),
            },
        )
    codes = {e.code for e in ei.value.errors}
    assert "relation_and_non_input" in codes


def test_and_pesticide_plus_special_input_passes():
    """Adjuvants are Special Inputs but still L0=INPUT, so they pass
    the AND-Input-only rule."""
    validate_relation_save(
        relation_type="AND",
        target_timeline_id="TL-1",
        parts=[[["A", "S"]]],
        practice_refs_by_id={
            "A": _ref("A"),
            "S": _ref("S", is_special=True, common="cn:adjuvant"),
        },
        practice_meta={
            "A": _meta(l0="INPUT", l1="PESTICIDE"),
            "S": _meta(l0="INPUT", l1="ADJUVANT"),
        },
    )


# ── OR L1 restriction ────────────────────────────────────────────────────────

def test_or_pesticide_plus_fertilizer_fails():
    """Spec: OR cannot span Pesticides + Fertilizers (Special Inputs
    are the only mixers)."""
    with pytest.raises(RelationValidationFailed) as ei:
        validate_relation_save(
            relation_type="OR",
            target_timeline_id="TL-1",
            parts=[[["A"], ["B"]]],
            practice_refs_by_id={"A": _ref("A"), "B": _ref("B")},
            practice_meta={
                "A": _meta(l1="PESTICIDE"),
                "B": _meta(l1="FERTILIZER"),
            },
        )
    codes = {e.code for e in ei.value.errors}
    assert "relation_or_cross_l1" in codes


def test_or_with_non_input_fails():
    with pytest.raises(RelationValidationFailed) as ei:
        validate_relation_save(
            relation_type="OR",
            target_timeline_id="TL-1",
            parts=[[["A"], ["X"]]],
            practice_refs_by_id={"A": _ref("A"), "X": _ref("X")},
            practice_meta={
                "A": _meta(l1="PESTICIDE"),
                "X": _meta(l0="NON_INPUT", l1="WATER_MGMT"),
            },
        )
    codes = {e.code for e in ei.value.errors}
    assert "relation_or_only_inputs" in codes


# ── Cross-timeline + already-in-relation ─────────────────────────────────────

def test_practice_from_different_timeline_fails():
    with pytest.raises(RelationValidationFailed) as ei:
        validate_relation_save(
            relation_type="AND",
            target_timeline_id="TL-1",
            parts=[[["A", "B"]]],
            practice_refs_by_id={"A": _ref("A"), "B": _ref("B")},
            practice_meta={
                "A": _meta(timeline_id="TL-1"),
                "B": _meta(timeline_id="TL-2"),
            },
        )
    codes = {e.code for e in ei.value.errors}
    assert "relation_cross_timeline" in codes


def test_practice_already_in_another_relation_fails():
    with pytest.raises(RelationValidationFailed) as ei:
        validate_relation_save(
            relation_type="AND",
            target_timeline_id="TL-1",
            parts=[[["A", "B"]]],
            practice_refs_by_id={"A": _ref("A"), "B": _ref("B")},
            practice_meta={
                "A": _meta(),
                "B": _meta(relation_id="EXISTING-REL"),
            },
        )
    codes = {e.code for e in ei.value.errors}
    assert "relation_practice_already_in_relation" in codes


# ── Structural: double brackets ──────────────────────────────────────────────

def test_double_brackets_fails():
    """Spec: `(A+B) or (C+D)` rejected at any stage. Two compound
    Options in the same Part is forbidden."""
    with pytest.raises(RelationValidationFailed) as ei:
        validate_relation_save(
            relation_type="OR",
            target_timeline_id="TL-1",
            parts=[[["A", "B"], ["C", "D"]]],
            practice_refs_by_id={
                "A": _ref("A"), "B": _ref("B"),
                "C": _ref("C"), "D": _ref("D"),
            },
            practice_meta={
                "A": _meta(l1="PESTICIDE"), "B": _meta(l1="PESTICIDE"),
                "C": _meta(l1="PESTICIDE"), "D": _meta(l1="PESTICIDE"),
            },
        )
    codes = {e.code for e in ei.value.errors}
    assert "relation_double_brackets" in codes


# ── Combinatorial duplicates ─────────────────────────────────────────────────

def test_branch_always_duplicates_fails():
    """Spec example: `(A + B) or (C + D) + A` — choosing branch
    `(A+B)` ALWAYS duplicates A. Reject even though `(C+D)+A` works."""
    with pytest.raises(RelationValidationFailed) as ei:
        validate_relation_save(
            relation_type="AND",
            target_timeline_id="TL-1",
            parts=[
                [["A", "B"], ["C", "D"]],
                [["A2"]],  # mandatory A
            ],
            practice_refs_by_id={
                "A": _ref("A", common="cn:X"),
                "B": _ref("B"),
                "C": _ref("C"),
                "D": _ref("D"),
                "A2": _ref("A2", common="cn:X"),  # same Common Name as A
            },
            practice_meta={
                "A": _meta(l1="PESTICIDE"),
                "B": _meta(l1="PESTICIDE"),
                "C": _meta(l1="PESTICIDE"),
                "D": _meta(l1="PESTICIDE"),
                "A2": _meta(l1="PESTICIDE"),
            },
        )
    codes = {e.code for e in ei.value.errors}
    assert "relation_branch_always_duplicates" in codes


def test_no_valid_combination_fails():
    """Mandatory inputs (single-Option Parts) duplicate each other."""
    with pytest.raises(RelationValidationFailed) as ei:
        validate_relation_save(
            relation_type="AND",
            target_timeline_id="TL-1",
            parts=[[["A1"]], [["A2"]]],
            practice_refs_by_id={
                "A1": _ref("A1", common="cn:X"),
                "A2": _ref("A2", common="cn:X"),  # same Common Name
            },
            practice_meta={
                "A1": _meta(l1="PESTICIDE"),
                "A2": _meta(l1="PESTICIDE"),
            },
        )
    codes = {e.code for e in ei.value.errors}
    assert "relation_no_valid_combination" in codes


def test_at_least_one_valid_combination_passes():
    """`(C+D) or E + A`: combinations {C,D,A} and {E,A} both avoid
    duplicates → valid. (Two compound Options in the same Part
    would be double-brackets, so one compound + one simple is the
    pattern that exercises this case.)"""
    validate_relation_save(
        relation_type="AND",
        target_timeline_id="TL-1",
        parts=[
            [["C", "D"], ["E"]],
            [["A"]],
        ],
        practice_refs_by_id={
            "C": _ref("C"), "D": _ref("D"),
            "E": _ref("E"), "A": _ref("A"),
        },
        practice_meta={
            "C": _meta(l1="PESTICIDE"), "D": _meta(l1="PESTICIDE"),
            "E": _meta(l1="PESTICIDE"), "A": _meta(l1="PESTICIDE"),
        },
    )


def test_special_input_excluded_from_duplicate_detection():
    """Special Inputs (adjuvants) are exempt from duplicate checks
    — they may appear freely. Same Common Name on a Special and a
    non-Special is not flagged."""
    validate_relation_save(
        relation_type="AND",
        target_timeline_id="TL-1",
        parts=[[["A", "S"]]],
        practice_refs_by_id={
            "A": _ref("A", common="cn:shared"),
            "S": _ref("S", is_special=True, common="cn:shared"),
        },
        practice_meta={
            "A": _meta(l1="PESTICIDE"),
            "S": _meta(l1="ADJUVANT"),
        },
    )


# ── build_structure_from_parts ───────────────────────────────────────────────

def test_build_structure_role_encoding():
    """Verify role strings are the standard PART/OPT/POS form."""
    structure = build_structure_from_parts(
        parts=[[["A", "B"], ["C"]], [["D"]]],
        practice_refs_by_id={
            "A": _ref("A"), "B": _ref("B"),
            "C": _ref("C"), "D": _ref("D"),
        },
        relation_id="REL-1",
        relation_type="OR",
    )
    role_a = structure.parts[0].options[0].practices[0].role
    role_b = structure.parts[0].options[0].practices[1].role
    role_c = structure.parts[0].options[1].practices[0].role
    role_d = structure.parts[1].options[0].practices[0].role
    assert role_a == "PART_1__OPT_1__POS_1"
    assert role_b == "PART_1__OPT_1__POS_2"
    assert role_c == "PART_1__OPT_2__POS_1"
    assert role_d == "PART_2__OPT_1__POS_1"


def test_practice_can_appear_multiple_times_within_relation():
    """Spec §6.4: 'The same practice can appear multiple times
    within a relation' — confirmed by user 2026-05-07. Modelled as
    `(A+B) or A`: one compound Option containing A+B, one simple
    Option containing A. A appears in both Options. Combinations
    are {A,B} or {A}, neither duplicates within itself.

    Two compound Options in the same Part (`(A+B) or (A+C)`) is
    rejected as double-brackets — that pattern is expressed as
    `A AND (B or C)`, two Parts, instead.
    """
    validate_relation_save(
        relation_type="OR",
        target_timeline_id="TL-1",
        parts=[[["A", "B"], ["A"]]],
        practice_refs_by_id={
            "A": _ref("A", common="cn:A"),
            "B": _ref("B", common="cn:B"),
        },
        practice_meta={
            "A": _meta(l1="PESTICIDE"),
            "B": _meta(l1="PESTICIDE"),
        },
    )


# ── Multi-error response ─────────────────────────────────────────────────────

def test_multiple_violations_collected_in_one_response():
    """User-facing rule: a single failed save returns ALL violations
    so the CA can fix them in one pass."""
    with pytest.raises(RelationValidationFailed) as ei:
        validate_relation_save(
            relation_type="OR",
            target_timeline_id="TL-1",
            parts=[[["A", "B"], ["C", "D"]]],  # double brackets
            practice_refs_by_id={
                "A": _ref("A"), "B": _ref("B"),
                "C": _ref("C"), "D": _ref("D"),
            },
            practice_meta={
                # Mixed L1: cross-l1 violation too
                "A": _meta(l1="PESTICIDE"),
                "B": _meta(l1="PESTICIDE"),
                "C": _meta(l1="FERTILIZER"),
                "D": _meta(l1="FERTILIZER"),
            },
        )
    codes = {e.code for e in ei.value.errors}
    assert "relation_or_cross_l1" in codes
    assert "relation_double_brackets" in codes
