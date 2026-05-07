"""CCA Step 2 / Batch 2A — Package field validation.

Spec §4.1 mandates duration constraints differ by Package Type:

  Annual:    1 ≤ duration_days ≤ 365, expert-entered, mandatory.
  Perennial: 365 days, system-set, not editable after save.

Pre-Batch-2A the live router defaulted Annual to 180 silently when
the field was missing, never range-checked, and the update path
blindly setattr'd whatever value was sent — which meant a Perennial
package's duration could be flipped to a different number, breaking
advisory alignment downstream.

Both validators raise `PackageValidationError(code, message)` and
the route layer maps to a 422 with a stable `code` so the CA portal
can surface the right message per failure.
"""
from __future__ import annotations

from typing import Optional


PACKAGE_TYPE_ANNUAL = "ANNUAL"
PACKAGE_TYPE_PERENNIAL = "PERENNIAL"


class PackageValidationError(Exception):
    """Raised when package field validation fails. `code` is a stable
    identifier the route layer maps to a 422 response."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def validate_package_duration_for_create(
    *, package_type: str, duration_days: Optional[int],
) -> int:
    """Return the validated `duration_days` for a fresh Package.

    Perennial: input is ignored, always returns 365 (spec §4.1).
    Annual: `duration_days` is mandatory and must be 1-365 inclusive.
    """
    if package_type == PACKAGE_TYPE_PERENNIAL:
        return 365
    if duration_days is None:
        raise PackageValidationError(
            "duration_required",
            "Annual packages require duration_days (1-365).",
        )
    if not (1 <= duration_days <= 365):
        raise PackageValidationError(
            "duration_out_of_range",
            f"duration_days must be 1-365 for Annual packages; got {duration_days}.",
        )
    return duration_days


def validate_package_duration_for_update(
    *, package_type: str, current_duration: int, new_duration: Optional[int],
) -> int:
    """Return the validated `duration_days` for an existing Package.

    Perennial: locked at 365. Re-sending 365 is accepted (clients that
    always send the full PackageUpdate body don't fail); any other
    value is rejected.
    Annual: must be 1-365 inclusive if changed.
    `new_duration is None` means the field wasn't sent — keep current.
    """
    if new_duration is None:
        return current_duration
    if package_type == PACKAGE_TYPE_PERENNIAL:
        if new_duration != 365:
            raise PackageValidationError(
                "perennial_duration_locked",
                "Perennial packages have a fixed 365-day duration; cannot be changed.",
            )
        return 365
    if not (1 <= new_duration <= 365):
        raise PackageValidationError(
            "duration_out_of_range",
            f"duration_days must be 1-365 for Annual packages; got {new_duration}.",
        )
    return new_duration
