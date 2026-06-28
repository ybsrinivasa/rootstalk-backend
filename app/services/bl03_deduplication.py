"""
BL-03 — Advisory Deduplication Engine
Pure function service. No database access. Runs at render time.
Spec: RootsTalk_Dev_BusinessLogic.pdf §BL-03
"""
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Optional


# Parse a Practice.relation_role of the form `PART_n__OPT_m__POS_p`.
# Used by the AND-atomicity post-pass to group same-Option positions
# across the active timelines so we can restore partial suppressions.
_ROLE_PARSER = re.compile(r"PART_(\d+)__OPT_(\d+)__POS_(\d+)")


@dataclass
class PracticeElement:
    element_type: str
    cosh_ref: Optional[str]
    value: Optional[str]
    unit_cosh_id: Optional[str]


@dataclass
class PracticeStub:
    id: str
    l0_type: str          # INPUT, NON_INPUT, INSTRUCTION, MEDIA
    l1_type: Optional[str]
    l2_type: Optional[str]
    display_order: int
    is_special_input: bool
    relation_id: Optional[str]
    elements: list[PracticeElement] = field(default_factory=list)
    relation_role: Optional[str] = None  # PART_n__OPT_m__POS_p (Practice Relations)
    relation_type: Optional[str] = None  # AND | OR | IF — copied from Relation when known
    frequency_days: Optional[int] = None  # NULL = one-time; >=1 = recurring every N days

    def primary_identity_ref(self) -> Optional[str]:
        """
        Identity key for deduplication.
        For INPUT practices: the cosh_ref of the COMMON_NAME or BRAND element.
        Returns None if no Cosh-sourced identity can be found.
        """
        if self.l0_type != "INPUT":
            return None
        for el in self.elements:
            if el.element_type in ("COMMON_NAME", "BRAND", "ACTIVE_INGREDIENT") and el.cosh_ref:
                return el.cosh_ref
        # Fallback: any element with a cosh_ref
        for el in self.elements:
            if el.cosh_ref:
                return el.cosh_ref
        return None


@dataclass
class TimelineWindow:
    id: str
    name: str
    from_date: date
    to_date: date
    created_at: date        # tie-breaking when from_date is equal
    practices: list[PracticeStub] = field(default_factory=list)
    source: str = "CCA"    # CCA | CHA | QUERY
    # 2026-06-19 — Stable identifier that survives publishes.
    # CCA: Timeline.lineage_id (UUID). CHA/QA: synthetic
    # cha-{sp|pg|qa}-{Timeline.lineage_id}. Drives the per-occurrence
    # practice-acknowledgement key.
    lineage_id: str = ""


@dataclass
class SuppressedPractice:
    practice_id: str
    timeline_id: str           # the timeline containing the suppressed copy
    governing_timeline_id: str # the earlier timeline that governs
    reason: str                # "OVERLAP" | "PURCHASED"


@dataclass
class DeduplicatedTimeline:
    timeline: TimelineWindow
    visible_practices: list[PracticeStub]
    suppressed: list[SuppressedPractice]


