"""
BL-03 — Advisory Deduplication Engine
All 10 test cases from RootsTalk_Dev_TestCases.pdf §BL-03.
All Critical priority.
"""
import pytest
from datetime import date, timedelta
from app.services.bl03_deduplication import (
    deduplicate_advisory, timelines_overlap,
    TimelineWindow, PracticeStub, PracticeElement,
)


# ── Helpers ────────────────────────────────────────────────────────────────────

# Use a fixed "today" for all tests so results never depend on the real calendar date
TODAY = date(2026, 5, 1)   # fixed reference point for test isolation

def d(offset: int) -> date:
    """Return TODAY + offset days. Negative = past, positive = future."""
    return TODAY + timedelta(days=offset)

def mk_el(cosh_ref: str) -> PracticeElement:
    return PracticeElement(element_type="COMMON_NAME", cosh_ref=cosh_ref, value=None, unit_cosh_id=None)

def input_practice(id: str, cosh_ref: str = "pesticide_x", special: bool = False,
                   relation_id: str = None) -> PracticeStub:
    return PracticeStub(
        id=id, l0_type="INPUT", l1_type="pesticide", l2_type="chemical_pesticide",
        display_order=0, is_special_input=special, relation_id=relation_id,
        elements=[mk_el(cosh_ref)],
    )

def instruction_practice(id: str) -> PracticeStub:
    return PracticeStub(
        id=id, l0_type="INSTRUCTION", l1_type=None, l2_type=None,
        display_order=0, is_special_input=False, relation_id=None, elements=[],
    )

def tl(id: str, from_d: date, to_d: date, practices: list, created: date = None) -> TimelineWindow:
    return TimelineWindow(
        id=id, name=id, from_date=from_d, to_date=to_d,
        created_at=created or from_d, practices=practices,
    )

def dedup(*args, **kwargs):
    """Wrapper that injects TODAY so tests are date-independent."""
    kwargs.setdefault("today", TODAY)
    return deduplicate_advisory(*args, **kwargs)


# ── TC-BL03-01: Same input in two directly overlapping timelines ──────────────

def test_bl03_01_same_input_overlapping_timelines():
    """TL_A starts 10 days ago (active). TL_B starts 5 days ago (overlapping). TL_A governs."""
    p_a = input_practice("pA1", "pesticide_x")
    p_b = input_practice("pB1", "pesticide_x")

    # TL_A is still active (to_date = today + 5) — both timelines overlap
    tl_a = tl("TL_A", d(-10), d(5),  [p_a], d(-12))  # earlier created_at
    tl_b = tl("TL_B", d(-5),  d(20), [p_b], d(-4))

    result = dedup([tl_a, tl_b], approved_practice_ids=set())

    r_a = next(r for r in result if r.timeline.id == "TL_A")
    r_b = next(r for r in result if r.timeline.id == "TL_B")

    assert len(r_a.visible_practices) == 1
    assert len(r_b.visible_practices) == 0   # suppressed by TL_A
    assert r_b.suppressed[0].governing_timeline_id == "TL_A"


# ── TC-BL03-02: Same input in two NON-overlapping timelines ───────────────────

def test_bl03_02_same_input_non_overlapping_timelines():
    """TL_A ends before TL_B starts. Both contain X. No suppression."""
    p_a = input_practice("pA1", "pesticide_x")
    p_b = input_practice("pB1", "pesticide_x")

    tl_a = tl("TL_A", d(-20), d(-10), [p_a])
    tl_b = tl("TL_B", d(5),   d(20),  [p_b])

    result = dedup([tl_a, tl_b], approved_practice_ids=set())

    for r in result:
        assert len(r.visible_practices) == 1
        assert len(r.suppressed) == 0


# ── TC-BL03-03: Special input (adjuvant) never suppressed ─────────────────────

def test_bl03_03_special_input_never_suppressed():
    """TL_A and TL_B overlap. Both contain adjuvant Z (is_special_input=True). Neither suppressed."""
    adj_a = input_practice("adjA", "adjuvant_z", special=True)
    adj_b = input_practice("adjB", "adjuvant_z", special=True)

    tl_a = tl("TL_A", d(-5), d(10), [adj_a])
    tl_b = tl("TL_B", d(0),  d(20), [adj_b])

    result = dedup([tl_a, tl_b], approved_practice_ids=set())

    for r in result:
        assert len(r.visible_practices) == 1  # Both appear


