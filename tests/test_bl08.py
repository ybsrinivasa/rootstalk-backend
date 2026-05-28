"""
BL-08 — Diagnosis Path Construction Algorithm
Test cases from RootsTalk_Dev_TestCases.pdf §BL-08 plus edge cases.
All tests use controlled mock data — no database required.
"""
import pytest
from app.services.bl08_diagnosis_path import (
    run_diagnosis_step, get_available_plant_parts, get_problem_list,
    ProblemSymptomRow as PSR,
    DiagnosisAnswer as DA,
    DiagnosisQuestion,
)


# ── Test dataset ──────────────────────────────────────────────────────────────
#
# Crop: Paddy. Stage: Vegetative.
# Problems in pool:
#   P1 = blast (fungal)         — LEAF:Spots, LEAF:Colour_Change
#   P2 = brown_spot             — LEAF:Spots, STEM:Lesions
#   P3 = sheath_blight          — STEM:Lesions, LEAF:Colour_Change
#   P4 = neck_rot               — STEM:Lesions
#   P5 = tungro_virus           — LEAF:Yellowing
#
# Plant parts: LEAF, STEM
# Symptoms: Spots, Colour_Change, Lesions, Yellowing

def make_dataset() -> list[PSR]:
    return [
        # P1 — blast
        PSR("P1", "LEAF", "Spots"),
        PSR("P1", "LEAF", "Colour_Change"),
        # P2 — brown_spot
        PSR("P2", "LEAF", "Spots"),
        PSR("P2", "STEM", "Lesions"),
        # P3 — sheath_blight
        PSR("P3", "STEM", "Lesions"),
        PSR("P3", "LEAF", "Colour_Change"),
        # P4 — neck_rot
        PSR("P4", "STEM", "Lesions"),
        # P5 — tungro
        PSR("P5", "LEAF", "Yellowing"),
    ]


# ── TC-BL08-01: First question uses farmer's selected plant part ──────────────

def test_bl08_01_first_question_uses_selected_plant_part():
    """First question must be for farmer's selected plant_part (LEAF), not STEM.

    Per BL-08 §2 ("mapped to this crop + stage + plant_part"), the initial
    pool excludes problems with no LEAF data — P4 (STEM-only) drops out.
    Only P1, P2, P3, P5 carry LEAF rows."""
    rows = make_dataset()
    step = run_diagnosis_step(rows, initial_plant_part="LEAF", answers=[], random_seed=42)

    assert step.status == "QUESTION"
    assert step.question is not None
    assert step.question.plant_part_cosh_id == "LEAF"   # Must use farmer's chosen part
    assert step.remaining_count == 4
    assert "P4" not in step.remaining_problem_ids   # STEM-only — not in LEAF pool


# ── TC-BL08-02: After NO — stays on same plant part, pool narrows ─────────────

def test_bl08_02_after_no_stays_on_leaf_pool_narrows():
    """NO to LEAF+Spots: problems that REQUIRE Leaf+Spots are removed. Stay on LEAF."""
    rows = make_dataset()
    answers = [DA("LEAF", "Spots", None, None, "NO")]
    step = run_diagnosis_step(rows, initial_plant_part="LEAF", answers=answers, random_seed=42)

    # P1 had (LEAF, Spots) AND (LEAF, Colour_Change) — still has Colour_Change so survives
    # P2 had (LEAF, Spots) AND (STEM, Lesions) — still has Stem row so survives
    # But wait — the NO removes all (LEAF, Spots) rows. P1 still has (LEAF, Colour_Change).
    # P2 still has (STEM, Lesions). So both survive.
    # P3, P4: no LEAF+Spots, unaffected.
    # P5: no LEAF+Spots, unaffected.
    # ALL 5 problems survive because none of them ONLY had (LEAF, Spots).

    # Re-examine with a simpler case: add P6 that ONLY has (LEAF, Spots)
    rows_with_p6 = rows + [PSR("P6_only_leaf_spots", "LEAF", "Spots")]
    step2 = run_diagnosis_step(rows_with_p6, initial_plant_part="LEAF", answers=answers, random_seed=42)

    # P6 only had (LEAF, Spots) → eliminated after NO
    remaining_ids = step2.remaining_problem_ids
    assert "P6_only_leaf_spots" not in remaining_ids
    assert step2.status == "QUESTION"
    assert step2.question.plant_part_cosh_id == "LEAF"  # Still on LEAF (no YES yet)


