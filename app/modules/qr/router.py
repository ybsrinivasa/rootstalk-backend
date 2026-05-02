import csv
import io
import json
import qrcode
from datetime import datetime, timezone, date
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Response, UploadFile, File
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm
from reportlab.pdfgen import canvas as pdf_canvas

from app.database import get_db
from app.dependencies import get_current_user
from app.modules.platform.models import User
from app.modules.qr.models import ManufacturerBrandPortfolio, ProductQRCode, QRScan
from app.modules.orders.models import OrderItem, OrderItemStatus
from app.modules.sync.models import CoshReferenceCache
from app.modules.subscriptions.models import Subscription
from app.modules.clients.models import Client

router = APIRouter(tags=["QR Codes"])

PUBLIC_DOMAIN = "https://coshdev.eywa.farm"   # replaced with env var in production

PRODUCT_TYPE_SIZES = {"SMALL": 2.0, "MEDIUM": 3.5, "LARGE": 5.0}


# ═══════════════════════════════════════════════════════════════════════════════
# PRODUCT AUTHENTICATION QR
# ════════════════════════════════════════════════════���══════════════════════════

# ── Brand Portfolio ─────────────────────────────────────────────────────────────

@router.get("/client/{client_id}/qr/portfolio")
async def list_brand_portfolio(
    client_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(ManufacturerBrandPortfolio).where(
            ManufacturerBrandPortfolio.client_id == client_id,
            ManufacturerBrandPortfolio.status == "ACTIVE",
        ).order_by(ManufacturerBrandPortfolio.product_type)
    )
    rows = result.scalars().all()
    out = []
    for r in rows:
        name = None
        if r.brand_cosh_id:
            entry = (await db.execute(
                select(CoshReferenceCache).where(CoshReferenceCache.cosh_id == r.brand_cosh_id)
            )).scalar_one_or_none()
            if entry:
                name = (entry.translations or {}).get("en") or r.brand_cosh_id
        out.append({
            "id": r.id,
            "product_type": r.product_type,
            "brand_cosh_id": r.brand_cosh_id,
            "variety_id": r.variety_id,
            "display_name": name or r.brand_cosh_id or r.variety_id,
        })
    return out


