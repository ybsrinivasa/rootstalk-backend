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
class MergeGroup:
    """Phase 2C (2026-07-02) — OR-merge across overlapping timelines.

    Emitted after the standard dedup + AND-atomicity passes when two
    or more survivors carry OR relations that share at least one
    identity. The anchor's relation gets extended with residual
    options from each member; members render nothing on their own
    (skipped via `merged_into_tl_id`). The union of member windows
    stretches over the anchor.

    Shapes covered by 2C.1 → 2C.4:
      - Pure-OR vs Pure-OR with shared identity           (2C.1)
      - AND-of-(OR Part, shared singleton) mutual         (2C.2)
      - COMPLEX_OR vs COMPLEX_OR, shared single-pos Opt   (2C.4)

    Semantic: merged relation shape is
        (shared_choice OR (residual_1 AND residual_2 …)) AND shared_singletons
    so that picking the shared atom satisfies every member's window,
    while the residual fallback requires applying each member's
    non-shared options together.
    """
    anchor_tl_id: str
    anchor_relation_id: str
    member_tl_ids: list[str]                             # excludes the anchor
    merged_window_from: date
    merged_window_to: date
    # Shared atoms — practice ids on the anchor whose identity appears
    # in every member's counterpart relation. Rendered as the "one
    # covers all" head option(s) of the merged card.
    shared_option_practice_ids: list[str]
    # Anchor's own non-shared OR-Part options. These join the members'
    # residuals in the merged fallback compound.
    anchor_residual_practice_ids: list[str]
    # Residual atoms per member — practice ids of options the member's
    # OR carries but the anchor's doesn't. Together with the anchor's
    # residuals, these form ONE compound fallback Option: apply all of
    # them together to cover every member's window.
    # Keys are member TL ids; values are practice ids from that member.
    residual_practice_ids_by_member: dict[str, list[str]]
    # Shared singletons (for 2C.2) — atoms outside the OR Part that
    # every member requires. Rendered as an outer AND alongside the
    # OR choice. Empty for 2C.1 / 2C.4.
    shared_singleton_practice_ids: list[str]

    def build_merged_options(self) -> list[list[str]]:
        """Return the merged relation's OR Options list.

        Each element is one Option — a list of practice ids that must
        be applied together to satisfy that Option.

        Shape produced:
          - N head options, one per shared identity (each is a
            single-position alternative that covers every member).
          - 1 compound fallback option combining anchor's + every
            member's non-shared residuals — apply all together.

        For 2C.1 (A/B) vs (A/C) → [[A], [B, C]].
        For 2C.4 A OR (X+Y) vs A OR (P+Q) → [[A], [X, Y, P, Q]].
        The outer AND with `shared_singleton_practice_ids` is applied
        alongside this Options list by the renderer (2C.2 case).
        """
        options: list[list[str]] = []
        for pid in self.shared_option_practice_ids:
            options.append([pid])
        compound = list(self.anchor_residual_practice_ids)
        for member_pids in self.residual_practice_ids_by_member.values():
            compound.extend(member_pids)
        if compound:
            options.append(compound)
        return options


@dataclass
class DeduplicatedTimeline:
    timeline: TimelineWindow
    visible_practices: list[PracticeStub]
    suppressed: list[SuppressedPractice]
    # 2026-06-29 — Phase 1 window absorption. When set, every INPUT
    # practice that this timeline contributed was suppressed by
    # matches in a single other timeline (the absorbing one). The
    # renderer skips this TL entirely and unions its date window
    # into the absorbing TL — surfacing the absorbing TL's
    # spec across the merged span so the farmer doesn't see an
    # empty "covered elsewhere" section in place of useful guidance.
    # Only set when:
    #   - the visible_practices list is empty, AND
    #   - every suppression points to the same governing_timeline_id.
    # Mixed-governor or has-residual-practices TLs stay rendered.
    absorbed_into_tl_id: Optional[str] = None
    # 2026-07-02 — Phase 2C merge. When set on a survivor, this TL is
    # the anchor of a MergeGroup — its OR relation carries extended
    # options + a stretched window sourced from members. When set on
    # a member (as `merged_into_tl_id`), the TL is skipped from the
    # standalone render; its residual options are lifted into the
    # anchor's merged card by the subscription router.
    merge_group: Optional[MergeGroup] = None
    merged_into_tl_id: Optional[str] = None


