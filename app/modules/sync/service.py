"""
Cosh → RootsTalk sync service.

Processes incoming sync payloads from Cosh and persists them into:
  • cosh_core_items            — flat Cosh entities (Cores).
  • cosh_connect_rows          — N-ary Connects with typed endpoints.

Built to the contract in docs/COSH_2_SYNC_CONTRACT.md. That document is
the alignment reference between Cosh 2.0's emitter and this ingest;
any divergence is resolved there before either side patches code.

Classification rule (§4 of the contract)
----------------------------------------
Each incoming item is routed by **payload shape**, not by a hardcoded
entity_type list:

  • item has `positions` dict  → Connect → cosh_connect_rows
  • item has `translations`    → Core    → cosh_core_items

This means new Cosh entities (new image Connects per crop, new Cores
for ITKs, etc.) onboard with zero backend code change. Cosh stays the
schema authority.

Connect endpoints adapter
-------------------------
Cosh emits a `positions` dict keyed by stringified position number:

    {"1": {"cosh_id": "...", "entity_type": "crop"},
     "2": {"cosh_id": "...", "entity_type": "crop_stage"},
     ...}

The adapter reshapes this into our internal endpoints array:

    [{"role": "crop",       "cosh_id": "...", "position": 1},
     {"role": "crop_stage", "cosh_id": "...", "position": 2},
     ...]

`role` mirrors Cosh's `entity_type` for the target — same vocabulary
end-to-end so consumers (BL-08, image lookup) read by Cosh's role
names without translation.

BlankBox sentinel (§5 of the contract)
--------------------------------------
A position whose value is the BlankBox sentinel is dropped from the
endpoints array — downstream code sees the position as absent.
"""
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.sync.models import CoshConnectRow, CoshCoreItem, CoshSyncLog


# ── BlankBox sentinel ───────────────────────────────────────────────────────
# Cosh's wildcard / "no relevant data" sentinel — appears as the value
# in connect endpoints where the row is intentionally unscoped (e.g.
# pest_diagnosis crop=BlankBox means "this diagnosis applies to all
# crops"). In flat Core lists it's noise and must be stripped from
# every UI surface.
#
# 2026-05-21: testing surfaced "BLANK BOX" (all caps with a space)
# in real Cosh data — the prior {"BlankBox", "Blank Box"} match was
# case-sensitive and let it through. Comparison is now case- and
# whitespace-normalised so every reasonable spelling (BlankBox /
# Blank Box / BLANK BOX / "  blank  box  ") matches.

def _normalise_for_blank_box(value: str) -> str:
    """Lowercase + collapse internal whitespace + strip — so any
    spelling Cosh emits collapses to the canonical 'blankbox' /
    'blank box' for matching."""
    return ' '.join(value.split()).strip().casefold()


_BLANK_BOX_NORMALISED: frozenset[str] = frozenset({"blankbox", "blank box"})

# Kept for back-compat with anything that imports this set directly.
# Don't rely on equality membership against this — use _is_blank_box().
BLANK_BOX_VALUES: frozenset[str] = frozenset({
    "BlankBox", "Blank Box", "BLANK BOX", "blankbox", "blank box",
})


def _is_blank_box(value: Optional[str]) -> bool:
    if value is None:
        return False
    return _normalise_for_blank_box(value) in _BLANK_BOX_NORMALISED


# ── Connect-vs-Core classification (shape-driven) ───────────────────────────

def _is_connect(item: dict) -> bool:
    """An item is a Connect when it carries a non-empty `positions`
    dict. Otherwise it's a Core (must carry `translations`)."""
    positions = item.get("positions")
    return isinstance(positions, dict) and bool(positions)


# ── Connect-positions adapter ───────────────────────────────────────────────

def _extract_endpoints(item: dict) -> list[dict]:
    """Reshape Cosh's `positions` dict into our endpoints array.

    Drops:
      • positions whose `cosh_id` is missing or BlankBox-sentinel-valued
      • positions whose `entity_type` is missing
    Sorts by integer position number for stable ordering.
    """
    positions = item.get("positions") or {}
    out: list[dict] = []
    for pos_num_str, pos in sorted(positions.items(), key=lambda kv: int(kv[0])):
        cosh_id = pos.get("cosh_id")
        role = pos.get("entity_type")
        if not cosh_id or not role:
            continue
        if _is_blank_box(cosh_id):
            continue
        out.append({
            "role": role,
            "cosh_id": cosh_id,
            "position": int(pos_num_str),
        })
    return out


