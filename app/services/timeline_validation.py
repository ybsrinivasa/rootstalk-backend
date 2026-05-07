"""CCA Step 3 / Batch 3-Hardening — Timeline field validation.

Three rules from spec §5 enforced together at create / update / import:

1. **Direction** (pre-existing, kept here for cohesion):
   - DBS: `from_value` > `to_value` (e.g. 15 → 8 DBS).
   - DAS / CALENDAR: `to_value` > `from_value` (e.g. 0 → 8 DAS).

2. **Sign** (new): "Pre-start (DBS) and post-start (DAS) timelines
   are strictly separate — no timeline spans both." Per the model
   `from_type` is one enum value per Timeline, so cross-spanning a
   single Timeline isn't representable. The remaining gap is the
   *sign* of the values inside one type:
   - DBS: both values strictly positive (days BEFORE start).
   - DAS: both values non-negative (start day onwards; from=0 is
     the start day).
   - CALENDAR: no sign rule (values are day-of-year ints).

3. **Type ↔ Package consistency** (new):
   - Annual package: DBS or DAS only.
   - Perennial package: CALENDAR only.

Pre-fix the live router only checked direction; sign and type were
silently accepted, so a CA could ship a Timeline with `from=0,
to=-5` (DBS sneaking past start) or a CALENDAR Timeline on an
Annual package (semantically meaningless).

Pure-function `validate_timeline` runs all three in order: type →
direction → sign. The route layer maps `TimelineValidationError`
(stable `code`) to a 422.
"""
from __future__ import annotations


PACKAGE_TYPE_ANNUAL = "ANNUAL"
PACKAGE_TYPE_PERENNIAL = "PERENNIAL"

FROM_TYPE_DBS = "DBS"
FROM_TYPE_DAS = "DAS"
FROM_TYPE_CALENDAR = "CALENDAR"


class TimelineValidationError(Exception):
    """Raised when timeline field validation fails. `code` is a
    stable identifier the route layer maps to a 422 response."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def validate_timeline_direction(
    *, from_type: str, from_value: int, to_value: int,
) -> None:
    """DBS: from > to. DAS / CALENDAR: to > from."""
    if from_type == FROM_TYPE_DBS:
        if to_value >= from_value:
            raise TimelineValidationError(
                "timeline_invalid_direction",
                f"DBS timeline: from_value ({from_value}) must be greater "
                f"than to_value ({to_value}).",
            )
    else:
        if to_value <= from_value:
            raise TimelineValidationError(
                "timeline_invalid_direction",
                f"{from_type} timeline: to_value ({to_value}) must be "
                f"greater than from_value ({from_value}).",
            )


def validate_timeline_sign(
    *, from_type: str, from_value: int, to_value: int,
) -> None:
    """DBS: both values strictly positive. DAS: both values
    non-negative. CALENDAR: passes through (day-of-year semantics)."""
    if from_type == FROM_TYPE_DBS:
        if from_value <= 0 or to_value <= 0:
            raise TimelineValidationError(
                "timeline_invalid_sign",
                "DBS timeline values must be strictly positive (days "
                "before crop start). Use DAS for the start day onwards.",
            )
    elif from_type == FROM_TYPE_DAS:
        if from_value < 0 or to_value < 0:
            raise TimelineValidationError(
                "timeline_invalid_sign",
                "DAS timeline values must be non-negative (start day "
                "onwards). Use DBS for days before crop start.",
            )
    # CALENDAR: no sign rule.


def validate_timeline_type_for_package(
    *, package_type: str, from_type: str,
) -> None:
    """Annual: DBS or DAS only. Perennial: CALENDAR only."""
    if package_type == PACKAGE_TYPE_ANNUAL:
        if from_type not in (FROM_TYPE_DBS, FROM_TYPE_DAS):
            raise TimelineValidationError(
                "timeline_type_mismatch",
                "Annual packages support DBS or DAS timelines only. "
                "Use a Perennial package for CALENDAR timelines.",
            )
    elif package_type == PACKAGE_TYPE_PERENNIAL:
        if from_type != FROM_TYPE_CALENDAR:
            raise TimelineValidationError(
                "timeline_type_mismatch",
                "Perennial packages support CALENDAR timelines only. "
                "Use an Annual package for DBS or DAS timelines.",
            )


def validate_timeline(
    *, package_type: str, from_type: str, from_value: int, to_value: int,
) -> None:
    """Run all three checks in order: type → direction → sign.
    Raises `TimelineValidationError` on the first failure."""
    validate_timeline_type_for_package(
        package_type=package_type, from_type=from_type,
    )
    validate_timeline_direction(
        from_type=from_type, from_value=from_value, to_value=to_value,
    )
    validate_timeline_sign(
        from_type=from_type, from_value=from_value, to_value=to_value,
    )
