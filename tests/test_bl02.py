"""
BL-02 — Conditional Question Pre-Advisory Flow
Test cases from RootsTalk_Dev_TestCases.pdf §BL-02.
"""
import pytest
from app.services.bl02_conditional import (
    filter_practices_by_conditionals,
    ConditionalQuestion as Q,
    PracticeConditionalLink as L,
)


def q(id: str, text: str, order: int = 0) -> Q:
    return Q(id=id, question_text=text, display_order=order)

def l(practice_id: str, question_id: str, answer: str) -> L:
    return L(practice_id=practice_id, question_id=question_id, required_answer=answer)


# ── TC-BL02-01: No conditional questions → all practices shown ────────────────

def test_bl02_01_no_conditionals_shows_all():
    """TC-BL02-01: Timeline has practices but no conditional_questions rows."""
    result = filter_practices_by_conditionals(
        all_practice_ids=["P1", "P2", "P3"],
        questions=[],
        practice_links=[],
        today_answers={},
    )
    assert result.visible_practices == ["P1", "P2", "P3"]
    assert result.pending_question is None
    assert result.all_questions_answered is True


# ── TC-BL02-02: Farmer answers YES → Practice A shown, Practice B hidden ──────

def test_bl02_02_yes_answer_shows_linked_practice():
    """TC-BL02-02: Practice A linked to YES. Practice B linked to NO. Farmer answers YES."""
    result = filter_practices_by_conditionals(
        all_practice_ids=["PA", "PB"],
        questions=[q("Q1", "Is there yellowing?", 1)],
        practice_links=[l("PA", "Q1", "YES"), l("PB", "Q1", "NO")],
        today_answers={"Q1": "YES"},
    )
    assert "PA" in result.visible_practices
    assert "PB" not in result.visible_practices
    assert result.all_questions_answered is True


# ── TC-BL02-03: BLANK path → warm message, non-conditional practices still show

def test_bl02_03_blank_path_warm_message_non_conditional_still_show():
    """TC-BL02-03: YES has no linked practices → BLANK path.
    Non-conditional practices in same timeline still shown."""
    # Q1 is linked to PC (YES) and PD (NO), but farmer answers... wait:
    # BLANK occurs when farmer's answer matches no practice's required_answer.
    # If farmer answers YES but no practice has required_answer=YES: BLANK.
    # But actually the setup in the test: we need an answer that leads to no matches.
    # Let's say farmer answers "YES" but practice is linked to "NO" only.
    result = filter_practices_by_conditionals(
        all_practice_ids=["PA_non_cond", "PB_cond_NO"],
        questions=[q("Q1", "Is root discoloured?", 1)],
        practice_links=[l("PB_cond_NO", "Q1", "NO")],
        today_answers={"Q1": "YES"},  # Farmer says YES but practice only linked to NO → BLANK
    )
    # PA has no conditional link → always shows
    assert "PA_non_cond" in result.visible_practices
    # PB is linked to NO but farmer said YES → hidden (BLANK path for that conditional)
    assert "PB_cond_NO" not in result.visible_practices
    # Q1 is in blank_path_questions
    assert "Q1" in result.blank_path_questions
    assert result.all_questions_answered is True


# ── TC-BL02-04: BLANK path question repeats next day ─────────────────────────

def test_bl02_04_blank_path_question_repeats_next_day():
    """TC-BL02-04: Previous day answer was BLANK. Today is a new day → question asked again."""
    # Today's answers dict does NOT contain yesterday's answer (date-keyed)
    result = filter_practices_by_conditionals(
        all_practice_ids=["PA"],
        questions=[q("Q1", "Is there spotting?", 1)],
        practice_links=[l("PA", "Q1", "YES")],
        today_answers={},  # No answer for today → question is pending again
    )
    assert result.pending_question is not None
    assert result.pending_question.id == "Q1"
    assert result.all_questions_answered is False


# ── TC-BL02-05: Multiple conditionals answered in display_order ───────────────

