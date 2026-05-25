"""Reference image lookup + Google fallback tests (5C + 5D).

Covers:
  • find_reference_images() walks pest_diagnosis_chain rows matching
    the question filter, then any Connect rows linking those rows to
    media items via {role: pest_diagnosis_chain, role: media} endpoint
    pairs — regardless of the image Connect's connect_type name.
  • Multiple image Connects per crop are supported (e.g. tomato_pest_images,
    paddy_pest_images) — connect_type-agnostic lookup.
  • Empty result when no curated images exist.
  • Google Images URL is built from translated terms; falls back to
    English when the requested language has no translation.
  • End-to-end via the /diagnosis/reference-images endpoint.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.modules.diagnosis.router import (
    ReferenceImagesRequest, get_reference_images,
)
from app.modules.sync.models import CoshConnectRow, CoshCoreItem
from app.services.diagnosis_images import (
    build_google_images_query, find_reference_images, google_images_url,
)
from tests.conftest import requires_docker
from tests.factories import make_user


# ── Seed helpers ────────────────────────────────────────────────────────────

CROP = "crop:tomato"
STAGE = "stage:fruiting"
PART = "part:leaf"
SYMPTOM = "sym:white_spots"


async def _seed_diagnosis_row(
    db, *, connect_id, pest, crop=CROP, crop_stage=STAGE,
    part=PART, symptom=SYMPTOM, sub_part=None, sub_symptom=None,
):
    """Build a real-shape `pest_diagnosis` row (9 positions). Uses the
    same helper logic as test_phase_bl08_diagnosis_integration.py's
    _pd_row, kept local here for test independence."""
    from app.services.cosh_constants import (
        PD_BLANK_BOX_BY_CORE,
        COSH_DAMAGE_SUBSYMPTOMS_CORE, COSH_PLANT_SUBPARTS_CORE,
        COSH_CROP_STAGES_CORE,
    )
    subsymptom_blank = PD_BLANK_BOX_BY_CORE[COSH_DAMAGE_SUBSYMPTOMS_CORE]
    subpart_blank = PD_BLANK_BOX_BY_CORE[COSH_PLANT_SUBPARTS_CORE]
    crop_stage_blank = PD_BLANK_BOX_BY_CORE[COSH_CROP_STAGES_CORE]
    db.add(CoshConnectRow(
        connect_id=connect_id,
        connect_type="pest_diagnosis",
        status="active",
        endpoints=[
            {"role": "damage_symptoms",    "cosh_id": symptom, "position": 1},
            {"role": "damage_subsymptoms", "cosh_id": sub_symptom or subsymptom_blank, "position": 2},
            {"role": "biological_names",   "cosh_id": pest, "position": 3},
            {"role": "pest_stages",        "cosh_id": "pest_stage:any", "position": 4},
            {"role": "plant_parts",        "cosh_id": part, "position": 5},
            {"role": "plant_subparts",     "cosh_id": sub_part or subpart_blank, "position": 6},
            {"role": "biological_names",   "cosh_id": crop, "position": 7},
            {"role": "crop_stages",        "cosh_id": crop_stage or crop_stage_blank, "position": 8},
        ],
        metadata_=None,
    ))


async def _seed_image_index(db, *, crop, slug_cosh_id):
    """One image_index row per crop pointing at its per-crop image
    Connect's slug Core item."""
    db.add(CoshConnectRow(
        connect_id=f"ii:{crop}",
        connect_type="image_index",
        status="active",
        endpoints=[
            {"role": "biological_names",    "cosh_id": crop, "position": 1},
            {"role": "symptom_image_slug",  "cosh_id": slug_cosh_id, "position": 2},
        ],
        metadata_=None,
    ))


async def _seed_slug_core(db, *, cosh_id, slug):
    """The slug Core item — its translations.en is the literal
    Connect slug the per-crop image Connect rows ship under."""
    db.add(CoshCoreItem(
        cosh_id=cosh_id,
        core_type="symptom_image_slug",
        status="active",
        translations={"en": slug},
        metadata_=None,
    ))