def _connect_row_metadata(item: dict) -> Optional[dict]:
    """Connect rows carry scalar attributes outside `positions` (e.g.
    `priority_rank` on pest_diagnosis_chain). Pull every top-level
    field that isn't part of the protocol envelope into metadata."""
    reserved = {"cosh_id", "entity_type", "status", "positions"}
    extras = {k: v for k, v in item.items() if k not in reserved}
    return extras or None


# ── Upserts ─────────────────────────────────────────────────────────────────

async def upsert_core_item(
    db: AsyncSession,
    *,
    cosh_id: str,
    core_type: str,
    parent_cosh_id: Optional[str],
    status: str,
    translations: dict,
    metadata: Optional[dict],
) -> str:
    """Upsert into cosh_core_items. Translations must include `en`."""
    if not translations.get("en"):
        raise ValueError("Missing required translation: en")
    now = datetime.now(timezone.utc)
    stmt = pg_insert(CoshCoreItem).values(
        cosh_id=cosh_id,
        core_type=core_type,
        parent_cosh_id=parent_cosh_id,
        status=status,
        translations=translations,
        metadata_=metadata,
        synced_at=now,
    ).on_conflict_do_update(
        constraint="uq_cosh_core_id_type",
        set_={
            "parent_cosh_id": parent_cosh_id,
            "status": status,
            "translations": translations,
            "metadata": metadata,
            "synced_at": now,
        },
    )
    result = await db.execute(stmt)
    return "inserted" if result.rowcount == 1 else "updated"


async def upsert_connect_row(
    db: AsyncSession,
    *,
    connect_id: str,
    connect_type: str,
    endpoints: list[dict],
    status: str,
    metadata: Optional[dict],
) -> str:
    """Upsert into cosh_connect_rows. `endpoints` must be non-empty."""
    if not endpoints:
        raise ValueError(
            f"Connect {connect_type} for {connect_id!r} has no endpoints"
        )
    now = datetime.now(timezone.utc)
    stmt = pg_insert(CoshConnectRow).values(
        connect_id=connect_id,
        connect_type=connect_type,
        endpoints=endpoints,
        status=status,
        metadata_=metadata,
        synced_at=now,
    ).on_conflict_do_update(
        constraint="uq_cosh_connect_id_type",
        set_={
            "endpoints": endpoints,
            "status": status,
            "metadata": metadata,
            "synced_at": now,
        },
    )
    result = await db.execute(stmt)
    return "inserted" if result.rowcount == 1 else "updated"


# ── Full-sync inactivation ──────────────────────────────────────────────────

async def inactivate_absent_entities(
    db: AsyncSession,
    *,
    entity_type: str,
    is_connect_batch: bool,
    seen_ids: set[str],
) -> None:
    """Full sync: mark active rows of this entity_type whose id is not
    in `seen_ids` as inactive. Routes to cosh_core_items or
    cosh_connect_rows by the batch's classification."""
    if is_connect_batch:
        q = update(CoshConnectRow).where(
            CoshConnectRow.connect_type == entity_type,
            CoshConnectRow.status == "active",
        )
        if seen_ids:
            q = q.where(CoshConnectRow.connect_id.not_in(seen_ids))
        await db.execute(q.values(status="inactive"))
    else:
        q = update(CoshCoreItem).where(
            CoshCoreItem.core_type == entity_type,
            CoshCoreItem.status == "active",
        )
        if seen_ids:
            q = q.where(CoshCoreItem.cosh_id.not_in(seen_ids))
        await db.execute(q.values(status="inactive"))


# ── Main payload processor ──────────────────────────────────────────────────

