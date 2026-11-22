"""SA-facing endpoints for Feature Access management.

Write operations are SA-only via `_require_sa`. Reads of a single
client's feature state are also SA-only here; CP/PWA code paths get
the derived "is-active" via `service.client_has_feature(...)` and
never touch this router directly.

Append-only semantics — every mutation writes a new row. The most
recent row is the current state; the full history is preserved.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.dependencies import get_current_user
from app.modules.clients.models import Client
from app.modules.platform.models import User
from app.modules.feature_access.models import (
    ClientFeatureAccess,
    KNOWN_FEATURES,
)
from app.modules.feature_access.service import (
    latest_feature_row,
    row_days_until_expiry,
    row_is_active,
)


router = APIRouter(tags=["SA Feature Access"])


def _require_sa(current_user: User) -> None:
    if current_user.email != settings.sa_email:
        raise HTTPException(
            status_code=403, detail="Super Admin access required",
        )


def _assert_known_feature(feature: str) -> None:
    if feature not in KNOWN_FEATURES:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "unknown_feature",
                "message": (
                    f"Unknown feature '{feature}'. Known values: "
                    f"{sorted(KNOWN_FEATURES)}."
                ),
            },
        )


# ── Body schemas ─────────────────────────────────────────────────────────

class ActivateBody(BaseModel):
    # Required for v1 — no perpetual-access concept yet.
    access_until: datetime = Field(
        ..., description="End of the paid access window (UTC).",
    )
    notes: Optional[str] = Field(
        None, description="Free-text note for audit (e.g. invoice ref, renewal terms).",
    )


class TurnOffBody(BaseModel):
    notes: Optional[str] = Field(
        None, description="Reason for manual turn-off before expiry.",
    )


# ── Shaping helpers ──────────────────────────────────────────────────────

def _shape_row(r: ClientFeatureAccess) -> dict:
    return {
        "id": r.id,
        "feature": r.feature,
        "access_until": r.access_until.isoformat() if r.access_until else None,
        "notes": r.notes,
        "activated_by_user_id": r.activated_by_user_id,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "is_active": row_is_active(r),
        "days_until_expiry": row_days_until_expiry(r),
    }


# ── Per-client read ──────────────────────────────────────────────────────

@router.get("/admin/sa/clients/{cid}/features")
async def list_client_features(
    cid: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Per-feature current state + full history for this client.

    Response shape:
    ```
    {
      "client_id": "...",
      "features": {
        "messaging":   { "current": {...} | null, "history": [...] },
        "touchpoints": { "current": {...} | null, "history": [...] }
      }
    }
    ```

    `current` is the most recent row (or null if never activated);
    `history` is every row in reverse-chronological order.
    """
    _require_sa(current_user)
    client = (await db.execute(
        select(Client).where(Client.id == cid)
    )).scalar_one_or_none()
    if client is None:
        raise HTTPException(status_code=404, detail="Client not found")

    rows = (await db.execute(
        select(ClientFeatureAccess)
        .where(ClientFeatureAccess.client_id == cid)
        .order_by(
            ClientFeatureAccess.feature,
            ClientFeatureAccess.created_at.desc(),
        )
    )).scalars().all()

    out: dict[str, dict] = {
        f: {"current": None, "history": []} for f in sorted(KNOWN_FEATURES)
    }
    for r in rows:
        bucket = out.setdefault(
            r.feature, {"current": None, "history": []},
        )
        shaped = _shape_row(r)
        bucket["history"].append(shaped)
        if bucket["current"] is None:
            # First row seen per feature = the most recent one (ordered desc).
            bucket["current"] = shaped
    return {"client_id": cid, "features": out}


# ── Activate / Extend ────────────────────────────────────────────────────