async def _seed_image_link(db, *, connect_id, connect_type,
                           pest_diagnosis_id, media_id):
    """Per-crop image Connect row: pos 1 = pest_diagnosis row id,
    pos 2 = image Core cosh_id."""
    db.add(CoshConnectRow(
        connect_id=connect_id,
        connect_type=connect_type,
        status="active",
        endpoints=[
            {"role": "diagnosis_row", "cosh_id": pest_diagnosis_id, "position": 1},
            {"role": "image",         "cosh_id": media_id, "position": 2},
        ],
        metadata_=None,
    ))


async def _seed_media(db, *, cosh_id, s3_path, en_caption, kn_caption=None):
    """Image Core item — carries the S3 URL in metadata_.s3_path."""
    translations = {"en": en_caption}
    if kn_caption:
        translations["kn"] = kn_caption
    db.add(CoshCoreItem(
        cosh_id=cosh_id,
        core_type="image",
        status="active",
        translations=translations,
        metadata_={"s3_path": s3_path, "media_type": "image"},
    ))


async def _seed_image_index_for_crop(db, *, crop, slug):
    """Convenience: wire up the image_index + slug Core in one call.
    Returns the slug string the per-crop image Connect should use."""
    slug_cosh_id = f"slug:{crop}"
    await _seed_slug_core(db, cosh_id=slug_cosh_id, slug=slug)
    await _seed_image_index(db, crop=crop, slug_cosh_id=slug_cosh_id)
    return slug


async def _seed_core_translation(db, *, cosh_id, core_type,
                                 en, kn=None, hi=None):
    translations = {"en": en}
    if kn:
        translations["kn"] = kn
    if hi:
        translations["hi"] = hi
    db.add(CoshCoreItem(
        cosh_id=cosh_id, core_type=core_type, status="active",
        translations=translations, metadata_=None,
    ))


# ── find_reference_images ───────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_returns_image_for_matching_question(db):
    await _seed_diagnosis_row(db, connect_id="pdc:r1", pest="pest:fruit_borer")
    slug = await _seed_image_index_for_crop(db, crop=CROP, slug="tomato_symptom_images")
    await _seed_media(db, cosh_id="med:img1",
                      s3_path="s3://rootstalk-images/tomato/leaf_white_spots_1.jpg",
                      en_caption="White spots on tomato leaf — early stage")
    await _seed_image_link(db,
                           connect_id="tpi:link1",
                           connect_type=slug,
                           pest_diagnosis_id="pdc:r1",
                           media_id="med:img1")
    await db.commit()

    result = await find_reference_images(
        db,
        crop_cosh_id=CROP, part_cosh_id=PART, symptom_cosh_id=SYMPTOM,
    )
    assert len(result) == 1
    assert result[0].cosh_id == "med:img1"
    assert "tomato/leaf_white_spots_1.jpg" in result[0].url
    assert result[0].media_type == "image"
    assert "White spots" in result[0].caption


@requires_docker
@pytest.mark.asyncio
async def test_multiple_diagnosis_rows_share_images(db):
    """Two different pests on the same (crop, part, symptom) — images
    linked via separate diagnosis rows are unioned."""
    await _seed_diagnosis_row(db, connect_id="pdc:r1", pest="pest:fruit_borer")
    await _seed_diagnosis_row(db, connect_id="pdc:r2", pest="pest:leaf_miner")
    slug = await _seed_image_index_for_crop(db, crop=CROP, slug="tomato_symptom_images")
    await _seed_media(db, cosh_id="med:imgA", s3_path="s3://.../A.jpg",
                      en_caption="Borer damage")
    await _seed_media(db, cosh_id="med:imgB", s3_path="s3://.../B.jpg",
                      en_caption="Miner trail")
    await _seed_image_link(db, connect_id="lnk1",
                           connect_type=slug,
                           pest_diagnosis_id="pdc:r1", media_id="med:imgA")
    await _seed_image_link(db, connect_id="lnk2",
                           connect_type=slug,
                           pest_diagnosis_id="pdc:r2", media_id="med:imgB")
    await db.commit()

    result = await find_reference_images(
        db,
        crop_cosh_id=CROP, part_cosh_id=PART, symptom_cosh_id=SYMPTOM,
    )
    cosh_ids = {img.cosh_id for img in result}
    assert cosh_ids == {"med:imgA", "med:imgB"}


