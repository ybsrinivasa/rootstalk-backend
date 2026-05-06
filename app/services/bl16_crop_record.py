"""BL-16 — Crop History QR + Public Record (pure functions, no DB).

Spec rules:
- On-demand QR encoding URL: `rootstalk.in/crop-record/[reference_number]`.
- Public web page: NO auth.
- Page shows: farmer name, crop, company, start date,
  `parameter_variable_summary`.
- NO advisory content. NO purchase history.

The two helpers in this module own the URL composition and the
spec-permitted public-record payload shape. The live route in
`app/modules/qr/router.py` reads from the DB and passes the values
in; the helpers do the rest.

`parameter_variable_summary` source: the
`FarmerSubscriptionHistory.parameter_variable_summary` column exists
on the model but no code in the backend writes it today. Until that
writer is implemented (deferred follow-up — needs a product call on
trigger condition), the helper passes None through. The frontend
should render a graceful placeholder (e.g. "Crop record being
prepared") for null values.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional


def crop_record_public_url(base_url: str, reference_number: str) -> str:
    """Compose the spec-faithful crop-record URL.

    `base_url` is the env-aware base (e.g. `https://rootstalk.in` in
    prod, `http://localhost:3000` in dev). The route's domain helper
    in `qr/router.py` provides this — mirrors the existing
    `_base_url()` pattern from `clients/router.py`. Trailing slashes
    on `base_url` are tolerated.

    Pre-fix the QR encoded `{base}/crop/{ref}`; spec requires
    `{base}/crop-record/{ref}`. This helper centralises the path so
    the URL can never drift again.
    """
    base = base_url.rstrip("/")
    return f"{base}/crop-record/{reference_number}"


def _format_start_date(value: object) -> Optional[str]:
    """Spec wants 'start date' on the public page. Crop start dates
    are stored as `DateTime(timezone=True)` on Subscription; we render
    them as ISO date strings (YYYY-MM-DD) — a public traceability
    page doesn't need time-of-day precision."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def public_record_payload(
    *,
    reference_number: str,
    farmer_name: Optional[str],
    crop_cosh_id: Optional[str],
    company_display_name: Optional[str],
    company_full_name: Optional[str],
    crop_start_date: object,
    parameter_variable_summary: Optional[str],
) -> dict:
    """Return the public crop-record payload, restricted to the five
    spec-permitted fields plus the reference number.

    Pre-audit, the live route also leaked `farmer_district`,
    `farmer_state`, `package_name`, `subscription_date`, `status`,
    and `company_display_name` alongside `company_name`. Privacy
    concern: location fields on an unauthenticated URL. This helper
    is the trim point.

    Field choices:
    - `crop`: returned as `crop_cosh_id` for V1. Spec just says
      "crop" — frontend can resolve to a translated display name
      from Cosh if needed.
    - `company`: prefers `display_name` over `full_name` (matches
      what farmers see in the PWA). Falls back to full_name if
      display_name is null. Pre-audit both were exposed
      simultaneously.
    - `start_date`: ISO date string (no time component).
    """
    company = company_display_name or company_full_name
    return {
        "reference_number": reference_number,
        "farmer_name": farmer_name,
        "crop": crop_cosh_id,
        "company": company,
        "start_date": _format_start_date(crop_start_date),
        "parameter_variable_summary": parameter_variable_summary,
    }