# ── TC-BL03-04: Chain suppression NOT applied ─────────────────────────────────

def test_bl03_04_chain_suppression_not_applied():
    """TL_A overlaps TL_B. TL_B overlaps TL_C. TL_A and TL_C do NOT overlap.
    TL_A suppresses TL_B. TL_C NOT suppressed (no direct overlap with TL_A)."""
    p_a = input_practice("pA", "pesticide_x")
    p_b = input_practice("pB", "pesticide_x")
    p_c = input_practice("pC", "pesticide_x")

    # TL_A: days -10 to +5. TL_B: days 0 to +15. TL_C: days +10 to +25.
    # TL_A ∩ TL_B = days 0..+5 (overlap). TL_B ∩ TL_C = days +10..+15 (overlap).
    # TL_A ∩ TL_C = no overlap (TL_A ends at +5, TL_C starts at +10).
    tl_a = tl("TL_A", d(-10), d(5),  [p_a], d(-12))  # earliest created_at
    tl_b = tl("TL_B", d(0),   d(15), [p_b], d(-5))
    tl_c = tl("TL_C", d(10),  d(25), [p_c], d(-3))

    result = dedup([tl_a, tl_b, tl_c], approved_practice_ids=set())

    r_a = next(r for r in result if r.timeline.id == "TL_A")
    r_b = next(r for r in result if r.timeline.id == "TL_B")
    r_c = next(r for r in result if r.timeline.id == "TL_C")

    assert len(r_a.visible_practices) == 1   # governs
    assert len(r_b.visible_practices) == 0   # suppressed by TL_A
    assert len(r_c.visible_practices) == 1   # NOT suppressed (TL_A and TL_C don't overlap)


# ── TC-BL03-05: Purchased input suppression survives timeline closure ─────────

def test_bl03_05_purchased_suppression_survives_tl_closure():
    """Input X approved in TL_A (now closed). TL_B still active. Still suppressed."""
    p_a = input_practice("pA", "pesticide_x")
    p_b = input_practice("pB", "pesticide_x")

    # TL_A is CLOSED (ended yesterday). TL_B is still active. They overlapped.
    tl_a = tl("TL_A", d(-20), d(-1),  [p_a], d(-22))
    tl_b = tl("TL_B", d(-10), d(10),  [p_b], d(-12))

    result = dedup([tl_a, tl_b], approved_practice_ids={"pA"})  # pA was purchased

    r_b = next(r for r in result if r.timeline.id == "TL_B")
    assert len(r_b.visible_practices) == 0
    assert r_b.suppressed[0].reason == "PURCHASED"


# ── TC-BL03-06: Unpurchased closed timeline input REINSTATED ──────────────────

def test_bl03_06_unpurchased_closed_timeline_reinstated():
    """TL_A closed AND not purchased. X reinstated in TL_B."""
    p_a = input_practice("pA", "pesticide_x")
    p_b = input_practice("pB", "pesticide_x")

    tl_a = tl("TL_A", d(-20), d(-1),  [p_a], d(-22))  # CLOSED
    tl_b = tl("TL_B", d(-10), d(10),  [p_b], d(-12))  # active

    result = dedup([tl_a, tl_b], approved_practice_ids=set())  # NOT purchased

    r_b = next(r for r in result if r.timeline.id == "TL_B")
    assert len(r_b.visible_practices) == 1  # reinstated
    assert len(r_b.suppressed) == 0


# ── TC-BL03-07: CHA timeline overlapping CCA — same rules apply ──────────────

