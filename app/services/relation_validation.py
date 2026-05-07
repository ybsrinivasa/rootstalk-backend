"""CCA Step 4 / Batch 4A — Save-time relation validation.

Spec §6.4 + §10.2 + user clarification 2026-05-07. Wiring up the
existing `relations.py` service (Gate 1 / Gate 2 / structure
builder) plus a few additional structural rules:

- **AND** relations: between Input Practices only (Pesticides /
  Fertilizers / Special Inputs in any combination). Non-Inputs,
  Instructions, Media cannot participate in AND.
- **OR** relations: within Pesticides L1 OR within Fertilizers L1
  (never across), with Special Inputs (adjuvants) as an exception
  that may mix in either way.
- **One relation per practice**: a Practice may be in at most one
  *saved* Relation per timeline. The Gate-1 in-progress add-list
  state is not yet persisted; this rule applies only when a
  Relation is being saved via `create_relation`.
- **Cross-timeline**: every practice in a Relation must belong to
  the same Timeline as the Relation itself.

Plus the Gate-1 and Gate-2 rules that the relations.py service
already encodes:
- Duplicate inputs within a single Option (`A+B+A` rejected).
- Two compound Options in the same Part (`(A+B) or (C+D)` —
  "double brackets" rejected at save).
- Branch that always produces duplicate purchases.
- No valid combination across Parts.

Special Inputs are exempt from duplicate detection — they're
adjuvants that may appear freely.

Returns a list of `MissingPublishField`-style errors with stable
codes so the route layer can map to a 422 with a checklist body.
"""
from __future__ import annotations

from dataclasses import dataclass
from itertools import product
from typing import Optional

from app.services.relations import (
    Option, Part, PracticeRef, RelationStructure,
    encode_role,
)


# ── L1 group constants ───────────────────────────────────────────────────────

L1_PESTICIDE_GROUP = {"PESTICIDE"}
L1_FERTILIZER_GROUP = {"FERTILIZER"}


@dataclass(frozen=True)
class RelationValidationError:
    """One validation failure. `code` is the stable identifier the
    portal dispatches on; `message` is the human-readable reason;
    `extra` carries optional structured data (e.g. offending part /
    option indices)."""
    code: str
    message: str
    extra: Optional[dict] = None


class RelationValidationFailed(Exception):
    """Raised when relation validation produces ≥1 error."""

    code = "relation_validation_failed"

    def __init__(self, errors: list[RelationValidationError]):
        self.errors = errors
        codes = ", ".join(e.code for e in errors)
        super().__init__(
            f"Relation save blocked: {len(errors)} rule(s) violated ({codes})."
        )


# ── L0 / L1 rules ────────────────────────────────────────────────────────────

def _check_and_input_only(
    relation_type: str, practices: list[PracticeRef],
    practice_meta: dict[str, dict],
) -> Optional[RelationValidationError]:
    """AND relations must only contain Input Practices (Pesticide,
    Fertilizer, Special Input). Non-Inputs / Instructions / Media
    cannot participate."""
    if relation_type != "AND":
        return None
    bad = []
    for p in practices:
        meta = practice_meta.get(p.practice_id, {})
        if meta.get("l0_type") != "INPUT":
            bad.append({
                "practice_id": p.practice_id,
                "l0_type": meta.get("l0_type"),
            })
    if bad:
        return RelationValidationError(
            "relation_and_non_input",
            "AND relations are restricted to Input Practices "
            "(Pesticides, Fertilizers, Special Inputs). Non-Input "
            "practices cannot participate.",
            extra={"non_input_practices": bad},
        )
    return None


def _check_or_l1_restriction(
    relation_type: str, practices: list[PracticeRef],
    practice_meta: dict[str, dict],
) -> Optional[RelationValidationError]:
    """OR relations must be within Pesticides L1 OR within Fertilizers
    L1 — never across the two. Special Inputs (adjuvants) are an
    exception and may appear with either side."""
    if relation_type != "OR":
        return None
    has_pesticide = False
    has_fertilizer = False
    has_non_input = []
    for p in practices:
        meta = practice_meta.get(p.practice_id, {})
        l0 = meta.get("l0_type")
        l1 = meta.get("l1_type")
        # Special Inputs are the exception — they may mix freely.
        if p.is_special_input:
            continue
        if l0 != "INPUT":
            has_non_input.append({
                "practice_id": p.practice_id,
                "l0_type": l0, "l1_type": l1,
            })
            continue
        if l1 in L1_PESTICIDE_GROUP:
            has_pesticide = True
        elif l1 in L1_FERTILIZER_GROUP:
            has_fertilizer = True
    if has_non_input:
        return RelationValidationError(
            "relation_or_only_inputs",
            "OR relations must contain only Pesticide / Fertilizer "
            "/ Special Input practices.",
            extra={"non_input_practices": has_non_input},
        )
    if has_pesticide and has_fertilizer:
        return RelationValidationError(
            "relation_or_cross_l1",
            "OR relations cannot span both Pesticides and Fertilizers. "
            "Build a separate relation for each L1 (Special Inputs may "
            "mix with either side).",
        )
    return None


