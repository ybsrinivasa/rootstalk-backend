import json
import qrcode
import io
from datetime import datetime, timezone
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.database import get_db
from app.dependencies import get_current_user
from app.modules.platform.models import User
from app.modules.qr.models import ManufacturerBrandPortfolio, ProductQRCode, QRScan
from app.modules.orders.models import OrderItem, OrderItemStatus

router = APIRouter(tags=["QR Codes"])


class QRCreate(BaseModel):
    product_type: str
    brand_cosh_id: Optional[str] = None
    variety_id: Optional[str] = None
    product_display_name: str
    manufacture_date: str
    expiry_date: str
    batch_lot_number: str


@router.get("/client/{client_id}/qr/codes")
async def list_qr_codes(
    client_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(ProductQRCode).where(ProductQRCode.client_id == client_id).order_by(ProductQRCode.created_at.desc())
    )
    return result.scalars().all()


@router.post("/client/{client_id}/qr/codes", status_code=201)
async def create_qr_code(
    client_id: str,
    request: QRCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """BL-18: Duplicate check before creating QR code."""
    existing = (await db.execute(
        select(ProductQRCode).where(
            ProductQRCode.client_id == client_id,
            ProductQRCode.brand_cosh_id == request.brand_cosh_id,
            ProductQRCode.batch_lot_number == request.batch_lot_number,
        )
    )).scalar_one_or_none()

    if existing:
        if existing.status == "ACTIVE":
            raise HTTPException(status_code=409,
                detail=f"A QR code for this product and batch number already exists. View: /client/{client_id}/qr/codes/{existing.id}")
        return {"warning": "An inactive QR code for this batch exists.", "existing_id": existing.id}

    payload = json.dumps({
        "client_id": client_id,
        "product_type": request.product_type,
        "brand_cosh_id": request.brand_cosh_id,
        "variety_id": request.variety_id,
        "batch_lot_number": request.batch_lot_number,
        "product_display_name": request.product_display_name,
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


@router.get("/client/{client_id}/qr/codes/{qr_id}/download")
async def download_qr_code(
    client_id: str, qr_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Generate QR code image as PNG."""
    result = await db.execute(
        select(ProductQRCode).where(ProductQRCode.id == qr_id, ProductQRCode.client_id == client_id)
    )
    qr_record = result.scalar_one_or_none()
    if not qr_record:
        raise HTTPException(status_code=404, detail="QR code not found")

    qr = qrcode.QRCode(version=1, box_size=10, border=4)
    qr.add_data(qr_record.qr_payload or qr_id)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return Response(content=buf.read(), media_type="image/png",
                    headers={"Content-Disposition": f"attachment; filename=qr_{qr_id[:8]}.png"})


@router.put("/client/{client_id}/qr/codes/{qr_id}/status")
async def toggle_qr_status(
    client_id: str, qr_id: str,
    data: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(ProductQRCode).where(ProductQRCode.id == qr_id, ProductQRCode.client_id == client_id)
    )
    qr = result.scalar_one_or_none()
    if not qr:
        raise HTTPException(status_code=404, detail="Not found")
    qr.status = data.get("status", "INACTIVE")
    await db.commit()
    return {"id": qr_id, "status": qr.status}


@router.post("/farmer/qr/scan")
async def scan_qr_code(
    data: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Farmer scans QR code on received product. Returns match/mismatch."""
    qr_code_id = data.get("qr_code_id")
    order_item_id = data.get("order_item_id")

    qr = (await db.execute(select(ProductQRCode).where(ProductQRCode.id == qr_code_id))).scalar_one_or_none()
    if not qr:
        scan = QRScan(qr_code_id=qr_code_id, farmer_user_id=current_user.id,
                      order_item_id=order_item_id, match_status="INACTIVE_CODE")
        db.add(scan)
        await db.commit()
        return {"match_status": "INACTIVE_CODE", "message": "This QR code is not recognised in the system."}

    item = (await db.execute(select(OrderItem).where(OrderItem.id == order_item_id))).scalar_one_or_none()
    match = item and item.brand_cosh_id == qr.brand_cosh_id

    scan = QRScan(
        qr_code_id=qr_code_id,
        farmer_user_id=current_user.id,
        order_item_id=order_item_id,
        match_status="MATCH" if match else "MISMATCH",
    )
    db.add(scan)
    await db.commit()

    return {
        "match_status": scan.match_status,
        "message": "Product verified — matches your order." if match else "Warning: this product does not match what was ordered.",
    }


@router.get("/client/{client_id}/qr/mismatches")
async def list_mismatches(
    client_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(QRScan)
        .join(ProductQRCode, ProductQRCode.id == QRScan.qr_code_id)
        .where(ProductQRCode.client_id == client_id, QRScan.match_status == "MISMATCH")
        .order_by(QRScan.scanned_at.desc())
    )
    return result.scalars().all()
