"""Image resolution + Google search query for the Diagnosis pipe
(Batch 22, 2026-05-14).

Tests the two-step image chain (image_index → symptom_image_slug
Core → {crop}_symptom_images Connect → image Core's metadata_.s3_path)
and the `build_google_search_query` helper, plus the integration into
`list_candidates`.
"""
from __future__ import annotations

import pytest

from app.modules.sync.models import CoshConnectRow, CoshCoreItem
from app.services.cosh_constants import (
    COSH_BIOLOGICAL_NAMES_CORE,
    COSH_CROP_STAGES_CORE,
    COSH_DAMAGE_SUBSYMPTOMS_CORE,
    COSH_DAMAGE_SYMPTOMS_CORE,
    COSH_IMAGE_INDEX_CONNECT,
    COSH_PEST_DIAGNOSIS_CONNECT,
    COSH_PEST_STAGES_CORE,
    COSH_PLANT_PARTS_CORE,
    COSH_PLANT_SUBPARTS_CORE,
    COSH_PRIORITY_RANK_PESTS_CORE,
    COSH_SYMPTOM_IMAGE_SLUG_CORE,
)
from app.services.pest_diagnosis_images_view import (
    build_google_search_query,
    image_connect_slug_for_crop,
    images_for_pest_diagnosis_rows,
)
from app.services.pest_diagnosis_view import list_candidates
from tests.conftest import requires_docker


# ── Shared seeds ──────────────────────────────────────────────────────────

def _core(cosh_id: str, core_type: str, name: str,
          metadata_: dict | None = None, status: str = "active") -> CoshCoreItem:
    return CoshCoreItem(
        cosh_id=cosh_id, core_type=core_type,
        translations={"en": name}, status=status,
        metadata_=metadata_,
    )


def _image_index_row(cid: str, crop_cosh_id: str, slug_cosh_id: str) -> CoshConnectRow:
    return CoshConnectRow(
        connect_id=cid, connect_type=COSH_IMAGE_INDEX_CONNECT, status="active",
        endpoints=[
            {"role": COSH_BIOLOGICAL_NAMES_CORE, "cosh_id": crop_cosh_id, "position": 1},
            {"role": COSH_SYMPTOM_IMAGE_SLUG_CORE, "cosh_id": slug_cosh_id, "position": 2},
        ],
    )


def _image_attach_row(
    cid: str, connect_type: str, *,
    diag_row_id: str, image_core_id: str, status: str = "active",
) -> CoshConnectRow:
    return CoshConnectRow(
        connect_id=cid, connect_type=connect_type, status=status,
        endpoints=[
            {"role": "pest_diagnosis", "cosh_id": diag_row_id, "position": 1},
            {"role": "images_tomato_symptoms_core", "cosh_id": image_core_id, "position": 2},
        ],
    )


def _pd_row(
    cid: str, *, crop: str, pest: str, pest_stage: str, rank: str,
    symptom: str = "sym:default", subsymptom: str = "sub:default",
    plant_part: str = "pp:default", plant_subpart: str = "psp:default",
    crop_stage: str = "cs:default", status: str = "active",
) -> CoshConnectRow:
    return CoshConnectRow(
        connect_id=cid, connect_type=COSH_PEST_DIAGNOSIS_CONNECT, status=status,
        endpoints=[
            {"role": COSH_DAMAGE_SYMPTOMS_CORE,     "cosh_id": symptom,       "position": 1},
            {"role": COSH_DAMAGE_SUBSYMPTOMS_CORE,  "cosh_id": subsymptom,    "position": 2},
            {"role": COSH_BIOLOGICAL_NAMES_CORE,    "cosh_id": pest,          "position": 3},
            {"role": COSH_PEST_STAGES_CORE,         "cosh_id": pest_stage,    "position": 4},
            {"role": COSH_PLANT_PARTS_CORE,         "cosh_id": plant_part,    "position": 5},
            {"role": COSH_PLANT_SUBPARTS_CORE,      "cosh_id": plant_subpart, "position": 6},
            {"role": COSH_BIOLOGICAL_NAMES_CORE,    "cosh_id": crop,          "position": 7},
            {"role": COSH_CROP_STAGES_CORE,         "cosh_id": crop_stage,    "position": 8},
            {"role": COSH_PRIORITY_RANK_PESTS_CORE, "cosh_id": rank,          "position": 9},
        ],
    )