@router.put("/admin/sa/clients/{cid}/features/{feature}")
async def activate_or_extend_feature(
    cid: str,
    feature: str,
    body: ActivateBody,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Activate a feature for this client, or extend its access_until.

    Append-only: always writes a new row. The caller doesn't need to
    distinguish "activate" from "extend" — same action.

    `access_until` must be in the future (we allow equal-to-now for
    testing, but reject earlier timestamps — that'd be a turn-off,
    which has its own endpoint).
    """
    _require_sa(current_user)
    _assert_known_feature(feature)
    client = (await db.execute(
        select(Client).where(Client.id == cid)
    )).scalar_one_or_none()
    if client is None:
        raise HTTPException(status_code=404, detail="Client not found")

    now = datetime.now(timezone.utc)
    if body.access_until < now:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "access_until_in_past",
                "message": (
                    "access_until must not be in the past. Use the "
                    "turn-off endpoint to end access immediately."
                ),
            },
        )

    row = ClientFeatureAccess(
        client_id=cid,
        feature=feature,
        access_until=body.access_until,
        activated_by_user_id=current_user.id,
        notes=body.notes,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return _shape_row(row)


# ── Turn-off (manual, before expiry) ─────────────────────────────────────

@router.delete("/admin/sa/clients/{cid}/features/{feature}")
async def turn_off_feature(
    cid: str,
    feature: str,
    body: TurnOffBody = TurnOffBody(),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Manually end access to this feature right now.

    Writes an append-only row with `access_until = now()` — makes the
    derived "is-active" check consistent for both natural expiry and
    early manual turn-off. If the feature is already expired, this is
    a no-op (returns 422 to avoid noise rows).
    """
    _require_sa(current_user)
    _assert_known_feature(feature)
    client = (await db.execute(
        select(Client).where(Client.id == cid)
    )).scalar_one_or_none()
    if client is None:
        raise HTTPException(status_code=404, detail="Client not found")

    latest = await latest_feature_row(db, cid, feature)
    if latest is None or not row_is_active(latest):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "feature_not_active",
                "message": "Feature is not currently active for this client.",
            },
        )

    now = datetime.now(timezone.utc)
    row = ClientFeatureAccess(
        client_id=cid,
        feature=feature,
        access_until=now,
        activated_by_user_id=current_user.id,
        notes=body.notes or "Manually turned off before expiry.",
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return _shape_row(row)


# ── Dashboard overview ───────────────────────────────────────────────────

@router.get("/admin/sa/feature-access/overview")
async def feature_access_overview(
    expiring_within_days: int = Query(30, ge=1, le=365),
    expired_within_days: int = Query(30, ge=1, le=365),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Data for the SA dashboard Feature Access panel.

    Returns, per known feature:
      - `active_count`: clients whose latest row has access_until > now
      - `expiring`: list of {client_id, display_name, access_until,
        days_until_expiry} for clients active-but-expiring within N days
      - `expired_recent`: list of recently expired clients (for renewal
        follow-up)
    """
    _require_sa(current_user)
    now = datetime.now(timezone.utc)

    # Latest-row-per-(client, feature) via a window function. Postgres-
    # only but we're Postgres everywhere.
    from sqlalchemy import text
    latest_sql = text("""
        WITH ranked AS (
          SELECT
            cfa.*,
            ROW_NUMBER() OVER (
              PARTITION BY cfa.client_id, cfa.feature
              ORDER BY cfa.created_at DESC
            ) AS rn
          FROM client_feature_access cfa
        )
        SELECT r.feature, r.client_id, r.access_until,
               c.display_name
        FROM ranked r
        JOIN clients c ON c.id = r.client_id
        WHERE r.rn = 1
    """)
    rows = (await db.execute(latest_sql)).all()

    out: dict[str, dict] = {
        f: {"active_count": 0, "expiring": [], "expired_recent": []}
        for f in sorted(KNOWN_FEATURES)
    }
    for feature, client_id, access_until, display_name in rows:
        bucket = out.setdefault(
            feature, {"active_count": 0, "expiring": [], "expired_recent": []},
        )
        delta_days = int((access_until - now).total_seconds() // 86400)
        if access_until > now:
            bucket["active_count"] += 1
            if delta_days <= expiring_within_days:
                bucket["expiring"].append({
                    "client_id": client_id,
                    "display_name": display_name,
                    "access_until": access_until.isoformat(),
                    "days_until_expiry": delta_days,
                })
        else:
            # Expired. delta_days is negative; show only those within
            # the recent-expiry window for renewal follow-up.
            days_since = -delta_days
            if days_since <= expired_within_days:
                bucket["expired_recent"].append({
                    "client_id": client_id,
                    "display_name": display_name,
                    "access_until": access_until.isoformat(),
                    "days_since_expiry": days_since,
                })

    # Sort within each list.
    for f in out:
        out[f]["expiring"].sort(key=lambda x: x["days_until_expiry"])
        out[f]["expired_recent"].sort(key=lambda x: x["days_since_expiry"])
    return {
        "generated_at": now.isoformat(),
        "expiring_within_days": expiring_within_days,
        "expired_within_days": expired_within_days,
        "features": out,
    }
