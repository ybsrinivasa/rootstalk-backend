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

from app.modules.farmpundit.diagnosis_router import (
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
    eps = [
        {"role": "crop",       "cosh_id": crop},
        {"role": "crop_stage", "cosh_id": crop_stage},
        {"role": "pest",       "cosh_id": pest},
        {"role": "part",       "cosh_id": part},
        {"role": "symptom",    "cosh_id": symptom},
    ]
    if sub_part:
        eps.append({"role": "sub_part", "cosh_id": sub_part})
    if sub_symptom:
        eps.append({"role": "sub_symptom", "cosh_id": sub_symptom})
    db.add(CoshConnectRow(
        connect_id=connect_id,
        connect_type="pest_diagnosis_chain",
        status="active",
        endpoints=eps,
        metadata_=None,
    ))


async def _seed_image_link(db, *, connect_id, connect_type,
                           pest_diagnosis_id, media_id):
    db.add(CoshConnectRow(
        connect_id=connect_id,
        connect_type=connect_type,
        status="active",
        endpoints=[
            {"role": "pest_diagnosis_chain", "cosh_id": pest_diagnosis_id},
            {"role": "media",                "cosh_id": media_id},
        ],
        metadata_=None,
    ))


async def _seed_media(db, *, cosh_id, s3_path, en_caption, kn_caption=None):
    translations = {"en": en_caption}
    if kn_caption:
        translations["kn"] = kn_caption
    db.add(CoshCoreItem(
        cosh_id=cosh_id,
        core_type="media",
        status="active",
        translations=translations,
        metadata_={"s3_path": s3_path, "media_type": "image"},
    ))


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
    await _seed_media(db, cosh_id="med:img1",
                      s3_path="s3://rootstalk-images/tomato/leaf_white_spots_1.jpg",
                      en_caption="White spots on tomato leaf — early stage")
    await _seed_image_link(db,
                           connect_id="tpi:link1",
                           connect_type="tomato_pest_images",
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
    await _seed_media(db, cosh_id="med:imgA", s3_path="s3://.../A.jpg",
                      en_caption="Borer damage")
    await _seed_media(db, cosh_id="med:imgB", s3_path="s3://.../B.jpg",
                      en_caption="Miner trail")
    await _seed_image_link(db, connect_id="lnk1",
                           connect_type="tomato_pest_images",
                           pest_diagnosis_id="pdc:r1", media_id="med:imgA")
    await _seed_image_link(db, connect_id="lnk2",
                           connect_type="tomato_pest_images",
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
async def test_image_connect_type_name_doesnt_matter(db):
    """A new crop's image Connect (e.g. paddy_pest_images) works
    without backend code change — lookup is connect_type-agnostic."""
    await _seed_diagnosis_row(db, connect_id="pdc:paddy",
                              pest="pest:stem_borer",
                              crop="crop:paddy", crop_stage="stage:tillering",
                              part="part:stem", symptom="sym:dead_heart")
    await _seed_media(db, cosh_id="med:paddy_img", s3_path="s3://.../paddy.jpg",
                      en_caption="Paddy stem borer dead heart")
    await _seed_image_link(db, connect_id="ppi:1",
                           connect_type="paddy_pest_images",
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
    """Diagnosis row exists but for a different crop — empty result."""
    await _seed_diagnosis_row(db, connect_id="pdc:r1", pest="pest:fruit_borer")
    await _seed_media(db, cosh_id="med:img1", s3_path="s3://.../1.jpg",
                      en_caption="Spots")
    await _seed_image_link(db, connect_id="lnk1",
                           connect_type="tomato_pest_images",
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
    await _seed_media(db, cosh_id="med:img1", s3_path="s3://.../1.jpg",
                      en_caption="Spots")
    db.add(CoshConnectRow(
        connect_id="lnk_inactive",
        connect_type="tomato_pest_images",
        status="inactive",
        endpoints=[
            {"role": "pest_diagnosis_chain", "cosh_id": "pdc:r1"},
            {"role": "media",                "cosh_id": "med:img1"},
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
    await _seed_media(db, cosh_id="med:img1", s3_path="s3://.../1.jpg",
                      en_caption="White spots", kn_caption="ಬಿಳಿ ಕಲೆಗಳು")
    await _seed_image_link(db, connect_id="lnk1",
                           connect_type="tomato_pest_images",
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
    await _seed_core_translation(db, cosh_id=CROP, core_type="crop",
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
    await _seed_media(db, cosh_id="med:img1", s3_path="s3://.../1.jpg",
                      en_caption="Borer damage")
    await _seed_image_link(db, connect_id="lnk1",
                           connect_type="tomato_pest_images",
                           pest_diagnosis_id="pdc:r1", media_id="med:img1")
    await _seed_core_translation(db, cosh_id=CROP, core_type="crop",
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
    await _seed_core_translation(db, cosh_id=CROP, core_type="crop",
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