async def _seed_minimal_world(db, *, with_image_index: bool = True) -> None:
    """A small Cosh world: tomato with one Aphid-on-Leaf diagnosis,
    plus optional image_index wiring + two attached images."""
    # Cores
    db.add(_core("crop:tomato", COSH_BIOLOGICAL_NAMES_CORE, "Tomato"))
    db.add(_core("crop:chilli", COSH_BIOLOGICAL_NAMES_CORE, "Chilli"))
    db.add(_core("pest:aphid", COSH_BIOLOGICAL_NAMES_CORE, "Aphid"))
    db.add(_core("pest:whitefly", COSH_BIOLOGICAL_NAMES_CORE, "Whitefly"))
    db.add(_core("sym:curl", COSH_DAMAGE_SYMPTOMS_CORE, "Leaf Curl"))
    db.add(_core("sub:upward", COSH_DAMAGE_SUBSYMPTOMS_CORE, "Upward Curl"))
    db.add(_core("ps:adult", COSH_PEST_STAGES_CORE, "Adult"))
    db.add(_core("pp:leaf", COSH_PLANT_PARTS_CORE, "Leaf"))
    db.add(_core("psp:lamina", COSH_PLANT_SUBPARTS_CORE, "Lamina"))
    db.add(_core("cs:flowering", COSH_CROP_STAGES_CORE, "Flowering"))
    db.add(_core("rank:1", COSH_PRIORITY_RANK_PESTS_CORE, "1"))
    db.add(_core("rank:2", COSH_PRIORITY_RANK_PESTS_CORE, "2"))

    # Two pest_diagnosis rows. Both on Tomato/Leaf, both targeting
    # Aphid+adult, but differing on subsymptom — they'll collapse
    # into one candidate.
    db.add(_pd_row(
        "diag:aphid:1",
        crop="crop:tomato", pest="pest:aphid", pest_stage="ps:adult",
        rank="rank:1", symptom="sym:curl", subsymptom="sub:upward",
        plant_part="pp:leaf", plant_subpart="psp:lamina",
        crop_stage="cs:flowering",
    ))
    db.add(_pd_row(
        "diag:aphid:2",
        crop="crop:tomato", pest="pest:aphid", pest_stage="ps:adult",
        rank="rank:2", symptom="sym:curl", subsymptom="sub:upward",
        plant_part="pp:leaf", plant_subpart="psp:lamina",
        crop_stage="cs:flowering",
    ))
    # One Whitefly row — used to confirm a candidate with no images
    # still surfaces with empty image_urls.
    db.add(_pd_row(
        "diag:wf:1",
        crop="crop:tomato", pest="pest:whitefly", pest_stage="ps:adult",
        rank="rank:2", symptom="sym:curl", subsymptom="sub:upward",
        plant_part="pp:leaf", plant_subpart="psp:lamina",
        crop_stage="cs:flowering",
    ))

    if with_image_index:
        # image_index: Tomato → symptom_image_slug Core item whose
        # en translation is the per-crop image Connect slug.
        db.add(_core("slug:tomato", COSH_SYMPTOM_IMAGE_SLUG_CORE, "tomato_symptom_images"))
        db.add(_image_index_row("ii:tomato", "crop:tomato", "slug:tomato"))

        # Image Cores with s3 URLs. Note: core_type is the per-crop
        # variant (images_tomato_symptoms_core) — opaque to RootsTalk.
        db.add(_core(
            "img:aphid:a", "images_tomato_symptoms_core", "Aphid_Leaf 1",
            metadata_={"s3_path": "https://cosh-media-prod1.s3.ap-south-1.amazonaws.com/A.jpg",
                       "media_type": "image"},
        ))
        db.add(_core(
            "img:aphid:b", "images_tomato_symptoms_core", "Aphid_Leaf 2",
            metadata_={"s3_path": "https://cosh-media-prod1.s3.ap-south-1.amazonaws.com/B.jpg",
                       "media_type": "image"},
        ))
        # Two images attached to diag:aphid:1, none on diag:aphid:2,
        # none on diag:wf:1.
        db.add(_image_attach_row(
            "att:1", "tomato_symptom_images",
            diag_row_id="diag:aphid:1", image_core_id="img:aphid:a",
        ))
        db.add(_image_attach_row(
            "att:2", "tomato_symptom_images",
            diag_row_id="diag:aphid:1", image_core_id="img:aphid:b",
        ))
    await db.commit()


