"""Read-time mirror of Cosh `package_parameters` / `package_variables`
into the local `parameters` / `variables` tables, keyed per crop.

Cosh ships a three-endpoint Connect (`crops_parameters_variables`,
synced 2026-05-12) linking each crop to a (parameter, variable)
pair. The PoP signature picker on the SA / CA portals reads the
local tables, so we materialise the Cosh entities lazily on
read — first-time list of parameters for a given crop walks the
Connect and upserts.

Why a mirror at all (rather than full read-through like
`cosh_crop_view`):
  - `package_variables.parameter_id` is a hard FK to
    `parameters.id`. Stored PoP signatures (PackageVariable
    junction rows) must FK to local primary keys.
  - Read-time materialisation keeps the local tables in sync
    with Cosh without needing a sync-handler hook today; we can
    add the hook later for proactive consistency.

Idempotent: the partial unique indexes on (crop_cosh_id, cosh_id)
and (parameter_id, cosh_id) keep concurrent re-runs safe.
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.advisory.models import Parameter, ParameterSource, Variable
from app.modules.sync.models import CoshConnectRow, CoshCoreItem
from app.services.cosh_constants import (
    COSH_BIOLOGICAL_NAMES_CORE,
    COSH_CROPS_PARAMS_VARS_CONNECT,
    COSH_PACKAGE_PARAMETERS_CORE,
    COSH_PACKAGE_VARIABLES_CORE,
)


def _translation_en(core: Optional[CoshCoreItem], fallback: str) -> str:
    """Pick `translations['en']` if present, else fall back."""
    if core is None:
        return fallback
    t = core.translations or {}
    return t.get("en") or t.get("English") or fallback


async def ensure_local_parameters_for_crop(
    db: AsyncSession, crop_cosh_id: str,
) -> None:
    """Walk the `crops_parameters_variables` Connect for one crop;
    upsert local `parameters` (one per unique cosh package_parameter)
    and `variables` (one per (parameter, cosh package_variable)) rows.

    Skips Custom (CUSTOM) rows entirely — they stay untouched. Only
    Cosh-mirrored rows (source=COSH, cosh_id NOT NULL) are managed
    here.

    Idempotent — relies on the partial unique indexes
    `uq_parameters_crop_cosh_id` and `uq_variables_parameter_cosh_id`
    to deduplicate. Name updates on existing rows happen when Cosh
    translations change.
    """
    # 1. Walk the Connect for active rows matching this crop.
    rows = (await db.execute(
        select(CoshConnectRow).where(
            CoshConnectRow.connect_type == COSH_CROPS_PARAMS_VARS_CONNECT,
            CoshConnectRow.status == "active",
        )
    )).scalars().all()

    # 2. Filter to rows mentioning this crop; collect distinct
    #    (parameter_cosh_id, variable_cosh_id) pairs.
    param_var_pairs: list[tuple[str, str]] = []
    param_cosh_ids: set[str] = set()
    variable_cosh_ids: set[str] = set()
    for r in rows:
        ep = {e.get("role"): e.get("cosh_id") for e in (r.endpoints or [])}
        if ep.get(COSH_BIOLOGICAL_NAMES_CORE) != crop_cosh_id:
            continue
        p_id = ep.get(COSH_PACKAGE_PARAMETERS_CORE)
        v_id = ep.get(COSH_PACKAGE_VARIABLES_CORE)
        if p_id and v_id:
            param_var_pairs.append((p_id, v_id))
            param_cosh_ids.add(p_id)
            variable_cosh_ids.add(v_id)

    if not param_cosh_ids:
        return  # Nothing to mirror for this crop.

    # 3. Resolve display names from CoshCoreItem.translations.
    param_cores = (await db.execute(
        select(CoshCoreItem).where(
            CoshCoreItem.core_type == COSH_PACKAGE_PARAMETERS_CORE,
            CoshCoreItem.cosh_id.in_(param_cosh_ids),
        )
    )).scalars().all()
    param_core_by_id = {c.cosh_id: c for c in param_cores}

    var_cores = (await db.execute(
        select(CoshCoreItem).where(
            CoshCoreItem.core_type == COSH_PACKAGE_VARIABLES_CORE,
            CoshCoreItem.cosh_id.in_(variable_cosh_ids),
        )
    )).scalars().all()
    var_core_by_id = {c.cosh_id: c for c in var_cores}

    # 4. Upsert Parameters. We need the local primary keys back for
    #    the subsequent variable upsert, so insert-or-fetch one by
    #    one. Volume is modest (≤ 32 parameters per crop).
    local_param_by_cosh: dict[str, Parameter] = {}
    for p_cosh in param_cosh_ids:
        existing = (await db.execute(
            select(Parameter).where(
                Parameter.crop_cosh_id == crop_cosh_id,
                Parameter.cosh_id == p_cosh,
            )
        )).scalar_one_or_none()
        name = _translation_en(param_core_by_id.get(p_cosh), p_cosh)
        if existing is None:
            existing = Parameter(
                crop_cosh_id=crop_cosh_id,
                client_id=None,
                cosh_id=p_cosh,
                name=name,
                source=ParameterSource.COSH,
            )
            db.add(existing)
            await db.flush()
        elif existing.name != name:
            existing.name = name
        local_param_by_cosh[p_cosh] = existing

    # 5. Upsert Variables. One per (local_parameter_id, cosh_variable).
    for p_cosh, v_cosh in param_var_pairs:
        local_param = local_param_by_cosh[p_cosh]
        existing = (await db.execute(
            select(Variable).where(
                Variable.parameter_id == local_param.id,
                Variable.cosh_id == v_cosh,
            )
        )).scalar_one_or_none()
        name = _translation_en(var_core_by_id.get(v_cosh), v_cosh)
        if existing is None:
            db.add(Variable(
                parameter_id=local_param.id,
                cosh_id=v_cosh,
                name=name,
            ))
        elif existing.name != name:
            existing.name = name

    await db.commit()