def deduplicate_advisory(
    active_timelines: list[TimelineWindow],
    approved_practice_ids: set[str],    # practice IDs that have APPROVED orders
    today: date = None,                 # injectable for testing; defaults to date.today()
) -> list[DeduplicatedTimeline]:
    """
    BL-03 core algorithm.

    Rules:
    - Only INPUT practices are deduplicated (NON_INPUT, INSTRUCTION, MEDIA always shown).
    - Special inputs (is_special_input=True) are never suppressed.
    - Identity check: same primary_identity_ref() on both practices.
    - Earlier start date governs; tie-break by created_at (earlier created_at governs).
    - Chain suppression NOT applied — direct overlap only.
    - Purchased rule: if approved in any timeline, suppress the same input in any
      overlapping timeline, even if the governing timeline has closed.
    - Reinstatement: if governing timeline is closed (today > to_date) AND the practice
      was NOT approved (purchased), reinstate the later timeline's practice.
    """
    if not active_timelines:
        return []

    if today is None:
        today = date.today()

    # Sort by (from_date, created_at) — deterministic, earlier governs
    sorted_tls = sorted(active_timelines, key=lambda t: (t.from_date, t.created_at))

    # Build suppression map: {practice_id_in_later_tl → SuppressedPractice}
    suppression: dict[str, SuppressedPractice] = {}

    for i, tl_later in enumerate(sorted_tls):
        for tl_earlier in sorted_tls[:i]:
            # Direct overlap check (at least one shared day)
            if tl_earlier.to_date < tl_later.from_date:
                continue  # No overlap — skip (also prevents chain suppression)

            for p_later in tl_later.practices:
                if p_later.l0_type != "INPUT":
                    continue
                if p_later.is_special_input:
                    continue

                later_ref = p_later.primary_identity_ref()
                if later_ref is None:
                    continue

                for p_earlier in tl_earlier.practices:
                    if p_earlier.l0_type != "INPUT":
                        continue
                    if p_earlier.is_special_input:
                        continue
                    # BL-03 chain suppression prevention:
                    # If this earlier practice is itself suppressed, it cannot govern later ones.
                    # This prevents: TL_A suppresses TL_B, TL_B suppresses TL_C → TL_C wrongly removed.
                    if p_earlier.id in suppression:
                        continue

                    earlier_ref = p_earlier.primary_identity_ref()
                    if earlier_ref is None:
                        continue

                    if earlier_ref == later_ref:
                        # 2026-06-28 — Standalone vs in-relation dedup
                        # asymmetry: when one side belongs to a Relation
                        # (AND / OR / IF) and the other is standalone,
                        # always suppress the STANDALONE — regardless of
                        # timeline order. In-relation members carry
                        # semantic obligation (AND = required together;
                        # OR = substitution alternative; IF = gated on a
                        # conditional question). Removing an OR member
                        # to keep its standalone twin shrinks the
                        # dealer's flexibility and breaks the spec the
                        # SE wrote. Two-standalone and two-in-relation
                        # cases still use the existing earlier-governs
                        # rule below.
                        earlier_in_rel = p_earlier.relation_id is not None
                        later_in_rel = p_later.relation_id is not None
                        if earlier_in_rel != later_in_rel:
                            standalone_side = (
                                "earlier" if not earlier_in_rel else "later"
                            )
                            if standalone_side == "later":
                                # Existing direction: suppress later.
                                # Keep the purchased / closed nuance
                                # below — drop through to the standard
                                # branch unchanged.
                                pass
                            else:
                                # New: suppress the EARLIER standalone.
                                # The in-relation later member wins.
                                # Don't `break` — p_later might still
                                # be matched by another earlier
                                # candidate (chain prevention catches
                                # the already-suppressed p_earlier on
                                # later iterations).
                                suppression[p_earlier.id] = SuppressedPractice(
                                    practice_id=p_earlier.id,
                                    timeline_id=tl_earlier.id,
                                    governing_timeline_id=tl_later.id,
                                    reason="OVERLAP",
                                )
                                continue
                        # Same input found in earlier timeline — suppress later
                        # Determine reason and check reinstatement
                        if p_earlier.id in approved_practice_ids:
                            # Purchased rule: suppress permanently regardless of TL_A closure
                            suppression[p_later.id] = SuppressedPractice(
                                practice_id=p_later.id,
                                timeline_id=tl_later.id,
                                governing_timeline_id=tl_earlier.id,
                                reason="PURCHASED",
                            )
                        elif tl_earlier.to_date < today:
                            # TL_earlier is CLOSED and not purchased → REINSTATE
                            # Don't add to suppression (or remove if already there)
                            suppression.pop(p_later.id, None)
                        else:
                            # TL_earlier is active → suppress
                            suppression[p_later.id] = SuppressedPractice(
                                practice_id=p_later.id,
                                timeline_id=tl_later.id,
                                governing_timeline_id=tl_earlier.id,
                                reason="OVERLAP",
                            )
                        break  # Found governing match — move to next p_later

    # 2026-06-28 — AND-atomicity post-pass.
    # A compound Option (multiple positions sharing the same Part /
    # Option in the relation_role) is atomic — every member is
    # required together. If the main loop suppressed some but not all
    # positions of a compound Option, the renderer would surface a
    # broken group (e.g. AND { A, B } shrunk to just B, or
    # (A + B) OR (C + D)'s first Option reduced to just B). That's a
    # spec-breaking move: AND members carry joint obligation, not
    # individual. Generic across AND / IF / Complex relations because
    # the check operates on the role encoding, not on relation_type.
    #
    # Walk every (relation_id, part, option) group across the active
    # timelines. If it's compound (> 1 positions) and only some
    # positions are in the suppression set, restore them all — keeping
    # the SE's structure intact at the cost of carrying a redundant
    # identity-match into the order. Single-position Options
    # (pure OR alternatives) are independent and skip this check.
    option_groups: dict[tuple[str, int, int], list[str]] = {}
    for tl in active_timelines:
        for p in tl.practices:
            if not p.relation_id or not p.relation_role:
                continue
            m = _ROLE_PARSER.match(p.relation_role)
            if not m:
                continue
            part_idx, opt_idx = int(m.group(1)), int(m.group(2))
            option_groups.setdefault(
                (p.relation_id, part_idx, opt_idx), []
            ).append(p.id)
    for practice_ids in option_groups.values():
        if len(practice_ids) <= 1:
            continue
        suppressed_in_group = [pid for pid in practice_ids if pid in suppression]
        if 0 < len(suppressed_in_group) < len(practice_ids):
            for pid in suppressed_in_group:
                suppression.pop(pid, None)

    # Build result per timeline
    result = []
    for tl in active_timelines:
        visible: list[PracticeStub] = []
        tl_suppressed: list[SuppressedPractice] = []

        for p in tl.practices:
            if p.id in suppression and suppression[p.id].timeline_id == tl.id:
                tl_suppressed.append(suppression[p.id])
            else:
                visible.append(p)

        # BL-03 step 9: if all practices in a relation are suppressed, remove the relation
        # (tracked via relation_id — filter surviving practices only)
        # A relation is kept only if at least one practice in it survives
        surviving_relations: set[str] = set()
        for p in visible:
            if p.relation_id:
                surviving_relations.add(p.relation_id)

        final_visible = [
            p for p in visible
            if p.relation_id is None or p.relation_id in surviving_relations
        ]

        result.append(DeduplicatedTimeline(
            timeline=tl,
            visible_practices=final_visible,
            suppressed=tl_suppressed,
        ))

    return result


def timelines_overlap(a: TimelineWindow, b: TimelineWindow) -> bool:
    """Two timelines overlap if their date ranges share at least one day."""
    return a.from_date <= b.to_date and b.from_date <= a.to_date
