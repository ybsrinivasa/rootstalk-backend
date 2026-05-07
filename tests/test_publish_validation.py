"""Pure-function tests for `validate_publish_readiness`.

Integration coverage of the API surface (publish_package returning
the consolidated 422 with a missing-checklist body) lives in
`tests/test_phase_cca_step2_integration.py`.
"""
from __future__ import annotations

from app.services.publish_validation import validate_publish_readiness


def _full_package() -> dict:
    """A package dict that satisfies every mandatory-field rule.
    Tests mutate this base to test specific missing-field cases."""
    return {
        "name": "Paddy Kharif PoP",
        "package_type": "ANNUAL",
        "duration_days": 120,
        "start_date_label_cosh_id": "label:sowing_date",
    }


def _codes(missing) -> set[str]:
    """Compact accessor: just the codes from the missing list."""
    return {m.code for m in missing}


# ── Happy path ───────────────────────────────────────────────────────────────

def test_all_fields_set_no_siblings_returns_empty():
    out = validate_publish_readiness(
        package=_full_package(),
        location_count=2, author_count=1, has_pv=True,
        siblings_with_shared_districts=[],
    )
    assert out == []


def test_no_pv_with_no_siblings_is_ok():
    """Spec §4.2: P/V is optional for the FIRST PoP. With no siblings
    sharing a district, empty PV is allowed."""
    out = validate_publish_readiness(
        package=_full_package(),
        location_count=1, author_count=1, has_pv=False,
        siblings_with_shared_districts=[],
    )
    assert out == []


# ── Mandatory-field misses ───────────────────────────────────────────────────

def test_missing_name_surfaced():
    pkg = _full_package(); pkg["name"] = None
    out = validate_publish_readiness(
        package=pkg, location_count=1, author_count=1, has_pv=True,
        siblings_with_shared_districts=[],
    )
    assert _codes(out) == {"missing_name"}


def test_missing_package_type_surfaced():
    pkg = _full_package(); pkg["package_type"] = None
    out = validate_publish_readiness(
        package=pkg, location_count=1, author_count=1, has_pv=True,
        siblings_with_shared_districts=[],
    )
    assert _codes(out) == {"missing_package_type"}


def test_missing_duration_surfaced():
    """0 and None both treated as missing — duration must be a
    real positive number."""
    for bad in (None, 0):
        pkg = _full_package(); pkg["duration_days"] = bad
        out = validate_publish_readiness(
            package=pkg, location_count=1, author_count=1, has_pv=True,
            siblings_with_shared_districts=[],
        )
        assert _codes(out) == {"missing_duration"}


def test_missing_start_date_label_surfaced():
    pkg = _full_package(); pkg["start_date_label_cosh_id"] = None
    out = validate_publish_readiness(
        package=pkg, location_count=1, author_count=1, has_pv=True,
        siblings_with_shared_districts=[],
    )
    assert _codes(out) == {"missing_start_date_label"}


def test_no_locations_surfaced():
    out = validate_publish_readiness(
        package=_full_package(),
        location_count=0, author_count=1, has_pv=True,
        siblings_with_shared_districts=[],
    )
    assert _codes(out) == {"no_locations"}


def test_no_authors_surfaced():
    out = validate_publish_readiness(
        package=_full_package(),
        location_count=1, author_count=0, has_pv=True,
        siblings_with_shared_districts=[],
    )
    assert _codes(out) == {"no_authors"}


def test_multiple_misses_all_surfaced():
    """The whole point of the comprehensive design — one publish
    attempt surfaces every problem so the CA fixes them in one pass."""
    pkg = _full_package(); pkg["name"] = None
    out = validate_publish_readiness(
        package=pkg, location_count=0, author_count=0, has_pv=True,
        siblings_with_shared_districts=[],
    )
    assert _codes(out) == {"missing_name", "no_locations", "no_authors"}


# ── §4.2 second-PoP rule ─────────────────────────────────────────────────────

def _shared_sib(*, has_pv: bool, name: str = "Sib PoP", id_: str = "sib1") -> dict:
    return {
        "id": id_, "name": name, "has_pv": has_pv,
        "shared_districts": [
            {"state_cosh_id": "S1", "district_cosh_id": "D1"},
        ],
    }


def test_no_pv_with_shared_district_sibling_surfaced():
    """Spec §4.2: when a 2nd PoP shares a district, both must have
    P/V. This PoP has none → block with the self-side code."""
    out = validate_publish_readiness(
        package=_full_package(),
        location_count=1, author_count=1, has_pv=False,
        siblings_with_shared_districts=[_shared_sib(has_pv=True)],
    )
    assert "no_pv_with_shared_district_sibling" in _codes(out)


def test_sibling_has_no_pv_surfaced_with_extra():
    """When THIS PoP has PV but a shared-district sibling doesn't,
    surface that with sibling info attached so the portal can name
    the offending PoP."""
    out = validate_publish_readiness(
        package=_full_package(),
        location_count=1, author_count=1, has_pv=True,
        siblings_with_shared_districts=[_shared_sib(has_pv=False, name="Bad Sib")],
    )
    codes = _codes(out)
    assert codes == {"sibling_has_no_pv"}
    [violation] = [m for m in out if m.code == "sibling_has_no_pv"]
    assert violation.extra == {
        "sibling_package_id": "sib1",
        "sibling_package_name": "Bad Sib",
        "shared_districts": [
            {"state_cosh_id": "S1", "district_cosh_id": "D1"},
        ],
    }


def test_both_self_and_sibling_lacking_pv_surfaces_both():
    """Defensive case (save-time guards should have caught this,
    but if we reach publish in this state, surface both sides)."""
    out = validate_publish_readiness(
        package=_full_package(),
        location_count=1, author_count=1, has_pv=False,
        siblings_with_shared_districts=[_shared_sib(has_pv=False)],
    )
    assert "no_pv_with_shared_district_sibling" in _codes(out)
    assert "sibling_has_no_pv" in _codes(out)


def test_multiple_shared_district_siblings_each_lacking_pv_listed():
    out = validate_publish_readiness(
        package=_full_package(),
        location_count=1, author_count=1, has_pv=True,
        siblings_with_shared_districts=[
            _shared_sib(has_pv=False, name="Sib A", id_="a"),
            _shared_sib(has_pv=False, name="Sib B", id_="b"),
            _shared_sib(has_pv=True, name="Good Sib", id_="c"),
        ],
    )
    sibling_ids = {
        m.extra["sibling_package_id"] for m in out
        if m.code == "sibling_has_no_pv"
    }
    assert sibling_ids == {"a", "b"}