async def process_payload(
    db: AsyncSession, payload: dict, sync_log: CoshSyncLog,
) -> dict:
    """Process a sync payload per the Cosh 2.0 contract.

    Each batch's items are classified by shape (positions ⇒ Connect,
    translations ⇒ Core) and upserted into the appropriate typed
    table. Mixed-shape batches are technically supported but unusual —
    Cosh's emitter sends one shape per batch."""
    sync_mode = payload.get("sync_mode", "incremental")
    entity_batches = payload.get("entity_batches", [])

    entity_results: list[dict] = []
    total_inserted = 0
    total_updated = 0
    total_failed = 0

    for batch in entity_batches:
        entity_type = batch.get("entity_type")
        items = batch.get("items", [])

        # Classify the batch by inspecting its first valid item.
        # Empty batches are skipped silently.
        batch_is_connect = any(_is_connect(i) for i in items)

        batch_inserted = 0
        batch_updated = 0
        batch_failed = 0
        errors: list[dict] = []
        seen_ids: set[str] = set()

        for item in items:
            cosh_id = item.get("cosh_id")
            try:
                if not cosh_id:
                    raise ValueError("Missing cosh_id")
                item_status = item.get("status", "active")

                if _is_connect(item):
                    endpoints = _extract_endpoints(item)
                    action = await upsert_connect_row(
                        db=db,
                        connect_id=cosh_id,
                        connect_type=entity_type,
                        endpoints=endpoints,
                        status=item_status,
                        metadata=_connect_row_metadata(item),
                    )
                else:
                    translations = item.get("translations", {})
                    action = await upsert_core_item(
                        db=db,
                        cosh_id=cosh_id,
                        core_type=entity_type,
                        parent_cosh_id=item.get("parent_cosh_id"),
                        status=item_status,
                        translations=translations,
                        metadata=item.get("metadata"),
                    )

                seen_ids.add(cosh_id)
                if action == "inserted":
                    batch_inserted += 1
                else:
                    batch_updated += 1

            except Exception as e:
                batch_failed += 1
                errors.append({"cosh_id": cosh_id or "unknown", "reason": str(e)})

        # Full sync: inactivate any (entity_type) rows not seen this run
        if sync_mode == "full":
            await inactivate_absent_entities(
                db,
                entity_type=entity_type,
                is_connect_batch=batch_is_connect,
                seen_ids=seen_ids,
            )

        total_inserted += batch_inserted
        total_updated += batch_updated
        total_failed += batch_failed

        entity_results.append({
            "entity_type": entity_type,
            "received": len(items),
            "inserted": batch_inserted,
            "updated": batch_updated,
            "failed": batch_failed,
            "errors": errors,
        })

    # 2026-06-12 — Auto-rebuild materialised caches that snapshot
    # cosh_core_items.translations at refresh time (not at read), so
    # a sync that brings new Hindi/Kannada/Tamil names has no PWA
    # effect until the caches are rebuilt. Two caches today:
    # brand_lookup_cache (BL-07 dealer brand picker, farmer purchased
    # heading) and dealer_manufacturer_catalog (the dealer Dealerships
    # page). Manual rebuild via the SA admin endpoints remains
    # available; this hook removes the coordination cost.
    #
    # Errors are caught and surfaced on the sync log but do NOT fail
    # the sync — the upstream entity rows are already committed.
    await _maybe_rebuild_caches(db, sync_log, entity_results)

    sync_log.items_synced = total_inserted + total_updated
    sync_log.items_failed = total_failed
    # 2026-05-21 — per-batch breakdown surfaced on the Sync Log UI.
    # Trimmed: drop the per-item `errors` list (can be large) and
    # the `received` count; the UI shows changed-rows-per-type,
    # not the noisy raw input.
    sync_log.entity_summary = [
        {
            "entity_type": e["entity_type"],
            "inserted": e["inserted"],
            "updated": e["updated"],
            "failed": e["failed"],
        }
        for e in entity_results
    ]
    if total_failed > 0 and (total_inserted + total_updated) > 0:
        sync_log.status = "partial"
    elif total_failed > 0:
        sync_log.status = "failed"
    else:
        sync_log.status = "completed"
    sync_log.completed_at = datetime.now(timezone.utc)

    return {
        "sync_id": payload.get("sync_id"),
        "received_at": datetime.now(timezone.utc).isoformat(),
        "status": sync_log.status,
        "summary": {
            "total_items": total_inserted + total_updated + total_failed,
            "inserted": total_inserted,
            "updated": total_updated,
            "inactivated": 0,
            "failed": total_failed,
        },
        "entity_results": entity_results,
    }


# ── Materialised-cache auto-refresh ─────────────────────────────────────────

