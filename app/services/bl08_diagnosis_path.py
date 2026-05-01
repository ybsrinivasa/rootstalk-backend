"""
BL-08 — Diagnosis Path Construction Algorithm
Pure function service. No database access.
Spec: RootsTalk_Dev_BusinessLogic.pdf §BL-08, AGR §8

Data source: problem_to_symptom rows from cosh_reference_cache.
Each row: one problem's known manifestation on a specific plant part + symptom combination.
"""
import random
from dataclasses import dataclass, field
from typing import Optional
from collections import Counter


@dataclass
class ProblemSymptomRow:
    """One row from cosh_reference_cache WHERE entity_type='problem_to_symptom'."""
    problem_cosh_id: str
    plant_part_cosh_id: str
    symptom_cosh_id: str
    sub_part_cosh_id: Optional[str] = None
    sub_symptom_cosh_id: Optional[str] = None


@dataclass
class DiagnosisAnswer:
    plant_part_cosh_id: str
    symptom_cosh_id: str
    sub_part_cosh_id: Optional[str]
    sub_symptom_cosh_id: Optional[str]
    answer: str   # "YES" | "NO"


@dataclass
class DiagnosisQuestion:
    plant_part_cosh_id: str
    symptom_cosh_id: str
    sub_part_cosh_id: Optional[str] = None
    sub_symptom_cosh_id: Optional[str] = None
    question_type: str = "SYMPTOM"  # "SYMPTOM" | "SUB_SYMPTOM" | "SUB_PART"


@dataclass
class DiagnosisStep:
    status: str                            # "QUESTION" | "DIAGNOSED" | "INCONCLUSIVE"
    question: Optional[DiagnosisQuestion] = None
    diagnosed_problem_cosh_id: Optional[str] = None
    remaining_count: int = 0
    remaining_problem_ids: list[str] = field(default_factory=list)
    has_yes_answer: bool = False
    error: Optional[str] = None           # "NO_MATCH" | "DATA_ERROR"


# ── Core algorithm ────────────────────────────────────────────────────────────

def run_diagnosis_step(
    all_rows: list[ProblemSymptomRow],        # ALL problem_to_symptom rows for this crop+stage
    initial_plant_part: str,                  # Farmer's selected plant part (locked until first YES)
    answers: list[DiagnosisAnswer],           # All answers so far
    random_seed: Optional[int] = None,        # For deterministic testing
) -> DiagnosisStep:
    """
    BL-08 core: Given the current problem pool (after applying all answers),
    returns the next question or the final diagnosis.

    Pool management rules:
    - YES answer: keep only rows where problem HAS this (part, symptom, [sub_part, sub_symptom])
    - NO answer: remove all rows matching this (part, symptom, [sub_part, sub_symptom]);
      problems with zero remaining rows are eliminated
    """
    if random_seed is not None:
        random.seed(random_seed)

    # Step 1: Start with all rows, apply answers
    active_rows = _apply_answers(all_rows, answers)
    has_yes = any(a.answer == "YES" for a in answers)

    # Remaining problem pool = unique problem IDs still in active_rows
    remaining_ids = list(dict.fromkeys(r.problem_cosh_id for r in active_rows))

    if len(remaining_ids) == 0:
        # This should not happen by BL-08 design (dead ends impossible)
        return DiagnosisStep(
            status="INCONCLUSIVE",
            remaining_count=0,
            has_yes_answer=has_yes,
            error="NO_MATCH",
        )

    if len(remaining_ids) == 1:
        return DiagnosisStep(
            status="DIAGNOSED",
            diagnosed_problem_cosh_id=remaining_ids[0],
            remaining_count=1,
            remaining_problem_ids=remaining_ids,
            has_yes_answer=has_yes,
        )

    # Step 2: Determine current plant part
    if not has_yes:
        # Locked to farmer's initial plant part until first YES
        current_part = initial_plant_part
    else:
        # Free to use any part — find most frequent among remaining problems
        current_part = _most_frequent_plant_part(active_rows, remaining_ids)

    # Step 3: Find unanswered symptom for current part
    answered_combos = {
        (a.plant_part_cosh_id, a.symptom_cosh_id, a.sub_part_cosh_id, a.sub_symptom_cosh_id)
        for a in answers
    }

    # First try: plain symptom question (no sub-part, no sub-symptom) for current part
    question = _find_next_plain_symptom(active_rows, remaining_ids, current_part, answered_combos)

    if question:
        return DiagnosisStep(
            status="QUESTION",
            question=question,
            remaining_count=len(remaining_ids),
            remaining_problem_ids=remaining_ids,
            has_yes_answer=has_yes,
        )

    # No plain symptom distinguishes further — try disambiguation
    disambiguation = _disambiguate(active_rows, remaining_ids, current_part, answered_combos)
    if disambiguation:
        return DiagnosisStep(
            status="QUESTION",
            question=disambiguation,
            remaining_count=len(remaining_ids),
            remaining_problem_ids=remaining_ids,
            has_yes_answer=has_yes,
        )

    # (d) Still undifferentiated: random selection from remaining pool
    winner = random.choice(remaining_ids)
    return DiagnosisStep(
        status="DIAGNOSED",
        diagnosed_problem_cosh_id=winner,
        remaining_count=len(remaining_ids),
        remaining_problem_ids=remaining_ids,
        has_yes_answer=has_yes,
    )