# ── image_connect_slug_for_crop ───────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_image_connect_slug_for_crop_resolves_when_indexed(db):
    await _seed_minimal_world(db)
    slug = await image_connect_slug_for_crop(db, "crop:tomato")
    assert slug == "tomato_symptom_images"


@requires_docker
@pytest.mark.asyncio
async def test_image_connect_slug_for_crop_returns_none_when_not_indexed(db):
    await _seed_minimal_world(db)
    # Chilli has no image_index row.
    slug = await image_connect_slug_for_crop(db, "crop:chilli")
    assert slug is None


@requires_docker
@pytest.mark.asyncio
async def test_image_connect_slug_for_crop_returns_none_when_slug_inactive(db):
    await _seed_minimal_world(db)
    from sqlalchemy import select
    slug_core = (await db.execute(
        select(CoshCoreItem).where(CoshCoreItem.cosh_id == "slug:tomato")
    )).scalar_one()
    slug_core.status = "inactive"
    await db.commit()
    slug = await image_connect_slug_for_crop(db, "crop:tomato")
    assert slug is None


# ── images_for_pest_diagnosis_rows ────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_images_for_diagnosis_rows_happy_path(db):
    await _seed_minimal_world(db)
    result = await images_for_pest_diagnosis_rows(
        db, crop_cosh_id="crop:tomato",
        diag_row_ids={"diag:aphid:1", "diag:aphid:2"},
    )
    assert "diag:aphid:1" in result
    assert "diag:aphid:2" not in result  # no attached images
    assert result["diag:aphid:1"] == [
        "https://cosh-media-prod1.s3.ap-south-1.amazonaws.com/A.jpg",
        "https://cosh-media-prod1.s3.ap-south-1.amazonaws.com/B.jpg",
    ]


@requires_docker
@pytest.mark.asyncio
async def test_images_empty_when_crop_not_indexed(db):
    await _seed_minimal_world(db)
    out = await images_for_pest_diagnosis_rows(
        db, crop_cosh_id="crop:chilli", diag_row_ids={"diag:aphid:1"},
    )
    assert out == {}


@requires_docker
@pytest.mark.asyncio
async def test_images_skip_inactive_core(db):
    await _seed_minimal_world(db)
    from sqlalchemy import select
    img = (await db.execute(
        select(CoshCoreItem).where(CoshCoreItem.cosh_id == "img:aphid:a")
    )).scalar_one()
    img.status = "inactive"
    await db.commit()
    out = await images_for_pest_diagnosis_rows(
        db, crop_cosh_id="crop:tomato", diag_row_ids={"diag:aphid:1"},
    )
    # Only B survives.
    assert out["diag:aphid:1"] == [
        "https://cosh-media-prod1.s3.ap-south-1.amazonaws.com/B.jpg",
    ]


@requires_docker
@pytest.mark.asyncio
async def test_images_skip_inactive_attachment(db):
    await _seed_minimal_world(db)
    from sqlalchemy import select
    att = (await db.execute(
        select(CoshConnectRow).where(CoshConnectRow.connect_id == "att:1")
    )).scalar_one()
    att.status = "inactive"
    await db.commit()
    out = await images_for_pest_diagnosis_rows(
        db, crop_cosh_id="crop:tomato", diag_row_ids={"diag:aphid:1"},
    )
    # Only B survives.
    assert out["diag:aphid:1"] == [
        "https://cosh-media-prod1.s3.ap-south-1.amazonaws.com/B.jpg",
    ]