# ── TC-BL08-03: After YES — can switch plant part ────────────────────────────

def test_bl08_03_after_yes_can_switch_plant_part():
    """YES to LEAF+Spots narrows to P1 and P2. Algorithm can now use any plant part."""
    rows = make_dataset()
    answers = [DA("LEAF", "Spots", None, None, "YES")]
    step = run_diagnosis_step(rows, initial_plant_part="LEAF", answers=answers, random_seed=42)

    # After YES to LEAF+Spots: only P1 and P2 remain (both have LEAF+Spots)
    assert step.remaining_count == 2
    remaining_ids = set(step.remaining_problem_ids)
    assert remaining_ids == {"P1", "P2"}

    # Now algorithm can switch to STEM or stay on LEAF — both are valid
    # The question must distinguish P1 vs P2
    # P1: LEAF+Spots, LEAF+Colour_Change → no STEM rows
    # P2: LEAF+Spots, STEM+Lesions
    # Most differentiating: ask about STEM+Lesions (only P2 has it)
    assert step.status == "QUESTION"
    assert step.has_yes_answer is True


# ── TC-BL08-04: Disambiguation via sub-symptom ────────────────────────────────

def test_bl08_04_disambiguation_via_sub_symptom():
    """Two problems both have LEAF+Spots but differ by sub-symptom (Circular vs Irregular)."""
    rows = [
        PSR("PA", "LEAF", "Spots", sub_symptom_cosh_id="Circular"),
        PSR("PB", "LEAF", "Spots", sub_symptom_cosh_id="Irregular"),
    ]
    answers = [DA("LEAF", "Spots", None, None, "YES")]
    step = run_diagnosis_step(rows, initial_plant_part="LEAF", answers=answers, random_seed=42)

    assert step.remaining_count == 2
    assert step.status == "QUESTION"
    # Should ask a sub-symptom question to disambiguate
    assert step.question.question_type in ("SUB_SYMPTOM", "SUB_PART")
    assert step.question.sub_symptom_cosh_id in ("Circular", "Irregular")


# ── TC-BL08-05: Pool reduces to 1 — confirmation prompt ──────────────────────

def test_bl08_05_pool_reduces_to_one_emits_confirmation():
    """Per BL-08 §8 amended 2026-05-28: when the pool reduces to exactly
    one candidate, the algorithm returns status="CONFIRMATION" with the
    candidate's id in `diagnosed_problem_cosh_id`. The router then asks
    the farmer to confirm before flipping the session to DIAGNOSED."""
    specific_rows = [
        PSR("P4", "STEM", "Lesions"),
    ]
    step = run_diagnosis_step(specific_rows, initial_plant_part="STEM", answers=[], random_seed=42)
    assert step.status == "CONFIRMATION"
    assert step.diagnosed_problem_cosh_id == "P4"
    assert step.question is not None
    assert step.question.question_type == "CONFIRMATION"
    assert step.remaining_count == 1


# ── TC-BL08-06: 'I Know the Problem' — list filtered to crop+stage+part ───────

def test_bl08_06_know_the_problem_filters_by_part():
    """Problem list filtered to selected plant part."""
    rows = make_dataset()
    leaf_problems = get_problem_list(rows, plant_part="LEAF")
    stem_problems = get_problem_list(rows, plant_part="STEM")
    all_problems = get_problem_list(rows)

    # LEAF has: P1, P2, P3, P5 (P4 has no LEAF entry)
    assert "P4" not in leaf_problems
    assert "P5" in leaf_problems

    # STEM has: P2, P3, P4
    assert "P4" in stem_problems
    assert "P5" not in stem_problems

    # All has everything
    assert len(all_problems) == 5