def test_bl02_05_multiple_questions_in_display_order():
    """TC-BL02-05: 3 questions with display_order 1, 2, 3. All must be answered in order."""
    # All three answered
    result = filter_practices_by_conditionals(
        all_practice_ids=["P1", "P2", "P3"],
        questions=[q("Q1", "Q1", 1), q("Q2", "Q2", 2), q("Q3", "Q3", 3)],
        practice_links=[l("P1", "Q1", "YES"), l("P2", "Q2", "NO"), l("P3", "Q3", "YES")],
        today_answers={"Q1": "YES", "Q2": "NO", "Q3": "YES"},
    )
    assert result.all_questions_answered is True
    assert "P1" in result.visible_practices   # Q1=YES matches
    assert "P2" in result.visible_practices   # Q2=NO matches
    assert "P3" in result.visible_practices   # Q3=YES matches

    # Only Q1 answered → Q2 is pending
    result2 = filter_practices_by_conditionals(
        all_practice_ids=["P1", "P2", "P3"],
        questions=[q("Q1", "Q1", 1), q("Q2", "Q2", 2), q("Q3", "Q3", 3)],
        practice_links=[l("P1", "Q1", "YES"), l("P2", "Q2", "NO"), l("P3", "Q3", "YES")],
        today_answers={"Q1": "YES"},  # Only Q1 answered
    )
    assert result2.pending_question.id == "Q2"
    assert result2.all_questions_answered is False


# ── TC-BL02-06: Timeline closes while BLANK path active ──────────────────────

def test_bl02_06_timeline_closed_blank_path_no_error():
    """TC-BL02-06: Timeline ends while question still BLANK.
    Practice simply not applied. No error. (Modelled by empty answers + timeline inactive.)
    BL-02 service itself has no timeline date logic — it just returns pending question.
    The router handles timeline closure via BL-04."""
    # When timeline is no longer active, the router won't call BL-02 for it.
    # From BL-02's perspective: question still pending = practice not shown. That's correct.
    result = filter_practices_by_conditionals(
        all_practice_ids=["PA"],
        questions=[q("Q1", "Is there spotting?", 1)],
        practice_links=[l("PA", "Q1", "YES")],
        today_answers={},  # Unanswered → practice not shown, no error
    )
    assert result.pending_question is not None
    assert len(result.visible_practices) == 0  # Practice withheld — no crash


# ── TC-BL02-07: Practice linked to BOTH always shown ─────────────────────────

def test_bl02_07_both_answer_always_shown():
    """TC-BL02-07: practice_conditionals.answer = BOTH. Appears regardless of farmer's answer."""
    for farmer_answer in ["YES", "NO"]:
        result = filter_practices_by_conditionals(
            all_practice_ids=["PBOTH"],
            questions=[q("Q1", "Any symptoms?", 1)],
            practice_links=[l("PBOTH", "Q1", "BOTH")],
            today_answers={"Q1": farmer_answer},
        )
        assert "PBOTH" in result.visible_practices, f"BOTH practice not shown for answer={farmer_answer}"


# ── TC-BL02-08: First unanswered question blocks subsequent ones ──────────────

def test_bl02_08_first_unanswered_blocks_later():
    """Q1 unanswered → Q2 not yet asked. Returns Q1 as pending."""
    result = filter_practices_by_conditionals(
        all_practice_ids=["P1", "P2"],
        questions=[q("Q1", "Q1", 1), q("Q2", "Q2", 2)],
        practice_links=[l("P1", "Q1", "YES"), l("P2", "Q2", "NO")],
        today_answers={"Q2": "NO"},  # Q2 answered but Q1 is unanswered and comes first
    )
    assert result.pending_question.id == "Q1"   # Q1 must be answered first
    assert result.all_questions_answered is False


# ── TC-BL02-09: No answer for question → practice excluded, not errored ───────

def test_bl02_09_non_conditional_practices_unaffected_by_blank():
    """Non-conditional practices always show even if some conditional questions are BLANK."""
    result = filter_practices_by_conditionals(
        all_practice_ids=["P_cond", "P_free"],
        questions=[q("Q1", "Any spots?", 1)],
        practice_links=[l("P_cond", "Q1", "YES")],  # P_free has no conditional link
        today_answers={"Q1": "NO"},  # Farmer says NO, but P_cond requires YES → BLANK
    )
    assert "P_free" in result.visible_practices   # Free practice always shows
    assert "P_cond" not in result.visible_practices  # Conditional practice hidden