@router.post("/client/{client_id}/qr/portfolio/search")
async def search_portfolio_brands(
    client_id: str,
    data: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Search cosh_reference_cache for brands matching a manufacturer name."""
    manufacturer_name = data.get("manufacturer_name", "").strip().lower()
    if not manufacturer_name:
        raise HTTPException(status_code=422, detail="manufacturer_name required")
    result = await db.execute(
        select(CoshReferenceCache).where(
            CoshReferenceCache.entity_type == "brand",
            CoshReferenceCache.status == "active",
        )
    )
    all_brands = result.scalars().all()
    matches = []
    for b in all_brands:
        meta = b.metadata_ or {}
        mfr = (meta.get("manufacturer_name") or "").lower()
        if manufacturer_name in mfr or mfr in manufacturer_name:
            matches.append({
                "cosh_id": b.cosh_id,
                "name": (b.translations or {}).get("en") or b.cosh_id,
                "manufacturer": meta.get("manufacturer_name"),
                "product_type": meta.get("product_type", "PESTICIDE"),
            })
    return matches


@router.post("/client/{client_id}/qr/portfolio", status_code=201)
async def add_to_portfolio(
    client_id: str,
    data: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    existing = (await db.execute(
        select(ManufacturerBrandPortfolio).where(
            ManufacturerBrandPortfolio.client_id == client_id,
            ManufacturerBrandPortfolio.brand_cosh_id == data.get("brand_cosh_id"),
        )
    )).scalar_one_or_none()
    if existing:
        if existing.status == "INACTIVE":
            existing.status = "ACTIVE"
            await db.commit()
        return {"id": existing.id, "detail": "Already in portfolio"}
    entry = ManufacturerBrandPortfolio(
        client_id=client_id,
        product_type=data.get("product_type", "PESTICIDE"),
        brand_cosh_id=data.get("brand_cosh_id"),
        variety_id=data.get("variety_id"),
    )
    db.add(entry)
    await db.commit()
    return {"id": entry.id, "detail": "Added to portfolio"}


@router.delete("/client/{client_id}/qr/portfolio/{portfolio_id}")
async def remove_from_portfolio(
    client_id: str, portfolio_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    entry = (await db.execute(
        select(ManufacturerBrandPortfolio).where(
            ManufacturerBrandPortfolio.id == portfolio_id,
            ManufacturerBrandPortfolio.client_id == client_id,
        )
    )).scalar_one_or_none()
    if not entry:
        raise HTTPException(status_code=404)
    entry.status = "INACTIVE"
    await db.commit()
    return {"detail": "Removed"}


# ── QR Code list and single generation ────��───────────────────────────────────

@router.get("/client/{client_id}/qr/codes")
async def list_qr_codes(
    client_id: str,
    product_type: Optional[str] = None,
    status: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    q = select(ProductQRCode).where(ProductQRCode.client_id == client_id).order_by(ProductQRCode.created_at.desc())
    if product_type:
        q = q.where(ProductQRCode.product_type == product_type)
    if status:
        q = q.where(ProductQRCode.status == status)
    result = await db.execute(q)
    codes = result.scalars().all()

    out = []
    for c in codes:
        scan_count = (await db.execute(
            select(QRScan).where(QRScan.qr_code_id == c.id)
        )).scalars().all()
        mismatch_count = sum(1 for s in scan_count if s.match_status == "MISMATCH")
        out.append({
            "id": c.id,
            "product_type": c.product_type,
            "product_display_name": c.product_display_name,
            "batch_lot_number": c.batch_lot_number,
            "manufacture_date": str(c.manufacture_date),
            "expiry_date": str(c.expiry_date),
            "status": c.status,
            "created_at": c.created_at,
            "scan_count": len(scan_count),
            "mismatch_count": mismatch_count,
        })
    return out


class QRCreate(BaseModel):
    product_type: str
    brand_cosh_id: Optional[str] = None
    variety_id: Optional[str] = None
    product_display_name: str
    manufacture_date: str
    expiry_date: str
    batch_lot_number: str


@router.post("/client/{client_id}/qr/codes", status_code=201)
async def create_qr_code(
    client_id: str,
    request: QRCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """BL-18: Duplicate check. Generate and store QR code record."""
    _validate_dates(request.manufacture_date, request.expiry_date)

    existing = (await db.execute(
        select(ProductQRCode).where(
            ProductQRCode.client_id == client_id,
            ProductQRCode.brand_cosh_id == request.brand_cosh_id,
            ProductQRCode.variety_id == request.variety_id,
            ProductQRCode.batch_lot_number == request.batch_lot_number,
        )
    )).scalar_one_or_none()
    if existing:
        if existing.status == "ACTIVE":
            raise HTTPException(status_code=409,
                detail=f"A QR code for this product and batch already exists. ID: {existing.id}")
        return {"warning": "An inactive QR code for this batch exists.", "existing_id": existing.id}

    client = (await db.execute(select(Client).where(Client.id == client_id))).scalar_one_or_none()
    payload = json.dumps({
        "rt_qr": True,
        "v": "1",
        "client_id": client_id,
        "company_name": client.full_name if client else "",
        "product_type": request.product_type,
        "brand_cosh_id": request.brand_cosh_id,
        "variety_id": request.variety_id,
        "batch_lot": request.batch_lot_number,
        "display_name": request.product_display_name,
        "mfr_date": request.manufacture_date,
        "exp_date": request.expiry_date,
    })
    qr = ProductQRCode(
        client_id=client_id,
        product_type=request.product_type,
        brand_cosh_id=request.brand_cosh_id,
        variety_id=request.variety_id,
        product_display_name=request.product_display_name,
        manufacture_date=request.manufacture_date,
        expiry_date=request.expiry_date,
        batch_lot_number=request.batch_lot_number,
        qr_payload=payload,
        created_by=current_user.id,
    )
    db.add(qr)
    await db.commit()
    await db.refresh(qr)
    return {"id": qr.id, "status": qr.status}


# ── CSV Bulk generation ───���────────────────────────────────────────────────────

@router.post("/client/{client_id}/qr/codes/bulk")
async def bulk_create_qr_codes(
    client_id: str,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """BL-18 Bulk: validate CSV rows, skip duplicates, generate valid rows."""
    content = await file.read()
    reader = csv.DictReader(io.StringIO(content.decode("utf-8-sig")))

    client = (await db.execute(select(Client).where(Client.id == client_id))).scalar_one_or_none()
    results = []
    generated = 0
    skipped_dup = 0
    failed = 0

    for i, row in enumerate(reader, start=2):
        product_type = (row.get("Product Type") or "").strip()
        display_name = (row.get("Trade Name / Variety Name") or row.get("Trade Name") or "").strip()
        mfr_date = (row.get("Manufacture or Production Date") or row.get("Manufacture Date") or "").strip()
        exp_date = (row.get("Expiry Date") or "").strip()
        batch_lot = (row.get("Batch/Lot Number") or row.get("Batch Number") or row.get("Lot Number") or "").strip()

        errors = []
        if not product_type:
            errors.append("Product Type missing")
        if not display_name:
            errors.append("Trade/Variety Name missing")
        if not mfr_date:
            errors.append("Manufacture/Production Date missing")
        if not exp_date:
            errors.append("Expiry Date missing")
        if not batch_lot:
            errors.append("Batch/Lot Number missing")
        if mfr_date and exp_date:
            try:
                m = datetime.strptime(mfr_date, "%d-%m-%Y").date()
                e = datetime.strptime(exp_date, "%d-%m-%Y").date()
                if e <= m:
                    errors.append("Expiry must be after Manufacture date")
            except ValueError:
                errors.append("Date format must be DD-MM-YYYY")

        if errors:
            results.append({"row": i, "status": "FAILED", "reason": "; ".join(errors), "display_name": display_name})
            failed += 1
            continue

        existing = (await db.execute(
            select(ProductQRCode).where(
                ProductQRCode.client_id == client_id,
                ProductQRCode.product_display_name == display_name,
                ProductQRCode.batch_lot_number == batch_lot,
            )
        )).scalar_one_or_none()
        if existing:
            results.append({"row": i, "status": "DUPLICATE", "reason": "Batch already generated", "display_name": display_name})
            skipped_dup += 1
            continue

        mfr_date_iso = datetime.strptime(mfr_date, "%d-%m-%Y").strftime("%Y-%m-%d")
        exp_date_iso = datetime.strptime(exp_date, "%d-%m-%Y").strftime("%Y-%m-%d")
        payload = json.dumps({
            "rt_qr": True, "v": "1",
            "client_id": client_id,
            "company_name": client.full_name if client else "",
            "product_type": product_type,
            "display_name": display_name,
            "batch_lot": batch_lot,
            "mfr_date": mfr_date,
            "exp_date": exp_date,
        })
        qr = ProductQRCode(
            client_id=client_id, product_type=product_type,
            product_display_name=display_name,
            manufacture_date=mfr_date_iso, expiry_date=exp_date_iso,
            batch_lot_number=batch_lot, qr_payload=payload,
            created_by=current_user.id,
        )
        db.add(qr)
        results.append({"row": i, "status": "OK", "display_name": display_name, "batch_lot": batch_lot})
        generated += 1

    await db.commit()
    return {
        "summary": {"generated": generated, "skipped_duplicates": skipped_dup, "failed": failed},
        "rows": results,
    }


@router.get("/client/{client_id}/qr/bulk-template")
async def download_bulk_template(client_id: str):
    """Return CSV template with headers and one sample row."""
    header = "Product Type,Trade Name / Variety Name,Manufacture or Production Date,Expiry Date,Batch/Lot Number\n"
    sample = "Pesticide,BrandXYZ Gold,01-01-2026,31-12-2026,BATCH001\n"
    return Response(
        content=(header + sample).encode(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=qr_bulk_template.csv"},
    )


# ── QR Code download ───────���───────────────────────────────────────────────────

@router.get("/client/{client_id}/qr/codes/{qr_id}/download")
async def download_qr_code(
    client_id: str, qr_id: str,
    format: str = "PNG",
    size: str = "MEDIUM",
    size_cm: Optional[float] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Download QR code as PNG or print-ready PDF."""
    qr_record = (await db.execute(
        select(ProductQRCode).where(ProductQRCode.id == qr_id, ProductQRCode.client_id == client_id)
    )).scalar_one_or_none()
    if not qr_record:
        raise HTTPException(status_code=404)

    px_size = int((size_cm or PRODUCT_TYPE_SIZES.get(size.upper(), 3.5)) * 37.8)
    box_size = max(3, px_size // 37)

    qr = qrcode.QRCode(version=1, box_size=box_size, border=3)
    qr.add_data(qr_record.qr_payload or qr_id)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")

    if format.upper() == "PNG":
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)
        fname = f"{qr_record.product_display_name}_{qr_record.batch_lot_number}.png"
        return Response(content=buf.read(), media_type="image/png",
                        headers={"Content-Disposition": f'attachment; filename="{fname}"'})

    # PDF single
    pdf_buf = io.BytesIO()
    c = pdf_canvas.Canvas(pdf_buf, pagesize=A4)
    w, h = A4
    dim_cm = size_cm or PRODUCT_TYPE_SIZES.get(size.upper(), 3.5)
    dim_pt = dim_cm * cm

    img_buf = io.BytesIO()
    img.save(img_buf, format="PNG")
    img_buf.seek(0)
    from reportlab.lib.utils import ImageReader
    c.drawImage(ImageReader(img_buf), (w - dim_pt) / 2, h - dim_pt - 100, dim_pt, dim_pt)
    c.setFont("Helvetica-Bold", 11)
    y = h - dim_pt - 130
    c.drawCentredString(w / 2, y, qr_record.product_display_name)
    c.setFont("Helvetica", 9)
    c.drawCentredString(w / 2, y - 14, f"Batch/Lot: {qr_record.batch_lot_number}")
    c.drawCentredString(w / 2, y - 26, f"Mfr: {qr_record.manufacture_date}  |  Exp: {qr_record.expiry_date}")
    c.drawCentredString(w / 2, y - 38, f"Type: {qr_record.product_type}")
    c.save()
    pdf_buf.seek(0)
    fname = f"{qr_record.product_display_name}_{qr_record.batch_lot_number}.pdf"
    return Response(content=pdf_buf.read(), media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{fname}"'})


@router.put("/client/{client_id}/qr/codes/{qr_id}/status")
async def toggle_qr_status(
    client_id: str, qr_id: str, data: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    qr = (await db.execute(
        select(ProductQRCode).where(ProductQRCode.id == qr_id, ProductQRCode.client_id == client_id)
    )).scalar_one_or_none()
    if not qr:
        raise HTTPException(status_code=404)
    qr.status = data.get("status", "INACTIVE")
    await db.commit()
    return {"id": qr_id, "status": qr.status}


# ── Farmer scan flow ────────────���──────────────────────��───────────────────────

@router.post("/farmer/qr/scan")
async def scan_qr_code(
    data: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Farmer scans QR code from Purchased Items. Returns MATCH / MISMATCH / INACTIVE."""
    qr_payload = data.get("qr_payload", "")
    order_item_id = data.get("order_item_id")

    # Decode payload to find qr_code_id
    try:
        payload_data = json.loads(qr_payload)
    except Exception:
        payload_data = {}

    client_id = payload_data.get("client_id")
    batch_lot = payload_data.get("batch_lot")
    scanned_brand_cosh_id = payload_data.get("brand_cosh_id")
    display_name = payload_data.get("display_name", "")

    qr_record = None
    if client_id and batch_lot:
        qr_record = (await db.execute(
            select(ProductQRCode).where(
                ProductQRCode.client_id == client_id,
                ProductQRCode.batch_lot_number == batch_lot,
            )
        )).scalar_one_or_none()

    item = (await db.execute(
        select(OrderItem).where(OrderItem.id == order_item_id)
    )).scalar_one_or_none()

    if not qr_record or qr_record.status == "INACTIVE":
        scan = QRScan(
            qr_code_id=qr_record.id if qr_record else None,
            farmer_user_id=current_user.id,
            order_item_id=order_item_id,
            match_status="INACTIVE_CODE",
        )
        db.add(scan)
        await db.commit()
        return {"match_status": "INACTIVE_CODE",
                "message": "This product code is no longer active. Please contact your dealer."}

    expected_brand = item.brand_cosh_id if item else None
    is_match = (scanned_brand_cosh_id and expected_brand and
                scanned_brand_cosh_id == expected_brand)

    # Count previous scan attempts for this item
    prev_scans = (await db.execute(
        select(QRScan).where(QRScan.order_item_id == order_item_id)
    )).scalars().all()
    attempt_num = len(prev_scans) + 1

    scan = QRScan(
        qr_code_id=qr_record.id,
        farmer_user_id=current_user.id,
        order_item_id=order_item_id,
        match_status="MATCH" if is_match else "MISMATCH",
        expected_brand_cosh_id=expected_brand,
        scanned_brand_cosh_id=scanned_brand_cosh_id,
        scan_attempt_number=attempt_num,
    )
    db.add(scan)

    if is_match and item:
        item.scan_verified = True

    await db.commit()

    if is_match:
        return {"match_status": "MATCH",
                "message": "Verified — Genuine Product. This matches your order."}
    return {
        "match_status": "MISMATCH",
        "message": f"The product you scanned does not match {display_name}. "
                   "Please check the label carefully and scan again.",
        "retry": attempt_num < 3,
    }


# ── Mismatch log ────────────────────────────────────────────────────────────────

@router.get("/client/{client_id}/qr/mismatches")
async def list_mismatches(
    client_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(QRScan, ProductQRCode)
        .join(ProductQRCode, ProductQRCode.id == QRScan.qr_code_id)
        .where(ProductQRCode.client_id == client_id, QRScan.match_status == "MISMATCH")
        .order_by(QRScan.scanned_at.desc())
    )
    rows = result.all()
    out = []
    for scan, qr_code in rows:
        farmer = (await db.execute(
            select(User).where(User.id == scan.farmer_user_id)
        )).scalar_one_or_none()
        item = (await db.execute(
            select(OrderItem).where(OrderItem.id == scan.order_item_id)
        )).scalar_one_or_none()
        out.append({
            "scan_id": scan.id,
            "scanned_at": scan.scanned_at,
            "farmer_name": farmer.name if farmer else None,
            "farmer_state": farmer.state_cosh_id if farmer else None,
            "farmer_district": farmer.district_cosh_id if farmer else None,
            "expected_product": qr_code.product_display_name,
            "expected_brand_cosh_id": scan.expected_brand_cosh_id,
            "scanned_brand_cosh_id": scan.scanned_brand_cosh_id,
            "batch_lot_number": qr_code.batch_lot_number,
            "dealer_user_id": item.order.dealer_user_id if item and hasattr(item, 'order') else None,
            "scan_attempt": scan.scan_attempt_number,
        })
    return out


# ═══════════════════════════════════��═══════════════════════════════════════════
# CROP HISTORY / TRACEABILITY QR
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/farmer/subscriptions/{sub_id}/crop-qr")
async def get_crop_history_qr(
    sub_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Generate a QR code image (PNG) for the crop history public page."""
    sub = (await db.execute(
        select(Subscription).where(
            Subscription.id == sub_id,
            Subscription.farmer_user_id == current_user.id,
        )
    )).scalar_one_or_none()
    if not sub:
        raise HTTPException(status_code=404, detail="Subscription not found")
    if not sub.reference_number:
        raise HTTPException(status_code=400, detail="Subscription has no reference number yet")

    public_url = f"{PUBLIC_DOMAIN}/crop/{sub.reference_number}"

    qr = qrcode.QRCode(version=1, box_size=8, border=4)
    qr.add_data(public_url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return Response(content=buf.read(), media_type="image/png",
                    headers={"Content-Disposition": f'inline; filename="crop-{sub.reference_number}.png"'})


@router.get("/public/crop/{reference_number}")
async def get_crop_public_page(
    reference_number: str,
    db: AsyncSession = Depends(get_db),
):
    """PUBLIC — no auth. Returns crop record data for the traceability web page."""
    sub = (await db.execute(
        select(Subscription).where(Subscription.reference_number == reference_number)
    )).scalar_one_or_none()
    if not sub:
        raise HTTPException(status_code=404, detail="Crop record not found")

    farmer = (await db.execute(select(User).where(User.id == sub.farmer_user_id))).scalar_one_or_none()
    client = (await db.execute(select(Client).where(Client.id == sub.client_id))).scalar_one_or_none()

    from app.modules.advisory.models import Package
    package = (await db.execute(select(Package).where(Package.id == sub.package_id))).scalar_one_or_none()

    return {
        "reference_number": sub.reference_number,
        "farmer_name": farmer.name if farmer else None,
        "farmer_district": farmer.district_cosh_id if farmer else None,
        "farmer_state": farmer.state_cosh_id if farmer else None,
        "company_name": client.full_name if client else None,
        "company_display_name": client.display_name if client else None,
        "crop_cosh_id": package.crop_cosh_id if package else None,
        "package_name": package.name if package else None,
        "subscription_date": sub.subscription_date,
        "crop_start_date": sub.crop_start_date,
        "status": sub.status,
    }


# ── Helpers ────────────────────────────────────────────────────────────────────

def _validate_dates(mfr_date: str, exp_date: str):
    try:
        m = datetime.strptime(mfr_date, "%Y-%m-%d").date()
        e = datetime.strptime(exp_date, "%Y-%m-%d").date()
        if e <= m:
            raise HTTPException(status_code=422, detail="Expiry date must be after manufacture date")
    except ValueError:
        raise HTTPException(status_code=422, detail="Date format must be YYYY-MM-DD")
