import csv
import io
import json
import os
import uuid
from pathlib import Path
from urllib.parse import urlparse
import qrcode
from PIL import Image, ImageDraw, ImageFont
from datetime import datetime, timezone, date
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Response, UploadFile, File
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm
from reportlab.pdfgen import canvas as pdf_canvas

from app.config import settings
from app.database import get_db
from app.dependencies import get_current_user
from app.modules.platform.models import User
from app.modules.qr.models import ManufacturerBrandPortfolio, ProductQRCode, QRScan
from app.modules.orders.models import OrderItem, OrderItemStatus
from app.modules.sync.models import CoshCoreItem
from app.modules.subscriptions.models import (
    FarmerSubscriptionHistory, Subscription,
)
from app.modules.clients.models import Client
from app.services.bl16_crop_record import (
    crop_record_public_url, public_record_payload,
)
from app.services.bl18_qr_dedup import (
    DedupKey, DedupKeyError, dedup_key, is_spec_faithful,
)
from sqlalchemy.exc import IntegrityError
import logging

_logger = logging.getLogger(__name__)


async def _find_qr_dupe(
    db: AsyncSession, client_id: str, key: DedupKey,
) -> Optional[ProductQRCode]:
    """Run the dedup query implied by `key`. Single source of truth
    used by both create_qr_code (single) and bulk_create_qr_codes
    (CSV) — pre-audit the two paths had different inline queries
    and silently disagreed on what counted as a duplicate."""
    column = getattr(ProductQRCode, key.column_name)
    return (await db.execute(
        select(ProductQRCode).where(
            ProductQRCode.client_id == client_id,
            column == key.column_value,
            ProductQRCode.batch_lot_number == key.batch_lot_number,
        )
    )).scalar_one_or_none()

router = APIRouter(tags=["QR Codes"])


def _public_base_url() -> str:
    """Env-aware public base URL for QR-encoded crop-record links.
    Mirrors the `_base_url()` pattern in `clients/router.py`.

    Resolution order:
    1. Dev → `http://localhost:3003` (PWA dev port; project_rootstalk_ports.md).
    2. `PWA_BASE_URL` env var — testing MUST set this to
       `https://rstalk-pwa.eywa.farm` so scanned QRs don't land on prod.
    3. Production fallback `https://rootstalk.in`.
    """
    if settings.environment == "development":
        return "http://localhost:3003"
    if settings.pwa_base_url:
        return settings.pwa_base_url.rstrip("/")
    return "https://rootstalk.in"


PRODUCT_TYPE_SIZES = {"SMALL": 2.0, "MEDIUM": 3.5, "LARGE": 5.0}

# 2026-07-05 — QR branding.
# Farmer often sees multiple QRs on the same package (batch, marketing,
# CE mark, etc). Ours needs to be instantly recognisable so they don't
# scan the wrong one. Recipe:
#   1. Error correction level H (30% redundancy) so the centred logo
#      overlay doesn't degrade scan reliability.
#   2. eywa logo (colored, text-stripped) inset at ~22% of the QR side.
#   3. "rootsTALK.in" label rendered below the QR both in PNG and PDF
#      outputs — the farmer opens the app under the same brand.
_LOGO_PATH = Path(__file__).parent.parent.parent / "static" / "logos" / "eywa-logo-notext-square.png"
_BRAND_LABEL = "rootsTALK.in"


def _blacken_logo(logo: Image.Image) -> Image.Image:
    """Convert the cream / colored eywa logo to a pure-black
    silhouette for mono-brand mode. Preserves the alpha channel so
    the outline and internal shape stay recognizable; just recolours
    every non-transparent pixel to (0, 0, 0). Called only when
    style='mono'."""
    logo = logo.convert("RGBA")
    pixels = logo.load()
    w, h = logo.size
    for y in range(h):
        for x in range(w):
            r, g, b, a = pixels[x, y]
            if a > 0:
                pixels[x, y] = (0, 0, 0, a)
    return logo


def _build_branded_qr_png(
    payload: str,
    px_size: int,
    style: str = "color",
    label: bool = True,
) -> Image.Image:
    """Render a QR for print or screen. Three modes via `style`:

      color — dark green modules + colored eywa logo overlay
              (default; for glossy printed labels).
      mono  — pure black modules + black eywa silhouette overlay
              (for mono printers that can render bitmap + text).
      raw   — pure black modules, NO logo overlay, NO label
              (for CIJ / dot-matrix printers that only rasterise
              black dots on seed pouches — govt-mandated QR use).

    `label` controls whether "rootsTALK.in" is baked in below the QR.
    Ignored (always False) when style='raw'. In color/mono mode the
    label carries the brand identity when a farmer scans multiple
    QRs from the same package (govt seed vs marketing vs ours).

    Error correction is always H (30% redundancy) so the logo
    overlay never degrades scan reliability even at low print DPI.
    """
    module_colour = "#0F2A0F" if style == "color" else "black"

    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_H,
        box_size=max(3, px_size // 37),
        border=3,
    )
    qr.add_data(payload)
    qr.make(fit=True)
    qr_img = qr.make_image(fill_color=module_colour, back_color="white").convert("RGBA")

    if qr_img.size[0] != px_size:
        qr_img = qr_img.resize((px_size, px_size), Image.NEAREST)

    # Raw mode: no logo overlay, no label, done.
    if style == "raw":
        return qr_img

    # Logo overlay for color / mono modes.
    if _LOGO_PATH.exists():
        logo = Image.open(_LOGO_PATH).convert("RGBA")
        if style == "mono":
            logo = _blacken_logo(logo)
        logo_side = int(px_size * 0.22)
        backing_side = int(logo_side * 1.15)
        backing = Image.new("RGBA", (backing_side, backing_side), (255, 255, 255, 255))
        bx = (px_size - backing_side) // 2
        by = (px_size - backing_side) // 2
        qr_img.paste(backing, (bx, by), backing)
        logo = logo.resize((logo_side, logo_side), Image.LANCZOS)
        lx = (px_size - logo_side) // 2
        ly = (px_size - logo_side) // 2
        qr_img.paste(logo, (lx, ly), logo)

    if not label:
        return qr_img

    # Label baked in below.
    label_height = max(24, int(px_size * 0.14))
    gap = max(4, int(px_size * 0.02))
    canvas = Image.new(
        "RGBA",
        (px_size, px_size + gap + label_height),
        (255, 255, 255, 255),
    )
    canvas.paste(qr_img, (0, 0))
    draw = ImageDraw.Draw(canvas)
    font = None
    for candidate in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/System/Library/Fonts/HelveticaNeue.ttc",
    ):
        if os.path.exists(candidate):
            try:
                font = ImageFont.truetype(candidate, size=int(label_height * 0.72))
                break
            except Exception:
                continue
    if font is None:
        font = ImageFont.load_default()
    text_bbox = draw.textbbox((0, 0), _BRAND_LABEL, font=font)
    text_w = text_bbox[2] - text_bbox[0]
    text_h = text_bbox[3] - text_bbox[1]
    text_x = (px_size - text_w) // 2
    text_y = px_size + gap + (label_height - text_h) // 2 - text_bbox[1]
    text_colour = "#0F2A0F" if style == "color" else "black"
    draw.text((text_x, text_y), _BRAND_LABEL, fill=text_colour, font=font)
    return canvas


