"""BL-16 — pure-function tests for the crop-record helpers.

Live router wiring is exercised by the integration tests in batch 2.
This file is hermetic.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from app.services.bl16_crop_record import (
    crop_record_public_url, public_record_payload,
)


# ── crop_record_public_url ────────────────────────────────────────────────────

def test_url_composes_spec_path_segments():
    """The headline URL fix. Pre-audit the route encoded `/crop/{ref}`;
    spec requires `/crop-record/{ref}`. This helper centralises the
    path so it can't drift again."""
    assert (
        crop_record_public_url("https://rootstalk.in", "PA-26-000147")
        == "https://rootstalk.in/crop-record/PA-26-000147"
    )


def test_url_tolerates_trailing_slash_on_base():
    """Defensive: a base_url with a trailing slash shouldn't double
    up — `https://rootstalk.in//crop-record/...` would 404 on most
    routers."""
    assert (
        crop_record_public_url("https://rootstalk.in/", "PA-26-000147")
        == "https://rootstalk.in/crop-record/PA-26-000147"
    )


def test_url_works_for_dev_environment():
    """The route's domain helper switches between prod and dev based
    on environment; this pin confirms the helper composes a sane URL
    in both modes (so a developer scanning a QR locally hits the
    right path)."""
    assert (
        crop_record_public_url("http://localhost:3000", "RT-26-000001")
        == "http://localhost:3000/crop-record/RT-26-000001"
    )


# ── public_record_payload ─────────────────────────────────────────────────────

def test_payload_includes_only_spec_permitted_fields():
    """The most consequential part of this audit. Spec lists exactly:
    farmer name, crop, company, start date, parameter_variable_summary,
    plus the reference_number itself. Pre-fix the live route also
    exposed farmer_district, farmer_state, package_name,
    subscription_date, status, and company_display_name alongside
    company_name. Privacy concern: location on an unauthenticated URL."""
    out = public_record_payload(
        reference_number="PA-26-000147",
        farmer_name="Ramu Krishnaswamy",
        crop_cosh_id="crop_paddy",
        company_display_name="Padmashali Seeds",
        company_full_name="Padmashali Seeds and Agro Private Limited",
        crop_start_date=datetime(2026, 5, 1, 8, 30, tzinfo=timezone.utc),
        parameter_variable_summary="Loam soil, NPK every 21 days",
    )
    assert set(out.keys()) == {
        "reference_number", "farmer_name", "crop", "company",
        "start_date", "parameter_variable_summary",
    }
    assert "farmer_district" not in out
    assert "farmer_state" not in out
    assert "package_name" not in out
    assert "subscription_date" not in out
    assert "status" not in out


def test_company_prefers_display_name_over_full_name():
    """Display name is what farmers see in the PWA; the public page
    should match. Falls back to full_name only if display_name is
    null."""
    with_display = public_record_payload(
        reference_number="x", farmer_name=None, crop_cosh_id=None,
        company_display_name="Padmashali Seeds",
        company_full_name="Padmashali Seeds and Agro Private Limited",
        crop_start_date=None, parameter_variable_summary=None,
    )
    assert with_display["company"] == "Padmashali Seeds"

    without_display = public_record_payload(
        reference_number="x", farmer_name=None, crop_cosh_id=None,
        company_display_name=None,
        company_full_name="Padmashali Seeds and Agro Private Limited",
        crop_start_date=None, parameter_variable_summary=None,
    )
    assert without_display["company"] == "Padmashali Seeds and Agro Private Limited"


def test_start_date_renders_as_iso_date_no_time_component():
    """A public traceability page doesn't need time-of-day precision.
    Pre-audit the route returned the raw datetime; this helper
    flattens to ISO date so the QR-scan landing page doesn't display
    something like '2026-05-01T08:30:00+00:00'."""
    out = public_record_payload(
        reference_number="x", farmer_name=None, crop_cosh_id=None,
        company_display_name=None, company_full_name=None,
        crop_start_date=datetime(2026, 5, 1, 8, 30, tzinfo=timezone.utc),
        parameter_variable_summary=None,
    )
    assert out["start_date"] == "2026-05-01"


def test_start_date_handles_plain_date_input():
    """Some code paths may already have a date object (vs datetime).
    The helper accepts both."""
    out = public_record_payload(
        reference_number="x", farmer_name=None, crop_cosh_id=None,
        company_display_name=None, company_full_name=None,
        crop_start_date=date(2026, 5, 1),
        parameter_variable_summary=None,
    )
    assert out["start_date"] == "2026-05-01"


def test_start_date_null_passes_through():
    """A subscription whose farmer hasn't set the start date yet —
    the field is null; render as null on the public page rather than
    the string 'None'."""
    out = public_record_payload(
        reference_number="x", farmer_name=None, crop_cosh_id=None,
        company_display_name=None, company_full_name=None,
        crop_start_date=None, parameter_variable_summary=None,
    )
    assert out["start_date"] is None


def test_parameter_variable_summary_passes_through():
    """The summary column exists on FarmerSubscriptionHistory but no
    code in the backend writes it today — so the route will pass
    None to this helper for now. Pin that the field is forwarded
    correctly when it's set, and stays null when it isn't."""
    out_present = public_record_payload(
        reference_number="x", farmer_name=None, crop_cosh_id=None,
        company_display_name=None, company_full_name=None,
        crop_start_date=None,
        parameter_variable_summary="Loam soil, NPK every 21 days",
    )
    assert out_present["parameter_variable_summary"] == "Loam soil, NPK every 21 days"

    out_absent = public_record_payload(
        reference_number="x", farmer_name=None, crop_cosh_id=None,
        company_display_name=None, company_full_name=None,
        crop_start_date=None, parameter_variable_summary=None,
    )
    assert out_absent["parameter_variable_summary"] is None