@requires_docker
@pytest.mark.asyncio
async def test_new_crop_self_registers_via_image_index(db):
    """A new crop wires up by adding one image_index row + a per-crop
    image Connect — no backend code change needed. Replaces the older
    'connect_type name doesn't matter' test, which assumed a
    connect-type-agnostic scan; the new image-index lookup chooses
    the right per-crop slug deterministically."""
    await _seed_diagnosis_row(db, connect_id="pdc:paddy",
                              pest="pest:stem_borer",
                              crop="crop:paddy", crop_stage="stage:tillering",
                              part="part:stem", symptom="sym:dead_heart")
    slug = await _seed_image_index_for_crop(
        db, crop="crop:paddy", slug="paddy_symptom_images",
    )
    await _seed_media(db, cosh_id="med:paddy_img", s3_path="s3://.../paddy.jpg",
                      en_caption="Paddy stem borer dead heart")
    await _seed_image_link(db, connect_id="ppi:1",
                           connect_type=slug,
                           pest_diagnosis_id="pdc:paddy",
                           media_id="med:paddy_img")
    await db.commit()

    result = await find_reference_images(
        db,
        crop_cosh_id="crop:paddy", part_cosh_id="part:stem",
        symptom_cosh_id="sym:dead_heart",
    )
    assert len(result) == 1
    assert result[0].cosh_id == "med:paddy_img"


@requires_docker
@pytest.mark.asyncio
async def test_no_match_returns_empty(db):
    """Diagnosis row exists but the caller asks for a different crop —
    image_index has no row for that crop, so empty result."""
    await _seed_diagnosis_row(db, connect_id="pdc:r1", pest="pest:fruit_borer")
    slug = await _seed_image_index_for_crop(db, crop=CROP, slug="tomato_symptom_images")
    await _seed_media(db, cosh_id="med:img1", s3_path="s3://.../1.jpg",
                      en_caption="Spots")
    await _seed_image_link(db, connect_id="lnk1",
                           connect_type=slug,
                           pest_diagnosis_id="pdc:r1", media_id="med:img1")
    await db.commit()

    result = await find_reference_images(
        db,
        crop_cosh_id="crop:other", part_cosh_id=PART, symptom_cosh_id=SYMPTOM,
    )
    assert result == []


@requires_docker
@pytest.mark.asyncio
async def test_diagnosis_row_without_image_link_returns_empty(db):
    """Diagnosis row matches the question but no image Connect points
    at it — gap path."""
    await _seed_diagnosis_row(db, connect_id="pdc:r1", pest="pest:fruit_borer")
    await db.commit()

    result = await find_reference_images(
        db,
        crop_cosh_id=CROP, part_cosh_id=PART, symptom_cosh_id=SYMPTOM,
    )
    assert result == []


@requires_docker
@pytest.mark.asyncio
async def test_inactive_image_link_excluded(db):
    """A status='inactive' image Connect row is skipped."""
    await _seed_diagnosis_row(db, connect_id="pdc:r1", pest="pest:fruit_borer")
    slug = await _seed_image_index_for_crop(db, crop=CROP, slug="tomato_symptom_images")
    await _seed_media(db, cosh_id="med:img1", s3_path="s3://.../1.jpg",
                      en_caption="Spots")
    db.add(CoshConnectRow(
        connect_id="lnk_inactive",
        connect_type=slug,
        status="inactive",
        endpoints=[
            {"role": "diagnosis_row", "cosh_id": "pdc:r1", "position": 1},
            {"role": "image",         "cosh_id": "med:img1", "position": 2},
        ],
    ))
    await db.commit()

    result = await find_reference_images(
        db,
        crop_cosh_id=CROP, part_cosh_id=PART, symptom_cosh_id=SYMPTOM,
    )
    assert result == []


# ── Caption language fallback ───────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_caption_in_requested_language(db):
    await _seed_diagnosis_row(db, connect_id="pdc:r1", pest="pest:fruit_borer")
    slug = await _seed_image_index_for_crop(db, crop=CROP, slug="tomato_symptom_images")
    await _seed_media(db, cosh_id="med:img1", s3_path="s3://.../1.jpg",
                      en_caption="White spots", kn_caption="ಬಿಳಿ ಕಲೆಗಳು")
    await _seed_image_link(db, connect_id="lnk1",
                           connect_type=slug,
                           pest_diagnosis_id="pdc:r1", media_id="med:img1")
    await db.commit()

    kn = await find_reference_images(
        db, crop_cosh_id=CROP, part_cosh_id=PART, symptom_cosh_id=SYMPTOM,
        language_code="kn",
    )
    assert kn[0].caption == "ಬಿಳಿ ಕಲೆಗಳು"

    en_fallback = await find_reference_images(
        db, crop_cosh_id=CROP, part_cosh_id=PART, symptom_cosh_id=SYMPTOM,
        language_code="hi",  # no Hindi caption
    )
    assert en_fallback[0].caption == "White spots"


