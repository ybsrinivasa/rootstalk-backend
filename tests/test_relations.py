"""Tests for the Practice Relations foundation service.

Covers worked examples from RootsTalk_Relations_Reference.pdf — role
encoding, structure builder, Gate 1, Gate 2, count display.
"""
import pytest

from app.services.relations import (
    CountDisplay,
    Gate1Result,
    Gate2Result,
    Option,
    Part,
    PracticeRef,
    RelationStructure,
    build_structure,
    compute_count_display,
    decode_role,
    encode_role,
    validate_gate1_option,
    validate_gate2,
)


# ── Helpers ──────────────────────────────────────────────────────────────────


def make_practice(cn_id: str, role: str, special: bool = False) -> PracticeRef:
    return PracticeRef(
        practice_id=f"prac-{cn_id}-{role}",
        common_name_cosh_id=cn_id,
        is_special_input=special,
        role=role,
    )


# ── Role encode / decode ─────────────────────────────────────────────────────


def test_role_roundtrip():
    assert encode_role(1, 2, 3) == "PART_1__OPT_2__POS_3"
    coords = decode_role("PART_1__OPT_2__POS_3")
    assert coords.part == 1 and coords.option == 2 and coords.position == 3


def test_encode_role_rejects_zero():
    with pytest.raises(ValueError):
        encode_role(0, 1, 1)
    with pytest.raises(ValueError):
        encode_role(1, 0, 1)
    with pytest.raises(ValueError):
        encode_role(1, 1, 0)


def test_decode_role_rejects_malformed():
    with pytest.raises(ValueError):
        decode_role("bogus")
    with pytest.raises(ValueError):
        decode_role("PART_1__OPT_1")
    with pytest.raises(ValueError):
        decode_role("part_1__opt_1__pos_1")


# ── Structure builder ────────────────────────────────────────────────────────


def test_build_structure_simple():
    """(A + B) or (C) or (D) + (P or Q) + (M) or (N)."""
    practices = [
        make_practice("A", "PART_1__OPT_1__POS_1"),
        make_practice("B", "PART_1__OPT_1__POS_2"),
        make_practice("C", "PART_1__OPT_2__POS_1"),
        make_practice("D", "PART_1__OPT_3__POS_1"),
        make_practice("P", "PART_2__OPT_1__POS_1"),
        make_practice("Q", "PART_2__OPT_2__POS_1"),
        make_practice("M", "PART_3__OPT_1__POS_1"),
        make_practice("N", "PART_3__OPT_2__POS_1"),
    ]
    s = build_structure(practices, "rel-1", "OR")
    assert len(s.parts) == 3
    assert s.parts[0].max_size == 2  # (A+B) is the largest option in Part 1
    assert s.parts[0].min_size == 1
    assert not s.parts[0].is_size_deterministic()
    assert s.parts[1].is_size_deterministic()
    assert s.total_max_count == 4  # 2 + 1 + 1
    assert not s.is_count_deterministic()


def test_build_structure_orders_positions_within_option():
    practices = [
        make_practice("B", "PART_1__OPT_1__POS_2"),
        make_practice("A", "PART_1__OPT_1__POS_1"),
        make_practice("C", "PART_1__OPT_1__POS_3"),
    ]
    s = build_structure(practices, "rel-x", "AND")
    assert [p.common_name_cosh_id for p in s.parts[0].options[0].practices] == ["A", "B", "C"]


def test_build_structure_empty():
    s = build_structure([], "rel-empty", "AND")
    assert s.parts == []
    assert s.total_max_count == 0
    assert s.is_count_deterministic()


# ── Gate 2: valid relations ──────────────────────────────────────────────────


def test_gate2_valid_or_or():
    """(A or B) + (A or C) → valid (A+C, B+A, B+C all unique combos)."""
    practices = [
        make_practice("A", "PART_1__OPT_1__POS_1"),
        make_practice("B", "PART_1__OPT_2__POS_1"),
        make_practice("A", "PART_2__OPT_1__POS_1"),
        make_practice("C", "PART_2__OPT_2__POS_1"),
    ]
    s = build_structure(practices, "rel-1", "OR")
    result = validate_gate2(s)
    assert result.valid


def test_gate2_invalid_all_dup():
    """(A) + (A) → only one combination, A+A, duplicate → invalid."""
    practices = [
        make_practice("A", "PART_1__OPT_1__POS_1"),
        make_practice("A", "PART_2__OPT_1__POS_1"),
    ]
    s = build_structure(practices, "rel-1", "AND")
    result = validate_gate2(s)
    assert not result.valid


def test_gate2_branch_precheck():
    """(A+B) or (C) + A → (A+B) branch always duplicates A (mandatory)."""
    practices = [
        make_practice("A", "PART_1__OPT_1__POS_1"),
        make_practice("B", "PART_1__OPT_1__POS_2"),
        make_practice("C", "PART_1__OPT_2__POS_1"),
        make_practice("A", "PART_2__OPT_1__POS_1"),  # mandatory A
    ]
    s = build_structure(practices, "rel-1", "OR")
    result = validate_gate2(s)
    assert not result.valid
    assert result.error_code == "BRANCH_ALWAYS_DUPLICATES"
    assert result.bad_branch == (1, 1)


def test_gate2_empty_structure_is_valid():
    s = build_structure([], "rel-empty", "AND")
    result = validate_gate2(s)
    assert result.valid