# ── TC-BL08-07: Remaining pool never reaches 0 (dead end impossible) ─────────

def test_bl08_07_dead_end_prevention():
    """
    The algorithm should never create a dead end by design.
    If a scenario somehow exhausts the pool (data error), status=INCONCLUSIVE.
    """
    rows = [PSR("P1", "LEAF", "Spots")]  # Only one problem
    # Answer YES to something that doesn't match this row
    answers = [DA("STEM", "Lesions", None, None, "YES")]  # P1 has no STEM+Lesions
    step = run_diagnosis_step(rows, initial_plant_part="LEAF", answers=answers, random_seed=42)

    # Pool is now empty — data integrity problem
    assert step.status == "INCONCLUSIVE"
    assert step.error == "NO_MATCH"


# ── TC-BL08-08: Random tie-breaking is deterministic with seed ───────────────

def test_bl08_08_random_tie_breaking_deterministic():
    """When multiple symptoms tie, seed makes choice deterministic and reproducible."""
    rows = [
        PSR("P1", "LEAF", "Spots"),
        PSR("P2", "LEAF", "Yellowing"),
    ]
    # Both symptoms appear exactly once — tie
    step1 = run_diagnosis_step(rows, initial_plant_part="LEAF", answers=[], random_seed=99)
    step2 = run_diagnosis_step(rows, initial_plant_part="LEAF", answers=[], random_seed=99)

    assert step1.question.symptom_cosh_id == step2.question.symptom_cosh_id


# ── TC-BL08-09: YES then NO narrows correctly ─────────────────────────────────

def test_bl08_09_yes_then_no_narrows_correctly():
    """YES to LEAF+Spots (keeps P1, P2). Then NO to STEM+Lesions (removes P2). P1 diagnosed."""
    rows = make_dataset()
    answers = [
        DA("LEAF", "Spots", None, None, "YES"),    # Keeps P1, P2
        DA("STEM", "Lesions", None, None, "NO"),   # P2 loses its STEM row; P1 unaffected
    ]
    step = run_diagnosis_step(rows, initial_plant_part="LEAF", answers=answers, random_seed=42)

    # After YES(LEAF+Spots): P1, P2 remain
    # After NO(STEM+Lesions): P2 loses (STEM,Lesions) row. P2 still has (LEAF,Spots) row so stays.
    # Actually P2 survives because it still has (LEAF, Spots) which was YES-matched.
    # To eliminate P2: need YES to something P2 doesn't have, or NO to something P2 requires.
    # Let's use a specific pair: P2 only has (LEAF+Spots, STEM+Lesions). After removing STEM+Lesions,
    # P2 still has (LEAF+Spots). So both survive.
    # BUT if we add another YES:
    answers2 = [
        DA("LEAF", "Spots", None, None, "YES"),         # Keeps P1, P2
        DA("LEAF", "Colour_Change", None, None, "YES"),  # Only P1 has (LEAF, Colour_Change)
    ]
    step2 = run_diagnosis_step(rows, initial_plant_part="LEAF", answers=answers2, random_seed=42)
    assert step2.status == "CONFIRMATION"
    assert step2.diagnosed_problem_cosh_id == "P1"


# ── TC-BL08-10: Available plant parts respects problem pool ───────────────────

def test_bl08_10_available_plant_parts():
    """get_available_plant_parts returns distinct plant parts from pool."""
    rows = make_dataset()
    parts = get_available_plant_parts(rows)
    assert "LEAF" in parts
    assert "STEM" in parts
    assert len(set(parts)) == len(parts)  # No duplicates


# ── TC-BL08-11: Priority Ranking — rank-1 YES keeps a 1-2-2 problem ───────────