# 2026-07-05 — Seed org_type is the same cosh_id the seed_mgmt module
# uses to decide "is seed company". Kept in sync with
# app.modules.seed_mgmt.router.SEED_COMPANY_COSH_ID; if it ever
# changes, both must move together.
_SEED_COMPANY_COSH_ID = "4b0847f9-a590-452f-9129-ee0e2d946dd9"


async def _is_seed_client(db: AsyncSession, client_id: str) -> bool:
    """True when the client carries the seed-company org-type marker.
    Independent of `is_manufacturer` — a Bayer-shaped client can be
    both. Used by the QR gate and by the seed-variety candidates
    endpoint to decide whether to surface the seed flow at all."""
    from app.modules.clients.models import ClientOrganisationType
    rows = (await db.execute(
        select(ClientOrganisationType.org_type_cosh_id).where(
            ClientOrganisationType.client_id == client_id,
        )
    )).scalars().all()
    return _SEED_COMPANY_COSH_ID in rows


async def _assert_client_can_qr(db: AsyncSession, client_id: str) -> Client:
    """QR module gate — passes if the Client is a Manufacturer (has
    branded pesticide/fertilizer products via Cosh) OR a Seed Company
    (has RootsTalk seed varieties). Both axes are independent:
    Bayer-shaped clients pass on both. Advisory-only clients pass on
    neither and get a 403.

    Returns the loaded Client so callers can reuse without a second
    hop. Raises 404 for missing client, 403 for ineligible.
    """
    client = (await db.execute(
        select(Client).where(Client.id == client_id)
    )).scalar_one_or_none()
    if client is None:
        raise HTTPException(status_code=404, detail="Client not found")
    is_seed = False
    if not client.is_manufacturer:
        is_seed = await _is_seed_client(db, client_id)
    if not (client.is_manufacturer or is_seed):
        raise HTTPException(
            status_code=403,
            detail={
                "code": "client_not_qr_eligible",
                "message": (
                    "QR Product Authentication is available only for "
                    "Manufacturer or Seed-Company clients."
                ),
            },
        )
    return client


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
    from app.modules.seed_mgmt.models import SeedVariety

    await _assert_client_can_qr(db, client_id)
    result = await db.execute(
        select(ManufacturerBrandPortfolio).where(
            ManufacturerBrandPortfolio.client_id == client_id,
            ManufacturerBrandPortfolio.status == "ACTIVE",
        ).order_by(ManufacturerBrandPortfolio.product_type)
    )
    rows = result.scalars().all()
    # Batch-resolve names in two lookups instead of N+1: brand names
    # from Cosh, variety names from RootsTalk's seed_varieties.
    brand_ids = [r.brand_cosh_id for r in rows if r.brand_cosh_id]
    variety_ids = [r.variety_id for r in rows if r.variety_id]
    brand_name_by_id: dict[str, str] = {}
    variety_name_by_id: dict[str, str] = {}
    if brand_ids:
        brand_rows = (await db.execute(
            select(CoshCoreItem.cosh_id, CoshCoreItem.translations).where(
                CoshCoreItem.cosh_id.in_(brand_ids),
            )
        )).all()
        for cid, tr in brand_rows:
            if isinstance(tr, dict):
                brand_name_by_id[cid] = tr.get("en") or next(
                    (v for v in tr.values() if v), cid,
                )
    if variety_ids:
        var_rows = (await db.execute(
            select(SeedVariety.id, SeedVariety.name).where(
                SeedVariety.id.in_(variety_ids),
            )
        )).all()
        variety_name_by_id = {vid: vname for vid, vname in var_rows}
    out = []
    for r in rows:
        name = None
        if r.brand_cosh_id:
            name = brand_name_by_id.get(r.brand_cosh_id)
        elif r.variety_id:
            name = variety_name_by_id.get(r.variety_id)
        out.append({
            "id": r.id,
            "product_type": r.product_type,
            "brand_cosh_id": r.brand_cosh_id,
            "variety_id": r.variety_id,
            "display_name": name or r.brand_cosh_id or r.variety_id,
        })
    return out