# ── Answer application ────────────────────────────────────────────────────────

def _apply_answers(rows: list[ProblemSymptomRow], answers: list[DiagnosisAnswer]) -> list[ProblemSymptomRow]:
    """
    Apply all answers to produce the current active row set.

    YES: keep only rows where the problem HAS a row matching (part, symptom, [sub_part], [sub_symptom]).
         After filtering, the remaining active rows for that problem are ALL its rows (not just the matching one).
    NO: remove all rows matching (part, symptom, [sub_part], [sub_symptom]) from the active set.
        Problems with zero remaining rows are effectively eliminated.
    """
    active = list(rows)

    for answer in answers:
        if answer.answer == "YES":
            # Find which problems HAVE this combination
            matching_problems = {
                r.problem_cosh_id for r in active
                if _row_matches(r, answer)
            }
            # Keep only rows for problems that have the matching combination
            active = [r for r in active if r.problem_cosh_id in matching_problems]

        elif answer.answer == "NO":
            # Remove all rows that match this combination
            # Problems with no remaining rows are implicitly eliminated
            active = [r for r in active if not _row_matches(r, answer)]

    return active


def _row_matches(row: ProblemSymptomRow, answer: DiagnosisAnswer) -> bool:
    """Check if a row matches an answer's combination."""
    if row.plant_part_cosh_id != answer.plant_part_cosh_id:
        return False
    if row.symptom_cosh_id != answer.symptom_cosh_id:
        return False
    # Sub-part and sub-symptom must match if specified in the answer
    if answer.sub_part_cosh_id and row.sub_part_cosh_id != answer.sub_part_cosh_id:
        return False
    if answer.sub_symptom_cosh_id and row.sub_symptom_cosh_id != answer.sub_symptom_cosh_id:
        return False
    return True


# ── Plant part selection ──────────────────────────────────────────────────────

def _most_frequent_plant_part(rows: list[ProblemSymptomRow], problem_ids: list[str]) -> str:
    """Find the plant part that appears in the most distinct problems."""
    relevant = [r for r in rows if r.problem_cosh_id in set(problem_ids)]
    counts = Counter()
    for r in relevant:
        counts[r.plant_part_cosh_id] += 1
    if not counts:
        return rows[0].plant_part_cosh_id if rows else ""
    # Return most frequent; ties: deterministic (alphabetical) for testability
    max_count = max(counts.values())
    candidates = sorted(p for p, c in counts.items() if c == max_count)
    return candidates[0]


# ── Next question selection ───────────────────────────────────────────────────

def _find_next_plain_symptom(
    rows: list[ProblemSymptomRow],
    problem_ids: list[str],
    plant_part: str,
    answered: set,
) -> Optional[DiagnosisQuestion]:
    """
    Find the symptom for the current plant_part that:
    1. Appears in the most distinct problems (maximises discrimination)
    2. Has not already been answered
    3. Uses only base (non-sub) combinations first
    """
    relevant = [
        r for r in rows
        if r.problem_cosh_id in set(problem_ids)
        and r.plant_part_cosh_id == plant_part
        and r.sub_part_cosh_id is None
        and r.sub_symptom_cosh_id is None
    ]

    counts = Counter()
    for r in relevant:
        combo = (r.plant_part_cosh_id, r.symptom_cosh_id, None, None)
        if combo not in answered:
            counts[r.symptom_cosh_id] += 1

    if not counts:
        return None

    max_count = max(counts.values())
    candidates = sorted(s for s, c in counts.items() if c == max_count)
    chosen_symptom = candidates[0]  # deterministic: alphabetical on tie

    return DiagnosisQuestion(
        plant_part_cosh_id=plant_part,
        symptom_cosh_id=chosen_symptom,
        question_type="SYMPTOM",
    )