# ── Structural rules: cross-timeline, one-relation-per-practice ──────────────

def _check_cross_timeline(
    target_timeline_id: str, practice_meta: dict[str, dict],
) -> Optional[RelationValidationError]:
    bad = [
        {"practice_id": pid, "timeline_id": meta.get("timeline_id")}
        for pid, meta in practice_meta.items()
        if meta.get("timeline_id") != target_timeline_id
    ]
    if bad:
        return RelationValidationError(
            "relation_cross_timeline",
            "All practices in a Relation must belong to the same "
            "Timeline as the Relation itself.",
            extra={"foreign_practices": bad},
        )
    return None


def _check_practice_already_in_relation(
    practice_meta: dict[str, dict],
) -> Optional[RelationValidationError]:
    """Spec: a practice can be in exactly one saved Relation. If
    `relation_id` is already set on any incoming practice, reject."""
    bad = [
        {"practice_id": pid, "relation_id": meta.get("relation_id")}
        for pid, meta in practice_meta.items()
        if meta.get("relation_id") is not None
    ]
    if bad:
        return RelationValidationError(
            "relation_practice_already_in_relation",
            "At least one practice is already part of another saved "
            "Relation. A practice can be in at most one Relation.",
            extra={"already_assigned_practices": bad},
        )
    return None


# ── Structural rules: double brackets (per-Part Gate-1 echo) ─────────────────

def _check_double_brackets(
    structure: RelationStructure,
) -> Optional[RelationValidationError]:
    """A Part may not contain more than one compound Option (size > 1).
    Spec example: `(A+B) or (C+D)` — rejected at any stage."""
    for part in structure.parts:
        compound_options = [o for o in part.options if o.is_compound()]
        if len(compound_options) > 1:
            return RelationValidationError(
                "relation_double_brackets",
                f"Part {part.part_index} contains more than one bracketed "
                "AND group (e.g. '(A+B) or (C+D)'). Only one compound "
                "Option is allowed per Part.",
                extra={"part_index": part.part_index},
            )
    return None


# ── Combinatorial rules: branch / no-valid-combination ───────────────────────

def _check_combinatorial_duplicates(
    structure: RelationStructure,
) -> Optional[RelationValidationError]:
    """Two checks combined:

    1. Mandatory inputs (single-Option Parts) must not duplicate
       each other or any branch's contents.
    2. At least one Cartesian-product combination across all Parts
       must produce zero duplicates. If every combination produces
       at least one duplicate, the relation is invalid.

    Special Inputs are excluded from duplicate detection.
    """
    if not structure.parts:
        return None

    mandatory: list[str] = []
    for part in structure.parts:
        if len(part.options) == 1:
            for prac in part.options[0].practices:
                if prac.common_name_cosh_id and not prac.is_special_input:
                    mandatory.append(prac.common_name_cosh_id)

    if len(mandatory) != len(set(mandatory)):
        return RelationValidationError(
            "relation_no_valid_combination",
            "Mandatory inputs across single-Option Parts duplicate "
            "each other — every combination produces a double purchase.",
        )

    # Branch pre-check: any single Option whose contents (including
    # mandatory) always duplicate.
    for part in structure.parts:
        if len(part.options) <= 1:
            continue
        for opt in part.options:
            opt_ids = [
                p.common_name_cosh_id for p in opt.practices
                if p.common_name_cosh_id and not p.is_special_input
            ]
            if len(opt_ids) != len(set(opt_ids)):
                return RelationValidationError(
                    "relation_branch_always_duplicates",
                    f"Option at Part {part.part_index} Option "
                    f"{opt.option_index} contains the same input twice.",
                    extra={
                        "part_index": part.part_index,
                        "option_index": opt.option_index,
                    },
                )
            combined = opt_ids + mandatory
            if len(combined) != len(set(combined)):
                return RelationValidationError(
                    "relation_branch_always_duplicates",
                    f"Branch at Part {part.part_index} Option "
                    f"{opt.option_index} always duplicates a mandatory "
                    "input.",
                    extra={
                        "part_index": part.part_index,
                        "option_index": opt.option_index,
                    },
                )

    # Cartesian product: at least one combination must have no duplicates.
    options_per_part = [part.options for part in structure.parts]
    for combination in product(*options_per_part):
        flat: list[str] = []
        for opt in combination:
            for prac in opt.practices:
                if prac.common_name_cosh_id and not prac.is_special_input:
                    flat.append(prac.common_name_cosh_id)
        if len(flat) == len(set(flat)):
            return None

    return RelationValidationError(
        "relation_no_valid_combination",
        "Every possible combination of selections across the Parts "
        "results in at least one duplicate input purchase.",
    )