@requires_docker
@pytest.mark.asyncio
async def test_images_skip_when_s3_path_missing(db):
    await _seed_minimal_world(db)
    # Strip metadata on image A — degenerate Cosh data; expect to be
    # silently skipped, not error.
    from sqlalchemy import select
    img = (await db.execute(
        select(CoshCoreItem).where(CoshCoreItem.cosh_id == "img:aphid:a")
    )).scalar_one()
    img.metadata_ = {"media_type": "image"}  # no s3_path
    await db.commit()
    out = await images_for_pest_diagnosis_rows(
        db, crop_cosh_id="crop:tomato", diag_row_ids={"diag:aphid:1"},
    )
    assert out["diag:aphid:1"] == [
        "https://cosh-media-prod1.s3.ap-south-1.amazonaws.com/B.jpg",
    ]


# ── list_candidates with images ───────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_candidates_carry_image_urls(db):
    await _seed_minimal_world(db)
    cands = await list_candidates(
        db, crop_cosh_id="crop:tomato",
        plant_part="pp:leaf", symptom="sym:curl",
    )
    by_name = {c["pest_name"]: c for c in cands}
    # Aphid candidate dedups two pest_diagnosis rows; only diag:aphid:1
    # has attached images → both A.jpg and B.jpg surface.
    assert by_name["Aphid"]["image_urls"] == [
        "https://cosh-media-prod1.s3.ap-south-1.amazonaws.com/A.jpg",
        "https://cosh-media-prod1.s3.ap-south-1.amazonaws.com/B.jpg",
    ]
    # Whitefly has no curated images → empty list, field still present.
    assert by_name["Whitefly"]["image_urls"] == []


@requires_docker
@pytest.mark.asyncio
async def test_candidates_image_urls_empty_when_crop_not_indexed(db):
    await _seed_minimal_world(db, with_image_index=False)
    cands = await list_candidates(db, crop_cosh_id="crop:tomato")
    assert cands  # candidates still surface
    for c in cands:
        assert c["image_urls"] == []


@requires_docker
@pytest.mark.asyncio
async def test_candidates_image_urls_dedup_across_contributing_rows(db):
    """If two diagnosis rows that collapse into one candidate both
    reference the SAME image, the URL must surface once not twice."""
    await _seed_minimal_world(db)
    # Add an extra attachment on diag:aphid:2 pointing at the same
    # image as one of diag:aphid:1's.
    db.add(_image_attach_row(
        "att:dup", "tomato_symptom_images",
        diag_row_id="diag:aphid:2", image_core_id="img:aphid:a",
    ))
    await db.commit()
    cands = await list_candidates(
        db, crop_cosh_id="crop:tomato",
        plant_part="pp:leaf", symptom="sym:curl",
    )
    aphid = next(c for c in cands if c["pest_name"] == "Aphid")
    # A.jpg present once (not twice), B.jpg present once.
    assert aphid["image_urls"].count(
        "https://cosh-media-prod1.s3.ap-south-1.amazonaws.com/A.jpg"
    ) == 1
    assert sorted(aphid["image_urls"]) == sorted([
        "https://cosh-media-prod1.s3.ap-south-1.amazonaws.com/A.jpg",
        "https://cosh-media-prod1.s3.ap-south-1.amazonaws.com/B.jpg",
    ])


# ── build_google_search_query ─────────────────────────────────────────────

def test_google_query_full_context():
    q = build_google_search_query(
        crop_name="Tomato", plant_part_name="Leaf", symptom_name="Leaf Curl",
    )
    assert q == "Tomato Leaf Leaf Curl"


def test_google_query_drops_unpinned_dimensions():
    q = build_google_search_query(
        crop_name="Tomato", plant_part_name=None, symptom_name="Leaf Curl",
    )
    assert q == "Tomato Leaf Curl"


def test_google_query_crop_only():
    q = build_google_search_query(
        crop_name="Tomato", plant_part_name=None, symptom_name=None,
    )
    assert q == "Tomato"


def test_google_query_empty_when_nothing_resolves():
    q = build_google_search_query(
        crop_name=None, plant_part_name=None, symptom_name=None,
    )
    assert q == ""
