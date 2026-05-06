"""BL-18 audit — DB-backed integration tests for the QR dedup flow.

Pure-function coverage of `dedup_key` lives in `tests/test_bl18.py`
(11 tests). This file drives `create_qr_code` (single) and
`bulk_create_qr_codes` (CSV) directly with seeded rows in the
testcontainer DB to verify the helper-driven dedup behaves end-to-
end across both paths.
"""
from __future__ import annotations

import io
from datetime import date

import pytest
from fastapi import HTTPException, UploadFile
from sqlalchemy import select

from app.modules.qr.models import ProductQRCode
from app.modules.qr.router import (
    QRCreate, bulk_create_qr_codes, create_qr_code,
)
from tests.conftest import requires_docker
from tests.factories import make_client, make_user


def _csv_upload(content: str) -> UploadFile:
    """Wrap a CSV string in a FastAPI UploadFile for the bulk route."""
    return UploadFile(filename="bulk.csv", file=io.BytesIO(content.encode()))


# ── Single create: dedup on pesticide path (brand_cosh_id) ───────────────────

@requires_docker
@pytest.mark.asyncio
async def test_single_create_dedup_hits_on_brand_plus_batch(db):
    """Pesticide path: spec-faithful key is (client, brand_cosh_id,
    batch). A second create with the same brand+batch returns 409."""
    sa = await make_user(db, name="SA")
    client = await make_client(db)
    await db.commit()

    first = await create_qr_code(
        client_id=client.id,
        request=QRCreate(
            product_type="PESTICIDE",
            brand_cosh_id="brand:dithane-m45",
            product_display_name="Dithane M-45",
            manufacture_date="2026-01-01",
            expiry_date="2026-12-31",
            batch_lot_number="B001",
        ),
        db=db, current_user=sa,
    )
    assert first["status"] == "ACTIVE"

    with pytest.raises(HTTPException) as exc:
        await create_qr_code(
            client_id=client.id,
            request=QRCreate(
                product_type="PESTICIDE",
                brand_cosh_id="brand:dithane-m45",
                product_display_name="Dithane M-45 (re-print)",  # different display
                manufacture_date="2026-01-01",
                expiry_date="2026-12-31",
                batch_lot_number="B001",
            ),
            db=db, current_user=sa,
        )
    assert exc.value.status_code == 409


# ── Single create: dedup on seed path (variety_id) ───────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_single_create_dedup_hits_on_variety_plus_batch(db):
    """Seed path: spec-faithful key is (client, variety_id, batch).
    Different products with the same variety+batch are duplicates."""
    sa = await make_user(db, name="SA")
    client = await make_client(db)
    await db.commit()

    await create_qr_code(
        client_id=client.id,
        request=QRCreate(
            product_type="SEED",
            variety_id="variety-uuid-1",
            product_display_name="Tomato Hybrid X",
            manufacture_date="2026-01-01",
            expiry_date="2026-12-31",
            batch_lot_number="S100",
        ),
        db=db, current_user=sa,
    )

    with pytest.raises(HTTPException) as exc:
        await create_qr_code(
            client_id=client.id,
            request=QRCreate(
                product_type="SEED",
                variety_id="variety-uuid-1",
                product_display_name="Tomato Hybrid X v2",
                manufacture_date="2026-01-01",
                expiry_date="2026-12-31",
                batch_lot_number="S100",
            ),
            db=db, current_user=sa,
        )
    assert exc.value.status_code == 409


# ── Single create: 422 when no identifier is supplied ─────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_single_create_rejects_missing_identifier(db):
    """A request with no brand_cosh_id, no variety_id, AND a blank
    display_name has no usable dedup key — DedupKeyError surfaces as
    422 with a clear message."""
    sa = await make_user(db, name="SA")
    client = await make_client(db)
    await db.commit()

    with pytest.raises(HTTPException) as exc:
        await create_qr_code(
            client_id=client.id,
            request=QRCreate(
                product_type="PESTICIDE",
                product_display_name="",
                manufacture_date="2026-01-01",
                expiry_date="2026-12-31",
                batch_lot_number="B900",
            ),
            db=db, current_user=sa,
        )
    assert exc.value.status_code == 422


# ── Single create: INACTIVE existing returns warning, not 409 ────────────────

@requires_docker
@pytest.mark.asyncio
async def test_single_create_returns_warning_when_existing_is_inactive(db):
    """Spec-existing behaviour preserved: an INACTIVE existing row
    surfaces as a `warning` field rather than a 409 — gives the
    dealer a chance to reactivate the old row instead of failing."""
    sa = await make_user(db, name="SA")
    client = await make_client(db)
    db.add(ProductQRCode(
        client_id=client.id,
        product_type="PESTICIDE",
        brand_cosh_id="brand:retired",
        product_display_name="Retired Brand",
        manufacture_date=date(2025, 1, 1), expiry_date=date(2025, 12, 31),
        batch_lot_number="OLD001",
        status="INACTIVE",
    ))
    await db.commit()

    out = await create_qr_code(
        client_id=client.id,
        request=QRCreate(
            product_type="PESTICIDE",
            brand_cosh_id="brand:retired",
            product_display_name="Retired Brand",
            manufacture_date="2026-01-01", expiry_date="2026-12-31",
            batch_lot_number="OLD001",
        ),
        db=db, current_user=sa,
    )
    assert "warning" in out
    assert out["existing_id"] is not None