def test_bl03_07_cha_cca_same_rules():
    """Earlier start governs regardless of source (CCA or CHA)."""
    p_a = input_practice("pA", "pesticide_y")
    p_b = input_practice("pB", "pesticide_y")

    tl_cca = tl("TL_CCA", d(-10), d(5),  [p_a], d(-12))  # earlier start
    tl_cca.source = "CCA"
    tl_cha = tl("TL_CHA", d(-5),  d(15), [p_b], d(-7))
    tl_cha.source = "CHA"

    result = dedup([tl_cca, tl_cha], approved_practice_ids=set())

    r_cca = next(r for r in result if r.timeline.id == "TL_CCA")
    r_cha = next(r for r in result if r.timeline.id == "TL_CHA")

    assert len(r_cca.visible_practices) == 1   # earlier → governs
    assert len(r_cha.visible_practices) == 0   # suppressed


# ── TC-BL03-08: Surviving TL_B practices not merged into TL_A ────────────────

def test_bl03_08_surviving_practices_not_merged():
    """TL_B has input C (different from TL_A's A and B). C survives in TL_B as separate group."""
    p_a = input_practice("pA", "pesticide_a", relation_id="rel1")
    p_b = input_practice("pB", "pesticide_b", relation_id="rel1")
    p_c = input_practice("pC", "pesticide_c")   # different → NOT suppressed

    tl_a = tl("TL_A", d(-10), d(10), [p_a, p_b])
    tl_b = tl("TL_B", d(-5),  d(20), [p_c])

    result = dedup([tl_a, tl_b], approved_practice_ids=set())

    r_a = next(r for r in result if r.timeline.id == "TL_A")
    r_b = next(r for r in result if r.timeline.id == "TL_B")

    assert len(r_a.visible_practices) == 2
    assert len(r_b.visible_practices) == 1
    assert r_b.visible_practices[0].id == "pC"


# ── TC-BL03-09: All TL_B practices suppressed — relation removed ──────────────

def test_bl03_09_all_tl_b_practices_suppressed_relation_removed():
    """TL_B's only practice (in a relation) is suppressed. Relation removed from view."""
    p_a = input_practice("pA", "pesticide_x")
    p_b = input_practice("pB", "pesticide_x", relation_id="rel_tl_b")

    tl_a = tl("TL_A", d(-10), d(10), [p_a], d(-12))  # earlier → governs
    tl_b = tl("TL_B", d(-5),  d(20), [p_b], d(-7))

    result = dedup([tl_a, tl_b], approved_practice_ids=set())

    r_b = next(r for r in result if r.timeline.id == "TL_B")
    assert len(r_b.visible_practices) == 0  # relation entirely gone


# ── TC-BL03-10: Equal start dates — tie-breaking by created_at ───────────────

def test_bl03_10_equal_start_dates_tie_breaking():
    """Same from_date. Earlier created_at governs. Result is deterministic regardless of input order."""
    p_a = input_practice("pA", "pesticide_x")
    p_b = input_practice("pB", "pesticide_x")

    same_from = d(5)   # both start 5 days from now
    tl_a = tl("TL_A", same_from, d(20), [p_a], d(-10))   # older created_at → governs
    tl_b = tl("TL_B", same_from, d(25), [p_b], d(-5))    # newer created_at → suppressed

    r1 = dedup([tl_a, tl_b], approved_practice_ids=set())
    r2 = dedup([tl_b, tl_a], approved_practice_ids=set())  # reversed order

    def suppressed_count(results, tl_id):
        return len(next(r for r in results if r.timeline.id == tl_id).suppressed)

    assert suppressed_count(r1, "TL_A") == 0
    assert suppressed_count(r1, "TL_B") == 1
    assert suppressed_count(r2, "TL_A") == 0
    assert suppressed_count(r2, "TL_B") == 1


# ── TC-BL03-EXTRA: Non-INPUT practices never deduplicated ────────────────────

def test_bl03_extra_non_input_practices_never_suppressed():
    """INSTRUCTION, NON_INPUT, MEDIA always appear even if content is identical."""
    inst_a = instruction_practice("instA")
    inst_b = instruction_practice("instB")

    tl_a = tl("TL_A", d(-5), d(10), [inst_a])
    tl_b = tl("TL_B", d(0),  d(20), [inst_b])

    result = dedup([tl_a, tl_b], approved_practice_ids=set())

    for r in result:
        assert len(r.visible_practices) == 1
        assert len(r.suppressed) == 0