# ── Google Images query construction ────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_google_query_uses_farmer_language(db):
    await _seed_core_translation(db, cosh_id=CROP, core_type="biological_names",
                                 en="Tomato", kn="ಟೊಮೆಟೊ")
    await _seed_core_translation(db, cosh_id=PART, core_type="part",
                                 en="Leaf", kn="ಎಲೆ")
    await _seed_core_translation(db, cosh_id=SYMPTOM, core_type="symptom",
                                 en="White spots", kn="ಬಿಳಿ ಕಲೆಗಳು")
    await db.commit()

    en_query = await build_google_images_query(
        db, crop_cosh_id=CROP, part_cosh_id=PART, symptom_cosh_id=SYMPTOM,
        language_code="en",
    )
    assert en_query == "White spots Leaf Tomato"

    kn_query = await build_google_images_query(
        db, crop_cosh_id=CROP, part_cosh_id=PART, symptom_cosh_id=SYMPTOM,
        language_code="kn",
    )
    assert kn_query == "ಬಿಳಿ ಕಲೆಗಳು ಎಲೆ ಟೊಮೆಟೊ"


def test_google_images_url_encoding():
    url = google_images_url("White spots Leaf Tomato")
    assert url.startswith("https://www.google.com/search?tbm=isch&q=")
    assert "White%20spots%20Leaf%20Tomato" in url


# ── Endpoint integration ────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_endpoint_returns_images_when_present(db):
    await _seed_diagnosis_row(db, connect_id="pdc:r1", pest="pest:fruit_borer")
    slug = await _seed_image_index_for_crop(db, crop=CROP, slug="tomato_symptom_images")
    await _seed_media(db, cosh_id="med:img1", s3_path="s3://.../1.jpg",
                      en_caption="Borer damage")
    await _seed_image_link(db, connect_id="lnk1",
                           connect_type=slug,
                           pest_diagnosis_id="pdc:r1", media_id="med:img1")
    await _seed_core_translation(db, cosh_id=CROP, core_type="biological_names",
                                 en="Tomato")
    await _seed_core_translation(db, cosh_id=PART, core_type="part",
                                 en="Leaf")
    await _seed_core_translation(db, cosh_id=SYMPTOM, core_type="symptom",
                                 en="White spots")
    user = await make_user(db)
    await db.commit()

    out = await get_reference_images(
        request=ReferenceImagesRequest(
            crop_cosh_id=CROP,
            plant_part_cosh_id=PART,
            symptom_cosh_id=SYMPTOM,
            language_code="en",
        ),
        db=db, current_user=user,
    )
    assert len(out["images"]) == 1
    assert out["google_images_url"].startswith("https://www.google.com/search?tbm=isch&q=")
    assert "White spots" in out["google_images_query"]


@requires_docker
@pytest.mark.asyncio
async def test_endpoint_returns_empty_images_with_google_fallback(db):
    """No-image fallback path: empty list + the Google URL is still
    populated so the PWA can offer it."""
    await _seed_core_translation(db, cosh_id=CROP, core_type="biological_names",
                                 en="Tomato")
    await _seed_core_translation(db, cosh_id=PART, core_type="part",
                                 en="Leaf")
    await _seed_core_translation(db, cosh_id=SYMPTOM, core_type="symptom",
                                 en="White spots")
    user = await make_user(db)
    await db.commit()

    out = await get_reference_images(
        request=ReferenceImagesRequest(
            crop_cosh_id=CROP,
            plant_part_cosh_id=PART,
            symptom_cosh_id=SYMPTOM,
            language_code="en",
        ),
        db=db, current_user=user,
    )
    assert out["images"] == []
    assert out["google_images_url"] is not None
    assert "White%20spots" in out["google_images_url"]
    assert out["google_images_query"] == "White spots Leaf Tomato"