# ── Special-input exemption ──────────────────────────────────────────────────


def test_special_input_exempt():
    """(A + Adj) or (B + Adj) + (C) — Adj appears repeatedly but is exempt."""
    practices = [
        make_practice("A", "PART_1__OPT_1__POS_1"),
        make_practice("Adj", "PART_1__OPT_1__POS_2", special=True),
        make_practice("B", "PART_1__OPT_2__POS_1"),
        make_practice("Adj", "PART_1__OPT_2__POS_2", special=True),
        make_practice("C", "PART_2__OPT_1__POS_1"),
    ]
    s = build_structure(practices, "rel-1", "OR")
    result = validate_gate2(s)
    assert result.valid


# ── Gate 1: duplicate in group ───────────────────────────────────────────────


def test_gate1_duplicate_in_group():
    new_option = [
        make_practice("A", "PART_1__OPT_1__POS_1"),
        make_practice("A", "PART_1__OPT_1__POS_2"),
    ]
    result = validate_gate1_option(new_option, [])
    assert not result.valid
    assert result.error_code == "DUPLICATE_IN_GROUP"


def test_gate1_duplicate_in_group_ignores_special():
    """Two special inputs of same id are allowed within an Option."""
    new_option = [
        make_practice("A", "PART_1__OPT_1__POS_1"),
        make_practice("Adj", "PART_1__OPT_1__POS_2", special=True),
        make_practice("Adj", "PART_1__OPT_1__POS_3", special=True),
    ]
    result = validate_gate1_option(new_option, [])
    assert result.valid


def test_gate1_double_brackets():
    """Adding a 2nd compound Option to a Part that already has one is rejected."""
    existing = [
        Option(
            option_index=1,
            practices=[
                make_practice("A", "PART_1__OPT_1__POS_1"),
                make_practice("B", "PART_1__OPT_1__POS_2"),
            ],
        )
    ]
    new_option = [
        make_practice("C", "PART_1__OPT_2__POS_1"),
        make_practice("D", "PART_1__OPT_2__POS_2"),
    ]
    result = validate_gate1_option(new_option, existing)
    assert not result.valid
    assert result.error_code == "DOUBLE_BRACKETS"


def test_gate1_compound_then_single_ok():
    """A Part may have one compound Option and any number of single-input Options."""
    existing = [
        Option(
            option_index=1,
            practices=[
                make_practice("A", "PART_1__OPT_1__POS_1"),
                make_practice("B", "PART_1__OPT_1__POS_2"),
            ],
        )
    ]
    new_option = [make_practice("C", "PART_1__OPT_2__POS_1")]
    result = validate_gate1_option(new_option, existing)
    assert result.valid


def test_gate1_duplicate_option():
    """Adding an Option whose inputs match an existing Option exactly is rejected."""
    existing = [
        Option(
            option_index=1,
            practices=[make_practice("A", "PART_1__OPT_1__POS_1")],
        )
    ]
    new_option = [make_practice("A", "PART_1__OPT_2__POS_1")]
    result = validate_gate1_option(new_option, existing)
    assert not result.valid
    assert result.error_code == "DUPLICATE_OPTION"


# ── Count display ────────────────────────────────────────────────────────────


def test_count_display_deterministic():
    """3 standalone practices, no relations → '3 items'."""
    cd = compute_count_display([], 3)
    assert cd.count == 3
    assert not cd.is_max
    assert str(cd) == "3 items"


def test_count_display_singular():
    cd = compute_count_display([], 1)
    assert str(cd) == "1 item"


def test_count_display_uncertain():
    """Order with relation (A+B) or (C) — Part 1 has different option sizes."""
    practices = [
        make_practice("A", "PART_1__OPT_1__POS_1"),
        make_practice("B", "PART_1__OPT_1__POS_2"),
        make_practice("C", "PART_1__OPT_2__POS_1"),
    ]
    s = build_structure(practices, "rel-1", "OR")
    cd = compute_count_display([s], standalone_count=0)
    assert cd.count == 2
    assert cd.is_max
    assert str(cd) == "Max 2 items"


def test_count_display_5_with_or():
    """A + B + C + (D or E) → 4 items, deterministic."""
    practices = [
        make_practice("A", "PART_1__OPT_1__POS_1"),
        make_practice("B", "PART_2__OPT_1__POS_1"),
        make_practice("C", "PART_3__OPT_1__POS_1"),
        make_practice("D", "PART_4__OPT_1__POS_1"),
        make_practice("E", "PART_4__OPT_2__POS_1"),
    ]
    s = build_structure(practices, "rel-1", "OR")
    cd = compute_count_display([s], standalone_count=0)
    assert cd.count == 4
    assert not cd.is_max
    assert str(cd) == "4 items"


def test_count_display_mixed_relations_and_standalone():
    """Standalone 2 + relation with Max 2 → Max 4 items."""
    practices = [
        make_practice("A", "PART_1__OPT_1__POS_1"),
        make_practice("B", "PART_1__OPT_1__POS_2"),
        make_practice("C", "PART_1__OPT_2__POS_1"),
    ]
    s = build_structure(practices, "rel-1", "OR")
    cd = compute_count_display([s], standalone_count=2)
    assert cd.count == 4
    assert cd.is_max
    assert str(cd) == "Max 4 items"