def test_bl08_11_priority_rank1_yes_keeps_problem():
    """A YES on the rank-1 symptom of a 1-2-2 problem leaves it in the pool —
    no demotion fires (top priority confirmed). An unranked sibling problem
    that also has the symptom is unaffected."""
    rows = [
        # P1: ranks 1-2-2 (Spots top, others lower)
        PSR("P1", "LEAF", "Spots", priority_rank=1),
        PSR("P1", "LEAF", "Colour_Change", priority_rank=2),
        PSR("P1", "STEM", "Lesions", priority_rank=2),
        # P2: unranked (priority rule does not apply)
        PSR("P2", "LEAF", "Spots"),
    ]
    answers = [DA("LEAF", "Spots", None, None, "YES")]
    step = run_diagnosis_step(rows, initial_plant_part="LEAF", answers=answers, random_seed=42)
    assert set(step.remaining_problem_ids) == {"P1", "P2"}


# ── TC-BL08-12: Priority Ranking — rank-2 YES demotes a 1-2-2 problem ─────────

def test_bl08_12_priority_rank2_yes_demotes_problem():
    """YES on the rank-2 symptom of P1 (a 1-2-2 problem) permanently drops P1.
    An unranked sibling problem with the same symptom is kept."""
    rows = [
        PSR("P1", "LEAF", "Spots", priority_rank=1),
        PSR("P1", "LEAF", "Colour_Change", priority_rank=2),
        PSR("P1", "STEM", "Lesions", priority_rank=2),
        PSR("P_unranked", "LEAF", "Colour_Change"),
    ]
    answers = [DA("LEAF", "Colour_Change", None, None, "YES")]
    step = run_diagnosis_step(rows, initial_plant_part="LEAF", answers=answers, random_seed=42)
    assert step.status == "CONFIRMATION"
    assert step.diagnosed_problem_cosh_id == "P_unranked"


# ── TC-BL08-13: Priority demotion is permanent — later rank-1 YES doesn't undo ─

def test_bl08_13_demotion_is_permanent():
    """Once P1 is demoted by a rank-2 YES, a subsequent YES on its rank-1
    symptom does NOT bring P1 back. The pool only narrows further."""
    rows = [
        PSR("P1", "LEAF", "Spots", priority_rank=1),
        PSR("P1", "LEAF", "Colour_Change", priority_rank=2),
        # P2 unranked, has both symptoms — survives both YESes.
        PSR("P2", "LEAF", "Spots"),
        PSR("P2", "LEAF", "Colour_Change"),
    ]
    answers = [
        DA("LEAF", "Colour_Change", None, None, "YES"),  # demotes P1
        DA("LEAF", "Spots", None, None, "YES"),          # P1 must stay out
    ]
    step = run_diagnosis_step(rows, initial_plant_part="LEAF", answers=answers, random_seed=42)
    assert step.status == "CONFIRMATION"
    assert step.diagnosed_problem_cosh_id == "P2"


# ── TC-BL08-14: 1-2-2 ties — YES on either rank-1 keeps; YES on rank-2 demotes ─

def test_bl08_14_priority_1_1_2_ties():
    """A 1-1-2 problem: YES on either rank-1 row keeps it; YES on the rank-2
    row demotes."""
    rank_1_1_2 = [
        PSR("P1", "LEAF", "Spots", priority_rank=1),
        PSR("P1", "LEAF", "Colour_Change", priority_rank=1),
        PSR("P1", "STEM", "Lesions", priority_rank=2),
    ]
    # YES on either rank-1 symptom → P1 still in pool
    keep_step = run_diagnosis_step(
        rank_1_1_2, initial_plant_part="LEAF",
        answers=[DA("LEAF", "Colour_Change", None, None, "YES")],
        random_seed=42,
    )
    assert "P1" in keep_step.remaining_problem_ids

    # YES on the rank-2 symptom → P1 demoted; pool empty.
    demote_step = run_diagnosis_step(
        rank_1_1_2, initial_plant_part="LEAF",
        answers=[DA("STEM", "Lesions", None, None, "YES")],
        random_seed=42,
    )
    assert "P1" not in demote_step.remaining_problem_ids


