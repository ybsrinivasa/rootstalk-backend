"""Pure-function tests for `build_snapshot_from_rows`.

Integration coverage of the actual fetch + the CA-add 422 paths
lives in `tests/test_phase_cca_step1_integration.py`.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.crop_snapshot import (
    CropSnapshot, CropSnapshotError, build_snapshot_from_rows,
)


_DEFAULT_TRANSLATIONS = {"en": "Paddy"}


def _cosh(translations=..., status="active"):
    return SimpleNamespace(
        translations=_DEFAULT_TRANSLATIONS if translations is ... else translations,
        status=status,
    )


def test_happy_path_returns_full_snapshot():
    """V1 (post 2026-05-09 live Cosh sync): scientific_name is None
    until the Scientific Names Connect ships separately."""
    snap = build_snapshot_from_rows(_cosh(), "AREA_WISE")
    assert snap == CropSnapshot(
        name_en="Paddy", scientific_name=None,
        area_or_plant="AREA_WISE",
    )


def test_plant_wise_measure_carried_through():
    snap = build_snapshot_from_rows(_cosh(), "PLANT_WISE")
    assert snap.area_or_plant == "PLANT_WISE"


def test_scientific_name_is_always_none_in_v1():
    """V1 doesn't source scientific names from Cosh — separate Connect
    will land that data later. Confirm the snapshot consistently
    returns None regardless of any legacy metadata field on cosh_row."""
    snap = build_snapshot_from_rows(_cosh(), "AREA_WISE")
    assert snap.scientific_name is None


def test_missing_cosh_row_raises_with_stable_code():
    with pytest.raises(CropSnapshotError) as ei:
        build_snapshot_from_rows(None, "AREA_WISE")
    assert ei.value.code == "crop_not_in_cosh"


def test_inactive_cosh_row_raises():
    """Spec rule for CHA imports: 'an inactive global PG cannot be
    imported.' Same logic for crops — an inactive Cosh entity must
    not be addable to a company's CCA list."""
    with pytest.raises(CropSnapshotError) as ei:
        build_snapshot_from_rows(_cosh(status="inactive"), "AREA_WISE")
    assert ei.value.code == "crop_inactive_in_cosh"


def test_missing_english_translation_raises():
    """Sync layer enforces 'en' on upsert — but defensive readers
    should fail loudly if it's somehow missing rather than write
    NULL silently."""
    with pytest.raises(CropSnapshotError) as ei:
        build_snapshot_from_rows(
            _cosh(translations={"kn": "ಭತ್ತ"}), "AREA_WISE",
        )
    assert ei.value.code == "crop_missing_english_name"


def test_missing_measure_raises():
    """measure=None → Cosh hasn't classified Area/Plant typing yet.
    Fail closed; never default to AREA_WISE silently because the
    consequences (volume calc, plant-wise additional elements)
    differ materially."""
    with pytest.raises(CropSnapshotError) as ei:
        build_snapshot_from_rows(_cosh(), None)
    assert ei.value.code == "crop_missing_measure"


def test_error_message_names_the_remediation_path():
    """The error message must point at the Cosh-side fix — RootsTalk
    can no longer write the measure locally; the curator must tag it
    via the `crop_area_plant_wise` Connect."""
    with pytest.raises(CropSnapshotError) as ei:
        build_snapshot_from_rows(_cosh(), None)
    assert "Cosh" in ei.value.message
    assert "crop_area_plant_wise" in ei.value.message