@router.get("/client/{client_id}/qr/portfolio/candidates")
async def list_portfolio_candidates(
    client_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """2026-07-05 — Replaces the fragile free-text
    `POST /qr/portfolio/search`. Auto-loads the pesticide/fertilizer
    brand candidates for this client using the deterministic
    Client.cosh_manufacturer_id link the SA set at approval time.

    Response shape:
      {
        "brands": [{cosh_id, name, product_type}],
        "cosh_manufacturer_linked": bool,
      }

    - `is_manufacturer=False` → 403 via `_assert_client_can_qr`.
    - `is_manufacturer=True` but `cosh_manufacturer_id` NULL →
      409 `cosh_manufacturer_not_linked` so the CA gets a clear
      "ask the SA to link your Cosh manufacturer" message.
    - Otherwise walks the `tradename_manufacturer` Cosh Connect,
      resolves trade-name names from `trade_names` Cosh Core.

    Seed varieties are NOT surfaced here — they come from RootsTalk
    (seed_varieties table), not Cosh, per the user's product model
    (2026-07-05). Seed-flavour clients use their existing variety
    portfolio surface for the QR flow's seed leg.
    """
    from app.services.cosh_options_view import _trade_names_for_manufacturer
    from app.services.cosh_constants import COSH_TRADE_NAMES_CORE

    client = await _assert_client_can_qr(db, client_id)
    if not client.is_manufacturer:
        # Pure seed-only client — no brand candidates by design.
        return {"brands": [], "cosh_manufacturer_linked": False}
    if not client.cosh_manufacturer_id:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "cosh_manufacturer_not_linked",
                "message": (
                    "This client isn't linked to a Cosh Manufacturer yet. "
                    "Ask the Super Admin to link it from the Company "
                    "Profile edit modal."
                ),
            },
        )
    tn_ids = await _trade_names_for_manufacturer(
        db, client.cosh_manufacturer_id,
    )
    if not tn_ids:
        return {"brands": [], "cosh_manufacturer_linked": True}
    tn_rows = (await db.execute(
        select(CoshCoreItem.cosh_id, CoshCoreItem.translations).where(
            CoshCoreItem.core_type == COSH_TRADE_NAMES_CORE,
            CoshCoreItem.cosh_id.in_(tn_ids),
            CoshCoreItem.status == "active",
        )
    )).all()
    brands = []
    for cosh_id, translations in tn_rows:
        name = None
        if isinstance(translations, dict):
            name = translations.get("en") or next(
                (v for v in translations.values() if v), None,
            )
        brands.append({
            "cosh_id": cosh_id,
            "name": name or cosh_id,
            # v1: product_type stays a per-brand-add choice on the
            # CA side. The taxonomy walk to derive it from the
            # common_name → L2 chain is a follow-up if the CA finds
            # the manual pick tedious.
            "product_type": None,
        })
    brands.sort(key=lambda r: (r["name"] or "").lower())
    return {"brands": brands, "cosh_manufacturer_linked": True}


