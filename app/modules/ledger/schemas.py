"""Pydantic schemas for the Farmer Ledger endpoints. See router.py."""
from datetime import date
from decimal import Decimal
from typing import Literal, Optional

from pydantic import BaseModel, Field


# ── Roster (GET /dealer/ledger/farmers) ─────────────────────────────

class RosterFarmer(BaseModel):
    user_id: str
    name: Optional[str] = None
    phone: Optional[str] = None
    photo_url: Optional[str] = None
    state_cosh_id: Optional[str] = None
    district_cosh_id: Optional[str] = None
    sub_district: Optional[str] = None  # stored on User.sub_district_cosh_id (free-form)
    state_name: Optional[str] = None
    district_name: Optional[str] = None
    last_purchase_date: Optional[date] = None
    recent_purchase_count: int = 0  # last 3 months (own + manual)
    no_purchases_in_12_months: bool = False


class RosterResponse(BaseModel):
    farmers: list[RosterFarmer]
    total_count: int


# ── Per-farmer detail (GET /dealer/ledger/farmers/{user_id}) ────────

class LedgerEntry(BaseModel):
    """A single ledger row. `source` distinguishes render style:
      - "own"          — this dealer's RootsTalk-mediated picked-up sale
      - "own_manual"   — this dealer's manual entry
      - "other_shop"   — anonymised RT-mediated sale at another dealer
    Other-shop rows omit price + product_name to prevent competitive
    intel; only common name / brand / manufacturer / qty / unit / date /
    crop context surface.
    """
    source: Literal["own", "own_manual", "other_shop"]
    date: date
    category: str  # SEED | PESTICIDE | FERTILIZER | other l1_type passthrough
    product_name: Optional[str] = None
    brand: Optional[str] = None
    manufacturer: Optional[str] = None
    qty: Optional[Decimal] = None
    unit: Optional[str] = None
    price: Optional[Decimal] = None  # None for other_shop
    # sub context (nullable — manual entries have no sub)
    subscription_id: Optional[str] = None
    package_id: Optional[str] = None
    crop_name: Optional[str] = None
    crop_start_date: Optional[date] = None
    advising_company: Optional[str] = None
    # Only present for own / own_manual — dealer's records ARE editable
    manual_sale_id: Optional[str] = None


class FarmerDetail(BaseModel):
    user_id: str
    name: Optional[str] = None
    phone: Optional[str] = None
    photo_url: Optional[str] = None
    state_cosh_id: Optional[str] = None
    district_cosh_id: Optional[str] = None
    sub_district: Optional[str] = None
    state_name: Optional[str] = None
    district_name: Optional[str] = None
    note: Optional[str] = None
    # True when the farmer has completed PWA self-registration
    # (password_hash set). Claimed farmers own their own profile —
    # the dealer cannot edit their name/address. False for
    # dealer-added unclaimed farmers, which stay editable by any
    # dealer (shared canonical, last-write-wins).
    is_claimed: bool = False
    entries: list[LedgerEntry]


# ── Phone lookup (POST /dealer/ledger/lookup-phone) ─────────────────

class PhoneLookupRequest(BaseModel):
    phone: str


class PhoneLookupResponse(BaseModel):
    found: bool
    user_id: Optional[str] = None
    name: Optional[str] = None
    phone: Optional[str] = None  # normalised
    photo_url: Optional[str] = None
    state_cosh_id: Optional[str] = None
    district_cosh_id: Optional[str] = None
    sub_district: Optional[str] = None
    state_name: Optional[str] = None
    district_name: Optional[str] = None


# ── Manual sale create / update / delete ────────────────────────────

class NewFarmerFields(BaseModel):
    phone: str
    name: str
    state_cosh_id: str
    district_cosh_id: str
    sub_district: Optional[str] = None


class SaleFields(BaseModel):
    category: Literal["SEED", "PESTICIDE", "FERTILIZER"]
    product_name: str = Field(..., min_length=1, max_length=255)
    brand: Optional[str] = Field(None, max_length=255)
    manufacturer: Optional[str] = Field(None, max_length=255)
    qty: Decimal
    unit: str = Field(..., min_length=1, max_length=20)
    price: Optional[Decimal] = None
    sale_date: date
    notes: Optional[str] = None


class ManualSaleCreateRequest(BaseModel):
    """Exactly one of farmer_user_id / new_farmer must be provided."""
    farmer_user_id: Optional[str] = None
    new_farmer: Optional[NewFarmerFields] = None
    sale: SaleFields


class ManualSaleUpdateRequest(BaseModel):
    category: Optional[Literal["SEED", "PESTICIDE", "FERTILIZER"]] = None
    product_name: Optional[str] = Field(None, min_length=1, max_length=255)
    brand: Optional[str] = Field(None, max_length=255)
    manufacturer: Optional[str] = Field(None, max_length=255)
    qty: Optional[Decimal] = None
    unit: Optional[str] = Field(None, min_length=1, max_length=20)
    price: Optional[Decimal] = None
    sale_date: Optional[date] = None
    notes: Optional[str] = None


class ManualSaleResponse(BaseModel):
    id: str
    farmer_user_id: str


# ── Personal note ───────────────────────────────────────────────────

class NoteUpdateRequest(BaseModel):
    note: str


# ── Farmer info edit (shared canonical, last-write-wins) ────────────

class FarmerInfoUpdateRequest(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    state_cosh_id: Optional[str] = None
    district_cosh_id: Optional[str] = None
    sub_district: Optional[str] = None
