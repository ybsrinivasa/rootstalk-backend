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
_DEFAULT_METADATA = {"scientific_name": "Oryza sativa"}


def _cosh(translations=..., metadata=..., status="active"):
    return SimpleNamespace(
        translations=_DEFAULT_TRANSLATIONS if translations is ... else translations,
        metadata_=_DEFAULT_METADATA if metadata is ... else metadata,
        status=status,
    )


def _measure(value="AREA_WISE"):
    return SimpleNamespace(measure=value)


def test_happy_path_returns_full_snapshot():
    snap = build_snapshot_from_rows(_cosh(), _measure())
    assert snap == CropSnapshot(
        name_en="Paddy", scientific_name="Oryza sativa",
        area_or_plant="AREA_WISE",
    )


def test_plant_wise_measure_carried_through():
    snap = build_snapshot_from_rows(_cosh(), _measure("PLANT_WISE"))
    assert snap.area_or_plant == "PLANT_WISE"


def test_missing_scientific_name_is_none_not_error():
    """Not all crops have scientific names — leave the snapshot
    field NULL rather than refuse to add the crop."""
    snap = build_snapshot_from_rows(
        _cosh(metadata={}), _measure(),
    )
    assert snap.scientific_name is None


def test_metadata_with_blank_scientific_name_normalises_to_none():
    """Empty string masquerading as a value would surface as a real
    string downstream — coerce to None at the snapshot boundary so
    NULL semantics are consistent."""
    snap = build_snapshot_from_rows(
        _cosh(metadata={"scientific_name": ""}), _measure(),
    )
    assert snap.scientific_name is None


def test_metadata_none_is_safe():
    """Cosh entry with no metadata at all should not error."""
    snap = build_snapshot_from_rows(_cosh(metadata=None), _measure())
    assert snap.scientific_name is None


def test_missing_cosh_row_raises_with_stable_code():
    with pytest.raises(CropSnapshotError) as ei:
        build_snapshot_from_rows(None, _measure())
    assert ei.value.code == "crop_not_in_cosh"


def test_inactive_cosh_row_raises():
    """Spec rule for CHA imports: 'an inactive global PG cannot be
    imported.' Same logic for crops — an inactive Cosh entity must
    not be addable to a company's CCA list."""
    with pytest.raises(CropSnapshotError) as ei:
        build_snapshot_from_rows(_cosh(status="inactive"), _measure())
    assert ei.value.code == "crop_inactive_in_cosh"


def test_missing_english_translation_raises():
    """Sync layer enforces 'en' on upsert — but defensive readers
    should fail loudly if it's somehow missing rather than write
    NULL silently."""
    with pytest.raises(CropSnapshotError) as ei:
        build_snapshot_from_rows(
            _cosh(translations={"kn": "ಭತ್ತ"}), _measure(),
        )
    assert ei.value.code == "crop_missing_english_name"


def test_missing_measure_raises():
    """No CropMeasure row → SA must seed area/plant typing first.
    Fail closed; never default to AREA_WISE silently because the
    consequences (volume calc, plant-wise additional elements)
    differ materially."""
    with pytest.raises(CropSnapshotError) as ei:
        build_snapshot_from_rows(_cosh(), None)
    assert ei.value.code == "crop_missing_measure"


def test_error_message_carries_actionable_guidance():
    """Each error message names the next action — the CA portal
    surfaces this verbatim, so SA sees what to do."""
    with pytest.raises(CropSnapshotError) as ei:
        build_snapshot_from_rows(_cosh(), None)
    assert "SA" in ei.value.message  # tells the CA who to escalate to