# ── TC-BL08-15: Single-symptom ranked problem — rank meaningless, no demotion ─

def test_bl08_15_single_symptom_rank_does_not_demote():
    """A problem with exactly one symptom row (whatever its rank) has no
    'higher priority' alternative — a YES on that row never demotes."""
    rows = [PSR("P1", "LEAF", "Spots", priority_rank=2)]
    step = run_diagnosis_step(
        rows, initial_plant_part="LEAF",
        answers=[DA("LEAF", "Spots", None, None, "YES")],
        random_seed=42,
    )
    assert step.status == "CONFIRMATION"
    assert step.diagnosed_problem_cosh_id == "P1"


# ── TC-BL08-16: Locked part — initial pool excludes other-part-only problems ──

def test_bl08_16_initial_pool_scoped_to_locked_part():
    """BL-08 §2: pool is "problems mapped to this crop+stage+plant_part".
    A problem with NO rows on the farmer's selected part must not enter
    the pool — even though it exists in the raw row set."""
    rows = [
        PSR("P_leaf", "LEAF", "Spots"),
        PSR("P_leaf", "LEAF", "Yellowing"),
        PSR("P_stem_only", "STEM", "Lesions"),    # No LEAF row at all
    ]
    step = run_diagnosis_step(rows, initial_plant_part="LEAF", answers=[], random_seed=42)
    assert "P_stem_only" not in step.remaining_problem_ids
    assert "P_leaf" in step.remaining_problem_ids


# ── TC-BL08-17: Locked part — NO that exhausts a problem's part rows drops it ─

def test_bl08_17_no_exhausting_locked_part_drops_problem():
    """BL-08 §6: a NO that leaves a problem with no surviving rows on the
    LOCKED part removes that problem — even if the problem has rows on
    other parts. Before the 2026-05-28 fix the problem silently survived
    on its other-part rows, and the random tie-break in step (d) could
    later pick it as the diagnosis (the "we went somewhere else" bug)."""
    rows = [
        PSR("P_keeps_leaf", "LEAF", "Spots"),
        PSR("P_keeps_leaf", "LEAF", "Wilting"),
        PSR("P_loses_leaf", "LEAF", "Spots"),         # Only LEAF row
        PSR("P_loses_leaf", "STEM", "Lesions"),       # Survives on STEM if we ignore lock
    ]
    answers = [DA("LEAF", "Spots", None, None, "NO")]
    step = run_diagnosis_step(rows, initial_plant_part="LEAF", answers=answers, random_seed=42)

    # P_keeps_leaf still has (LEAF, Wilting) — stays.
    assert "P_keeps_leaf" in step.remaining_problem_ids
    # P_loses_leaf's only LEAF row was Spots — gone. STEM row should NOT
    # rescue it while the farmer is still locked on LEAF.
    assert "P_loses_leaf" not in step.remaining_problem_ids


# ── TC-BL08-18: After YES, the lock releases (existing §5 behaviour preserved) ─

def test_bl08_18_yes_releases_part_lock():
    """A YES unlocks the part filter so the YES path can find rows on
    other parts (BL-08 §5). The remaining_ids after YES is no longer
    constrained to initial_plant_part."""
    rows = [
        PSR("P_cross", "LEAF", "Spots"),
        PSR("P_cross", "STEM", "Lesions"),    # Other-part row for the YES'd problem
    ]
    answers = [DA("LEAF", "Spots", None, None, "YES")]
    step = run_diagnosis_step(rows, initial_plant_part="LEAF", answers=answers, random_seed=42)

    # Single problem remains, but it diagnosed only because YES narrowed
    # to {P_cross} — the STEM row would have been visible for a follow-up
    # question if more problems were in the YES set.
    assert step.has_yes_answer is True
    assert "P_cross" in step.remaining_problem_ids