@router.get("/client/{client_id}/qr/portfolio/varieties/candidates")
async def list_variety_candidates(
    client_id: str,
    crop_cosh_id: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """2026-07-05 — Seed-side companion to
    `/qr/portfolio/candidates`. Seed varieties come from RootsTalk
    (`seed_varieties`), not Cosh — so the picker is scoped to the
    client's own variety catalog.

    Two-axis response:
      {
        "crops": [{cosh_id, name, variety_count}, ...],  # populated only
        "varieties": [{id, name, crop_cosh_id}, ...],    # empty until crop chosen
        "is_seed_client": bool,
      }

    - `crops`: alphabetical, includes only crops with ≥1 ACTIVE
      variety saved by this client. If the CA hasn't entered any
      varieties yet, this is empty and the frontend renders a
      pointer to the Seed → Varieties page.
    - `?crop_cosh_id=<id>`: filters `varieties` to that crop only;
      without the query param, `varieties` is `[]` (crop-first
      selection).
    - Non-seed clients (Manufacturer-only): `is_seed_client=false` +
      empty payload — the frontend suppresses the tab entirely.
      Kept as a 200 (not 403) so a Bayer-shaped client can hit both
      candidate endpoints on load without one erroring.
    """
    from app.modules.seed_mgmt.models import SeedVariety

    await _assert_client_can_qr(db, client_id)
    is_seed = await _is_seed_client(db, client_id)
    if not is_seed:
        return {"crops": [], "varieties": [], "is_seed_client": False}

    # Populated-crops list — one row per crop_cosh_id with the count of
    # ACTIVE varieties this client has entered under it.
    from sqlalchemy import func
    crop_rows = (await db.execute(
        select(
            SeedVariety.crop_cosh_id,
            func.count(SeedVariety.id).label("variety_count"),
        ).where(
            SeedVariety.client_id == client_id,
            SeedVariety.status == "ACTIVE",
        ).group_by(SeedVariety.crop_cosh_id)
    )).all()
    crop_ids = [r[0] for r in crop_rows]
    crop_name_by_id: dict[str, str] = {}
    if crop_ids:
        name_rows = (await db.execute(
            select(CoshCoreItem.cosh_id, CoshCoreItem.translations).where(
                CoshCoreItem.cosh_id.in_(crop_ids),
            )
        )).all()
        for cid, tr in name_rows:
            if isinstance(tr, dict):
                crop_name_by_id[cid] = tr.get("en") or next(
                    (v for v in tr.values() if v), cid,
                )
    crops = [
        {
            "cosh_id": cid,
            "name": crop_name_by_id.get(cid, cid),
            "variety_count": vc,
        }
        for cid, vc in crop_rows
    ]
    crops.sort(key=lambda r: (r["name"] or "").lower())

    varieties: list[dict] = []
    if crop_cosh_id:
        var_rows = (await db.execute(
            select(SeedVariety.id, SeedVariety.name, SeedVariety.crop_cosh_id).where(
                SeedVariety.client_id == client_id,
                SeedVariety.crop_cosh_id == crop_cosh_id,
                SeedVariety.status == "ACTIVE",
            ).order_by(SeedVariety.name)
        )).all()
        varieties = [
            {"id": vid, "name": vname, "crop_cosh_id": vcrop}
            for vid, vname, vcrop in var_rows
        ]

    return {
        "crops": crops,
        "varieties": varieties,
        "is_seed_client": True,
    }


@router.post("/client/{client_id}/qr/portfolio", status_code=201)
async def add_to_portfolio(
    client_id: str,
    data: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Add a brand (Cosh) or seed variety (RootsTalk) to the client's
    QR portfolio. The two paths share a table with the same product_type
    discriminator + one identifier column each (`brand_cosh_id` XOR
    `variety_id`)."""
    await _assert_client_can_qr(db, client_id)
    brand_cosh_id = data.get("brand_cosh_id")
    variety_id = data.get("variety_id")
    if not brand_cosh_id and not variety_id:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "portfolio_identifier_required",
                "message": "Either brand_cosh_id or variety_id must be provided.",
            },
        )
    if brand_cosh_id:
        existing = (await db.execute(
            select(ManufacturerBrandPortfolio).where(
                ManufacturerBrandPortfolio.client_id == client_id,
                ManufacturerBrandPortfolio.brand_cosh_id == brand_cosh_id,
            )
        )).scalar_one_or_none()
    else:
        existing = (await db.execute(
            select(ManufacturerBrandPortfolio).where(
                ManufacturerBrandPortfolio.client_id == client_id,
                ManufacturerBrandPortfolio.variety_id == variety_id,
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
        brand_cosh_id=brand_cosh_id,
        variety_id=variety_id,
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
    await _assert_client_can_qr(db, client_id)
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
    await _assert_client_can_qr(db, client_id)
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
    """BL-18: Duplicate check. Generate and store QR code record.

    2026-07-05 — Gated on `_assert_client_can_qr`: only manufacturer
    or seed-company clients can generate QR codes.

    BL-18 audit (2026-05-06): dedup-key derivation moved to the
    bl18_qr_dedup service — same helper drives the bulk path so the
    two writers can never disagree on what counts as a duplicate.
    """
    await _assert_client_can_qr(db, client_id)
    _validate_dates(request.manufacture_date, request.expiry_date)

    try:
        key = dedup_key(
            brand_cosh_id=request.brand_cosh_id,
            variety_id=request.variety_id,
            product_display_name=request.product_display_name,
            batch_lot_number=request.batch_lot_number,
        )
    except DedupKeyError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    existing = await _find_qr_dupe(db, client_id, key)
    if existing:
        if existing.status == "ACTIVE":
            raise HTTPException(status_code=409,
                detail=f"A QR code for this product and batch already exists. ID: {existing.id}")
        return {"warning": "An inactive QR code for this batch exists.", "existing_id": existing.id}

    # 2026-07-05 — QR payload is now a URL, not JSON. Rationale:
    # a farmer scanning with a native camera / Google Lens / any
    # generic QR app previously saw the JSON blob as raw text
    # (unusable). URL routes them to a public verify page on the
    # rootsTALK domain — friendly landing that confirms authenticity
    # even for farmers without the PWA installed. The PWA scanner
    # extracts the qr_id from the URL and calls the scoped scan
    # endpoint.
    # UUID is client-side default (SQLAlchemy default=new_uuid runs
    # in Python), so we can build the URL before flush.
    # Pre-generate the UUID so we can embed it in the payload URL
    # before insert. SA's `default=new_uuid` runs at INSERT time, not
    # at __init__ — so `qr.id` is None right after instantiation.
    qr_id = str(uuid.uuid4())
    qr = ProductQRCode(
        id=qr_id,
        client_id=client_id,
        product_type=request.product_type,
        brand_cosh_id=request.brand_cosh_id,
        variety_id=request.variety_id,
        product_display_name=request.product_display_name,
        manufacture_date=date.fromisoformat(request.manufacture_date),
        expiry_date=date.fromisoformat(request.expiry_date),
        batch_lot_number=request.batch_lot_number,
        qr_payload=f"{_public_base_url()}/verify/{qr_id}",
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
    """BL-18 Bulk: validate CSV rows, skip duplicates, generate valid rows.

    2026-07-05 — Portfolio-anchored. Each CSV row's Trade / Variety
    Name must resolve to a Portfolio entry — otherwise the resulting
    QR would have no brand_cosh_id / variety_id in its payload and
    every scan would mismatch. Unmatched names come back as FAILED
    with a "not in Portfolio — add it first" message.
    """
    from app.modules.seed_mgmt.models import SeedVariety

    await _assert_client_can_qr(db, client_id)
    content = await file.read()
    reader = csv.DictReader(io.StringIO(content.decode("utf-8-sig")))

    client = (await db.execute(select(Client).where(Client.id == client_id))).scalar_one_or_none()

    # Build a display_name → (brand_cosh_id, variety_id) resolver from
    # the client's active portfolio. Case-insensitive on the name so
    # small CSV typos ("Bt Cotton XYZ-42" vs "bt cotton xyz-42") match.
    portfolio_rows = (await db.execute(
        select(ManufacturerBrandPortfolio).where(
            ManufacturerBrandPortfolio.client_id == client_id,
            ManufacturerBrandPortfolio.status == "ACTIVE",
        )
    )).scalars().all()
    resolver: dict[str, tuple[Optional[str], Optional[str]]] = {}
    # Brand names come from Cosh trade_names; variety names come from
    # seed_varieties. Batch-resolve both.
    brand_ids = [p.brand_cosh_id for p in portfolio_rows if p.brand_cosh_id]
    var_ids = [p.variety_id for p in portfolio_rows if p.variety_id]
    brand_name_by_id: dict[str, str] = {}
    var_name_by_id: dict[str, str] = {}
    if brand_ids:
        brand_rows = (await db.execute(
            select(CoshCoreItem.cosh_id, CoshCoreItem.translations).where(
                CoshCoreItem.cosh_id.in_(brand_ids),
            )
        )).all()
        for cid, tr in brand_rows:
            if isinstance(tr, dict):
                brand_name_by_id[cid] = (tr.get("en") or next(
                    (v for v in tr.values() if v), cid,
                ))
    if var_ids:
        var_rows = (await db.execute(
            select(SeedVariety.id, SeedVariety.name).where(
                SeedVariety.id.in_(var_ids),
            )
        )).all()
        var_name_by_id = {vid: vname for vid, vname in var_rows}
    for p in portfolio_rows:
        if p.brand_cosh_id and p.brand_cosh_id in brand_name_by_id:
            resolver[brand_name_by_id[p.brand_cosh_id].strip().lower()] = (p.brand_cosh_id, None)
        elif p.variety_id and p.variety_id in var_name_by_id:
            resolver[var_name_by_id[p.variety_id].strip().lower()] = (None, p.variety_id)

    results = []
    generated = 0
    skipped_dup = 0
    failed = 0

    for i, row in enumerate(reader, start=2):
        product_type = (row.get("Product Type") or "").strip()
        display_name = (row.get("Trade Name / Variety Name") or row.get("Trade Name") or row.get("Variety Name") or "").strip()
        mfr_date = (row.get("Manufacture or Production Date") or row.get("Manufacture Date") or row.get("Production Date") or "").strip()
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

        resolved = resolver.get(display_name.strip().lower())
        if not resolved:
            results.append({
                "row": i, "status": "FAILED",
                "reason": "Not in Portfolio — add this Trade/Variety name to your Portfolio first.",
                "display_name": display_name,
            })
            failed += 1
            continue
        row_brand_cosh_id, row_variety_id = resolved

        try:
            key = dedup_key(
                brand_cosh_id=row_brand_cosh_id,
                variety_id=row_variety_id,
                product_display_name=display_name,
                batch_lot_number=batch_lot,
            )
        except DedupKeyError as exc:
            results.append({"row": i, "status": "FAILED", "reason": str(exc), "display_name": display_name})
            failed += 1
            continue

        existing = await _find_qr_dupe(db, client_id, key)
        if existing:
            results.append({"row": i, "status": "DUPLICATE", "reason": "Batch already generated", "display_name": display_name})
            skipped_dup += 1
            continue

        # BL-18 audit (2026-05-06): keep dates as date objects rather
        # than re-stringifying — asyncpg rejects raw strings on Date
        # columns. Was a latent bug; not exercised because the bulk
        # path had no tests before this audit.
        mfr_date_obj = datetime.strptime(mfr_date, "%d-%m-%Y").date()
        exp_date_obj = datetime.strptime(exp_date, "%d-%m-%Y").date()
        # URL-encoded payload — matches the single-generation path.
        # Pre-generate qr_id so it's baked into the URL at insert.
        qr_id = str(uuid.uuid4())
        payload = f"{_public_base_url()}/verify/{qr_id}"
        # BL-18 audit (2026-05-06): wrap each insert in a SAVEPOINT
        # so an IntegrityError on one row (e.g. a race against a
        # concurrent insert that the in-app check missed) marks just
        # that row as DUPLICATE — pre-fix the whole bulk transaction
        # rolled back and every valid sibling row was lost despite
        # being reported as OK in the summary.
        try:
            async with db.begin_nested():
                qr = ProductQRCode(
                    id=qr_id,
                    client_id=client_id, product_type=product_type,
                    brand_cosh_id=row_brand_cosh_id,
                    variety_id=row_variety_id,
                    product_display_name=display_name,
                    manufacture_date=mfr_date_obj, expiry_date=exp_date_obj,
                    batch_lot_number=batch_lot, qr_payload=payload,
                    created_by=current_user.id,
                )
                db.add(qr)
                await db.flush()
        except IntegrityError:
            results.append({"row": i, "status": "DUPLICATE", "reason": "Caught by unique constraint at insert", "display_name": display_name})
            skipped_dup += 1
            continue
        results.append({"row": i, "status": "OK", "display_name": display_name, "batch_lot": batch_lot})
        generated += 1

    await db.commit()
    return {
        "summary": {"generated": generated, "skipped_duplicates": skipped_dup, "failed": failed},
        "rows": results,
    }


@router.get("/client/{client_id}/qr/bulk-template")
async def download_bulk_template(client_id: str, kind: str = "pesticide"):
    """Return a CSV template with headers and one sample row. Split
    by `kind` — 'pesticide' (default, covers pesticide + fertilizer)
    or 'seed'. Column labels + sample rows differ so the CA doesn't
    have to hunt for the right one:

      pesticide.csv → Trade Name column, Manufacture Date phrasing.
      seed.csv      → Variety Name column, Production Date phrasing,
                       Lot Number instead of Batch Number.

    The upload endpoint accepts both header variants (it reads any
    of "Trade Name / Variety Name" | "Trade Name" | "Variety Name"
    and any of "Manufacture..." | "Production..." | "Batch..." |
    "Lot..."), so mixing rows still works — the split is just to
    make the download step less confusing."""
    if kind == "seed":
        header = "Product Type,Variety Name,Production Date,Expiry Date,Lot Number\n"
        sample = "Seed,Bt Cotton XYZ-42,01-01-2026,31-12-2026,LOT001\n"
        fname = "qr_bulk_template_seed.csv"
    else:
        header = "Product Type,Trade Name,Manufacture Date,Expiry Date,Batch Number\n"
        sample = "Pesticide,BrandXYZ Gold,01-01-2026,31-12-2026,BATCH001\n"
        fname = "qr_bulk_template_pesticide.csv"
    return Response(
        content=(header + sample).encode(),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={fname}"},
    )


# ── QR Code download ───────���───────────────────────────────────────────────────

@router.get("/client/{client_id}/qr/codes/{qr_id}/download")
async def download_qr_code(
    client_id: str, qr_id: str,
    format: str = "PNG",
    size: str = "MEDIUM",
    size_cm: Optional[float] = None,
    style: str = "color",
    label: bool = True,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Download QR code as PNG or print-ready PDF.

    Print modes via `style`:
      color (default) — dark-green modules + colored logo + label.
      mono            — pure-black modules + black-silhouette logo + label.
      raw             — pure-black modules, no logo, no label (dot-matrix / CIJ).
    `label` toggles the "rootsTALK.in" line under the QR — auto False when style=raw.
    """
    await _assert_client_can_qr(db, client_id)
    qr_record = (await db.execute(
        select(ProductQRCode).where(ProductQRCode.id == qr_id, ProductQRCode.client_id == client_id)
    )).scalar_one_or_none()
    if not qr_record:
        raise HTTPException(status_code=404)

    px_size = int((size_cm or PRODUCT_TYPE_SIZES.get(size.upper(), 3.5)) * 37.8)

    branded = _build_branded_qr_png(
        qr_record.qr_payload or qr_id,
        px_size,
        style=style if style in ("color", "mono", "raw") else "color",
        label=label,
    )

    if format.upper() == "PNG":
        buf = io.BytesIO()
        branded.save(buf, format="PNG")
        buf.seek(0)
        fname = f"{qr_record.product_display_name}_{qr_record.batch_lot_number}.png"
        return Response(content=buf.read(), media_type="image/png",
                        headers={"Content-Disposition": f'attachment; filename="{fname}"'})

    # PDF single. The branded PNG already carries the rootsTALK.in
    # label baked in, so the PDF just drops it onto a page + adds the
    # product / batch / date metadata below.
    pdf_buf = io.BytesIO()
    c = pdf_canvas.Canvas(pdf_buf, pagesize=A4)
    w, h = A4
    dim_cm = size_cm or PRODUCT_TYPE_SIZES.get(size.upper(), 3.5)
    dim_pt = dim_cm * cm
    # Branded PNG is a taller-than-wide rectangle (QR + label). Preserve
    # aspect ratio in the PDF so the label doesn't get squished.
    aspect = branded.size[1] / branded.size[0]
    img_w = dim_pt
    img_h = dim_pt * aspect

    img_buf = io.BytesIO()
    branded.save(img_buf, format="PNG")
    img_buf.seek(0)
    from reportlab.lib.utils import ImageReader
    top_y = h - img_h - 90
    c.drawImage(ImageReader(img_buf), (w - img_w) / 2, top_y, img_w, img_h)
    c.setFont("Helvetica-Bold", 11)
    y = top_y - 22
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
    await _assert_client_can_qr(db, client_id)
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
    """Farmer scans QR code from Purchased Items. Returns MATCH / MISMATCH / INACTIVE.

    Accepts EITHER `order_item_id` (pesticide/fertilizer path, matches
    against OrderItem.brand_cosh_id) OR `seed_order_id` (seed path,
    matches against SeedOrderFull.variety_id). Exactly one must be
    provided — 422 otherwise.
    """
    from app.modules.seed_mgmt.models import SeedOrderFull

    qr_payload = data.get("qr_payload", "")
    order_item_id = data.get("order_item_id")
    seed_order_id = data.get("seed_order_id")

    if bool(order_item_id) == bool(seed_order_id):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "scan_target_required",
                "message": "Exactly one of order_item_id / seed_order_id is required.",
            },
        )

    # Payload lookup — supports two formats:
    #   1. New (2026-07-05+): URL "<base>/verify/<qr_id>". Extract
    #      qr_id and look up the row by ID directly. Farmer scanning
    #      via native camera lands on the same URL as a public
    #      verify page; PWA scanner extracts the ID and POSTs here.
    #   2. Legacy JSON blob (pre-2026-07-05 QRs, if any exist by the
    #      time this ships): still parseable for compat.
    qr_record = None
    scanned_brand_cosh_id = None
    scanned_variety_id = None
    display_name = ""
    if qr_payload.startswith(("http://", "https://")):
        try:
            path_parts = [p for p in urlparse(qr_payload).path.split("/") if p]
            if path_parts and path_parts[-2:-1] == ["verify"]:
                scan_qr_id = path_parts[-1]
                qr_record = (await db.execute(
                    select(ProductQRCode).where(ProductQRCode.id == scan_qr_id)
                )).scalar_one_or_none()
                if qr_record:
                    scanned_brand_cosh_id = qr_record.brand_cosh_id
                    scanned_variety_id = qr_record.variety_id
                    display_name = qr_record.product_display_name
        except Exception:
            pass
    else:
        try:
            payload_data = json.loads(qr_payload)
        except Exception:
            payload_data = {}
        legacy_client_id = payload_data.get("client_id")
        batch_lot = payload_data.get("batch_lot")
        scanned_brand_cosh_id = payload_data.get("brand_cosh_id")
        scanned_variety_id = payload_data.get("variety_id")
        display_name = payload_data.get("display_name", "")
        if legacy_client_id and batch_lot:
            qr_record = (await db.execute(
                select(ProductQRCode).where(
                    ProductQRCode.client_id == legacy_client_id,
                    ProductQRCode.batch_lot_number == batch_lot,
                )
            )).scalar_one_or_none()

    # Fetch the appropriate target row and derive the expected match
    # value + column. Kept in a small tuple so the persist / compare
    # code below is uniform across both paths.
    item = None
    seed_order = None
    expected_brand = None
    expected_variety = None
    if order_item_id:
        item = (await db.execute(
            select(OrderItem).where(OrderItem.id == order_item_id)
        )).scalar_one_or_none()
        expected_brand = item.brand_cosh_id if item else None
    else:
        seed_order = (await db.execute(
            select(SeedOrderFull).where(SeedOrderFull.id == seed_order_id)
        )).scalar_one_or_none()
        expected_variety = seed_order.variety_id if seed_order else None

    if not qr_record or qr_record.status == "INACTIVE":
        scan = QRScan(
            qr_code_id=qr_record.id if qr_record else None,
            farmer_user_id=current_user.id,
            order_item_id=order_item_id,
            seed_order_id=seed_order_id,
            match_status="INACTIVE_CODE",
        )
        db.add(scan)
        await db.commit()
        return {"match_status": "INACTIVE_CODE",
                "message": "This product code is no longer active. Please contact your dealer."}

    is_match = False
    if order_item_id:
        is_match = bool(scanned_brand_cosh_id and expected_brand
                        and scanned_brand_cosh_id == expected_brand)
    else:
        is_match = bool(scanned_variety_id and expected_variety
                        and scanned_variety_id == expected_variety)

    # Count previous scan attempts for this target
    if order_item_id:
        prev_scans = (await db.execute(
            select(QRScan).where(QRScan.order_item_id == order_item_id)
        )).scalars().all()
    else:
        prev_scans = (await db.execute(
            select(QRScan).where(QRScan.seed_order_id == seed_order_id)
        )).scalars().all()
    attempt_num = len(prev_scans) + 1

    scan = QRScan(
        qr_code_id=qr_record.id,
        farmer_user_id=current_user.id,
        order_item_id=order_item_id,
        seed_order_id=seed_order_id,
        match_status="MATCH" if is_match else "MISMATCH",
        expected_brand_cosh_id=expected_brand,
        scanned_brand_cosh_id=scanned_brand_cosh_id,
        scan_attempt_number=attempt_num,
    )
    db.add(scan)

    if is_match:
        if item:
            item.scan_verified = True
        elif seed_order:
            seed_order.scan_verified = True

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
    """Mismatch log for the CA. Includes scans of both pesticide/
    fertilizer (OrderItem-anchored) and seed (SeedOrderFull-anchored)
    QRs — the CA's view of "farmer scanned something that didn't
    match their order" is unified across both product paths."""
    from app.modules.seed_mgmt.models import SeedOrderFull

    await _assert_client_can_qr(db, client_id)
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
        dealer_user_id = None
        if scan.order_item_id:
            item = (await db.execute(
                select(OrderItem).where(OrderItem.id == scan.order_item_id)
            )).scalar_one_or_none()
            if item and hasattr(item, 'order') and item.order:
                dealer_user_id = item.order.dealer_user_id
        elif scan.seed_order_id:
            seed_order = (await db.execute(
                select(SeedOrderFull).where(SeedOrderFull.id == scan.seed_order_id)
            )).scalar_one_or_none()
            if seed_order:
                dealer_user_id = seed_order.dealer_user_id
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
            "product_type": qr_code.product_type,
            "dealer_user_id": dealer_user_id,
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

    # BL-16 audit (2026-05-06): URL composition lifted into the
    # bl16_crop_record service. Pre-fix the path was `/crop/...` and
    # the domain was hardcoded to a non-prod, non-spec value.
    public_url = crop_record_public_url(_public_base_url(), sub.reference_number)

    qr = qrcode.QRCode(version=1, box_size=8, border=4)
    qr.add_data(public_url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return Response(content=buf.read(), media_type="image/png",
                    headers={"Content-Disposition": f'inline; filename="crop-{sub.reference_number}.png"'})


@router.get("/public/qr-verify/{qr_id}")
async def public_qr_verify(qr_id: str, db: AsyncSession = Depends(get_db)):
    """PUBLIC — no auth. Backing endpoint for the /verify/{qr_id}
    public page rendered by the PWA. When a farmer scans a rootsTALK
    QR with a generic camera / QR app, their browser opens
    <base>/verify/<qr_id>; that page calls this endpoint to render
    the full "genuine + who + product + how-to-grow" landing.

    This is the unscoped verify: it doesn't compare against a
    farmer's order — that requires the scoped /farmer/qr/scan
    endpoint from inside the PWA (auth'd, matches against
    OrderItem or SeedOrderFull).

    2026-07-05 (Phase 7) — response expanded to power a full,
    brand-coloured landing:
      product: display name + type + batch/lot + mfr/exp
      company: name + logo + tagline + brand primary+secondary
               colours + office address + phone + website
      seed:    cultivation_notes (when the QR resolves to a
               SeedVariety with notes populated — satisfies the
               govt-mandated seed-pouch QR write-up)
    """
    from app.modules.seed_mgmt.models import SeedVariety

    qr = (await db.execute(
        select(ProductQRCode).where(ProductQRCode.id == qr_id)
    )).scalar_one_or_none()
    if not qr:
        raise HTTPException(
            status_code=404,
            detail={
                "verified": False,
                "reason": "This is not a valid rootsTALK QR code.",
            },
        )
    client = (await db.execute(
        select(Client).where(Client.id == qr.client_id)
    )).scalar_one_or_none()

    company: dict[str, str | None] = {
        "name": None, "tagline": None, "logo_url": None,
        "primary_colour": None, "secondary_colour": None,
        "hq_address": None, "website": None,
        "support_phone": None, "office_phone": None,
    }
    if client:
        company = {
            "name": client.display_name or client.full_name,
            "tagline": client.tagline,
            "logo_url": client.logo_url,
            "primary_colour": client.primary_colour,
            "secondary_colour": client.secondary_colour,
            "hq_address": client.hq_address,
            "website": client.website,
            "support_phone": client.support_phone,
            "office_phone": client.office_phone,
        }

    cultivation_notes = None
    if qr.variety_id:
        variety = (await db.execute(
            select(SeedVariety.cultivation_notes).where(
                SeedVariety.id == qr.variety_id,
            )
        )).scalar_one_or_none()
        cultivation_notes = variety

    return {
        "verified": qr.status == "ACTIVE",
        "reason": (
            None if qr.status == "ACTIVE"
            else "This QR code has been deactivated by the manufacturer. Please contact your dealer."
        ),
        "status": qr.status,
        # Product block
        "product_display_name": qr.product_display_name,
        "product_type": qr.product_type,
        "batch_lot_number": qr.batch_lot_number,
        "manufacture_date": str(qr.manufacture_date),
        "expiry_date": str(qr.expiry_date),
        # Company block — for the brand-coloured landing.
        "company": company,
        # Seed-only cultivation write-up.
        "cultivation_notes": cultivation_notes,
    }


@router.get("/public/crop-record/{reference_number}")
async def get_crop_public_page(
    reference_number: str,
    db: AsyncSession = Depends(get_db),
):
    """PUBLIC — no auth. Returns crop record data for the
    traceability web page reached by scanning the QR.

    BL-16 audit (2026-05-06): route path moved from `/public/crop/`
    to `/public/crop-record/` to match the spec URL the QR encodes;
    response trimmed to the spec-permitted fields via
    `public_record_payload` (privacy: pre-fix the route leaked
    farmer_district + farmer_state + package_name + subscription_date
    + status on this unauthenticated URL); reads
    `parameter_variable_summary` from FarmerSubscriptionHistory
    (column exists but is currently never written by the backend —
    deferred follow-up; field will be null until that writer lands).
    """
    # 2026-06-06 — Spec widened by user direction. Public page now
    # shows: farmer name + phone, district + state, crop name (resolved
    # from cosh), company, start date, closure date (start +
    # package.duration_days), package name + id, and the package's
    # parameters-options fingerprint (one row per Parameter with the
    # selected Variable for this package). Earlier BL-16 trim is
    # superseded — user explicitly wants this info on the
    # unauthenticated record they themselves print on the QR.
    from app.modules.advisory.models import Package, Parameter, Variable, PackageVariable
    from app.modules.sync.models import CoshCoreItem
    from datetime import timedelta

    sub = (await db.execute(
        select(Subscription).where(Subscription.reference_number == reference_number)
    )).scalar_one_or_none()
    if not sub:
        raise HTTPException(status_code=404, detail="Crop record not found")

    farmer = (await db.execute(select(User).where(User.id == sub.farmer_user_id))).scalar_one_or_none()
    client = (await db.execute(select(Client).where(Client.id == sub.client_id))).scalar_one_or_none()
    package = (await db.execute(select(Package).where(Package.id == sub.package_id))).scalar_one_or_none()

    # Resolve cosh-id names (crop, district, state) via translations.
    cosh_ids = [
        x for x in (
            package.crop_cosh_id if package else None,
            farmer.district_cosh_id if farmer else None,
            farmer.state_cosh_id if farmer else None,
        ) if x
    ]
    cosh_name_by_id: dict[str, str | None] = {}
    if cosh_ids:
        rows = (await db.execute(
            select(CoshCoreItem.cosh_id, CoshCoreItem.translations)
            .where(CoshCoreItem.cosh_id.in_(cosh_ids))
        )).all()
        for cid, tr in rows:
            cosh_name_by_id[cid] = (tr or {}).get("en") if isinstance(tr, dict) else None

    # Package fingerprint: one row per Parameter with the selected
    # Variable for this Package. Surfaces what the package is
    # configured for (e.g. Soil Type: Black, Irrigation: Drip).
    parameters_options: list[dict] = []
    if package:
        pv_rows = (await db.execute(
            select(Parameter.name, Variable.name, Parameter.display_order)
            .join(PackageVariable, PackageVariable.parameter_id == Parameter.id)
            .join(Variable, Variable.id == PackageVariable.variable_id)
            .where(PackageVariable.package_id == package.id)
            .order_by(Parameter.display_order, Parameter.name)
        )).all()
        for p_name, v_name, _ord in pv_rows:
            parameters_options.append({
                "parameter_name": p_name,
                "option_name": v_name,
            })

    # Closure date — start + duration. Both nullable; if either is
    # absent the page shows "—". Subscription.crop_start_date can be
    # null when the farmer hasn't set it yet.
    closure_date = None
    if sub.crop_start_date and package and package.duration_days:
        start_d = sub.crop_start_date.date() if hasattr(sub.crop_start_date, "date") else sub.crop_start_date
        closure_date = (start_d + timedelta(days=package.duration_days)).isoformat()

    crop_name = (
        cosh_name_by_id.get(package.crop_cosh_id)
        if package and package.crop_cosh_id else None
    )

    return {
        "reference_number": sub.reference_number,
        "farmer_name": farmer.name if farmer else None,
        "farmer_phone": farmer.phone if farmer else None,
        "farmer_district": cosh_name_by_id.get(farmer.district_cosh_id) if farmer and farmer.district_cosh_id else None,
        "farmer_state": cosh_name_by_id.get(farmer.state_cosh_id) if farmer and farmer.state_cosh_id else None,
        "crop_name": crop_name,
        "company": client.display_name or client.full_name if client else None,
        "package_id": sub.package_id,
        "package_name": package.name if package else None,
        "crop_start_date": (
            sub.crop_start_date.date().isoformat()
            if sub.crop_start_date and hasattr(sub.crop_start_date, "date")
            else (sub.crop_start_date.isoformat() if sub.crop_start_date else None)
        ),
        "crop_closure_date": closure_date,
        "parameters_options": parameters_options,
    }


@router.get("/public/crop/{reference_number}", include_in_schema=False)
async def get_crop_public_page_legacy_alias(reference_number: str):
    """Legacy alias for the BL-16 audit (2026-05-06). The public route
    moved from `/public/crop/{ref}` to `/public/crop-record/{ref}` to
    match the spec URL the QR now encodes. Anything still calling the
    old path (PWA frontend code that hasn't been updated, a printed
    QR generated against a previous build) gets a 301 redirect to the
    new path. Hidden from OpenAPI schema since it's a deprecation
    bridge, not a documented surface.
    """
    return RedirectResponse(
        url=f"/public/crop-record/{reference_number}",
        status_code=301,
    )


# ── Helpers ────────────────────────────────────────────────────────────────────

def _validate_dates(mfr_date: str, exp_date: str):
    try:
        m = datetime.strptime(mfr_date, "%Y-%m-%d").date()
        e = datetime.strptime(exp_date, "%Y-%m-%d").date()
        if e <= m:
            raise HTTPException(status_code=422, detail="Expiry date must be after manufacture date")
    except ValueError:
        raise HTTPException(status_code=422, detail="Date format must be YYYY-MM-DD")
