"""
BL-02 — Conditional Question Pre-Advisory Flow
Pure function service. No database access.
Spec: RootsTalk_Dev_BusinessLogic.pdf §BL-02

Rules summary:
- Conditional questions are asked BEFORE practices are shown for that timeline.
- Each question has YES/NO options. Answers are stored per (subscription, question, date).
- Practices are filtered: include if (a) no conditional, (b) answer matches conditional,
  or (c) conditional.answer = BOTH.
- BLANK path: farmer's answer leads to no linked practices → warm message, try again tomorrow.
- BLANK path does NOT block non-conditional practices in the same timeline.
"""
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ConditionalQuestion:
    id: str
    question_text: str
    display_order: int


@dataclass
class PracticeConditionalLink:
    """One row from practice_conditionals: links a practice to a question + expected answer."""
    practice_id: str
    question_id: str
    required_answer: str    # "YES" | "NO" | "BOTH"


@dataclass
class ConditionalFilterResult:
    """Result of filtering one timeline's practices by conditional answers."""
    visible_practices: list[str]          # practice IDs to show
    pending_question: Optional[ConditionalQuestion]   # next unanswered question, or None
    blank_path_questions: list[str]       # question IDs where farmer got BLANK warm message
    all_questions_answered: bool          # True when no more questions need answers today


def filter_practices_by_conditionals(
    all_practice_ids: list[str],                     # all practices in this timeline
    questions: list[ConditionalQuestion],             # conditional questions (sorted by display_order)
    practice_links: list[PracticeConditionalLink],    # all practice_conditional rows for this timeline
    today_answers: dict[str, str],                    # {question_id: "YES"|"NO"} — today's stored answers
) -> ConditionalFilterResult:
    """
    BL-02 core: filter practices and determine if questions still need answering.

    Steps:
    1. If no questions → show all practices, no pending question.
    2. For each question in display_order:
       a. If already answered today → use stored answer.
       b. If not answered → return this as pending_question (stop here).
    3. All answered → build practice filter:
       - Practice with no conditional link: always visible
       - Practice linked to a question: visible only if stored_answer matches required_answer (or BOTH)
       - BLANK path: if all conditional links for a question have required_answer != stored_answer
         → that question's conditional practices are hidden, but non-conditional practices still show
    """
    if not questions:
        return ConditionalFilterResult(
            visible_practices=all_practice_ids,
            pending_question=None,
            blank_path_questions=[],
            all_questions_answered=True,
        )

    # Step 2: Find first unanswered question
    for question in sorted(questions, key=lambda q: q.display_order):
        if question.id not in today_answers:
            return ConditionalFilterResult(
                visible_practices=[],
                pending_question=question,
                blank_path_questions=[],
                all_questions_answered=False,
            )

    # Step 3: All answered — build the practice filter
    # Build a map: practice_id → list of (question_id, required_answer)
    practice_to_links: dict[str, list[PracticeConditionalLink]] = {}
    for link in practice_links:
        practice_to_links.setdefault(link.practice_id, []).append(link)

    visible: list[str] = []
    blank_path_questions: list[str] = []

    # Detect BLANK path questions (stored answer matches no practice's required_answer)
    question_has_matching_practice: dict[str, bool] = {q.id: False for q in questions}
    for pid in all_practice_ids:
        for link in practice_to_links.get(pid, []):
            stored = today_answers.get(link.question_id, "")
            if link.required_answer == "BOTH" or stored == link.required_answer:
                question_has_matching_practice[link.question_id] = True

    for q in questions:
        if not question_has_matching_practice[q.id]:
            blank_path_questions.append(q.id)

    for pid in all_practice_ids:
        links = practice_to_links.get(pid, [])

        if not links:
            # No conditional — always show
            visible.append(pid)
            continue

        # Practice has conditionals: show if any link matches the farmer's answer
        should_show = False
        for link in links:
            if link.question_id in blank_path_questions:
                # BLANK path for this question — skip this link but don't block non-conditional practices
                continue
            stored = today_answers.get(link.question_id, "")
            if link.required_answer == "BOTH":
                should_show = True
                break
            if stored == link.required_answer:
                should_show = True
                break

        if should_show:
            visible.append(pid)

    return ConditionalFilterResult(
        visible_practices=visible,
        pending_question=None,
        blank_path_questions=blank_path_questions,
        all_questions_answered=True,
    )