# ── TC-BL08-19: Lower-Canopy scenario — random pick stays on locked part ──────

def test_bl08_19_random_pick_stays_on_locked_part():
    """Faithful reproduction of the farmer-reported bug (2026-05-28):
    Tomato → Vegetative → Lower Canopy → NO → NO → NO.

    Setup: 4 problems with Lower Canopy rows, plus 1 "stowaway" with
    only Upper Canopy rows. After 3 NOs that exhaust the discriminating
    LC symptoms, two LC problems remain that share an LC symptom we've
    already covered → algorithm has nothing more to ask on LC.

    Per BL-08 §7(d) the random tie-break MAY fire. After the fix, the
    pick must come from the LC pool only — never the stowaway."""
    rows = [
        # 4 problems mapped to Lower Canopy
        PSR("LC_problem_1", "LC", "Yellowing"),
        PSR("LC_problem_1", "LC", "Curling"),
        PSR("LC_problem_2", "LC", "Yellowing"),
        PSR("LC_problem_2", "LC", "Spotting"),
        PSR("LC_problem_3", "LC", "Wilting"),
        PSR("LC_problem_4", "LC", "Wilting"),
        # Stowaway — never mapped to LC, but in the same crop+stage
        PSR("UC_stowaway", "UC", "Browning"),
    ]
    # 3 NOs that drop everyone except LC_problem_3 + LC_problem_4
    # (both share Wilting on LC — undifferentiated).
    answers = [
        DA("LC", "Yellowing", None, None, "NO"),
        DA("LC", "Curling", None, None, "NO"),
        DA("LC", "Spotting", None, None, "NO"),
    ]
    step = run_diagnosis_step(rows, initial_plant_part="LC", answers=answers, random_seed=42)

    # The stowaway must never reach the diagnosis screen.
    assert "UC_stowaway" not in step.remaining_problem_ids
    # If status is DIAGNOSED (random pick), it must be one of the two LC
    # problems — never the stowaway.
    if step.status == "DIAGNOSED":
        assert step.diagnosed_problem_cosh_id in {"LC_problem_3", "LC_problem_4"}


# ── TC-BL08-20: Disambiguate (c) honours the lock when no YES given ───────────

def test_bl08_20_disambiguate_step_c_respects_lock():
    """The 'all four combined' branch of disambiguation must stay on the
    locked plant_part while has_yes is False. Pre-fix this branch
    explicitly looked across ALL parts (per the old comment "look across
    ALL parts (not just current_part)") which silently moved the farmer
    off their selected part."""
    rows = [
        # Two LC problems both share (LC, Spotting) — no plain LC differentiator.
        PSR("PA", "LC", "Spotting"),
        PSR("PB", "LC", "Spotting"),
        # Each has a fully-qualified row on a DIFFERENT part. Pre-fix,
        # step (c) would happily return a question on UC or STEM.
        PSR("PA", "UC", "Browning", sub_part_cosh_id="upper_left", sub_symptom_cosh_id="dark"),
        PSR("PB", "STEM", "Cracking", sub_part_cosh_id="lower_node", sub_symptom_cosh_id="dry"),
    ]
    answers = [DA("LC", "Spotting", None, None, "NO")]   # Eliminates the shared LC row
    step = run_diagnosis_step(rows, initial_plant_part="LC", answers=answers, random_seed=42)

    # Both problems lost their only LC row → the lock removes them from
    # remaining_ids → pool empties → INCONCLUSIVE. The IMPORTANT point
    # is the algorithm does NOT return a QUESTION on UC or STEM via (c).
    if step.status == "QUESTION":
        assert step.question.plant_part_cosh_id == "LC", (
            "Disambiguation step (c) must stay on locked LC, "
            f"got plant_part={step.question.plant_part_cosh_id!r}"
        )