# Two materialised caches need refreshing when Cosh data changes. They
# each have their own feeding set so a sync that only touches one chain
# (e.g. tradename_formulation) doesn't redundantly rebuild the other.
#
# Cores + Connects that feed `brand_lookup_cache` via
# services/brand_cache.py::rebuild_brand_cache. Mirrors the constants
# imported by that file.
_BRAND_CACHE_FEEDING_ENTITY_TYPES = frozenset({
    # Cores
    "common_names_of_inputs",
    "trade_names",
    "input_manufacturers",
    "formulations",
    "units_data",
    # Connects
    "tradename_commonname",
    "tradename_manufacturer",
    "tradename_formulation",
    "tradenames_units",
})

# Cores + Connects that feed `dealer_manufacturer_catalog` via
# orders/router.py::_rebuild_manufacturer_catalog. Mirrors the
# `_walk_cosh_manufacturers` 3-pass walk: L2 → CN → TN → MFR.
_DEALER_CATALOG_FEEDING_ENTITY_TYPES = frozenset({
    # Cores
    "common_names_of_inputs",
    "input_manufacturers",
    "l2_data",
    # Connects
    "commonnames_l2",
    "tradename_commonname",
    "tradename_manufacturer",
})


def _touched(entity_results: list[dict], feeding: frozenset[str]) -> bool:
    """True if any batch in this sync touched a feeding entity_type AND
    landed at least one inserted/updated row."""
    return any(
        e.get("entity_type") in feeding
        and (e.get("inserted", 0) + e.get("updated", 0)) > 0
        for e in entity_results
    )


def _record_rebuild(
    entity_results: list[dict],
    synthetic_type: str,
    updated: int = 0,
    failed: int = 0,
    reason: str | None = None,
) -> None:
    """Append a synthetic entity_results row so the SA Sync Log surfaces
    the rebuild + its rowcount alongside the rest of the run."""
    entity_results.append({
        "entity_type": synthetic_type,
        "received": 0,
        "inserted": 0,
        "updated": updated,
        "failed": failed,
        "errors": [{"cosh_id": "rebuild", "reason": reason}] if reason else [],
    })


async def _maybe_rebuild_caches(
    db: AsyncSession,
    sync_log: CoshSyncLog,
    entity_results: list[dict],
) -> None:
    """Rebuild brand_lookup_cache and/or dealer_manufacturer_catalog
    when this sync changed any of their feeding Cores/Connects. No-op
    when nothing relevant changed.

    Hooked into process_payload as a fire-and-log step — never fails
    the sync. Any error is caught and recorded on the synthetic
    entity_results row so the operator knows to retry manually:

      - /admin/brand-cache/refresh
      - /admin/dealer/manufacturers-catalog/refresh
    """
    if _touched(entity_results, _BRAND_CACHE_FEEDING_ENTITY_TYPES):
        # Import here to avoid a circular import at module load.
        from app.services.brand_cache import rebuild_brand_cache
        try:
            written = await rebuild_brand_cache(db)
            _record_rebuild(entity_results, "_brand_lookup_cache_rebuild", updated=written)
        except Exception as e:
            _record_rebuild(
                entity_results, "_brand_lookup_cache_rebuild",
                failed=1, reason=str(e),
            )

    if _touched(entity_results, _DEALER_CATALOG_FEEDING_ENTITY_TYPES):
        # Import here for the same circular-import reason; also keeps
        # the orders/router import-graph out of the sync module.
        from app.modules.orders.router import _rebuild_manufacturer_catalog
        try:
            written = await _rebuild_manufacturer_catalog(db)
            _record_rebuild(entity_results, "_dealer_manufacturer_catalog_rebuild", updated=written)
        except Exception as e:
            _record_rebuild(
                entity_results, "_dealer_manufacturer_catalog_rebuild",
                failed=1, reason=str(e),
            )


def get_cosh_entity(db_sync, cosh_id: str, entity_type: str):
    """Synchronous lookup for use in business logic layers."""
    pass  # implemented as async in router


async def get_cosh_translation(
    db: AsyncSession,
    cosh_id: str,
    entity_type: str,
    language_code: str = "en",
) -> Optional[str]:
    """Returns the display name for a Cosh entity in the requested
    language. Falls back to English if the language is not available.
    Reads from cosh_core_items (Connect rows have no translations)."""
    result = await db.execute(
        select(CoshCoreItem.translations).where(
            CoshCoreItem.cosh_id == cosh_id,
            CoshCoreItem.core_type == entity_type,
        )
    )
    row = result.scalar_one_or_none()
    if not row:
        return None
    return row.get(language_code) or row.get("en")