def _disambiguate(
    rows: list[ProblemSymptomRow],
    problem_ids: list[str],
    plant_part: str,
    answered: set,
) -> Optional[DiagnosisQuestion]:
    """
    Disambiguation priority (AGR §8.3, BL-08 step 7):
    (a) Sub-symptom: problems differ by sub_symptom on the same part+symptom
    (b) Sub-part: problems differ by sub_part on the same part
    (c) All four combined (part + sub_part + symptom + sub_symptom)
    Returns a question using the first differentiator found.
    """
    relevant = [r for r in rows if r.problem_cosh_id in set(problem_ids) and r.plant_part_cosh_id == plant_part]

    # (a) Sub-symptom: find a (part, symptom) pair where sub-symptoms differ
    sub_symptom_counts = Counter()
    for r in relevant:
        if r.sub_symptom_cosh_id:
            combo = (r.plant_part_cosh_id, r.symptom_cosh_id, None, r.sub_symptom_cosh_id)
            if combo not in answered:
                sub_symptom_counts[(r.symptom_cosh_id, r.sub_symptom_cosh_id)] += 1

    if sub_symptom_counts:
        max_count = max(sub_symptom_counts.values())
        candidates = sorted((k for k, v in sub_symptom_counts.items() if v == max_count))
        symptom_id, sub_symptom_id = candidates[0]
        return DiagnosisQuestion(
            plant_part_cosh_id=plant_part,
            symptom_cosh_id=symptom_id,
            sub_symptom_cosh_id=sub_symptom_id,
            question_type="SUB_SYMPTOM",
        )

    # (b) Sub-part: find a (part, symptom) pair where sub-parts differ
    sub_part_counts = Counter()
    for r in relevant:
        if r.sub_part_cosh_id:
            combo = (r.plant_part_cosh_id, r.symptom_cosh_id, r.sub_part_cosh_id, None)
            if combo not in answered:
                sub_part_counts[(r.symptom_cosh_id, r.sub_part_cosh_id)] += 1

    if sub_part_counts:
        max_count = max(sub_part_counts.values())
        candidates = sorted((k for k, v in sub_part_counts.items() if v == max_count))
        symptom_id, sub_part_id = candidates[0]
        return DiagnosisQuestion(
            plant_part_cosh_id=plant_part,
            symptom_cosh_id=symptom_id,
            sub_part_cosh_id=sub_part_id,
            question_type="SUB_PART",
        )

    # (c) All four combined — look across ALL parts (not just current_part)
    all_relevant = [r for r in rows if r.problem_cosh_id in set(problem_ids)]
    for r in all_relevant:
        if r.sub_part_cosh_id and r.sub_symptom_cosh_id:
            combo = (r.plant_part_cosh_id, r.symptom_cosh_id, r.sub_part_cosh_id, r.sub_symptom_cosh_id)
            if combo not in answered:
                return DiagnosisQuestion(
                    plant_part_cosh_id=r.plant_part_cosh_id,
                    symptom_cosh_id=r.symptom_cosh_id,
                    sub_part_cosh_id=r.sub_part_cosh_id,
                    sub_symptom_cosh_id=r.sub_symptom_cosh_id,
                    question_type="SUB_PART",
                )

    return None  # Caller will do random selection


# ── Utility: get available plant parts for starting diagnosis ─────────────────

def get_available_plant_parts(rows: list[ProblemSymptomRow]) -> list[str]:
    """Return distinct plant parts that appear in the problem pool."""
    return list(dict.fromkeys(r.plant_part_cosh_id for r in rows))


def get_problem_list(rows: list[ProblemSymptomRow], plant_part: Optional[str] = None) -> list[str]:
    """Return distinct problem IDs, optionally filtered by plant part ('I Know the Problem')."""
    if plant_part:
        return list(dict.fromkeys(
            r.problem_cosh_id for r in rows if r.plant_part_cosh_id == plant_part
        ))
    return list(dict.fromkeys(r.problem_cosh_id for r in rows))
