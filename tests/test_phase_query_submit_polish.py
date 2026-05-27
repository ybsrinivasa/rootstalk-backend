"""Query submission rewrite (2026-05-27).

Pins the new contract on POST /farmer/queries:

  - `query_type_cosh_id` mandatory; must resolve to an ACTIVE
    `query_types` Cosh core (422 `query_type_invalid` otherwise).
  - `title` is auto-derived from the resolved English translation —
    PWA no longer asks for one.
  - ≥1 IMAGE media row required (422 `image_required`).
    >4 IMAGE → `too_many_images`. >1 AUDIO → `too_many_audios`.
  - `expires_at` = end-of-Day-2 in IST (counting from submission date).
  - VIDEO accepted in payload even though the PWA doesn't ship the
    picker in V1.

Plus quota + Cosh-type endpoints.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from app.modules.farmpundit.models import Query, QueryMedia
from app.modules.farmpundit.router import (
    FREE_QUERIES_PER_COMPANY, QUERY_PAID_PRICE_PAISE,
    QueryCreate, QueryMediaItem, cosh_query_types,
    get_query_quota, submit_query,
)
from app.modules.sync.models import CoshCoreItem
from tests.conftest import requires_docker
from tests.factories import (
    make_client, make_package, make_subscription, make_user,
)
from sqlalchemy import select


QT_INSECT = "qt-insect-disease"
QT_GERM = "qt-poor-germination"


async def _seed_query_types(db):
    db.add(CoshCoreItem(
        cosh_id=QT_INSECT, core_type="query_types",
        translations={"en": "Insect and disease problems"}, status="active",
    ))
    db.add(CoshCoreItem(
        cosh_id=QT_GERM, core_type="query_types",
        translations={"en": "Poor seed germination"}, status="active",
    ))
    await db.flush()


async def _farmer_sub(db, client=None):
    client = client or await make_client(db)
    farmer = await make_user(db, name="Q-Farmer")
    pkg = await make_package(db, client, crop_cosh_id="crop:tomato")
    sub = await make_subscription(db, farmer=farmer, client=client, package=pkg)
    return farmer, client, sub


def _img(url: str = "https://placeholder.rootstalk.in/q/photo.jpg") -> QueryMediaItem:
    return QueryMediaItem(media_type="IMAGE", url=url)


# ── Cosh nature gate ────────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_submit_refuses_unknown_query_type(db):
    farmer, client, sub = await _farmer_sub(db)
    await _seed_query_types(db)
    await db.commit()

    with pytest.raises(HTTPException) as exc:
        await submit_query(
            request=QueryCreate(
                subscription_id=sub.id, client_id=client.id,
                query_type_cosh_id="qt-fictional", severity="HIGH",
                media=[_img()],
            ),
            db=db, current_user=farmer,
        )
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "query_type_invalid"


@requires_docker
@pytest.mark.asyncio
async def test_submit_autosets_title_from_query_type(db):
    farmer, client, sub = await _farmer_sub(db)
    await _seed_query_types(db)
    await db.commit()

    out = await submit_query(
        request=QueryCreate(
            subscription_id=sub.id, client_id=client.id,
            query_type_cosh_id=QT_INSECT, severity="HIGH",
            media=[_img()],
        ),
        db=db, current_user=farmer,
    )
    q = (await db.execute(select(Query).where(Query.id == out["id"]))).scalar_one()
    assert q.title == "Insect and disease problems"
    assert q.query_type_cosh_id == QT_INSECT


# ── Photo / audio constraints ───────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_submit_refuses_zero_photos(db):
    farmer, client, sub = await _farmer_sub(db)
    await _seed_query_types(db)
    await db.commit()

    with pytest.raises(HTTPException) as exc:
        await submit_query(
            request=QueryCreate(
                subscription_id=sub.id, client_id=client.id,
                query_type_cosh_id=QT_INSECT, severity="HIGH",
                media=[],
            ),
            db=db, current_user=farmer,
        )
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "image_required"


@requires_docker
@pytest.mark.asyncio
async def test_submit_refuses_more_than_four_photos(db):
    farmer, client, sub = await _farmer_sub(db)
    await _seed_query_types(db)
    await db.commit()

    with pytest.raises(HTTPException) as exc:
        await submit_query(
            request=QueryCreate(
                subscription_id=sub.id, client_id=client.id,
                query_type_cosh_id=QT_INSECT, severity="HIGH",
                media=[_img(f"https://x/p{i}.jpg") for i in range(5)],
            ),
            db=db, current_user=farmer,
        )
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "too_many_images"


@requires_docker
@pytest.mark.asyncio
async def test_submit_refuses_two_audios(db):
    farmer, client, sub = await _farmer_sub(db)
    await _seed_query_types(db)
    await db.commit()

    with pytest.raises(HTTPException) as exc:
        await submit_query(
            request=QueryCreate(
                subscription_id=sub.id, client_id=client.id,
                query_type_cosh_id=QT_INSECT, severity="HIGH",
                media=[
                    _img(),
                    QueryMediaItem(media_type="AUDIO", url="https://x/a1.mp3"),
                    QueryMediaItem(media_type="AUDIO", url="https://x/a2.mp3"),
                ],
            ),
            db=db, current_user=farmer,
        )
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "too_many_audios"


@requires_docker
@pytest.mark.asyncio
async def test_submit_writes_all_media_rows(db):
    farmer, client, sub = await _farmer_sub(db)
    await _seed_query_types(db)
    await db.commit()

    out = await submit_query(
        request=QueryCreate(
            subscription_id=sub.id, client_id=client.id,
            query_type_cosh_id=QT_INSECT, severity="HIGH",
            description="Big spots on lower leaves",
            media=[
                _img("https://x/p1.jpg"), _img("https://x/p2.jpg"),
                QueryMediaItem(media_type="AUDIO", url="https://x/voice.m4a"),
                # VIDEO is accepted even though the PWA doesn't ship it.
                QueryMediaItem(media_type="VIDEO", url="https://x/v.mp4"),
            ],
        ),
        db=db, current_user=farmer,
    )
    rows = (await db.execute(
        select(QueryMedia).where(QueryMedia.query_id == out["id"])
    )).scalars().all()
    types = sorted(r.media_type for r in rows)
    assert types == ["AUDIO", "IMAGE", "IMAGE", "VIDEO"]


# ── Expiry: end of Day-2 IST ───────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_submit_expires_at_end_of_day_two_ist(db):
    """If the IST date of submission is D, expires_at == 23:59:59 IST
    on D+2. Two-day response window for the expert, excluding the
    submission day itself."""
    farmer, client, sub = await _farmer_sub(db)
    await _seed_query_types(db)
    await db.commit()

    before_utc = datetime.now(timezone.utc)
    out = await submit_query(
        request=QueryCreate(
            subscription_id=sub.id, client_id=client.id,
            query_type_cosh_id=QT_INSECT, severity="HIGH",
            media=[_img()],
        ),
        db=db, current_user=farmer,
    )
    ist = timezone(timedelta(hours=5, minutes=30))
    expiry_ist = out["expires_at"].astimezone(ist)
    submit_ist = before_utc.astimezone(ist)
    assert (expiry_ist.date() - submit_ist.date()).days == 2
    assert (expiry_ist.hour, expiry_ist.minute, expiry_ist.second) == (23, 59, 59)


# ── Free quota + Cosh-types endpoints ──────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_quota_counts_per_farmer_client_pair(db):
    farmer, client, sub = await _farmer_sub(db)
    await _seed_query_types(db)
    await db.commit()

    # Submit 3 queries, check quota math.
    for _ in range(3):
        await submit_query(
            request=QueryCreate(
                subscription_id=sub.id, client_id=client.id,
                query_type_cosh_id=QT_INSECT, severity="MODERATE",
                media=[_img()],
            ),
            db=db, current_user=farmer,
        )

    quota = await get_query_quota(
        client_id=client.id, db=db, current_user=farmer,
    )
    assert quota["used"] == 3
    assert quota["free_limit"] == FREE_QUERIES_PER_COMPANY
    assert quota["free_remaining"] == FREE_QUERIES_PER_COMPANY - 3
    assert quota["price_paise"] == QUERY_PAID_PRICE_PAISE
    assert quota["next_query_is_paid"] is False

    # Other-client quota stays at zero — counting is per (farmer, client).
    other_client = await make_client(db)
    await db.commit()
    other_quota = await get_query_quota(
        client_id=other_client.id, db=db, current_user=farmer,
    )
    assert other_quota["used"] == 0


@requires_docker
@pytest.mark.asyncio
async def test_quota_flags_paid_after_free_limit(db):
    farmer, client, sub = await _farmer_sub(db)
    await _seed_query_types(db)
    await db.commit()

    for _ in range(FREE_QUERIES_PER_COMPANY):
        await submit_query(
            request=QueryCreate(
                subscription_id=sub.id, client_id=client.id,
                query_type_cosh_id=QT_INSECT, severity="MODERATE",
                media=[_img()],
            ),
            db=db, current_user=farmer,
        )
    quota = await get_query_quota(
        client_id=client.id, db=db, current_user=farmer,
    )
    assert quota["free_remaining"] == 0
    assert quota["next_query_is_paid"] is True


@requires_docker
@pytest.mark.asyncio
async def test_cosh_query_types_returns_sorted_active_only(db):
    farmer = await make_user(db)
    db.add(CoshCoreItem(
        cosh_id="qt-zeta", core_type="query_types",
        translations={"en": "Zeta concern"}, status="active",
    ))
    db.add(CoshCoreItem(
        cosh_id="qt-alpha", core_type="query_types",
        translations={"en": "Alpha concern"}, status="active",
    ))
    db.add(CoshCoreItem(
        cosh_id="qt-archived", core_type="query_types",
        translations={"en": "Old"}, status="archived",
    ))
    await db.commit()

    out = await cosh_query_types(db=db, current_user=farmer)
    names = [o["name"] for o in out]
    assert names == sorted(names, key=str.casefold)
    assert "Old" not in names
