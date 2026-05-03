"""Practice Relations service.

Foundation for the Practice Relations system per RootsTalk_Relations_Reference.pdf.

Provides:
- Role encoding/decoding (PART_n__OPT_m__POS_p)
- Structure builder (Relation -> Parts -> Options -> Practices)
- Gate 1 validation (Add-to-List time)
- Gate 2 validation (Save time)
- Count display formula (Max N items / N items)
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from itertools import product
from typing import List, NamedTuple, Optional, Tuple


# ── Role encode/decode ────────────────────────────────────────────────────────


class RoleCoords(NamedTuple):
    part: int
    option: int
    position: int


ROLE_PATTERN = re.compile(r"^PART_(\d+)__OPT_(\d+)__POS_(\d+)$")


def encode_role(part: int, option: int, position: int) -> str:
    """part, option, position are all 1-based. Returns 'PART_n__OPT_m__POS_p'."""
    if part < 1 or option < 1 or position < 1:
        raise ValueError("part, option, position must all be >= 1")
    return f"PART_{part}__OPT_{option}__POS_{position}"


def decode_role(role: str) -> RoleCoords:
    """Reverse of encode_role. Raises ValueError on malformed input."""
    m = ROLE_PATTERN.match(role)
    if not m:
        raise ValueError(f"Invalid relation_role: {role!r}")
    return RoleCoords(part=int(m.group(1)), option=int(m.group(2)), position=int(m.group(3)))


# ── Structure model ───────────────────────────────────────────────────────────


@dataclass
class PracticeRef:
    """A lightweight reference to a practice within a relation structure."""
    practice_id: str
    common_name_cosh_id: Optional[str]  # for duplicate detection
    is_special_input: bool
    role: str  # the raw relation_role string
    # Optional brand info — populated when needed
    brand_cosh_id: Optional[str] = None
    is_locked_brand: bool = False
    l2_type: Optional[str] = None


@dataclass
class Option:
    option_index: int  # 1-based
    practices: List[PracticeRef] = field(default_factory=list)  # ordered by position

    @property
    def size(self) -> int:
        return len(self.practices)

    def is_compound(self) -> bool:
        """A compound Option is a bracketed AND group (size > 1)."""
        return self.size > 1


@dataclass
class Part:
    part_index: int  # 1-based
    options: List[Option] = field(default_factory=list)  # ordered by option index

    @property
    def is_choice(self) -> bool:
        """A Part is a choice if it has more than one Option."""
        return len(self.options) > 1

    @property
    def max_size(self) -> int:
        """Maximum number of inputs the dealer might supply for this Part."""
        return max((o.size for o in self.options), default=0)

    @property
    def min_size(self) -> int:
        """Minimum number of inputs the dealer might supply for this Part."""
        return min((o.size for o in self.options), default=0)

    def is_size_deterministic(self) -> bool:
        """True if all Options have the same size (count is exact)."""
        if not self.options:
            return True
        return len({o.size for o in self.options}) == 1


@dataclass
class RelationStructure:
    relation_id: Optional[str]
    relation_type: str  # 'AND' | 'OR' | 'IF'
    parts: List[Part] = field(default_factory=list)

    @property
    def total_max_count(self) -> int:
        """Sum over Parts of max Option size — pessimistic count."""
        return sum(p.max_size for p in self.parts)

    @property
    def total_min_count(self) -> int:
        """Sum over Parts of min Option size — optimistic count."""
        return sum(p.min_size for p in self.parts)

    def is_count_deterministic(self) -> bool:
        """True if all Parts have deterministic size — count is exact."""
        return all(p.is_size_deterministic() for p in self.parts)


def build_structure(
    practices: List[PracticeRef],
    relation_id: Optional[str],
    relation_type: str,
) -> RelationStructure:
    """Given a list of practices with relation_role strings, reconstruct the
    Part -> Option -> Practice tree.
    """
    by_part: dict[int, dict[int, list[tuple[int, PracticeRef]]]] = {}
    for p in practices:
        coords = decode_role(p.role)
        by_part.setdefault(coords.part, {}).setdefault(coords.option, []).append((coords.position, p))

    parts: List[Part] = []
    for part_idx in sorted(by_part.keys()):
        options: List[Option] = []
        for opt_idx in sorted(by_part[part_idx].keys()):
            position_practices = sorted(by_part[part_idx][opt_idx], key=lambda x: x[0])
            options.append(
                Option(
                    option_index=opt_idx,
                    practices=[pr for (_, pr) in position_practices],
                )
            )
        parts.append(Part(part_index=part_idx, options=options))

    return RelationStructure(relation_id=relation_id, relation_type=relation_type, parts=parts)


# ── Gate 1 validation (Add-to-List) ───────────────────────────────────────────


@dataclass
class Gate1Result:
    valid: bool
    error_code: Optional[str] = None  # 'DUPLICATE_IN_GROUP' | 'DUPLICATE_OPTION' | 'MIXED_AND_OR' | 'DOUBLE_BRACKETS'
    error_message: Optional[str] = None


def validate_gate1_option(
    option_practices: List[PracticeRef],
    existing_options_in_part: List[Option],
) -> Gate1Result:
    """Called when Subject Expert tries to add a new Option to a Part.

    Checks:
    - No duplicate inputs within the new Option (AND-group duplicates)
    - The new Option doesn't duplicate an existing Option's inputs exactly
    - If the new Option is compound (size > 1) AND an existing Option is also
      compound, reject (double brackets)

    Special inputs are exempt from all duplicate checks.
    """
    # Duplicate within new Option (excluding special inputs)
    cn_ids = [
        p.common_name_cosh_id
        for p in option_practices
        if not p.is_special_input and p.common_name_cosh_id
    ]
    if len(cn_ids) != len(set(cn_ids)):
        return Gate1Result(False, "DUPLICATE_IN_GROUP", "Same input listed twice in this group")

    # Double brackets check: if this Option is compound AND any existing Option
    # is compound, reject. (A Part may not contain more than one bracketed AND
    # group.)
    if len(option_practices) > 1:
        for existing in existing_options_in_part:
            if existing.is_compound():
                return Gate1Result(
                    False,
                    "DOUBLE_BRACKETS",
                    "A Part cannot contain more than one bracketed AND group",
                )

    # Duplicate Option (exact match against an existing Option, ignoring specials)
    new_set = frozenset(
        p.common_name_cosh_id
        for p in option_practices
        if p.common_name_cosh_id and not p.is_special_input
    )
    for existing in existing_options_in_part:
        ex_set = frozenset(
            p.common_name_cosh_id
            for p in existing.practices
            if p.common_name_cosh_id and not p.is_special_input
        )
        if new_set and new_set == ex_set:
            return Gate1Result(False, "DUPLICATE_OPTION", "This Option already exists in the Part")

    return Gate1Result(True)


# ── Gate 2 validation (Save) ──────────────────────────────────────────────────


@dataclass
class Gate2Result:
    valid: bool
    error_code: Optional[str] = None  # 'NO_VALID_COMBINATION' | 'BRANCH_ALWAYS_DUPLICATES'
    error_message: Optional[str] = None
    bad_branch: Optional[Tuple[int, int]] = None  # (part_index, option_index)


def validate_gate2(structure: RelationStructure) -> Gate2Result:
    """Save-time validation. Two checks:
    1. Branch pre-check: any single Branch (Option) that always duplicates
       with mandatory single-Option Parts → INVALID
    2. Cartesian product: at least one combination across all Parts must have
       no duplicates

    Special inputs are excluded from both checks.
    """
    if not structure.parts:
        # Empty relation is structurally valid; the caller decides whether it
        # should exist at all.
        return Gate2Result(True)

    # Step 1: Mandatory inputs (Parts with exactly one Option)
    mandatory_cn_ids: list[str] = []
    for part in structure.parts:
        if len(part.options) == 1:
            for prac in part.options[0].practices:
                if prac.common_name_cosh_id and not prac.is_special_input:
                    mandatory_cn_ids.append(prac.common_name_cosh_id)

    # Mandatory inputs must themselves not duplicate (the AND of all single-
    # Option Parts is forced).
    if len(mandatory_cn_ids) != len(set(mandatory_cn_ids)):
        return Gate2Result(
            False,
            "NO_VALID_COMBINATION",
            "Mandatory inputs across single-Option Parts duplicate each other",
        )

    # Step 2: Branch pre-check
    for part in structure.parts:
        if len(part.options) <= 1:
            continue  # No choice in this Part; handled above
        for opt in part.options:
            opt_cn_ids = [
                p.common_name_cosh_id
                for p in opt.practices
                if p.common_name_cosh_id and not p.is_special_input
            ]
            # Duplicates within the Option itself
            if len(opt_cn_ids) != len(set(opt_cn_ids)):
                return Gate2Result(
                    False,
                    "BRANCH_ALWAYS_DUPLICATES",
                    f"Option at Part {part.part_index} Option {opt.option_index} contains duplicate inputs",
                    bad_branch=(part.part_index, opt.option_index),
                )
            # Combine with mandatory; check for duplicates
            combined = opt_cn_ids + mandatory_cn_ids
            if len(combined) != len(set(combined)):
                return Gate2Result(
                    False,
                    "BRANCH_ALWAYS_DUPLICATES",
                    f"Branch at Part {part.part_index} Option {opt.option_index} always results in duplicate purchase",
                    bad_branch=(part.part_index, opt.option_index),
                )

    # Step 3: Cartesian product across all Parts — at least one combination
    # must have no duplicates.
    options_per_part = [part.options for part in structure.parts]
    for combination in product(*options_per_part):
        flat: list[str] = []
        for opt in combination:
            for prac in opt.practices:
                if prac.common_name_cosh_id and not prac.is_special_input:
                    flat.append(prac.common_name_cosh_id)
        if len(flat) == len(set(flat)):
            return Gate2Result(True)  # Found at least one valid combination

    return Gate2Result(
        False,
        "NO_VALID_COMBINATION",
        "Every possible combination of selections results in duplicate inputs",
    )


# ── Count display formula ─────────────────────────────────────────────────────


@dataclass
class CountDisplay:
    count: int
    is_max: bool  # True if "Max N items", False if "N items"

    def __str__(self) -> str:
        prefix = "Max " if self.is_max else ""
        return f"{prefix}{self.count} item{'s' if self.count != 1 else ''}"


def compute_count_display(
    structures: List[RelationStructure],
    standalone_count: int,
) -> CountDisplay:
    """Compute the count display for an order containing relations + standalone practices.

    - Standalone count: practices in the order with no relation_id
    - For each relation: max count = sum of Part max_sizes
    - is_max = True if any Part in any relation has Options of varying sizes
    """
    total = standalone_count
    is_max = False
    for s in structures:
        total += s.total_max_count
        if not s.is_count_deterministic():
            is_max = True
    return CountDisplay(count=total, is_max=is_max)