# ── Build structure from 3-D parts shape ─────────────────────────────────────

def build_structure_from_parts(
    parts: list[list[list[str]]],
    practice_refs_by_id: dict[str, PracticeRef],
    relation_id: Optional[str],
    relation_type: str,
) -> RelationStructure:
    """Convert a 3-D parts shape (parts × options × positions of
    practice_ids) into a `RelationStructure` with role strings
    populated. Mirrors the reverse of `relations.build_structure`,
    which decodes role strings back into the structure."""
    built_parts: list[Part] = []
    for part_idx, options in enumerate(parts, start=1):
        built_options: list[Option] = []
        for opt_idx, positions in enumerate(options, start=1):
            opt_practices: list[PracticeRef] = []
            for pos_idx, pid in enumerate(positions, start=1):
                base = practice_refs_by_id[pid]
                opt_practices.append(PracticeRef(
                    practice_id=base.practice_id,
                    common_name_cosh_id=base.common_name_cosh_id,
                    is_special_input=base.is_special_input,
                    role=encode_role(part_idx, opt_idx, pos_idx),
                    brand_cosh_id=base.brand_cosh_id,
                    is_locked_brand=base.is_locked_brand,
                    l2_type=base.l2_type,
                ))
            built_options.append(Option(option_index=opt_idx, practices=opt_practices))
        built_parts.append(Part(part_index=part_idx, options=built_options))
    return RelationStructure(
        relation_id=relation_id,
        relation_type=relation_type,
        parts=built_parts,
    )


# ── Top-level validator ──────────────────────────────────────────────────────

def validate_relation_save(
    *,
    relation_type: str,
    target_timeline_id: str,
    parts: list[list[list[str]]],
    practice_refs_by_id: dict[str, PracticeRef],
    practice_meta: dict[str, dict],
) -> RelationStructure:
    """Run every save-time check in order; raise
    `RelationValidationFailed(errors)` carrying ALL discovered
    errors so the CA portal can render one consolidated checklist.

    Returns the built `RelationStructure` if every check passes.

    `practice_meta[practice_id]` must contain at least:
        l0_type, l1_type, timeline_id, relation_id
    `practice_refs_by_id[practice_id]` provides PracticeRef shape
    with `common_name_cosh_id`, `is_special_input` and (optional)
    L2 / brand info.
    """
    errors: list[RelationValidationError] = []

    err = _check_cross_timeline(target_timeline_id, practice_meta)
    if err: errors.append(err)
    err = _check_practice_already_in_relation(practice_meta)
    if err: errors.append(err)

    pracs_flat: list[PracticeRef] = []
    for opts in parts:
        for positions in opts:
            for pid in positions:
                if pid in practice_refs_by_id:
                    pracs_flat.append(practice_refs_by_id[pid])

    err = _check_and_input_only(relation_type, pracs_flat, practice_meta)
    if err: errors.append(err)
    err = _check_or_l1_restriction(relation_type, pracs_flat, practice_meta)
    if err: errors.append(err)

    structure = build_structure_from_parts(
        parts=parts,
        practice_refs_by_id=practice_refs_by_id,
        relation_id=None,
        relation_type=relation_type,
    )

    err = _check_double_brackets(structure)
    if err: errors.append(err)
    err = _check_combinatorial_duplicates(structure)
    if err: errors.append(err)

    if errors:
        raise RelationValidationFailed(errors)
    return structure