# ── Bulk: dedup catches in-app duplicates ─────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_bulk_dedup_catches_inflight_duplicate_rows(db):
    """Two CSV rows with the same display_name + batch are caught by
    the in-app dedup — second row marked DUPLICATE, only first
    inserted. Today's CSV has no brand/variety columns so the
    helper's display_name fallback applies."""
    sa = await make_user(db, name="SA")
    client = await make_client(db)
    await db.commit()

    csv = (
        "Product Type,Trade Name / Variety Name,Manufacture or Production Date,Expiry Date,Batch/Lot Number\n"
        "Pesticide,BrandXYZ Gold,01-01-2026,31-12-2026,B100\n"
        "Pesticide,BrandXYZ Gold,01-01-2026,31-12-2026,B100\n"
    )
    out = await bulk_create_qr_codes(
        client_id=client.id, file=_csv_upload(csv),
        db=db, current_user=sa,
    )
    assert out["summary"]["generated"] == 1
    assert out["summary"]["skipped_duplicates"] == 1


# ── Cross-path: bulk import catches a single-created row ─────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_bulk_dedup_catches_a_single_created_sibling(db):
    """Headline cross-path test: a single-created QR with display
    "BrandXYZ Gold" + batch "B200" exists. A bulk import of a row
    with the same display + batch is caught as DUPLICATE — pre-audit
    the bulk path's inline `(client, display, batch)` query and the
    single path's `(client, brand, variety, batch)` query disagreed,
    so this scenario silently committed a second row that conflicted
    on display_name."""
    sa = await make_user(db, name="SA")
    client = await make_client(db)
    await db.commit()

    await create_qr_code(
        client_id=client.id,
        request=QRCreate(
            product_type="PESTICIDE",
            brand_cosh_id="brand:bxyz-gold",
            product_display_name="BrandXYZ Gold",
            manufacture_date="2026-01-01", expiry_date="2026-12-31",
            batch_lot_number="B200",
        ),
        db=db, current_user=sa,
    )
    await db.commit()

    # Bulk row with the same display+batch (no brand_cosh_id available
    # in CSV today — fallback path applies).
    csv = (
        "Product Type,Trade Name / Variety Name,Manufacture or Production Date,Expiry Date,Batch/Lot Number\n"
        "Pesticide,BrandXYZ Gold,01-01-2026,31-12-2026,B200\n"
    )
    out = await bulk_create_qr_codes(
        client_id=client.id, file=_csv_upload(csv),
        db=db, current_user=sa,
    )
    assert out["summary"]["generated"] == 0
    assert out["summary"]["skipped_duplicates"] == 1
    # The original single-created row is still there.
    rows = (await db.execute(
        select(ProductQRCode).where(
            ProductQRCode.client_id == client.id,
            ProductQRCode.batch_lot_number == "B200",
        )
    )).scalars().all()
    assert len(rows) == 1
    assert rows[0].brand_cosh_id == "brand:bxyz-gold"


# ── Bulk: valid sibling rows survive a duplicate row ─────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_bulk_valid_rows_survive_a_duplicate_row(db):
    """Pre-audit, an IntegrityError on one row aborted the entire
    bulk transaction and lost every valid sibling. The new SAVEPOINT
    pattern + helper-driven in-app dedup means a duplicate row is
    isolated — flagged in the summary, every other row commits."""
    sa = await make_user(db, name="SA")
    client = await make_client(db)
    await db.commit()

    csv = (
        "Product Type,Trade Name / Variety Name,Manufacture or Production Date,Expiry Date,Batch/Lot Number\n"
        "Pesticide,Product A,01-01-2026,31-12-2026,A001\n"
        "Pesticide,Product A,01-01-2026,31-12-2026,A001\n"  # duplicate
        "Pesticide,Product B,01-01-2026,31-12-2026,B001\n"
        "Pesticide,Product C,01-01-2026,31-12-2026,C001\n"
    )
    out = await bulk_create_qr_codes(
        client_id=client.id, file=_csv_upload(csv),
        db=db, current_user=sa,
    )
    assert out["summary"]["generated"] == 3
    assert out["summary"]["skipped_duplicates"] == 1

    rows = (await db.execute(
        select(ProductQRCode).where(ProductQRCode.client_id == client.id)
    )).scalars().all()
    assert len(rows) == 3
    display_names = {r.product_display_name for r in rows}
    assert display_names == {"Product A", "Product B", "Product C"}