def deduplicate_advisory(
    active_timelines: list[TimelineWindow],
    committed_practice_ids: set[str],   # practice IDs the farmer has committed to via an in-flight or approved order
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
    - In-flight precedence rule (formerly "purchased rule",
      broadened 2026-06-29): if the practice already has a live
      OrderItem on this subscription — status in
      {PENDING, AVAILABLE, POSTPONED, SENT_FOR_APPROVAL, APPROVED} —
      suppress the same input in any overlapping timeline, even if
      the governing timeline has closed. This honours the principle
      "what has been ordered gains precedence." Callers compute the
      set via the `IN_FLIGHT_ITEM_STATUSES` constant in
      `app.services.order_bundle`.
    - Reinstatement: if governing timeline is closed (today > to_date) AND the practice
      has NO live OrderItem, reinstate the later timeline's practice.
    """
    if not active_timelines:
        return []

    if today is None:
        today = date.today()

    # Sort by (from_date, created_at) — deterministic, earlier governs
    sorted_tls = sorted(active_timelines, key=lambda t: (t.from_date, t.created_at))

    # 2026-06-29 — Phase 2 precomputation.
    # Per-(timeline, relation_id), classify the relation's STRUCTURE
    # (AND / PURE_OR / COMPLEX_OR) and collect the identity set the
    # relation contributes. Both feed the new in-relation-vs-in-relation
    # rules below: "AND covers pure-OR" needs to know which side is
    # which; "AND vs AND subset" needs identity sets to compare.
    #
    # Structure inferred from the role encoding (PART_n__OPT_m__POS_p):
    #   - AND        : single Option, regardless of how many positions
    #                  (single member or compound AND group).
    #   - PURE_OR    : ≥ 2 Options, each with exactly 1 position.
    #   - COMPLEX_OR : ≥ 2 Options, at least one with > 1 position.
    #   - UNKNOWN    : malformed roles or empty.
    relation_structure: dict[tuple[str, str], str] = {}
    relation_identities: dict[tuple[str, str], frozenset[str]] = {}
    for tl in active_timelines:
        rel_ids = {p.relation_id for p in tl.practices if p.relation_id}
        for rid in rel_ids:
            options: dict[int, set[int]] = {}
            idents: set[str] = set()
            for p in tl.practices:
                if p.relation_id != rid:
                    continue
                ref = p.primary_identity_ref()
                if ref:
                    idents.add(ref)
                if not p.relation_role:
                    continue
                m = _ROLE_PARSER.match(p.relation_role)
                if not m:
                    continue
                _part, opt, pos = m.groups()
                options.setdefault(int(opt), set()).add(int(pos))
            if not options:
                struct = "UNKNOWN"
            elif len(options) == 1:
                struct = "AND"
            elif all(len(positions) == 1 for positions in options.values()):
                struct = "PURE_OR"
            else:
                struct = "COMPLEX_OR"
            relation_structure[(tl.id, rid)] = struct
            relation_identities[(tl.id, rid)] = frozenset(idents)

    # Build suppression map: {practice_id_in_later_tl → SuppressedPractice}
    suppression: dict[str, SuppressedPractice] = {}

    def _suppress_whole_relation(
        tl_target: TimelineWindow, relation_id: str,
        governing_tl_id: str, reason: str = "OVERLAP",
    ) -> None:
        """Mark every INPUT practice in (tl_target, relation_id) as
        suppressed by governing_tl_id. Used by Phase 2 rules where the
        winning side absorbs the *entire* losing relation, not just
        the matching member (AND-covers-pure-OR, AND-vs-AND subset)."""
        for p in tl_target.practices:
            if p.relation_id != relation_id:
                continue
            if p.l0_type != "INPUT":
                continue
            if p.is_special_input:
                continue
            if p.id in suppression:
                continue
            suppression[p.id] = SuppressedPractice(
                practice_id=p.id,
                timeline_id=tl_target.id,
                governing_timeline_id=governing_tl_id,
                reason=reason,
            )

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
                        elif earlier_in_rel and later_in_rel:
                            # 2026-06-29 — Phase 2 in-relation-vs-in-relation
                            # rules. Both sides belong to relations and
                            # share an identity; the matching-pair has
                            # been found. Apply structure-aware rules:
                            earlier_struct = relation_structure.get(
                                (tl_earlier.id, p_earlier.relation_id),
                                "UNKNOWN",
                            )
                            later_struct = relation_structure.get(
                                (tl_later.id, p_later.relation_id),
                                "UNKNOWN",
                            )
                            # Rule: AND covers pure-OR. When an AND
                            # member's identity appears in a pure-OR's
                            # options, the AND is delivering that
                            # identity anyway — the OR's flexibility is
                            # moot, suppress the *entire* OR group so
                            # the farmer pays for one OR card less.
                            # Conservative: only fires for pure-OR
                            # (single-position Options). Compound OR
                            # options have their own atomic semantics
                            # (e.g. (A+E) OR (C+D) — E is a real input
                            # the farmer needs even when A is covered
                            # by another timeline), so leave those to
                            # the existing per-practice rule + AND
                            # atomicity post-pass.
                            if earlier_struct == "AND" and later_struct == "PURE_OR":
                                _suppress_whole_relation(
                                    tl_later, p_later.relation_id,
                                    tl_earlier.id,
                                )
                                break
                            if earlier_struct == "PURE_OR" and later_struct == "AND":
                                _suppress_whole_relation(
                                    tl_earlier, p_earlier.relation_id,
                                    tl_later.id,
                                )
                                # The earlier OR is now suppressed.
                                # p_later might still match other
                                # candidates — keep iterating earlier.
                                continue
                            # Rule: AND vs AND subset. When two AND
                            # groups share an identity AND one's full
                            # identity set is a strict subset of the
                            # other's, the superset covers the subset
                            # — suppress the subset's whole AND. This
                            # is the case the user listed as
                            # "(A+B) vs (A+B+C)". When neither is a
                            # subset (e.g. "(A+B) vs (A+C)"), we
                            # deliberately do NOT suppress here — fall
                            # through to the standard branch, which
                            # would individually suppress the later
                            # match, and then the AND-atomicity
                            # post-pass restores it so both ANDs stay
                            # whole. (User rule: "we cannot stop the
                            # dealer giving A two times.")
                            if earlier_struct == "AND" and later_struct == "AND":
                                e_idents = relation_identities.get(
                                    (tl_earlier.id, p_earlier.relation_id),
                                    frozenset(),
                                )
                                l_idents = relation_identities.get(
                                    (tl_later.id, p_later.relation_id),
                                    frozenset(),
                                )
                                if (
                                    e_idents and l_idents
                                    and e_idents != l_idents
                                ):
                                    if e_idents.issubset(l_idents):
                                        _suppress_whole_relation(
                                            tl_earlier, p_earlier.relation_id,
                                            tl_later.id,
                                        )
                                        continue
                                    if l_idents.issubset(e_idents):
                                        _suppress_whole_relation(
                                            tl_later, p_later.relation_id,
                                            tl_earlier.id,
                                        )
                                        break
                            # Fall through for: AND vs AND (no subset)
                            # — single match suppressed, AND atomicity
                            # restores both.
                            # OR vs OR — handled in Phase 2C
                            # (merge), not here.
                            # COMPLEX_OR involvement — fall through
                            # to existing per-practice rule.
                        # Same input found in earlier timeline — suppress later
                        # Determine reason and check reinstatement
                        if p_earlier.id in committed_practice_ids:
                            # In-flight / purchased rule: suppress
                            # permanently regardless of TL_A closure.
                            # The farmer already has an order moving
                            # for this input — don't surface a
                            # duplicate recommendation from a later
                            # CCA / CHA timeline.
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

        # 2026-06-29 — Phase 1 absorption detection.
        # A timeline is "fully absorbed" when (a) it contributes zero
        # visible practices, AND (b) every suppression on this TL
        # points to the same governing timeline. That governor is
        # the absorbing TL; its window will be unioned to cover this
        # TL's span by the today-advisory renderer, and this TL will
        # be skipped from the output.
        # Mixed governors (different suppressions land on different
        # other TLs) — leave absorbed_into_tl_id None; the existing
        # "Covered elsewhere" empty section will continue to render.
        # Anything still visible — also None; the TL renders normally.
        absorbed_into: Optional[str] = None
        if not final_visible and tl_suppressed:
            governors = {sp.governing_timeline_id for sp in tl_suppressed}
            if len(governors) == 1:
                absorbed_into = next(iter(governors))

        result.append(DeduplicatedTimeline(
            timeline=tl,
            visible_practices=final_visible,
            suppressed=tl_suppressed,
            absorbed_into_tl_id=absorbed_into,
        ))

    # 2026-07-02 — Phase 2C merge detection.
    # Runs on survivors (post-Phase-1 absorption, post-Phase-2
    # suppression, post-AND-atomicity). Composition order per user
    # design: absorption first, then merge on what's left. Prevents
    # merge from cascading into already-absorbed rows.
    _detect_merge_groups(
        result,
        relation_structure=relation_structure,
        relation_identities=relation_identities,
    )

    return result


def _detect_merge_groups(
    result: list["DeduplicatedTimeline"],
    *,
    relation_structure: dict[tuple[str, str], str],
    relation_identities: dict[tuple[str, str], frozenset[str]],
) -> None:
    """Phase 2C — detect + build MergeGroups over the dedup survivors.

    Mutates the input list in place: anchors get `merge_group` set,
    members get `merged_into_tl_id` set. No new TLs are added.

    Detection is symmetric — we walk pairs of surviving TLs, sorted by
    (from_date, created_at), so the earlier TL becomes the anchor. If
    a third TL merges into the same anchor, it joins the existing
    MergeGroup as another member.

    Rules covered:
      2C.1 — pure-OR vs pure-OR, share ≥1 identity.
      2C.2 — AND-of-(OR Part, shared singleton) mutual share
             (implemented alongside 2C.1 using the same primitive).
      2C.4 — COMPLEX_OR vs COMPLEX_OR, shared single-position Option.
             (implemented in the same pass — the merge shape is the
             same as 2C.1: the shared atom becomes the head option,
             non-shared members' Options become residuals.)
    """
    # Filter to survivors that are still standalone-render candidates.
    # Anything absorbed by Phase 1 is out of the running.
    standalone_survivors = [
        d for d in result
        if d.absorbed_into_tl_id is None and d.visible_practices
    ]
    if len(standalone_survivors) < 2:
        return

    # Sort by (from_date, created_at) to make anchor selection deterministic.
    standalone_survivors.sort(
        key=lambda d: (d.timeline.from_date, d.timeline.created_at)
    )

    # Track which TL ids are already merged (as anchor or member) so we
    # don't try to merge them twice this pass.
    already_anchored: set[str] = set()
    already_a_member: set[str] = set()

    for i, anchor_dt in enumerate(standalone_survivors):
        if anchor_dt.timeline.id in already_a_member:
            continue
        anchor_tl = anchor_dt.timeline
        # Consider each of the anchor's OR-shaped relations in turn.
        anchor_or_rels = _or_relations_on_tl(
            anchor_dt, relation_structure=relation_structure,
        )
        if not anchor_or_rels:
            continue

        for anchor_rid in anchor_or_rels:
            anchor_idents = relation_identities.get(
                (anchor_tl.id, anchor_rid), frozenset(),
            )
            if not anchor_idents:
                continue

            # Look for later survivors that share ≥ 1 identity with this
            # anchor relation on one of THEIR OR-shaped relations.
            for member_dt in standalone_survivors[i + 1:]:
                if member_dt.timeline.id in already_a_member:
                    continue
                if not _tls_overlap_windows(anchor_tl, member_dt.timeline):
                    continue
                member_or_rels = _or_relations_on_tl(
                    member_dt, relation_structure=relation_structure,
                )
                for member_rid in member_or_rels:
                    member_idents = relation_identities.get(
                        (member_dt.timeline.id, member_rid), frozenset(),
                    )
                    shared = anchor_idents & member_idents
                    if not shared:
                        continue
                    # Candidate — attempt the merge. It may be rejected
                    # by the OR-Part gate (e.g. pure-AND vs pure-AND
                    # sharing an atom). Only mark the member as merged
                    # if the merge actually fired.
                    merged = _extend_or_create_merge_group(
                        anchor_dt=anchor_dt,
                        anchor_rid=anchor_rid,
                        member_dt=member_dt,
                        member_rid=member_rid,
                        shared_idents=shared,
                    )
                    if merged:
                        already_anchored.add(anchor_tl.id)
                        already_a_member.add(member_dt.timeline.id)
                        break  # one merge per member-anchor pair


def _or_relations_on_tl(
    dt: "DeduplicatedTimeline",
    *,
    relation_structure: dict[tuple[str, str], str],
) -> list[str]:
    """Return the relation ids on this TL whose structure is any OR
    flavour (PURE_OR or COMPLEX_OR). Excludes AND — those don't
    participate in 2C.1 / 2C.4 merges. (2C.2's AND-with-OR-Part case
    is handled by looking at the OR sub-Part identities — we treat
    the whole AND relation as OR-mergeable when at least one of its
    Parts is an OR shape carrying shared identity.)"""
    out: list[str] = []
    seen: set[str] = set()
    for p in dt.visible_practices:
        if not p.relation_id or p.relation_id in seen:
            continue
        seen.add(p.relation_id)
        struct = relation_structure.get((dt.timeline.id, p.relation_id))
        if struct in ("PURE_OR", "COMPLEX_OR", "AND"):
            # Include AND so 2C.2 (AND-with-OR-Part) has a chance to
            # match. The identity comparison at the caller handles
            # whether the OR sub-Part actually shares.
            out.append(p.relation_id)
    return out


def _tls_overlap_windows(a: TimelineWindow, b: TimelineWindow) -> bool:
    return a.from_date <= b.to_date and b.from_date <= a.to_date


def _extend_or_create_merge_group(
    *,
    anchor_dt: "DeduplicatedTimeline",
    anchor_rid: str,
    member_dt: "DeduplicatedTimeline",
    member_rid: str,
    shared_idents: frozenset[str],
) -> bool:
    """Attach (or extend) a MergeGroup on the anchor and mark the
    member as merged. Splits the anchor's + member's practices into
    shared / residual / singleton buckets.

    Returns True when the merge fired, False when it was rejected
    because the shared identity does not live in an OR Part on both
    sides. The caller uses this to decide whether to mark the member
    as `merged_into_tl_id`.

    Why the OR-Part gate matters: for pure-AND vs pure-AND like
    (A+B) vs (A+C), atom A is REQUIRED on each side — not an
    alternative. Applying A once does NOT relieve either TL's
    demand for the OTHER atom, so the merge shape "A OR (B AND C)"
    would misrepresent the SE's intent. Per the user's locked rule
    ("we cannot stop the dealer giving A two times"), pure-AND
    duplicates stay. The merge only makes sense when the shared
    atom lives in an OR Part on both sides — it's a genuine
    alternative, and applying it once satisfies both windows.
    """
    # Split anchor's practices by Part shape. Structure inferred from
    # the FULL original practices list so a suppressed Option (e.g.
    # the shared identity that got dedup'd) doesn't collapse an OR
    # Part into a singleton.
    #  • Part is a singleton iff it has exactly one Option with exactly
    #    one position — the atom is required (AND wrt outer relation).
    #  • Otherwise it's an OR Part.
    # Shared identities in OR Parts → head choice on the merged card.
    # Shared identities in singleton Parts → outer AND (only when the
    # member also has a singleton on the same identity).
    anchor_full_by_part = _group_by_part(
        anchor_dt.timeline.practices, anchor_rid,
    )
    visible_anchor_ids = {p.id for p in anchor_dt.visible_practices}
    anchor_singleton_pids: dict[str, str] = {}
    anchor_or_part_shared_pids: dict[str, str] = {}
    anchor_or_part_residual_pids: list[str] = []
    for _part_idx, opt_map in anchor_full_by_part.items():
        is_singleton = len(opt_map) == 1 and all(len(ps) == 1 for ps in opt_map.values())
        for _opt_idx, ps in opt_map.items():
            for p in ps:
                if p.id not in visible_anchor_ids:
                    continue
                ref = p.primary_identity_ref()
                if not ref:
                    continue
                if is_singleton:
                    if ref in shared_idents:
                        anchor_singleton_pids[ref] = p.id
                    # Non-shared singletons on the anchor stay in the
                    # anchor's own render — they're not part of the
                    # merge shape.
                else:
                    if ref in shared_idents:
                        anchor_or_part_shared_pids[ref] = p.id
                    else:
                        # Anchor's non-shared OR-Part option — joins
                        # the fallback compound.
                        anchor_or_part_residual_pids.append(p.id)

    member_singleton_refs = _singleton_identity_refs(member_dt, member_rid)
    shared_singletons = [
        pid for ref, pid in anchor_singleton_pids.items()
        if ref in member_singleton_refs
    ]
    shared_on_anchor = list(anchor_or_part_shared_pids.values())

    # OR-Part gate — the shared identity must live in an OR Part on
    # BOTH sides. Otherwise this is a pure-AND-vs-pure-AND overlap
    # (or one side is AND-only) and we must NOT merge. Even when A is
    # the same molecule, (A+B) and (A+C) are different mixtures with
    # potentially different tank behaviour — the farmer applies them
    # separately, dealer sells A twice. Per locked user rule.
    if not shared_on_anchor:
        return False
    member_or_shared_refs = _or_part_identity_refs(member_dt, member_rid) & shared_idents
    if not member_or_shared_refs:
        return False

    # Residual practices on the member — non-shared options in the
    # member's OR Part(s). Structure from full practices, content
    # filtered to survivors.
    member_full_by_part = _group_by_part(
        member_dt.timeline.practices, member_rid,
    )
    visible_member_ids = {p.id for p in member_dt.visible_practices}
    residuals_on_member: list[str] = []
    for _part_idx, opt_map in member_full_by_part.items():
        is_singleton = len(opt_map) == 1 and all(len(ps) == 1 for ps in opt_map.values())
        if is_singleton:
            continue  # non-shared singletons aren't merged in 2C.1-4
        for _opt_idx, ps in opt_map.items():
            for p in ps:
                if p.id not in visible_member_ids:
                    continue
                ref = p.primary_identity_ref()
                if ref and ref not in shared_idents:
                    residuals_on_member.append(p.id)

    # Compute merged window (union).
    from_date = min(anchor_dt.timeline.from_date, member_dt.timeline.from_date)
    to_date = max(anchor_dt.timeline.to_date, member_dt.timeline.to_date)

    if anchor_dt.merge_group is None:
        anchor_dt.merge_group = MergeGroup(
            anchor_tl_id=anchor_dt.timeline.id,
            anchor_relation_id=anchor_rid,
            member_tl_ids=[member_dt.timeline.id],
            merged_window_from=from_date,
            merged_window_to=to_date,
            shared_option_practice_ids=shared_on_anchor,
            anchor_residual_practice_ids=anchor_or_part_residual_pids,
            residual_practice_ids_by_member={
                member_dt.timeline.id: residuals_on_member,
            },
            shared_singleton_practice_ids=shared_singletons,
        )
    else:
        # Third+ member — extend the existing group.
        mg = anchor_dt.merge_group
        mg.member_tl_ids.append(member_dt.timeline.id)
        mg.merged_window_from = min(mg.merged_window_from, from_date)
        mg.merged_window_to = max(mg.merged_window_to, to_date)
        mg.residual_practice_ids_by_member[member_dt.timeline.id] = (
            residuals_on_member
        )
        # Shared singletons: intersection tightens. If a third member
        # doesn't carry one of the previously-shared singletons, drop
        # it from the merged set.
        mg.shared_singleton_practice_ids = [
            pid for pid in mg.shared_singleton_practice_ids
            if pid in shared_singletons
        ]

    member_dt.merged_into_tl_id = anchor_dt.timeline.id
    return True


def _group_by_part(
    practices: list[PracticeStub], rid: str,
) -> dict[int, dict[int, list[PracticeStub]]]:
    """Return {part_idx → {opt_idx → [practices in that Option]}} for
    a given relation. Callers pass the FULL original practices list
    when they need structural inference (a Part with a suppressed
    Option is still an OR Part), or `visible_practices` when they
    need to filter to survivors."""
    out: dict[int, dict[int, list[PracticeStub]]] = {}
    for p in practices:
        if p.relation_id != rid or not p.relation_role:
            continue
        m = _ROLE_PARSER.match(p.relation_role)
        if not m:
            continue
        part_idx, opt_idx = int(m.group(1)), int(m.group(2))
        out.setdefault(part_idx, {}).setdefault(opt_idx, []).append(p)
    return out


def _singleton_identity_refs(
    dt: "DeduplicatedTimeline", rid: str,
) -> set[str]:
    """Identity refs that occupy a singleton Part in this relation.
    Structure inferred from the ORIGINAL practices so a suppressed
    Option in an OR Part doesn't reduce that Part to a singleton."""
    grouped = _group_by_part(dt.timeline.practices, rid)
    out: set[str] = set()
    for _part_idx, opt_map in grouped.items():
        if len(opt_map) != 1:
            continue
        (only_opt,) = opt_map.values()
        if len(only_opt) != 1:
            continue
        ref = only_opt[0].primary_identity_ref()
        if ref:
            out.add(ref)
    return out


def _or_part_identity_refs(
    dt: "DeduplicatedTimeline", rid: str,
) -> set[str]:
    """Identity refs that occupy a Part with more than one Option in
    this relation — i.e. the identity is an authored alternative, not
    a mandatory AND member. Structure inferred from the ORIGINAL
    practices so a suppressed Option doesn't collapse the Part shape."""
    grouped = _group_by_part(dt.timeline.practices, rid)
    out: set[str] = set()
    for _part_idx, opt_map in grouped.items():
        if len(opt_map) < 2:
            continue  # singleton Part — not an OR alternative
        for _opt_idx, ps in opt_map.items():
            for p in ps:
                ref = p.primary_identity_ref()
                if ref:
                    out.add(ref)
    return out


def timelines_overlap(a: TimelineWindow, b: TimelineWindow) -> bool:
    """Two timelines overlap if their date ranges share at least one day."""
    return a.from_date <= b.to_date and b.from_date <= a.to_date
