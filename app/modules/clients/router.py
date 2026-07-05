from datetime import date, datetime, timedelta, timezone
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from pydantic import BaseModel
from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update
from sqlalchemy.orm import selectinload
from app.database import get_db
from app.config import settings
from app.dependencies import get_current_user
from app.modules.platform.models import User, StatusEnum, RoleType, UserRole
from app.modules.subscriptions.models import Subscription, SubscriptionStatus
from app.modules.clients.models import (
    Client, ClientOrganisationType, ClientUser, ClientUserRole,
    ClientLocation, ClientCrop, ClientStatus, ClientPromoter,
    CMClientAssignment, CMPrivilegeModel, CMRights, CMPrivilege
)
from app.modules.clients.schemas import (
    ClientInitiate, ClientCASubmit, ClientReject, ClientEdit,
    ClientStatusUpdate, ClientOut, OnboardingLinkOut, CMAssignment, CMPrivilegeGrant,
    LocationCreate, LocationOut, CropCreate, CropOut, ClientBrandingOut,
    PortalUserCreate, PortalUserOut, PortalUserUpdate,
)
from app.modules.clients.service import (
    generate_token, send_onboarding_email, send_ca_credentials_email,
    send_portal_user_welcome_email,
    get_client_by_token, create_ca_user
)
from app.modules.advisory.models import Package, PackageStatus
from app.services.crop_lifecycle import (
    cascade_inactivate_packages_for_crop,
    derive_active_crop_set,
    restore_cascade_inactivated_packages,
)
from app.services.crop_snapshot import (
    CropSnapshotError, fetch_snapshot,
)

router = APIRouter(tags=["Clients"])


def _require_sa(current_user: User):
    if current_user.email != settings.sa_email:
        raise HTTPException(status_code=403, detail="Super Admin access required")


@router.post("/admin/tasks/run-daily-alerts")
async def admin_run_daily_alerts(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """SA-only manual trigger for the daily-alerts task.

    Useful for testing the alerts pipeline without waiting for the
    06:00 UTC beat schedule — and as a workaround on testing where
    no celery worker / beat is running. Calls
    `_run_daily_alerts_with_session` directly (no celery hop), so
    it returns synchronously with the number of ACTIVE subscriptions
    processed.

    Idempotent per `(subscription, alert_type, day)` — the inner
    loop checks `_alert_sent_today` before sending. Hitting this
    twice on the same day is safe.
    """
    _require_sa(current_user)
    from app.tasks.alerts import _run_daily_alerts_with_session
    n = await _run_daily_alerts_with_session(db)
    return {"subscriptions_processed": n}


async def _require_sa_or_cm_assigned(
    db: AsyncSession, current_user: User, client_id: str,
) -> None:
    """Read-only access widening: lets a Content Manager who has an
    ACTIVE CMClientAssignment for `client_id` reach SA-side detail
    endpoints (GET client, GET cm-assignment). Per user 2026-05-18:
    the My Clients flow on SA Portal navigates a CM to /clients/{cid}
    and that page must work for the CM, not just the SA.

    PUT / DELETE on these resources still require SA — CMs can't
    reassign themselves or edit client metadata. Add the same gate
    to other read endpoints as the My Clients UI grows.
    """
    if current_user.email == settings.sa_email:
        return
    assignment = (await db.execute(
        select(CMClientAssignment).where(
            CMClientAssignment.cm_user_id == current_user.id,
            CMClientAssignment.client_id == client_id,
            CMClientAssignment.status == StatusEnum.ACTIVE,
        ).limit(1)
    )).scalar_one_or_none()
    if assignment is None:
        raise HTTPException(
            status_code=403,
            detail="Super Admin access or active CM assignment required for this client",
        )


async def _assert_unique_legal_ids(
    db: AsyncSession,
    *,
    self_client_id: str,
    gst_number: str | None,
    pan_number: str | None,
) -> None:
    """Ensure GST/PAN aren't already in use by another client.

    Postgres has unique constraints on both columns (clients.models),
    so a clash without this pre-check surfaces as an IntegrityError →
    raw 500. The CA can't act on a 500 — they don't know what to fix.
    A structured 422 lets the onboarding form pin the message to the
    right field instead of dumping a generic banner. Surfaced
    2026-05-08 in testing-server flow when the testing crew reused a
    PAN across two onboarding stubs."""
    if gst_number:
        clash = (await db.execute(
            select(Client.id).where(
                Client.gst_number == gst_number,
                Client.id != self_client_id,
            )
        )).scalar_one_or_none()
        if clash:
            raise HTTPException(status_code=422, detail={
                "field": "gst_number",
                "code": "gst_already_registered",
                "message": "This GST number is already registered to another client. Please verify and re-enter, or contact RootsTalk support if you believe this is an error.",
            })
    if pan_number:
        clash = (await db.execute(
            select(Client.id).where(
                Client.pan_number == pan_number,
                Client.id != self_client_id,
            )
        )).scalar_one_or_none()
        if clash:
            raise HTTPException(status_code=422, detail={
                "field": "pan_number",
                "code": "pan_already_registered",
                "message": "This PAN number is already registered to another client. Please verify and re-enter, or contact RootsTalk support if you believe this is an error.",
            })


def _base_url() -> str:
    """Public base URL for **CA-facing** links — the onboarding magic
    link (`/onboarding/{token}`) and the post-approval branded login
    URL (`/login/{short_name}`). Both routes live in the client-portal
    Next.js app, NOT the SA admin app.

    Production deployment topology:
      One Next.js app at `rootstalk.eywa.farm` serves the SA portal
      at `/` and per-client portals at `/{short_name}` via path-based
      multi-tenant routing. The same host is correct here.

    Testing-server topology (per docs/SETUP_TESTING_SERVER.md):
      Two Next.js apps. SA at `rstalk.eywa.farm`, CA at
      `rstalk-ca.eywa.farm`. Subdomain-based split. **This env var
      MUST point at the CA portal subdomain** — the SA portal has no
      `/onboarding/{token}` or `/login/{short_name}` route and would
      404 if the email's link were stitched against it.

    Resolution order:
    1. `FRONTEND_BASE_URL` env var (the only path for non-dev envs).
    2. Dev fallback `http://localhost:3004` (the client-portal dev
       port — see project_rootstalk_ports.md).

    No production fallback. The startup gate in `app/main.py` refuses
    to boot a non-dev process if `FRONTEND_BASE_URL` is unset, but if
    something mutates settings at runtime to bypass that gate, this
    function raises rather than silently building wrong URLs.
    Production prior to 2026-05-06 had a `https://rootstalk.in`
    fallback here — that was incorrect once `rootstalk.in` was
    earmarked for the PWA, so the fallback was dropped.
    """
    if settings.frontend_base_url:
        return settings.frontend_base_url.rstrip("/")
    if settings.environment == "development":
        return "http://localhost:3004"
    raise RuntimeError(
        "FRONTEND_BASE_URL is required in non-dev environments. "
        "The startup gate in app/main.py should have caught this — "
        "see that file for the env-var contract."
    )


# ── Public: per-client branding for the login page ───────────────────────────

@router.get(
    "/public/clients/{short_name}/branding",
    response_model=ClientBrandingOut,
)
async def get_public_client_branding(
    short_name: str,
    db: AsyncSession = Depends(get_db),
):
    """Public — no auth required.

    Returns the branding fields the CA portal needs to render its
    per-client login page (`/login/<short_name>`) before the user
    has authenticated. Only ACTIVE clients are exposed; any other
    state (PENDING_REVIEW, INACTIVE, REJECTED) returns 404 to avoid
    leaking the existence of pre-launch or wound-down clients.

    Wired 2026-05-06 alongside the fix to `send_ca_credentials_email`
    that now points the CA at `{frontend_base_url}/login/{short_name}`.
    The frontend `app/login/[shortName]/page.tsx` (in
    `rootstalk-client-portal`) consumes this endpoint to populate the
    logo, tagline, and brand colours.
    """
    client = (await db.execute(
        select(Client).where(
            Client.short_name == short_name,
            Client.status == ClientStatus.ACTIVE,
        )
    )).scalar_one_or_none()
    if client is None:
        raise HTTPException(status_code=404, detail="Client not found")

    return ClientBrandingOut(
        short_name=client.short_name,
        full_name=client.full_name,
        tagline=client.tagline,
        logo_url=client.logo_url,
        primary_colour=client.primary_colour,
        secondary_colour=client.secondary_colour,
    )


# ── SA: List all clients ───────────────────────────────────────────────────────

@router.get("/admin/clients/check-short-name")
async def check_short_name(
    short_name: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Real-time short name uniqueness check (item #7)."""
    _require_sa(current_user)
    existing = (await db.execute(
        select(Client).where(Client.short_name == short_name.lower().strip())
    )).scalar_one_or_none()
    return {"available": existing is None, "short_name": short_name.lower().strip()}


async def _client_to_out(db: AsyncSession, client: Client) -> ClientOut:
    """Convert a Client row to ClientOut, fill in the env-driven
    login_url, and join the current `org_type_cosh_ids` from
    `client_organisation_types`. Centralised so list, get, and
    other admin endpoints return a consistent shape — no
    rootstalk.in / wrong-host hardcoding on the frontend, and no
    missing org-types regression like the 2026-05-22 SA Edit-modal
    bug (the SAVE was correct; the GET was silently dropping the
    field, so the modal misled the SA into wiping their own tags).

    `list_clients` uses the bulk variant `_clients_to_out_bulk`
    to avoid an N+1 query."""
    org_rows = (await db.execute(
        select(ClientOrganisationType).where(
            ClientOrganisationType.client_id == client.id,
        )
    )).scalars().all()
    out = ClientOut.model_validate(client)
    out.login_url = f"{_base_url()}/login/{client.short_name}"
    out.org_type_cosh_ids = [r.org_type_cosh_id for r in org_rows]
    return out


async def _clients_to_out_bulk(
    db: AsyncSession, clients: list[Client],
) -> list[ClientOut]:
    """Bulk variant — single query for all clients' org types so the
    list endpoint stays one round-trip."""
    if not clients:
        return []
    rows = (await db.execute(
        select(ClientOrganisationType).where(
            ClientOrganisationType.client_id.in_([c.id for c in clients]),
        )
    )).scalars().all()
    by_client: dict[str, list[str]] = {}
    for r in rows:
        by_client.setdefault(r.client_id, []).append(r.org_type_cosh_id)
    out_list: list[ClientOut] = []
    for c in clients:
        out = ClientOut.model_validate(c)
        out.login_url = f"{_base_url()}/login/{c.short_name}"
        out.org_type_cosh_ids = by_client.get(c.id, [])
        out_list.append(out)
    return out_list


@router.get("/admin/clients", response_model=list[ClientOut])
async def list_clients(
    status_filter: str = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_sa(current_user)
    q = select(Client).order_by(Client.created_at.desc())
    if status_filter:
        q = q.where(Client.status == status_filter)
    result = await db.execute(q)
    return await _clients_to_out_bulk(db, list(result.scalars().all()))


@router.get("/admin/clients/{client_id}", response_model=ClientOut)
async def get_client(
    client_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _require_sa_or_cm_assigned(db, current_user, client_id)
    result = await db.execute(select(Client).where(Client.id == client_id))
    client = result.scalar_one_or_none()
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")
    return await _client_to_out(db, client)


# ── SA: Initiate onboarding ────────────────────────────────────────────────────

@router.post("/admin/clients/initiate", response_model=OnboardingLinkOut, status_code=201)
async def initiate_onboarding(
    request: ClientInitiate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_sa(current_user)

    # Validate short_name uniqueness
    existing = (await db.execute(
        select(Client).where(Client.short_name == request.short_name.lower())
    )).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=409, detail="This short name is already taken")

    token = generate_token()
    expires_at = datetime.now(timezone.utc) + timedelta(hours=24)

    client = Client(
        full_name=request.full_name,
        short_name=request.short_name.lower(),
        ca_name=request.ca_name,
        ca_phone=request.ca_phone,
        ca_email=request.ca_email,
        is_manufacturer=request.is_manufacturer,
        payment_model=request.payment_model,
        status=ClientStatus.PENDING_REVIEW,
        onboarding_link_token=token,
        onboarding_link_expires_at=expires_at,
    )
    db.add(client)
    await db.commit()
    await db.refresh(client)

    link = f"{_base_url()}/onboarding/{token}"

    if settings.environment != "development" and settings.email_smtp_user:
        await send_onboarding_email(client, link)

    return OnboardingLinkOut(
        client_id=client.id,
        short_name=client.short_name,
        onboarding_link=link,
        expires_at=expires_at,
    )


@router.post("/admin/clients/{client_id}/regenerate-link", response_model=OnboardingLinkOut)
async def regenerate_link(
    client_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_sa(current_user)
    result = await db.execute(select(Client).where(Client.id == client_id))
    client = result.scalar_one_or_none()
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")

    token = generate_token()
    expires_at = datetime.now(timezone.utc) + timedelta(hours=24)
    client.onboarding_link_token = token
    client.onboarding_link_expires_at = expires_at

    # Reset the lifecycle so the new link is genuinely usable. Without
    # this, a client whose previous submission was REJECTED would have
    # status=REJECTED here; the submit endpoint's
    # `status != PENDING_REVIEW` guard would then fire on every retry
    # with the message "This onboarding link has already been used".
    # Surfaced 2026-05-08 in testing-server flow.
    client.status = ClientStatus.PENDING_REVIEW
    client.rejection_reason = None

    await db.commit()

    link = f"{_base_url()}/onboarding/{token}"
    if settings.environment != "development" and settings.email_smtp_user:
        await send_onboarding_email(client, link)

    return OnboardingLinkOut(
        client_id=client.id,
        short_name=client.short_name,
        onboarding_link=link,
        expires_at=expires_at,
    )


# ── CA: Submit onboarding form (public) ───────────────────────────────────────

@router.get("/onboarding/{token}")
async def get_onboarding_context(token: str, db: AsyncSession = Depends(get_db)):
    """Return basic client info for pre-filling the onboarding form."""
    client = await get_client_by_token(db, token)
    if not client:
        raise HTTPException(status_code=404, detail="Invalid or expired onboarding link")
    return {
        "full_name": client.full_name,
        "short_name": client.short_name,
        "ca_name": client.ca_name,
        "ca_email": client.ca_email,
        "is_manufacturer": client.is_manufacturer,
    }


@router.post("/onboarding/{token}/logo-upload")
async def upload_onboarding_logo(
    token: str,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
):
    """Public logo-upload endpoint scoped to a specific onboarding
    token. Same auth boundary as `/onboarding/{token}/submit` —
    presenting a valid, unexpired token authorises the upload. The
    underlying S3 logic is shared with `/media/upload` via
    `upload_to_s3()` in app.modules.media.router.

    The CA isn't logged in yet during onboarding, so they can't use
    the authed `/media/upload` endpoint. This endpoint exists
    specifically to close that gap without opening up unauthenticated
    uploads to the world."""
    from app.modules.media.router import upload_to_s3, IMAGE_CONTENT_TYPES

    client = await get_client_by_token(db, token)
    if not client:
        raise HTTPException(status_code=404, detail="Invalid or expired onboarding link")
    if client.status != ClientStatus.PENDING_REVIEW:
        raise HTTPException(status_code=400, detail="This onboarding link has already been used")
    # Logos stay image-only with the original 5 MB cap; the widened
    # 25 MB / +audio default is for advisory media authoring, not
    # company branding artwork.
    return await upload_to_s3(
        file, folder="logos",
        allowed_types=IMAGE_CONTENT_TYPES,
        max_size_bytes=5 * 1024 * 1024,
    )


@router.post("/onboarding/{token}/submit", response_model=ClientOut)
async def submit_onboarding(
    token: str,
    request: ClientCASubmit,
    db: AsyncSession = Depends(get_db),
):
    client = await get_client_by_token(db, token)
    if not client:
        raise HTTPException(status_code=404, detail="Invalid or expired onboarding link")
    if client.status != ClientStatus.PENDING_REVIEW:
        raise HTTPException(status_code=400, detail="This onboarding link has already been used")

    # Validate GST (15 chars alphanumeric) and PAN (10 chars)
    if len(request.gst_number) != 15:
        raise HTTPException(status_code=422, detail="GST number must be 15 characters")
    if len(request.pan_number) != 10:
        raise HTTPException(status_code=422, detail="PAN number must be 10 characters")

    await _assert_unique_legal_ids(
        db,
        self_client_id=client.id,
        gst_number=request.gst_number.upper(),
        pan_number=request.pan_number.upper(),
    )

    client.display_name = request.display_name
    client.tagline = request.tagline
    client.primary_colour = request.primary_colour
    client.secondary_colour = request.secondary_colour
    client.hq_address = request.hq_address
    client.gst_number = request.gst_number.upper()
    client.pan_number = request.pan_number.upper()
    client.website = request.website
    client.support_phone = request.support_phone
    client.office_phone = request.office_phone
    client.social_links = request.social_links
    client.onboarding_link_token = None  # invalidate link after use

    # Clear any prior org_type rows before re-creating from this
    # submission. Handles the rejected → regenerated → resubmitted
    # case cleanly (each resubmission replaces the previous selection
    # rather than accumulating duplicates). ClientOrganisationType
    # has no unique constraint, so prior code silently added
    # duplicates on each resubmit.
    existing_org_types = (await db.execute(
        select(ClientOrganisationType).where(
            ClientOrganisationType.client_id == client.id,
        )
    )).scalars().all()
    for ot in existing_org_types:
        await db.delete(ot)

    for cosh_id in request.org_type_cosh_ids:
        db.add(ClientOrganisationType(client_id=client.id, org_type_cosh_id=cosh_id))

    await db.commit()
    await db.refresh(client)
    return client


# ── SA: Approve / Reject ───────────────────────────────────────────────────────

@router.put("/admin/clients/{client_id}/approve", response_model=ClientOut)
async def approve_client(
    client_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_sa(current_user)
    result = await db.execute(select(Client).where(Client.id == client_id))
    client = result.scalar_one_or_none()
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")
    if client.status != ClientStatus.PENDING_REVIEW:
        raise HTTPException(status_code=400, detail="Client is not pending review")
    if not client.display_name:
        raise HTTPException(status_code=400, detail="CA has not submitted their details yet")

    ca_user, plain_password = await create_ca_user(db, client)
    client.status = ClientStatus.ACTIVE
    client.approved_at = datetime.now(timezone.utc)
    client.approved_by = current_user.id
    await db.commit()
    await db.refresh(client)

    if settings.email_smtp_user:
        login_url = f"{_base_url()}/login/{client.short_name}"
        await send_ca_credentials_email(
            client.ca_email, client.ca_name, login_url, plain_password,
        )

    return client


@router.put("/admin/clients/{client_id}/reject", response_model=ClientOut)
async def reject_client(
    client_id: str,
    request: ClientReject,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_sa(current_user)
    result = await db.execute(select(Client).where(Client.id == client_id))
    client = result.scalar_one_or_none()
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")

    client.status = ClientStatus.REJECTED
    client.rejection_reason = request.reason
    await db.commit()
    await db.refresh(client)
    return client


# ── SA: Edit and toggle status ─────────────────────────────────────────────────

@router.put("/admin/clients/{client_id}", response_model=ClientOut)
async def edit_client(
    client_id: str,
    request: ClientEdit,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_sa(current_user)
    result = await db.execute(select(Client).where(Client.id == client_id))
    client = result.scalar_one_or_none()
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")

    data = request.model_dump(exclude_unset=True)

    # 2026-07-05 — cosh_manufacturer_id is meaningful only for
    # is_manufacturer clients. Non-manufacturer clients can't be linked
    # to a Cosh input_manufacturers row; the pair would break QR
    # portfolio queries. Coerce empty-string to None (clears the link).
    # Refuse if the (effective) is_manufacturer is False and the SA
    # tries to set a non-null value.
    if "cosh_manufacturer_id" in data:
        val = data["cosh_manufacturer_id"]
        if val == "":
            data["cosh_manufacturer_id"] = None
            val = None
        if val is not None:
            effective_is_mfg = data.get(
                "is_manufacturer", client.is_manufacturer,
            )
            if not bool(effective_is_mfg):
                raise HTTPException(
                    status_code=422,
                    detail={
                        "code": "manufacturer_id_requires_is_manufacturer",
                        "message": (
                            "Cosh Manufacturer can be linked only when "
                            "the client is marked as a Manufacturer."
                        ),
                    },
                )

    # Auto-clear the linked Cosh manufacturer if is_manufacturer is
    # being turned OFF. Prevents a stale link surviving the flip and
    # then re-appearing if is_manufacturer is toggled back on later.
    if data.get("is_manufacturer") is False and "cosh_manufacturer_id" not in data:
        data["cosh_manufacturer_id"] = None

    # 2026-07-04 — hidden_from_discovery is COMPANY_PAYS-only. FARMER_PAYS
    # clients must remain discoverable by definition; hiding one would
    # create a client with no path for farmers to find it. Refuse the
    # write cleanly if the SA tries. Also blocked when the effective
    # payment_model AFTER this edit is FARMER_PAYS (e.g. flipping model
    # + setting hidden in one PUT).
    if data.get("hidden_from_discovery") is True:
        effective_model = data.get("payment_model", client.payment_model)
        effective_model_val = (
            effective_model.value if hasattr(effective_model, "value")
            else str(effective_model)
        )
        if effective_model_val != "COMPANY_PAYS":
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "hidden_from_discovery_requires_company_pays",
                    "message": (
                        "hidden_from_discovery can be enabled only for "
                        "COMPANY_PAYS clients. FARMER_PAYS clients must "
                        "remain discoverable so farmers can subscribe."
                    ),
                },
            )

    # Handle org_type_cosh_ids separately — replace the existing list
    new_org_types = data.pop("org_type_cosh_ids", None)
    if new_org_types is not None:
        existing_types = (await db.execute(
            select(ClientOrganisationType).where(ClientOrganisationType.client_id == client_id)
        )).scalars().all()
        for ot in existing_types:
            await db.delete(ot)
        for cosh_id in new_org_types:
            db.add(ClientOrganisationType(client_id=client_id, org_type_cosh_id=cosh_id))

    for field, value in data.items():
        setattr(client, field, value)

    await db.commit()
    await db.refresh(client)
    return await _client_to_out(db, client)


@router.get("/admin/cosh/manufacturers")
async def list_cosh_input_manufacturers(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """SA-only: list every active Cosh `input_manufacturers` Core row,
    for the client-edit-modal dropdown that populates
    `Client.cosh_manufacturer_id`. Each row is `{cosh_id, name}`,
    sorted by name.

    Powers the QR Brand Portfolio picker: once the SA sets
    `cosh_manufacturer_id` on a client, the CA's
    `/client/{id}/qr/portfolio/candidates` walks the
    `tradename_manufacturer` Cosh Connect to list brands under that
    manufacturer — no free-text search, no fuzzy match.
    """
    _require_sa(current_user)
    from app.modules.sync.models import CoshCoreItem
    rows = (await db.execute(
        select(CoshCoreItem.cosh_id, CoshCoreItem.translations).where(
            CoshCoreItem.core_type == "input_manufacturers",
            CoshCoreItem.status == "active",
        )
    )).all()
    out: list[dict] = []
    for cosh_id, translations in rows:
        name = None
        if isinstance(translations, dict):
            name = translations.get("en") or next(
                (v for v in translations.values() if v), None,
            )
        out.append({"cosh_id": cosh_id, "name": name or cosh_id})
    out.sort(key=lambda r: (r["name"] or "").lower())
    return out


@router.put("/admin/clients/{client_id}/status", response_model=ClientOut)
async def toggle_client_status(
    client_id: str,
    request: ClientStatusUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_sa(current_user)
    result = await db.execute(select(Client).where(Client.id == client_id))
    client = result.scalar_one_or_none()
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")

    old_status = client.status
    client.status = request.status

    # BL-11: Suspend or resume subscriptions when client goes inactive/active
    if request.status == ClientStatus.INACTIVE and old_status == ClientStatus.ACTIVE:
        subs_result = await db.execute(
            select(Subscription).where(
                Subscription.client_id == client_id,
                Subscription.status == SubscriptionStatus.ACTIVE,
            )
        )
        for sub in subs_result.scalars().all():
            sub.status = SubscriptionStatus.SUSPENDED

    elif request.status == ClientStatus.ACTIVE and old_status == ClientStatus.INACTIVE:
        subs_result = await db.execute(
            select(Subscription).where(
                Subscription.client_id == client_id,
                Subscription.status == SubscriptionStatus.SUSPENDED,
            )
        )
        for sub in subs_result.scalars().all():
            sub.status = SubscriptionStatus.ACTIVE

    await db.commit()
    await db.refresh(client)
    return client


# ── Client Portal Login ────────────────────────────────────────────────────────

@router.get("/portal/{short_name}/branding")
async def get_portal_branding(short_name: str, db: AsyncSession = Depends(get_db)):
    """Public endpoint — returns branding + the client-level payment
    configuration for the CA portal. The CA portal caches this in
    localStorage right after login and reads it in pages that need to
    display the payment model or gate UI affordances on it (e.g. the
    dashboard banner, hiding "Subscribe" tiles for COMPANY_PAYS clients
    in future PWA work)."""
    result = await db.execute(
        select(Client).where(Client.short_name == short_name, Client.status == ClientStatus.ACTIVE)
    )
    client = result.scalar_one_or_none()
    if not client:
        raise HTTPException(status_code=404, detail="Company not found")
    org_types = (await db.execute(
        select(ClientOrganisationType.org_type_cosh_id).where(ClientOrganisationType.client_id == client.id)
    )).scalars().all()
    return {
        "id": client.id,
        "short_name": client.short_name,
        "display_name": client.display_name,
        "tagline": client.tagline,
        "logo_url": client.logo_url,
        "primary_colour": client.primary_colour,
        "org_type_cosh_ids": list(org_types),
        "payment_model": client.payment_model.value
            if hasattr(client.payment_model, "value") else client.payment_model,
        # 2026-07-05 — Manufacturer flag + Cosh manufacturer link.
        # CA-portal sidebar gates /qr on (is_manufacturer OR seed
        # org_type); Brand Portfolio page reads cosh_manufacturer_id
        # to know whether to render "Ask SA to link" or the auto-
        # loaded candidate list.
        "is_manufacturer": bool(client.is_manufacturer),
        "cosh_manufacturer_id": client.cosh_manufacturer_id,
    }


# ── PWA: Client info by UUID ──────────────────────────────────────────────────

@router.get("/client/{client_id}/info")
async def get_client_info_by_id(
    client_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """PWA: fetch client branding and contact info by UUID (used on home screen)."""
    result = await db.execute(select(Client).where(Client.id == client_id))
    client = result.scalar_one_or_none()
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")
    org_types = (await db.execute(
        select(ClientOrganisationType.org_type_cosh_id).where(ClientOrganisationType.client_id == client_id)
    )).scalars().all()
    return {
        "id": client.id, "short_name": client.short_name,
        "display_name": client.display_name, "tagline": client.tagline,
        "logo_url": client.logo_url, "primary_colour": client.primary_colour,
        "support_phone": client.support_phone, "office_phone": client.office_phone,
        "website": client.website, "social_links": client.social_links or {},
        "org_type_cosh_ids": list(org_types),
        "payment_model": client.payment_model.value
            if hasattr(client.payment_model, "value") else client.payment_model,
    }


# ── Portal: Locations ──────────────────────────────────────────────────────────

@router.get("/client/{client_id}/locations", response_model=list[LocationOut])
async def list_locations(
    client_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(ClientLocation).where(ClientLocation.client_id == client_id)
        .order_by(ClientLocation.added_at)
    )
    return result.scalars().all()


@router.get("/client/{client_id}/location-options-for-package")
async def list_package_location_options(
    client_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """The universe for the Package detail Edit Locations picker —
    bounded to this company's ACTIVE ClientLocation footprint.

    Same shape as /cosh/locations/india (`{states: [{cosh_id, name,
    districts: [{cosh_id, name}]}]}`) but only states/districts the
    CA has enabled in Setup. Pairs whose state/district isn't in
    cosh_core_items (pending sync) surface with `name: null`.

    Empty `states` means the CA hasn't set up the footprint yet —
    the package modal should display a clear nudge to Setup."""
    from app.modules.sync.models import CoshCoreItem

    rows = (await db.execute(
        select(ClientLocation.state_cosh_id, ClientLocation.district_cosh_id)
        .where(
            ClientLocation.client_id == client_id,
            ClientLocation.status == StatusEnum.ACTIVE,
        )
    )).all()
    pairs = {(sid, did) for sid, did in rows}
    if not pairs:
        return {"states": []}

    needed_ids = {sid for sid, _ in pairs} | {did for _, did in pairs}
    cores = (await db.execute(
        select(CoshCoreItem.cosh_id, CoshCoreItem.core_type, CoshCoreItem.translations)
        .where(
            CoshCoreItem.cosh_id.in_(needed_ids),
            CoshCoreItem.core_type.in_(["state_list", "district_list"]),
        )
    )).all()
    state_names: dict[str, str] = {}
    district_names: dict[str, str] = {}
    for cosh_id, core_type, translations in cores:
        name = (translations or {}).get("en") if isinstance(translations, dict) else None
        if core_type == "state_list":
            state_names[cosh_id] = name
        elif core_type == "district_list":
            district_names[cosh_id] = name

    by_state: dict[str, list[dict]] = {}
    for sid, did in pairs:
        by_state.setdefault(sid, []).append({
            "cosh_id": did,
            "name": district_names.get(did),
        })

    states = []
    for sid, districts in by_state.items():
        districts.sort(key=lambda d: (d["name"] or "").lower())
        states.append({
            "cosh_id": sid,
            "name": state_names.get(sid),
            "districts": districts,
        })
    states.sort(key=lambda s: (s["name"] or "").lower())
    return {"states": states}


@router.post("/client/{client_id}/locations", response_model=LocationOut, status_code=201)
async def add_location(
    client_id: str,
    request: LocationCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    loc = ClientLocation(
        client_id=client_id,
        state_cosh_id=request.state_cosh_id,
        district_cosh_id=request.district_cosh_id,
    )
    db.add(loc)
    await db.commit()
    await db.refresh(loc)
    return loc


@router.put("/client/{client_id}/locations")
async def set_client_locations(
    client_id: str,
    pairs: list[LocationCreate],
    force: bool = False,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Atomic replace of the company's location footprint. Wipes
    existing ACTIVE rows and inserts the supplied set.

    Batch FF (2026-05-19): the package-footprint boundary is now
    strict. Districts removed by this PUT cascade into matching
    `package_locations` rows — they are hard-deleted. A package left
    with zero locations is auto-INACTIVATED with a stamped reason.

    Without `?force=true`, an impact-causing diff first returns 422
    `footprint_cascade_confirmation_required` so the CA portal can
    show "N packages will shrink, M will inactivate. Continue?"
    Re-sending with `force=true` executes the cascade.

    Add-only diffs (no removals) skip the confirmation gate."""
    from app.services.footprint_cascade import (
        diff_footprint_and_cascade,
        FootprintCascadeConfirmationRequired,
    )

    new_pairs: set[tuple[str, str]] = set()
    for p in pairs:
        new_pairs.add((p.state_cosh_id, p.district_cosh_id))

    try:
        await diff_footprint_and_cascade(
            db, client_id=client_id, new_pairs=new_pairs, force=force,
        )
    except FootprintCascadeConfirmationRequired as e:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "footprint_cascade_confirmation_required",
                "message": (
                    "Removing these districts will affect existing packages. "
                    "Resend with ?force=true to confirm the cascade."
                ),
                "impact": e.impact.to_dict(),
            },
        )

    existing = (await db.execute(
        select(ClientLocation).where(ClientLocation.client_id == client_id)
    )).scalars().all()
    for row in existing:
        await db.delete(row)
    await db.flush()

    for state_id, district_id in sorted(new_pairs):
        db.add(ClientLocation(
            client_id=client_id,
            state_cosh_id=state_id,
            district_cosh_id=district_id,
            status=StatusEnum.ACTIVE,
        ))
    await db.commit()
    return {"saved": len(new_pairs)}


@router.delete("/client/{client_id}/locations/{location_id}", status_code=204)
async def remove_location(
    client_id: str, location_id: str,
    force: bool = False,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Batch FF (2026-05-19): single-row delete now cascades the same
    way the bulk PUT does. Without `?force=true`, an impact-causing
    delete returns 422 with the affected-package list; resend with
    force=true to execute."""
    from app.services.footprint_cascade import (
        diff_footprint_and_cascade,
        FootprintCascadeConfirmationRequired,
    )

    loc = (await db.execute(
        select(ClientLocation).where(ClientLocation.id == location_id, ClientLocation.client_id == client_id)
    )).scalar_one_or_none()
    if not loc:
        raise HTTPException(status_code=404, detail="Location not found")

    current_pairs = {
        (s, d) for s, d in (await db.execute(
            select(ClientLocation.state_cosh_id, ClientLocation.district_cosh_id)
            .where(
                ClientLocation.client_id == client_id,
                ClientLocation.status == StatusEnum.ACTIVE,
            )
        )).all()
    }
    new_pairs = current_pairs - {(loc.state_cosh_id, loc.district_cosh_id)}

    try:
        await diff_footprint_and_cascade(
            db, client_id=client_id, new_pairs=new_pairs, force=force,
        )
    except FootprintCascadeConfirmationRequired as e:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "footprint_cascade_confirmation_required",
                "message": (
                    "Removing this district will affect existing packages. "
                    "Resend with ?force=true to confirm the cascade."
                ),
                "impact": e.impact.to_dict(),
            },
        )

    await db.delete(loc)
    await db.commit()


# ── Portal: Crops ──────────────────────────────────────────────────────────────

def _crop_to_out(crop: ClientCrop, *, is_active: bool) -> CropOut:
    """Build a CropOut from a ClientCrop row with the derived
    active/inactive flag attached. `is_active` and `status` carry
    the same signal — the latter is kept as a string for portals
    that render a chip."""
    return CropOut(
        id=crop.id, crop_cosh_id=crop.crop_cosh_id,
        status="ACTIVE" if is_active else "INACTIVE",
        is_active=is_active,
        added_at=crop.added_at, removed_at=crop.removed_at,
        crop_name_en=crop.crop_name_en,
        crop_scientific_name=crop.crop_scientific_name,
        crop_area_or_plant=crop.crop_area_or_plant,
    )


async def _is_crop_active(
    db: AsyncSession, *, client_id: str, crop_cosh_id: str,
) -> bool:
    """EXISTS check used after add/restore to compute is_active for
    the response. Cheap; uses the (client_id, crop_cosh_id, status)
    columns that already serve the publish-package query."""
    row = (await db.execute(
        select(Package.id).where(
            Package.client_id == client_id,
            Package.crop_cosh_id == crop_cosh_id,
            Package.status == PackageStatus.ACTIVE,
        ).limit(1)
    )).first()
    return row is not None


@router.get("/client/{client_id}/crops", response_model=list[CropOut])
async def list_crops(
    client_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """CCA Step 1 — SE/CM-facing list of crops on the company's
    conveyor belt. `is_active` is derived from PoP existence: a
    crop is ACTIVE iff at least one Package under it is ACTIVE
    (Batch 1D)."""
    crops = (await db.execute(
        select(ClientCrop).where(
            ClientCrop.client_id == client_id,
            ClientCrop.removed_at.is_(None),
        ).order_by(ClientCrop.added_at)
    )).scalars().all()

    packages = (await db.execute(
        select(Package).where(Package.client_id == client_id)
    )).scalars().all()
    active_set = derive_active_crop_set(packages)

    return [
        _crop_to_out(cc, is_active=cc.crop_cosh_id in active_set)
        for cc in crops
    ]


@router.get("/client/{client_id}/available-crops")
async def list_available_crops(
    client_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """CA-portal "Add Crop" picker — Cosh's full Crop universe minus
    crops this client already has on the belt. Returns
    `[{cosh_id, name_en, status}]` sorted by English name.

    Note: until the Area/Plant Connect ships, the CA can browse but
    `add_crop` will 422 with `crop_missing_measure` for any pick. The
    picker is still useful — the SA team can stage area/plant typing
    in `crop_measures` for the names CAs actually want."""
    from app.services.cosh_crop_view import list_crops as list_cosh_crops
    all_crops = await list_cosh_crops(db)

    already_added = (await db.execute(
        select(ClientCrop.crop_cosh_id).where(
            ClientCrop.client_id == client_id,
            ClientCrop.removed_at.is_(None),
        )
    )).scalars().all()
    already_set = set(already_added)
    return [c for c in all_crops if c["cosh_id"] not in already_set]


@router.post("/client/{client_id}/crops", response_model=CropOut, status_code=201)
async def add_crop(
    client_id: str,
    request: CropCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """CCA Step 1 — CA puts a crop on the conveyor belt.

    A row that exists with `removed_at IS NOT NULL` was previously
    soft-removed by the CA. Re-adding revives it: clears the
    timestamp and restores every Package that was cascade-inactivated
    by the prior removal. Packages inactivated for other reasons stay
    inactive.

    Snapshot (Batch 1B): on every add — fresh and re-add — the
    crop's English name, scientific name, and area/plant typing are
    captured from `CoshCoreItem` + `CropMeasure`. Either source
    missing or inactive → 422 with a stable error code so the CA
    portal can surface the right escalation path.
    """
    existing = (await db.execute(
        select(ClientCrop).where(
            ClientCrop.client_id == client_id,
            ClientCrop.crop_cosh_id == request.crop_cosh_id,
        )
    )).scalar_one_or_none()
    if existing is not None and existing.removed_at is None:
        raise HTTPException(status_code=409, detail="This crop is already added")

    try:
        snapshot = await fetch_snapshot(db, request.crop_cosh_id)
    except CropSnapshotError as e:
        raise HTTPException(
            status_code=422,
            detail={"code": e.code, "message": e.message},
        )

    if existing is not None:
        existing.removed_at = None
        existing.crop_name_en = snapshot.name_en
        existing.crop_scientific_name = snapshot.scientific_name
        existing.crop_area_or_plant = snapshot.area_or_plant
        packages = (await db.execute(
            select(Package).where(
                Package.client_id == client_id,
                Package.crop_cosh_id == request.crop_cosh_id,
            )
        )).scalars().all()
        restore_cascade_inactivated_packages(packages)
        await db.commit()
        await db.refresh(existing)
        is_active = await _is_crop_active(
            db, client_id=client_id, crop_cosh_id=existing.crop_cosh_id,
        )
        return _crop_to_out(existing, is_active=is_active)

    crop = ClientCrop(
        client_id=client_id, crop_cosh_id=request.crop_cosh_id,
        crop_name_en=snapshot.name_en,
        crop_scientific_name=snapshot.scientific_name,
        crop_area_or_plant=snapshot.area_or_plant,
    )
    db.add(crop)
    await db.commit()
    await db.refresh(crop)
    is_active = await _is_crop_active(
        db, client_id=client_id, crop_cosh_id=crop.crop_cosh_id,
    )
    return _crop_to_out(crop, is_active=is_active)


@router.delete("/client/{client_id}/crops/{crop_id}", status_code=204)
async def remove_crop(
    client_id: str, crop_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """CCA Step 1 — CA removes a crop from the conveyor belt.

    Soft-removal: stamps `removed_at` on the ClientCrop row and
    cascade-inactivates every ACTIVE Package under that (client,
    crop). Existing farmer subscriptions on those Packages continue
    unabated; new subscriptions are blocked because the Package is
    INACTIVE. DRAFT and already-INACTIVE Packages are left alone so
    the eventual re-add can revive only what we ourselves
    cascade-inactivated.
    """
    crop = (await db.execute(
        select(ClientCrop).where(
            ClientCrop.id == crop_id,
            ClientCrop.client_id == client_id,
            ClientCrop.removed_at.is_(None),
        )
    )).scalar_one_or_none()
    if not crop:
        raise HTTPException(status_code=404, detail="Crop not found")

    now = datetime.now(timezone.utc)
    crop.removed_at = now

    packages = (await db.execute(
        select(Package).where(
            Package.client_id == client_id,
            Package.crop_cosh_id == crop.crop_cosh_id,
        )
    )).scalars().all()
    cascade_inactivate_packages_for_crop(packages, now)

    await db.commit()


# ── Portal: Users ──────────────────────────────────────────────────────────────

@router.get("/client/{client_id}/users", response_model=list[PortalUserOut])
async def list_portal_users(
    client_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(ClientUser, User)
        .join(User, User.id == ClientUser.user_id)
        .where(ClientUser.client_id == client_id)
        .order_by(ClientUser.created_at)
    )
    rows = result.all()
    out = []
    for cu, user in rows:
        out.append(PortalUserOut(
            id=user.id,
            email=user.email,
            name=user.name,
            role=cu.role.value,
            status=cu.status.value,
            created_at=cu.created_at,
            designation=user.designation,
            professional_profile=user.professional_profile,
        ))
    return out


@router.post("/client/{client_id}/users", response_model=PortalUserOut, status_code=201)
async def add_portal_user(
    client_id: str,
    request: PortalUserCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """CA creates a portal user (Subject Expert / Field Manager / SDM
    / Report User / Client RM / Product Manager) for their org.

    Welcome email (Batch CA-Welcome, 2026-05-06): when a fresh User
    row is created, the new user automatically receives an email
    with their login URL, email, password, and role. The login URL
    is the per-client branded `{frontend_base_url}/login/{short_name}`.

    Existing-user case (the email already belongs to a User created
    via another client/role): the password the CA typed is silently
    ignored by the model layer (we don't overwrite an existing
    password_hash here), so emailing the new password would mislead
    the recipient. That cross-client invite flow is a separate
    concern — no welcome email is sent on this path.
    """
    from app.modules.auth.service import hash_password

    client = (await db.execute(
        select(Client).where(Client.id == client_id)
    )).scalar_one_or_none()
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")

    existing_user = (await db.execute(
        select(User).where(User.email == request.email)
    )).scalar_one_or_none()
    is_new_user = existing_user is None

    if existing_user:
        user = existing_user
        # Batch D (2026-05-18) — when the CA fills in designation /
        # professional_profile while assigning a new role to a user
        # who already exists, save them. Skip values that are None
        # (CA might be re-using an existing SE who already has a bio
        # — don't blow it away with empty fields).
        if request.designation is not None:
            user.designation = request.designation
        if request.professional_profile is not None:
            user.professional_profile = request.professional_profile
    else:
        user = User(
            email=request.email,
            name=request.name,
            password_hash=hash_password(request.password),
            language_code="en",
            designation=request.designation,
            professional_profile=request.professional_profile,
        )
        db.add(user)
        await db.flush()

    conflict = (await db.execute(
        select(ClientUser).where(
            ClientUser.client_id == client_id,
            ClientUser.user_id == user.id,
            ClientUser.role == request.role,
        )
    )).scalar_one_or_none()
    if conflict:
        raise HTTPException(status_code=409, detail="This user already has this role for this client")

    # CA-exclusivity (Batch K, 2026-05-18). Per user rule: every
    # ClientUser except CA can hold multiple roles; CA is mutually
    # exclusive. So:
    #   - Adding CA to a user who has any non-CA role → refuse.
    #   - Adding any non-CA role to a user who is already CA → refuse.
    other_roles = (await db.execute(
        select(ClientUser).where(
            ClientUser.client_id == client_id,
            ClientUser.user_id == user.id,
            ClientUser.status == StatusEnum.ACTIVE,
        )
    )).scalars().all()
    if request.role == ClientUserRole.CA and any(
        r.role != ClientUserRole.CA for r in other_roles
    ):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "ca_role_exclusive",
                "message": (
                    "This user already holds non-CA roles. CA is a single "
                    "exclusive role per user — remove the other roles first "
                    "or assign CA to a different user."
                ),
            },
        )
    if request.role != ClientUserRole.CA and any(
        r.role == ClientUserRole.CA for r in other_roles
    ):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "ca_role_exclusive",
                "message": (
                    "This user is the CA. CA is a single exclusive role — "
                    "they cannot also hold other roles. Remove the CA "
                    "assignment first or pick a different user."
                ),
            },
        )

    cu = ClientUser(
        client_id=client_id,
        user_id=user.id,
        role=request.role,
        status=StatusEnum.ACTIVE,
    )
    db.add(cu)
    await db.commit()
    await db.refresh(cu)

    if is_new_user and settings.email_smtp_user:
        login_url = f"{_base_url()}/login/{client.short_name}"
        await send_portal_user_welcome_email(
            email=user.email,
            name=user.name,
            company_name=client.full_name,
            login_url=login_url,
            password=request.password,
            role_value=cu.role.value,
        )

    return PortalUserOut(
        id=user.id,
        email=user.email,
        name=user.name,
        role=cu.role.value,
        status=cu.status.value,
        created_at=cu.created_at,
        designation=user.designation,
        professional_profile=user.professional_profile,
    )


@router.put("/client/{client_id}/users/{user_id}")
async def update_portal_user(
    client_id: str,
    user_id: str,
    request: PortalUserUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """CA-side edit of a portal user's name / designation /
    professional_profile (Batch D, 2026-05-18).

    Gated to ClientUsers of this client — caller must be a CA (or
    any ClientUser; this endpoint is benign, but the cross-client
    guard in get_current_user is the architectural protection).

    Returns the updated user with role pulled from the FIRST
    ClientUser row found for the target (any one will do — the
    user's tenant binding is what matters for display)."""
    target = (await db.execute(
        select(User).where(User.id == user_id)
    )).scalar_one_or_none()
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    # Ensure target is a member of this client (defence-in-depth).
    membership = (await db.execute(
        select(ClientUser).where(
            ClientUser.client_id == client_id,
            ClientUser.user_id == user_id,
            ClientUser.status == StatusEnum.ACTIVE,
        ).limit(1)
    )).scalar_one_or_none()
    if not membership:
        raise HTTPException(
            status_code=404,
            detail="User is not an active member of this client",
        )
    if request.name is not None:
        target.name = request.name
    if request.designation is not None:
        target.designation = request.designation
    if request.professional_profile is not None:
        target.professional_profile = request.professional_profile
    await db.commit()
    await db.refresh(target)
    return PortalUserOut(
        id=target.id,
        email=target.email,
        name=target.name,
        role=membership.role.value,
        status=membership.status.value,
        created_at=membership.created_at,
        designation=target.designation,
        professional_profile=target.professional_profile,
    )


@router.put("/client/{client_id}/users/{user_id}/status")
async def toggle_portal_user_status(
    client_id: str, user_id: str,
    data: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    new_status = data.get("status")  # "ACTIVE" or "INACTIVE"
    if new_status not in ("ACTIVE", "INACTIVE"):
        raise HTTPException(status_code=422, detail="status must be ACTIVE or INACTIVE")
    cu = (await db.execute(
        select(ClientUser).where(ClientUser.client_id == client_id, ClientUser.user_id == user_id)
    )).scalar_one_or_none()
    if not cu:
        raise HTTPException(status_code=404, detail="User not found")
    cu.status = StatusEnum.ACTIVE if new_status == "ACTIVE" else StatusEnum.INACTIVE
    await db.commit()
    return {"detail": f"User status set to {new_status}"}


# ── CA: Per-client single-holder privileges (Batch X, 2026-05-19) ───────────

async def _assert_caller_can_assign_client_privileges(
    db: AsyncSession, user_id: str, client_id: str,
) -> None:
    """Only the CA of the client (or a CM-EDIT impersonator) may
    assign client-scoped privileges. Other roles 403 with
    `ca_privilege_assign_only`."""
    from app.modules.clients.models import (
        CMClientAssignment, CMRights, ClientUser, ClientUserRole,
    )
    from app.modules.platform.models import StatusEnum
    cus = (await db.execute(
        select(ClientUser).where(
            ClientUser.user_id == user_id,
            ClientUser.client_id == client_id,
            ClientUser.status == StatusEnum.ACTIVE,
        )
    )).scalars().all()
    if any(cu.role == ClientUserRole.CA for cu in cus):
        return
    cm = (await db.execute(
        select(CMClientAssignment.id).where(
            CMClientAssignment.cm_user_id == user_id,
            CMClientAssignment.client_id == client_id,
            CMClientAssignment.status == StatusEnum.ACTIVE,
            CMClientAssignment.rights == CMRights.EDIT,
        ).limit(1)
    )).scalar_one_or_none()
    if cm is not None:
        return
    raise HTTPException(status_code=403, detail={
        "code": "ca_privilege_assign_only",
        "message": (
            "Only the CA of this company can assign client-level "
            "responsibilities."
        ),
    })


@router.get("/client/{client_id}/privileges")
async def list_client_privileges(
    client_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Return one row per ClientUserPrivilege with its current holder
    (or None). Feeds the CA Users page "Client Responsibilities"
    panel. Visible to any active member of the client; the assign
    endpoint is CA-only."""
    from app.modules.clients.models import (
        ClientUser, ClientUserPrivilege, ClientUserPrivilegeModel,
    )
    from app.modules.platform.models import StatusEnum, User as UserModel
    # Any active ClientUser of this client can view.
    member = (await db.execute(
        select(ClientUser.id).where(
            ClientUser.user_id == current_user.id,
            ClientUser.client_id == client_id,
            ClientUser.status == StatusEnum.ACTIVE,
        ).limit(1)
    )).scalar_one_or_none()
    if member is None:
        # CMs also allowed.
        from app.modules.clients.models import CMClientAssignment
        cm = (await db.execute(
            select(CMClientAssignment.id).where(
                CMClientAssignment.cm_user_id == current_user.id,
                CMClientAssignment.client_id == client_id,
                CMClientAssignment.status == StatusEnum.ACTIVE,
            ).limit(1)
        )).scalar_one_or_none()
        if cm is None:
            raise HTTPException(status_code=403, detail="Forbidden")

    rows = (await db.execute(
        select(ClientUserPrivilegeModel, UserModel)
        .join(UserModel, UserModel.id == ClientUserPrivilegeModel.user_id)
        .where(ClientUserPrivilegeModel.client_id == client_id)
    )).all()
    holders: dict[str, dict] = {
        p.value: {"privilege": p.value, "user_id": None, "name": None, "email": None}
        for p in ClientUserPrivilege
    }
    for priv, user in rows:
        holders[priv.privilege.value] = {
            "privilege": priv.privilege.value,
            "user_id": user.id,
            "name": user.name,
            "email": user.email,
        }
    order = [ClientUserPrivilege.SEED_DATA.value]
    return [holders[p] for p in order]


@router.put("/client/{client_id}/privileges/{privilege}")
async def set_client_privilege_holder(
    client_id: str, privilege: str,
    data: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """CA assigns one SE as the holder of a client-scoped privilege
    (or unassigns by passing user_id=null). Atomically demotes any
    other holder of the same privilege in this client.

    Refuses if:
      • Caller isn't the CA / CM-EDIT (403 `ca_privilege_assign_only`).
      • Target user isn't an ACTIVE SUBJECT_EXPERT of this client
        (422 `target_must_be_active_se`).
      • Unknown privilege (422 `unknown_privilege`).
    """
    from app.modules.clients.models import (
        ClientUser, ClientUserPrivilege, ClientUserPrivilegeModel,
        ClientUserRole,
    )
    from app.modules.platform.models import StatusEnum
    await _assert_caller_can_assign_client_privileges(
        db, current_user.id, client_id,
    )
    valid = {p.value for p in ClientUserPrivilege}
    if privilege not in valid:
        raise HTTPException(status_code=422, detail={
            "code": "unknown_privilege",
            "message": f"Unknown privilege: {privilege}",
        })
    priv = ClientUserPrivilege(privilege)
    target_user_id = data.get("user_id")

    # Demote current holder (if any). Always do this so we end up
    # with at most one row.
    from sqlalchemy import delete as sa_delete
    await db.execute(
        sa_delete(ClientUserPrivilegeModel).where(
            ClientUserPrivilegeModel.client_id == client_id,
            ClientUserPrivilegeModel.privilege == priv,
        )
    )

    if target_user_id is not None:
        cu = (await db.execute(
            select(ClientUser).where(
                ClientUser.user_id == target_user_id,
                ClientUser.client_id == client_id,
                ClientUser.status == StatusEnum.ACTIVE,
                ClientUser.role == ClientUserRole.SUBJECT_EXPERT,
            ).limit(1)
        )).scalar_one_or_none()
        if cu is None:
            raise HTTPException(status_code=422, detail={
                "code": "target_must_be_active_se",
                "message": (
                    "The target user must be an active Subject Expert of "
                    "this client to hold a client-scoped privilege."
                ),
            })
        db.add(ClientUserPrivilegeModel(
            client_id=client_id, user_id=target_user_id, privilege=priv,
        ))
    await db.commit()
    return {"privilege": privilege, "user_id": target_user_id}


# ── CA: Self-serve company profile ─────────────────────────────────────────────

@router.get("/client/{client_id}/profile")
async def get_client_profile(
    client_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(select(Client).where(Client.id == client_id))
    client = result.scalar_one_or_none()
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")
    org_types = (await db.execute(
        select(ClientOrganisationType.org_type_cosh_id).where(ClientOrganisationType.client_id == client_id)
    )).scalars().all()
    return {
        "id": client.id, "short_name": client.short_name, "display_name": client.display_name,
        "tagline": client.tagline, "logo_url": client.logo_url,
        "primary_colour": client.primary_colour, "secondary_colour": client.secondary_colour,
        "hq_address": client.hq_address, "gst_number": client.gst_number, "pan_number": client.pan_number,
        "website": client.website, "support_phone": client.support_phone, "office_phone": client.office_phone,
        "social_links": client.social_links or {},
        "org_type_cosh_ids": list(org_types),
        "ca_name": client.ca_name, "ca_email": client.ca_email,
        "status": client.status.value, "approved_at": client.approved_at,
    }


@router.put("/client/{client_id}/profile")
async def update_client_profile(
    client_id: str,
    data: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """CA self-serve: update company branding and contact info. GST and PAN are read-only."""
    result = await db.execute(select(Client).where(Client.id == client_id))
    client = result.scalar_one_or_none()
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")
    editable = [
        "display_name", "tagline", "logo_url", "primary_colour", "secondary_colour",
        "hq_address", "website", "support_phone", "office_phone", "social_links",
    ]
    for field in editable:
        if field in data and data[field] is not None:
            setattr(client, field, data[field])
    await db.commit()
    await db.refresh(client)
    return {"detail": "Profile updated"}


# ── Field Manager: Dealers and Facilitators ────────────────────────────────────

@router.get("/client/{client_id}/field-manager/promoters")
async def list_promoters(
    client_id: str,
    promoter_type: str = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    from app.modules.orders.models import DealerProfile

    q = select(ClientPromoter, User).join(User, User.id == ClientPromoter.user_id).where(
        ClientPromoter.client_id == client_id
    )
    if promoter_type:
        q = q.where(ClientPromoter.promoter_type == promoter_type.upper())
    result = await db.execute(q.order_by(ClientPromoter.registered_at.desc()))
    rows = result.all()

    # Bulk-fetch dealer profiles (one query, not N+1) so each
    # dealer row carries its shop name + GPS for the FM's "View on
    # Map" link. Facilitators have no DealerProfile.
    dealer_user_ids = [u.id for _, u in rows if u]
    dealer_profiles_by_uid: dict[str, DealerProfile] = {}
    if dealer_user_ids:
        for dp in (await db.execute(
            select(DealerProfile).where(DealerProfile.user_id.in_(dealer_user_ids))
        )).scalars().all():
            dealer_profiles_by_uid[dp.user_id] = dp

    out = []
    for cp, user in rows:
        row = {
            "id": cp.id, "user_id": user.id,
            "name": user.name, "phone": user.phone, "email": user.email,
            "promoter_type": cp.promoter_type, "status": cp.status,
            "is_promoter": cp.is_promoter,
            # 2026-05-31 — FM-side P-P designation. Drives the
            # "Promoter-Pundit" toggle on each row in the FM Promoter
            # list. Defaults to False (existing rows pre-migration);
            # mutually exclusive (per user, client) with the
            # FarmPundit-path P-P flag on real registered Pundits.
            "is_promoter_pundit": cp.is_promoter_pundit,
            "promoter_request_status": cp.promoter_request_status,
            "promoter_request_sent_at": cp.promoter_request_sent_at,
            "promoter_request_responded_at": cp.promoter_request_responded_at,
            "territory_notes": cp.territory_notes, "registered_at": cp.registered_at,
            "shop_name": None,
            "shop_address": None,
            "shop_gps_lat": None,
            "shop_gps_lng": None,
        }
        # Initialise the extended DealerProfile fields so callers
        # can always read them (saves an "is field present" check
        # on the FM frontend).
        row["sell_categories"] = []
        row["shop_photo_url"] = None
        row["shop_registration_url"] = None
        dp = dealer_profiles_by_uid.get(user.id)
        if dp:
            row["shop_name"] = dp.shop_name
            row["shop_address"] = dp.shop_address
            row["shop_gps_lat"] = float(dp.shop_gps_lat) if dp.shop_gps_lat else None
            row["shop_gps_lng"] = float(dp.shop_gps_lng) if dp.shop_gps_lng else None
            row["sell_categories"] = dp.sell_categories or []
            row["shop_photo_url"] = dp.shop_photo_url
            row["shop_registration_url"] = dp.shop_registration_url
        out.append(row)
    return out


@router.post("/client/{client_id}/field-manager/promoters", status_code=201)
async def register_promoter(
    client_id: str,
    request: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Recognise a self-registered Dealer or Facilitator at this
    client. Per the five-ecosystem architecture (2026-05-08), the
    user must have already self-registered as a Dealer / Facilitator
    on the PWA — the FM's job is recognition, not creation. The
    pre-V1.1 flow that silently created Users from the FM modal was
    replaced 2026-05-09; FMs now type a phone, see the user's
    self-registered profile, and click Onboard.

    Required gates (all 422 with structured detail on failure):
      - Phone matches an existing User (no silent creation).
      - User has the corresponding UserRole (DEALER / FACILITATOR).
        If they don't, ask them to self-register on the PWA first
        — the FM cannot give the role on their behalf.
      - The Facilitator-Promoter exclusivity rule from §11.2 still
        applies (see block below).
    """
    from app.modules.platform.models import RoleType, UserRole

    phone = request.get("phone")
    promoter_type = request.get("promoter_type", "DEALER").upper()
    territory_notes = request.get("territory_notes")

    if promoter_type not in ("DEALER", "FACILITATOR"):
        raise HTTPException(status_code=422, detail="promoter_type must be DEALER or FACILITATOR")
    if not phone or not str(phone).strip():
        raise HTTPException(status_code=422, detail="Phone is required.")
    # Normalise same as lookup_user_for_onboarding (and
    # /platform/lookup-user-by-phone). Frontend often sends bare
    # 10 digits; User.phone is stored +91XXXXXXXXXX.
    digits = ''.join(ch for ch in str(phone) if ch.isdigit())
    if len(digits) < 10:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "phone_invalid",
                "message": "Enter a 10-digit Indian mobile number.",
            },
        )
    phone = '+91' + digits[-10:]

    user = (await db.execute(
        select(User).where(User.phone == phone)
    )).scalar_one_or_none()
    if user is None:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "user_not_self_registered",
                "message": (
                    "No RootsTalk user with this phone. The person "
                    "must register on the RootsTalk PWA first; you can "
                    "then onboard them here."
                ),
            },
        )

    role_type = RoleType.DEALER if promoter_type == "DEALER" else RoleType.FACILITATOR
    has_role = (await db.execute(
        select(UserRole).where(
            UserRole.user_id == user.id,
            UserRole.role_type == role_type,
            UserRole.status == StatusEnum.ACTIVE,
        )
    )).scalar_one_or_none()
    if has_role is None:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "user_lacks_self_claimed_role",
                "message": (
                    f"This person hasn't self-registered as a {promoter_type.title()} "
                    "on the RootsTalk PWA. Ask them to do that first; the FM cannot "
                    "give the role on their behalf."
                ),
            },
        )

    # Same-client duplicate check.
    existing_cp = (await db.execute(
        select(ClientPromoter).where(
            ClientPromoter.client_id == client_id,
            ClientPromoter.user_id == user.id,
            ClientPromoter.promoter_type == promoter_type,
        )
    )).scalar_one_or_none()
    if existing_cp:
        raise HTTPException(
            status_code=409,
            detail=f"This person is already registered as a {promoter_type.title()} for this client.",
        )

    # V1.1 Item 4 (2026-05-09): onboarding ≠ Promoter designation
    # per spec §11.2 ("Onboarding and Promoter designation are
    # separate steps" for Facilitator-Promoters). Newly-onboarded
    # rows default to `is_promoter=False`. The FM marks them as a
    # Promoter explicitly via PUT
    # /field-manager/promoters/{id}/promoter-flag, where the
    # spec §11.2 Facilitator-uniqueness check now lives.
    cp = ClientPromoter(
        client_id=client_id,
        user_id=user.id,
        promoter_type=promoter_type,
        is_promoter=False,
        territory_notes=territory_notes,
        registered_by=current_user.id,
    )
    db.add(cp)
    await db.commit()
    await db.refresh(cp)

    return {
        "id": cp.id, "user_id": user.id,
        "name": user.name, "phone": user.phone,
        "promoter_type": cp.promoter_type, "status": cp.status,
        "is_promoter": cp.is_promoter,
        "territory_notes": cp.territory_notes, "registered_at": cp.registered_at,
    }


@router.get("/admin/users/lookup-for-onboarding")
async def lookup_user_for_onboarding(
    phone: str,
    client_id: str,
    promoter_type: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Rich phone lookup for the Field Manager onboarding modal —
    drives the phone-only UX (V1.1 Item 3, 2026-05-09).

    The FM types a phone, modal blurs and calls this endpoint, then
    renders one of four states:
      - exists=False                             → "User must self-register first"
      - exists=True, has_role=False              → "Has not claimed Dealer/Facilitator yet"
      - exists=True, has_role=True, onboarded=True  → "Already onboarded at this company"
      - exists=True, has_role=True, onboarded=False → ready to onboard; show profile preview

    Privacy: returns only this client's view (already_onboarded scoped
    to client_id; never names other clients the user is onboarded at).
    For Dealers, the public-ish DealerProfile fields are returned so
    the FM can verify identity (shop name, address, GPS, sell
    categories). Government-licence URLs are also returned because
    KK confirmed offline verification of pesticide / fertiliser
    licences is part of the FM's onboarding workflow.
    """
    from app.modules.platform.models import RoleType, UserRole
    from app.modules.orders.models import DealerProfile

    promoter_type = promoter_type.upper()
    if promoter_type not in ("DEALER", "FACILITATOR"):
        raise HTTPException(
            status_code=422,
            detail="promoter_type must be DEALER or FACILITATOR",
        )

    # Normalise — frontend sends what the FM typed (often bare 10
    # digits). User.phone is stored as +91XXXXXXXXXX. Without this
    # normalisation, every onboard attempt returned exists=False
    # even for users who were demonstrably registered (user report
    # 2026-05-21). Same shape as /platform/lookup-user-by-phone.
    digits = ''.join(ch for ch in (phone or '') if ch.isdigit())
    if len(digits) < 10:
        return {
            "exists": False, "user": None, "has_role": False,
            "already_onboarded": False, "dealer_profile": None,
        }
    normalised_phone = '+91' + digits[-10:]

    user = (await db.execute(
        select(User).where(User.phone == normalised_phone)
    )).scalar_one_or_none()
    if user is None:
        return {
            "exists": False, "user": None, "has_role": False,
            "already_onboarded": False, "dealer_profile": None,
        }

    role_type = RoleType.DEALER if promoter_type == "DEALER" else RoleType.FACILITATOR
    has_role = (await db.execute(
        select(UserRole).where(
            UserRole.user_id == user.id,
            UserRole.role_type == role_type,
            UserRole.status == StatusEnum.ACTIVE,
        )
    )).scalar_one_or_none() is not None

    already_onboarded = (await db.execute(
        select(ClientPromoter).where(
            ClientPromoter.user_id == user.id,
            ClientPromoter.client_id == client_id,
            ClientPromoter.promoter_type == promoter_type,
            ClientPromoter.status == "ACTIVE",
        )
    )).scalar_one_or_none() is not None

    dealer_profile = None
    if promoter_type == "DEALER" and has_role:
        dp = (await db.execute(
            select(DealerProfile).where(DealerProfile.user_id == user.id)
        )).scalar_one_or_none()
        if dp:
            dealer_profile = {
                "shop_name": dp.shop_name,
                "shop_address": dp.shop_address,
                "sell_categories": dp.sell_categories or [],
                "shop_photo_url": dp.shop_photo_url,
                "shop_registration_url": dp.shop_registration_url,
                "pesticide_licence_url": dp.pesticide_licence_url,
                "fertiliser_licence_url": dp.fertiliser_licence_url,
                "shop_gps_lat": float(dp.shop_gps_lat) if dp.shop_gps_lat else None,
                "shop_gps_lng": float(dp.shop_gps_lng) if dp.shop_gps_lng else None,
            }

    return {
        "exists": True,
        "user": {
            "id": user.id,
            "name": user.name,
            "phone": user.phone,
            "photo_url": user.photo_url,
        },
        "has_role": has_role,
        "already_onboarded": already_onboarded,
        "dealer_profile": dealer_profile,
    }


@router.get("/admin/users/exists")
async def lookup_user_by_phone(
    phone: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Pre-flight check used by the CA portal's Promoter register form.

    When the CA fills in a phone number, the form blurs and hits this
    endpoint so it can show inline whether the phone already
    corresponds to a RootsTalk user. Without this, the
    register-promoter call silently attaches a ClientPromoter row to
    an existing User (intended behaviour — same person CAN be a
    promoter at multiple companies) but the CA gets no signal that
    they're attaching an existing user vs creating a fresh account.

    Privacy: returns only `exists: bool` and `name` (the existing
    User's display name). No cross-client information leaks — the CA
    can't tell which other companies the user is a promoter at, only
    that the user exists. Same surface area as the existing
    register-promoter call (which also reveals existence by 409
    behaviour), so this isn't a new fish-the-phone-book vector."""
    if not phone:
        raise HTTPException(status_code=422, detail="phone is required")
    # Normalise — same shape as the other two onboarding lookups.
    digits = ''.join(ch for ch in str(phone) if ch.isdigit())
    if len(digits) < 10:
        return {"exists": False, "name": None}
    normalised_phone = '+91' + digits[-10:]
    existing = (await db.execute(
        select(User).where(User.phone == normalised_phone)
    )).scalar_one_or_none()
    if existing:
        return {"exists": True, "name": existing.name}
    return {"exists": False, "name": None}


@router.put("/client/{client_id}/field-manager/promoters/{promoter_id}/deactivate")
async def deactivate_promoter(
    client_id: str,
    promoter_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    cp = (await db.execute(
        select(ClientPromoter).where(ClientPromoter.id == promoter_id, ClientPromoter.client_id == client_id)
    )).scalar_one_or_none()
    if not cp:
        raise HTTPException(status_code=404, detail="Promoter not found")
    cp.status = "INACTIVE"
    # R12 (2026-05-29): cascade-clear the Promoter flag. Leaving
    # is_promoter=True on an INACTIVE row is logically dead (every
    # gate filters on status='ACTIVE') but it stales the row and
    # would silently re-grant Promoter status if the Client later
    # reactivates the same row. A re-onboarded Facilitator should
    # be re-assigned as Promoter explicitly, not via a hidden
    # side-effect of reactivation.
    cp.is_promoter = False
    await db.commit()
    return {"status": "INACTIVE"}


@router.put("/client/{client_id}/field-manager/promoters/{promoter_id}/reactivate")
async def reactivate_promoter(
    client_id: str,
    promoter_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Inverse of deactivate — flip status back to ACTIVE so the
    promoter can resume assigning packages. Same Promoter user_id;
    same ClientPromoter row, no re-onboarding needed."""
    cp = (await db.execute(
        select(ClientPromoter).where(
            ClientPromoter.id == promoter_id,
            ClientPromoter.client_id == client_id,
        )
    )).scalar_one_or_none()
    if not cp:
        raise HTTPException(status_code=404, detail="Promoter not found")
    if cp.status == "ACTIVE":
        raise HTTPException(
            status_code=400,
            detail="This promoter is already active.",
        )
    cp.status = "ACTIVE"
    await db.commit()
    return {"status": "ACTIVE"}


@router.put("/client/{client_id}/field-manager/promoters/{promoter_id}/promoter-pundit")
async def fm_toggle_promoter_pundit(
    client_id: str,
    promoter_id: str,
    data: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Field Manager designates one of this client's Promoters
    (Facilitator or Dealer) as a Promoter-Pundit.

    Per V1 user direction 2026-05-31: P-P designation belongs in the
    FM's Promoter-management surface, NOT the CA's FarmPundits tab.
    Sanjay can be a P-P without first registering as a regular
    FarmPundit.

    On toggle-ON the endpoint auto-provisions a shadow
    `FarmPunditProfile` + `ClientFarmPundit` row (searchable=False,
    role=PROMOTER_PUNDIT) so the existing query routing chain works
    unmodified. On toggle-OFF it deletes the shadow row so the
    routing stops picking them.

    Mutual exclusion (per (user, client)): refuses with
    `pp_via_real_farmpundit_exists` if the user is already
    designated as a P-P via a *real* (searchable=True)
    ClientFarmPundit row at this client — that means the CA has
    already named them as a P-P through the registered-pundit path,
    and V1 keeps the two non-overlapping.

    Single-company PP constraint (2026-06-23): also refuses if the
    user is already PROMOTER_PUNDIT at another client.
    """
    from app.modules.farmpundit.models import (
        ClientFarmPundit, FarmPunditProfile, PunditRole,
    )
    from app.modules.platform.models import User as PlatformUser

    cp = (await db.execute(
        select(ClientPromoter).where(
            ClientPromoter.id == promoter_id,
            ClientPromoter.client_id == client_id,
        )
    )).scalar_one_or_none()
    if not cp:
        raise HTTPException(status_code=404, detail="Promoter not found")
    if cp.status != "ACTIVE":
        raise HTTPException(
            status_code=409,
            detail={
                "code": "promoter_not_active",
                "message": (
                    "Reactivate this promoter before designating "
                    "them as a Promoter-Pundit."
                ),
            },
        )

    new_value = bool(data.get("is_promoter_pundit", not cp.is_promoter_pundit))

    if new_value and not cp.is_promoter_pundit:
        # Mutual-exclusion guard — only refuse against a REAL CFP P-P
        # row (searchable=True). Phantom rows (searchable=False) are
        # bookkeeping under our own control and can be safely
        # re-created or updated. The user's V1 rule is "no overlap
        # between FarmPundit and P-P paths"; the phantom is part of
        # the P-P path, not the FarmPundit path.
        real_cfp_pp = (await db.execute(
            select(ClientFarmPundit.id)
            .join(
                FarmPunditProfile,
                FarmPunditProfile.id == ClientFarmPundit.pundit_id,
            )
            .where(
                ClientFarmPundit.client_id == client_id,
                ClientFarmPundit.role == PunditRole.PROMOTER_PUNDIT,
                ClientFarmPundit.searchable == True,  # noqa: E712
                FarmPunditProfile.user_id == cp.user_id,
            )
            .limit(1)
        )).scalar_one_or_none()
        if real_cfp_pp is not None:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "pp_via_real_farmpundit_exists",
                    "message": (
                        "This user is already a Promoter-Pundit via the "
                        "FarmPundit path at this client. V1 keeps the "
                        "two paths non-overlapping — remove the "
                        "FarmPundit-side P-P designation before "
                        "switching to the Promoter-side designation."
                    ),
                },
            )

        # Single-company PP constraint (2026-06-23): also refuse if the
        # user is already PROMOTER_PUNDIT at another client.
        profile_for_check = (await db.execute(
            select(FarmPunditProfile).where(FarmPunditProfile.user_id == cp.user_id)
        )).scalar_one_or_none()
        if profile_for_check is not None:
            other_pp = (await db.execute(
                select(ClientFarmPundit.client_id).where(
                    ClientFarmPundit.pundit_id == profile_for_check.id,
                    ClientFarmPundit.role == PunditRole.PROMOTER_PUNDIT,
                    ClientFarmPundit.client_id != client_id,
                ).limit(1)
            )).scalar_one_or_none()
            if other_pp is not None:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "promoter_pundit_already_at_another_client",
                        "message": (
                            "This user is already designated as a "
                            "Promoter-Pundit at another company. A "
                            "Promoter-Pundit may serve only one company "
                            "at a time."
                        ),
                    },
                )

    cp.is_promoter_pundit = new_value

    # Auto-provision / clear the shadow CFP row so the query routing
    # chain (which reads from ClientFarmPundit) sees the change.
    profile = (await db.execute(
        select(FarmPunditProfile).where(FarmPunditProfile.user_id == cp.user_id)
    )).scalar_one_or_none()

    if new_value:
        if profile is None:
            profile = FarmPunditProfile(user_id=cp.user_id)
            db.add(profile)
            await db.flush()
        cfp = (await db.execute(
            select(ClientFarmPundit).where(
                ClientFarmPundit.client_id == client_id,
                ClientFarmPundit.pundit_id == profile.id,
            )
        )).scalar_one_or_none()
        if cfp is None:
            cfp = ClientFarmPundit(
                client_id=client_id,
                pundit_id=profile.id,
                role=PunditRole.PROMOTER_PUNDIT,
                status="ACTIVE",
                # Phantom: hidden from any farmer-facing pundit picker.
                searchable=False,
            )
            db.add(cfp)
        elif cfp.role == PunditRole.PROMOTER_PUNDIT:
            # Already PP — just reactivate idempotently.
            cfp.status = "ACTIVE"
        else:
            # Existing row is a regular pundit (PRIMARY/PANEL). The
            # mutual-exclusion guard above only catches existing PP
            # rows; here we forbid the regular → PP transition too,
            # consistent with the user's 2026-06-23 rule.
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "regular_pundit_cannot_become_promoter_pundit",
                    "message": (
                        "This user is already a regular pundit (Primary / "
                        "Panel) at this company. Remove that designation "
                        "before adding them as a Promoter-Pundit."
                    ),
                },
            )
    else:
        # Toggle OFF — delete the shadow row (searchable=False is the
        # FM-provisioned phantom; safe to remove). Real-FarmPundit
        # rows are never created here, so we only ever target the
        # phantom.
        if profile is not None:
            from sqlalchemy import delete as sa_delete
            await db.execute(
                sa_delete(ClientFarmPundit).where(
                    ClientFarmPundit.client_id == client_id,
                    ClientFarmPundit.pundit_id == profile.id,
                    ClientFarmPundit.role == PunditRole.PROMOTER_PUNDIT,
                    ClientFarmPundit.searchable == False,  # noqa: E712
                )
            )

    await db.commit()
    target = (await db.execute(
        select(PlatformUser).where(PlatformUser.id == cp.user_id)
    )).scalar_one_or_none()
    return {
        "id": cp.id,
        "user_id": cp.user_id,
        "name": target.name if target else None,
        "phone": target.phone if target else None,
        "promoter_type": cp.promoter_type,
        "is_promoter_pundit": cp.is_promoter_pundit,
    }


@router.put("/client/{client_id}/field-manager/promoters/{promoter_id}/request-promoter")
async def request_promoter(
    client_id: str,
    promoter_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """R9 (2026-05-29): Client's Field Manager designates this
    onboarded user as a Promoter.

    Both `promoter_type` paths now use a two-sided handshake
    (status: NONE | DECLINED → PENDING). The user accepts via
    /{facilitator|dealer}/promoter-invitations/{id}/accept before
    `is_promoter` flips True.

      FACILITATOR — §11.2 exclusivity split across request-time
                    (here, refuse if already ACCEPTED elsewhere)
                    and accept-time (final racy check).
      DEALER      — multi-company per §11.2. No exclusivity check at
                    either step. The handshake exists for explicit
                    consent only (2026-06-23 user direction: dealers
                    can be Promoter at multiple companies but each
                    company must obtain consent).

    Multiple PENDING invitations are allowed in both cases — the
    user can pick which to accept, the others survive as options
    (for facilitator until one accepts; for dealer all may be
    accepted independently).
    """
    cp = (await db.execute(
        select(ClientPromoter).where(
            ClientPromoter.id == promoter_id,
            ClientPromoter.client_id == client_id,
        )
    )).scalar_one_or_none()
    if not cp:
        raise HTTPException(status_code=404, detail="Promoter row not found")
    if cp.status != "ACTIVE":
        raise HTTPException(
            status_code=409,
            detail="Reactivate this person before designating them as a Promoter.",
        )
    if cp.promoter_request_status in ("PENDING", "ACCEPTED"):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "promoter_already_outstanding",
                "message": (
                    "A Promoter designation is already outstanding for "
                    "this person at this company. Revoke it first to "
                    "send a new one."
                ),
            },
        )

    now = datetime.now(timezone.utc)

    if cp.promoter_type == "FACILITATOR":
        # §11.2 request-time gate.
        accepted_elsewhere = (await db.execute(
            select(ClientPromoter).where(
                ClientPromoter.user_id == cp.user_id,
                ClientPromoter.promoter_type == "FACILITATOR",
                ClientPromoter.status == "ACTIVE",
                ClientPromoter.is_promoter == True,  # noqa: E712
                ClientPromoter.client_id != client_id,
            )
        )).scalar_one_or_none()
        if accepted_elsewhere:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "facilitator_already_active_elsewhere",
                    "message": (
                        "This person is already an active Facilitator-"
                        "Promoter at another company. Per spec §11.2, a "
                        "Facilitator-Promoter can only be active at one "
                        "company at a time. They must step down (or be "
                        "revoked) at the previous company before being "
                        "invited here."
                    ),
                },
            )
        cp.promoter_request_status = "PENDING"
        cp.promoter_request_sent_at = now
        cp.promoter_request_responded_at = None
    else:
        # DEALER — two-sided handshake (2026-06-23, was auto-accept).
        # No exclusivity check: dealers stay multi-company Promoters
        # per §11.2; they just have to consent.
        cp.promoter_request_status = "PENDING"
        cp.promoter_request_sent_at = now
        cp.promoter_request_responded_at = None

    await db.commit()
    await db.refresh(cp)
    return {
        "id": cp.id,
        "promoter_type": cp.promoter_type,
        "status": cp.status,
        "is_promoter": cp.is_promoter,
        "promoter_request_status": cp.promoter_request_status,
        "promoter_request_sent_at": cp.promoter_request_sent_at,
    }


@router.put("/client/{client_id}/field-manager/promoters/{promoter_id}/revoke-promoter")
async def revoke_promoter(
    client_id: str,
    promoter_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """R9 / R10 Client side (2026-05-29): unconditional teardown of
    the Promoter sub-role. Works regardless of current invitation
    state (PENDING / ACCEPTED / DECLINED): clears `is_promoter`,
    resets `promoter_request_status` to 'NONE'. Releases the §11.2
    lock so the Facilitator can be invited elsewhere.

    The Facilitator-onboarding link itself (the row's existence and
    its `status='ACTIVE'`) is NOT touched. To end the onboarding
    relationship, use the `deactivate_promoter` endpoint."""
    cp = (await db.execute(
        select(ClientPromoter).where(
            ClientPromoter.id == promoter_id,
            ClientPromoter.client_id == client_id,
        )
    )).scalar_one_or_none()
    if not cp:
        raise HTTPException(status_code=404, detail="Promoter row not found")

    cp.is_promoter = False
    cp.promoter_request_status = "NONE"
    cp.promoter_request_responded_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(cp)
    return {
        "id": cp.id,
        "promoter_type": cp.promoter_type,
        "status": cp.status,
        "is_promoter": cp.is_promoter,
        "promoter_request_status": cp.promoter_request_status,
    }


# ── Field Manager: Get farmers for assignment ──────────────────────────────────

@router.get("/client/{client_id}/field-manager/farmers")
async def list_client_farmers(
    client_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List all farmers who have subscriptions with this client."""
    from app.modules.subscriptions.models import Subscription
    result = await db.execute(
        select(Subscription, User)
        .join(User, User.id == Subscription.farmer_user_id)
        .where(Subscription.client_id == client_id)
        .order_by(Subscription.created_at.desc())
    )
    rows = result.all()
    # Deduplicate by farmer_user_id
    seen = set()
    out = []
    for sub, user in rows:
        if user.id not in seen:
            seen.add(user.id)
            out.append({
                "user_id": user.id, "name": user.name, "phone": user.phone,
                "subscription_id": sub.id, "package_id": sub.package_id,
                "subscription_status": sub.status,
                "crop_start_date": sub.crop_start_date,
            })
    return out


# ── Client Portal: Alerts dashboard ───────────────────────────────────────────

@router.get("/client/{client_id}/alerts/pending-start-dates")
async def get_pending_start_dates(
    client_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Farmers with ACTIVE subscriptions but no crop start date set."""
    from app.modules.subscriptions.models import Subscription, SubscriptionStatus
    result = await db.execute(
        select(Subscription, User)
        .join(User, User.id == Subscription.farmer_user_id)
        .where(
            Subscription.client_id == client_id,
            Subscription.status == SubscriptionStatus.ACTIVE,
            Subscription.crop_start_date == None,  # noqa: E711
        )
        .order_by(Subscription.created_at)
    )
    return [
        {
            "subscription_id": sub.id,
            "farmer_name": user.name,
            "farmer_phone": user.phone,
            "package_id": sub.package_id,
            "subscribed_at": sub.subscription_date,
        }
        for sub, user in result.all()
    ]


@router.get("/client/{client_id}/alerts/overdue-inputs")
async def get_overdue_inputs(
    client_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Farmers whose input practices are due today but have no active order (simplified check)."""
    from app.modules.subscriptions.models import Subscription, SubscriptionStatus
    from app.modules.orders.models import Order, OrderStatus
    from app.modules.advisory.models import Timeline, Practice, PracticeL0
    from datetime import date
    today = date.today()

    result = await db.execute(
        select(Subscription, User)
        .join(User, User.id == Subscription.farmer_user_id)
        .where(
            Subscription.client_id == client_id,
            Subscription.status == SubscriptionStatus.ACTIVE,
            Subscription.crop_start_date != None,  # noqa: E711
        )
    )
    rows = result.all()

    overdue = []
    for sub, user in rows:
        crop_start = sub.crop_start_date.date() if hasattr(sub.crop_start_date, 'date') else sub.crop_start_date
        day_offset = (today - crop_start).days

        tl_result = await db.execute(
            select(Timeline).where(Timeline.package_id == sub.package_id)
        )
        for tl in tl_result.scalars().all():
            from_type = tl.from_type.value if hasattr(tl.from_type, 'value') else str(tl.from_type)
            active = False
            if from_type == "DAS" and tl.from_value <= day_offset <= tl.to_value:
                active = True
            elif from_type == "DBS" and -tl.to_value <= day_offset <= -tl.from_value:
                active = True

            if active:
                p_result = await db.execute(
                    select(Practice).where(
                        Practice.timeline_id == tl.id,
                        Practice.l0_type == PracticeL0.INPUT,
                    )
                )
                if p_result.scalars().first():
                    # Check if there's an active (non-cancelled) order
                    order_result = await db.execute(
                        select(Order).where(
                            Order.subscription_id == sub.id,
                            Order.status.notin_(["CANCELLED", "EXPIRED"]),
                        )
                    )
                    if not order_result.scalar_one_or_none():
                        overdue.append({
                            "subscription_id": sub.id,
                            "farmer_name": user.name,
                            "farmer_phone": user.phone,
                            "day_offset": day_offset,
                            "timeline_name": tl.name,
                            "package_id": sub.package_id,
                        })
                        break  # One entry per subscription

    return overdue


# ═══════════════════════════════════════════════════════════════════════════════
# SA: CM Client Assignments
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/admin/clients/{client_id}/cm-assignment")
async def get_cm_assignment(
    client_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get the current CM assignment for a client. Visible to the SA
    and to the CM who is currently assigned (so they can see their
    own role + rights on the client detail page)."""
    await _require_sa_or_cm_assigned(db, current_user, client_id)
    assignment = (await db.execute(
        select(CMClientAssignment).where(
            CMClientAssignment.client_id == client_id,
            CMClientAssignment.status == StatusEnum.ACTIVE,
        )
    )).scalar_one_or_none()
    if not assignment:
        return {"cm_user_id": None, "cm_name": None, "cm_email": None, "rights": None}
    cm = (await db.execute(select(User).where(User.id == assignment.cm_user_id))).scalar_one_or_none()
    return {
        "assignment_id": assignment.id,
        "cm_user_id": assignment.cm_user_id,
        "cm_name": cm.name if cm else None,
        "cm_email": cm.email if cm else None,
        "rights": assignment.rights.value,
        "assigned_at": assignment.assigned_at,
    }


@router.put("/admin/clients/{client_id}/cm-assignment")
async def assign_cm_to_client(
    client_id: str,
    data: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """SA: assign or update CM for a client. One CM per client at a time."""
    _require_sa(current_user)
    cm_user_id = data.get("cm_user_id")
    rights = data.get("rights", "EDIT")
    if not cm_user_id:
        raise HTTPException(status_code=422, detail="cm_user_id required")

    # Verify the user is a Content Manager
    cm_role = (await db.execute(
        select(UserRole).where(
            UserRole.user_id == cm_user_id,
            UserRole.role_type == RoleType.CONTENT_MANAGER,
        )
    )).scalar_one_or_none()
    if not cm_role:
        raise HTTPException(status_code=400, detail="User is not a Content Manager")

    # 2026-06-30 — Refuse to assign the Super Admin as a CM. SA already
    # holds implicit view rights to every client; assigning them adds
    # nothing, and historically a stray SA-also-CM role on the SA user
    # let them surface in the dropdown. Defence in depth — the list
    # endpoint also filters them out, but a hand-rolled PUT could
    # bypass that.
    target_user = (await db.execute(
        select(User).where(User.id == cm_user_id)
    )).scalar_one_or_none()
    sa_email = (settings.sa_email or "").lower()
    if target_user and sa_email and (target_user.email or "").lower() == sa_email:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "sa_not_assignable",
                "message": (
                    "The Super Admin has implicit view rights to every "
                    "client and cannot be assigned as a Content Manager."
                ),
            },
        )

    # 2026-06-30 — Bug fix: the UniqueConstraint on
    # (cm_user_id, client_id) doesn't include `status`, so an older
    # INACTIVE row for the same (cm_user_id, client_id) blocked the
    # INSERT path when reassigning to a previously-used CM. The
    # error was raised at commit time and not caught — the page
    # reload then refetched the unchanged state, looking like a
    # silent no-op. Fix: mass-deactivate any ACTIVE assignment for
    # this client first, then either reactivate the existing
    # (cm_user_id, client_id) row or INSERT a fresh one.
    existing_active = (await db.execute(
        select(CMClientAssignment).where(
            CMClientAssignment.client_id == client_id,
            CMClientAssignment.status == StatusEnum.ACTIVE,
        )
    )).scalar_one_or_none()
    if existing_active and existing_active.cm_user_id == cm_user_id:
        # Same CM, possibly different rights. Just update.
        existing_active.rights = CMRights(rights)
        await db.commit()
        return {"detail": "Rights updated", "cm_user_id": cm_user_id, "rights": rights}

    if existing_active:
        existing_active.status = StatusEnum.INACTIVE

    # Either reactivate a prior (cm_user_id, client_id) row or insert
    # a new one. The lookup ignores status so we find any prior row.
    prior = (await db.execute(
        select(CMClientAssignment).where(
            CMClientAssignment.cm_user_id == cm_user_id,
            CMClientAssignment.client_id == client_id,
        )
    )).scalar_one_or_none()
    if prior is not None:
        prior.status = StatusEnum.ACTIVE
        prior.rights = CMRights(rights)
        prior.assigned_at = datetime.now(timezone.utc)
    else:
        db.add(CMClientAssignment(
            cm_user_id=cm_user_id,
            client_id=client_id,
            rights=CMRights(rights),
            status=StatusEnum.ACTIVE,
        ))
    await db.commit()
    return {"detail": "CM assigned", "cm_user_id": cm_user_id, "rights": rights}


@router.delete("/admin/clients/{client_id}/cm-assignment")
async def remove_cm_assignment(
    client_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """SA: remove CM from a client."""
    _require_sa(current_user)
    assignment = (await db.execute(
        select(CMClientAssignment).where(
            CMClientAssignment.client_id == client_id,
            CMClientAssignment.status == StatusEnum.ACTIVE,
        )
    )).scalar_one_or_none()
    if not assignment:
        raise HTTPException(status_code=404, detail="No active CM assignment")
    assignment.status = StatusEnum.INACTIVE
    await db.commit()
    return {"detail": "CM assignment removed"}


@router.get("/admin/cm/my-clients")
async def cm_my_clients(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """CM: list all clients assigned to me with rights level."""
    assignments = (await db.execute(
        select(CMClientAssignment).where(
            CMClientAssignment.cm_user_id == current_user.id,
            CMClientAssignment.status == StatusEnum.ACTIVE,
        )
    )).scalars().all()
    out = []
    for a in assignments:
        client = (await db.execute(select(Client).where(Client.id == a.client_id))).scalar_one_or_none()
        if client:
            out.append({
                "client_id": client.id,
                "full_name": client.full_name,
                "display_name": client.display_name,
                "short_name": client.short_name,
                "logo_url": client.logo_url,
                "primary_colour": client.primary_colour,
                "status": client.status.value,
                "rights": a.rights.value,
                "assigned_at": a.assigned_at,
                # Env-driven base URL — same _base_url() helper used by
                # login URLs + onboarding links. Hardcoding rootstalk.in
                # (the prior value) 404s on testing because the CA portal
                # lives on rstalk-ca.eywa.farm there. Mirror of the fix
                # in commit 556113b for the welcome-email login URL.
                "portal_url": f"{_base_url()}/login/{client.short_name}",
            })
    return out


@router.post("/admin/cm/clients/{client_id}/login-as")
async def cm_login_as(
    client_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """SSO from SA Portal → CA Portal for a CM with an ACTIVE EDIT
    assignment on the target client. Issues a fresh JWT bound to
    `client_id` (same shape as a normal portal login — includes the
    `client_id` and `client_short_name` claims that drive the
    tenant-isolation gate in get_current_user).

    The frontend then opens https://<ca-portal>/cm-login#token=<jwt>
    in a new tab; the CA Portal's /cm-login route persists the token
    and lands on /dashboard with full CA-equivalent access (CM-EDIT
    bypasses every role guard inside the client portal).

    Per user 2026-05-18: "The CM will have all the privileges inside
    the Client — that of the CA, Subject Experts, and all other
    roles. The CM will be the person who bails them out of any
    trouble, or handles any support request."

    Auth: the CM must have an ACTIVE CMClientAssignment for this
    client with EDIT rights. VIEW assignments don't grant login-as
    (a read-only CM can browse via the SA Portal's /clients/{cid}
    page; they don't need to BE the client). The SA itself can also
    call this endpoint for any client — useful for support
    walkthrough sessions.
    """
    from app.modules.auth.service import _build_token, start_new_session

    client = (await db.execute(
        select(Client).where(Client.id == client_id)
    )).scalar_one_or_none()
    if client is None:
        raise HTTPException(status_code=404, detail="Client not found")

    # SA can login-as anyone; CM needs an ACTIVE EDIT assignment.
    if current_user.email != settings.sa_email:
        assignment = (await db.execute(
            select(CMClientAssignment).where(
                CMClientAssignment.cm_user_id == current_user.id,
                CMClientAssignment.client_id == client_id,
                CMClientAssignment.status == StatusEnum.ACTIVE,
                CMClientAssignment.rights == CMRights.EDIT,
            ).limit(1)
        )).scalar_one_or_none()
        if assignment is None:
            raise HTTPException(
                status_code=403,
                detail={
                    "code": "cm_login_as_forbidden",
                    "message": (
                        "Login-as requires an active CM assignment "
                        "with EDIT rights on this client."
                    ),
                },
            )

    # Rotate the session so the new token is the only valid one for
    # this user — the previous SA-portal token gets invalidated.
    # That's the right semantics: the CM is "switching" into the
    # client; their SA-portal tab will need to re-login when they
    # come back. Matches the single-device model the JWT jti enforces.
    await start_new_session(db, current_user)
    token = _build_token(
        current_user,
        client_id=client.id,
        client_short_name=client.short_name,
    )
    return {
        "access_token": token,
        "client_short_name": client.short_name,
        "ca_portal_url": _base_url().rstrip("/"),
    }


# ── EL Subscription Management Module (2026-05-30) ────────────────────────────
# SA-managed surface for clients that don't pay via Razorpay top-ups —
# either invoice-paid pool grants or flat-fee Enterprise Licences.

class SubscriptionGrantRequest(BaseModel):
    units: int                       # may go up to 6 digits
    note: Optional[str] = None       # invoice / PO reference


class EnterpriseLicenseRequest(BaseModel):
    from_date: date
    to_date: date
    note: Optional[str] = None


def _validate_units(units: int) -> None:
    if not isinstance(units, int) or isinstance(units, bool):
        raise HTTPException(status_code=422, detail={
            "code": "units_invalid",
            "message": "units must be a positive integer.",
        })
    if units <= 0:
        raise HTTPException(status_code=422, detail={
            "code": "units_invalid",
            "message": "units must be greater than 0.",
        })
    if units > 999_999:
        raise HTTPException(status_code=422, detail={
            "code": "units_too_large",
            "message": "units must be at most 999999.",
        })


@router.post("/admin/clients/{client_id}/subscription-grants", status_code=201)
async def sa_grant_subscriptions(
    client_id: str,
    request: SubscriptionGrantRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """SA grants invoice-paid pool units to a client.

    Writes a SubscriptionPool row with Razorpay columns NULL,
    `purchased_by_user_id` = SA, and the request's `note` as the
    invoice / PO reference. Adds to the company's unallocated balance
    immediately — CA can allocate to promoter kitties right away.
    """
    from app.modules.subscriptions.models import SubscriptionPool

    _require_sa(current_user)
    _validate_units(request.units)
    client = (await db.execute(
        select(Client).where(Client.id == client_id)
    )).scalar_one_or_none()
    if client is None:
        raise HTTPException(status_code=404, detail="Client not found")

    pool = SubscriptionPool(
        client_id=client_id,
        units_purchased=request.units,
        units_consumed=0,
        purchased_by_user_id=current_user.id,
        note=(request.note or "").strip() or None,
    )
    db.add(pool)
    await db.commit()
    await db.refresh(pool)
    return {
        "id": pool.id,
        "client_id": client_id,
        "units": pool.units_purchased,
        "note": pool.note,
        "purchased_at": pool.purchased_at,
        "purchased_by_user_id": pool.purchased_by_user_id,
    }


@router.post("/admin/clients/{client_id}/enterprise-licenses", status_code=201)
async def sa_grant_enterprise_license(
    client_id: str,
    request: EnterpriseLicenseRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """SA grants an Enterprise Licence to a client.

    Refuses (409) if another ACTIVE licence already exists for the
    same client — there's only ever one active licence per client at
    a time. Old EXPIRED / REVOKED rows are retained for the audit
    trail.

    Date validation:
      - to_date strictly after from_date
      - to_date not in the past (so the daily sweep doesn't auto-
        expire on the same day it's granted)
    """
    from app.modules.subscriptions.models import EnterpriseLicense

    _require_sa(current_user)
    client = (await db.execute(
        select(Client).where(Client.id == client_id)
    )).scalar_one_or_none()
    if client is None:
        raise HTTPException(status_code=404, detail="Client not found")
    if request.to_date <= request.from_date:
        raise HTTPException(status_code=422, detail={
            "code": "to_date_must_follow_from_date",
            "message": "to_date must be strictly after from_date.",
        })
    if request.to_date < date.today():
        raise HTTPException(status_code=422, detail={
            "code": "to_date_in_past",
            "message": "to_date cannot be in the past.",
        })

    existing = (await db.execute(
        select(EnterpriseLicense).where(
            EnterpriseLicense.client_id == client_id,
            EnterpriseLicense.status == "ACTIVE",
        )
    )).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=409, detail={
            "code": "active_license_exists",
            "message": (
                "This client already has an active Enterprise Licence. "
                "Revoke or wait for it to expire before granting another."
            ),
        })

    lic = EnterpriseLicense(
        client_id=client_id,
        from_date=request.from_date,
        to_date=request.to_date,
        status="ACTIVE",
        granted_by_user_id=current_user.id,
        note=(request.note or "").strip() or None,
    )
    db.add(lic)
    await db.commit()
    await db.refresh(lic)
    return {
        "id": lic.id,
        "client_id": client_id,
        "from_date": lic.from_date,
        "to_date": lic.to_date,
        "status": lic.status,
        "note": lic.note,
        "granted_by_user_id": lic.granted_by_user_id,
        "created_at": lic.created_at,
    }


@router.put("/admin/clients/{client_id}/enterprise-licenses/{license_id}/revoke")
async def sa_revoke_enterprise_license(
    client_id: str,
    license_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """SA kills an active Enterprise Licence early (e.g. renewal didn't
    come through). Same client-INACTIVE flip as natural expiry.

    Existing farmer subscriptions assigned during the active window
    continue to their natural close — same rule as natural expiry.
    Only new subscriptions and portal logins are blocked.
    """
    from app.modules.subscriptions.models import EnterpriseLicense

    _require_sa(current_user)
    lic = (await db.execute(
        select(EnterpriseLicense).where(
            EnterpriseLicense.id == license_id,
            EnterpriseLicense.client_id == client_id,
            EnterpriseLicense.status == "ACTIVE",
        )
    )).scalar_one_or_none()
    if lic is None:
        raise HTTPException(status_code=404, detail="Active licence not found")

    lic.status = "REVOKED"
    client = (await db.execute(
        select(Client).where(Client.id == client_id)
    )).scalar_one_or_none()
    if client is not None:
        client.status = ClientStatus.INACTIVE
    await db.commit()
    await db.refresh(lic)
    return {
        "id": lic.id,
        "status": lic.status,
        "client_status": (
            client.status.value if client and hasattr(client.status, "value")
            else str(client.status) if client else None
        ),
    }


@router.get("/admin/clients/{client_id}/subscription-mgmt")
async def sa_subscription_mgmt_view(
    client_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """SA-side detail view: totals + active licence + grants history.

    Powers the SA Portal's "Subscriptions" section on a client detail
    page. Returns:
      - `pool_totals`: aggregates from SubscriptionPool (purchased,
        consumed, unallocated_balance).
      - `active_license`: the current ACTIVE EnterpriseLicense if
        any, plus `days_remaining`.
      - `grants_history`: every SubscriptionPool row, newest first,
        with source ("RAZORPAY" or "INVOICE"/"SA_GRANT") tagged.
      - `licenses_history`: every EnterpriseLicense row, newest first.
    """
    from app.modules.subscriptions.models import (
        EnterpriseLicense, SubscriptionPool,
    )
    from app.services.promoter_pool import (
        get_company_unallocated_balance, is_enterprise_licensed,
    )
    from sqlalchemy import func as sa_func

    _require_sa(current_user)
    client = (await db.execute(
        select(Client).where(Client.id == client_id)
    )).scalar_one_or_none()
    if client is None:
        raise HTTPException(status_code=404, detail="Client not found")

    purchased_total = (await db.execute(
        select(sa_func.coalesce(sa_func.sum(SubscriptionPool.units_purchased), 0))
        .where(SubscriptionPool.client_id == client_id)
    )).scalar() or 0
    consumed_total = (await db.execute(
        select(sa_func.coalesce(sa_func.sum(SubscriptionPool.units_consumed), 0))
        .where(SubscriptionPool.client_id == client_id)
    )).scalar() or 0
    unallocated = await get_company_unallocated_balance(db, client_id)
    el_active = await is_enterprise_licensed(db, client_id)

    active_license = None
    today = date.today()
    if el_active:
        lic_row = (await db.execute(
            select(EnterpriseLicense).where(
                EnterpriseLicense.client_id == client_id,
                EnterpriseLicense.status == "ACTIVE",
            )
        )).scalar_one_or_none()
        if lic_row:
            active_license = {
                "id": lic_row.id,
                "from_date": lic_row.from_date,
                "to_date": lic_row.to_date,
                "days_remaining": (lic_row.to_date - today).days,
                "note": lic_row.note,
                "granted_by_user_id": lic_row.granted_by_user_id,
                "created_at": lic_row.created_at,
            }

    grants_rows = (await db.execute(
        select(SubscriptionPool)
        .where(SubscriptionPool.client_id == client_id)
        .order_by(SubscriptionPool.purchased_at.desc())
    )).scalars().all()
    grants_history = [
        {
            "id": g.id,
            "units": int(g.units_purchased),
            "consumed": int(g.units_consumed),
            "purchased_at": g.purchased_at,
            "source": "RAZORPAY" if g.razorpay_payment_id else "SA_GRANT",
            "note": g.note,
            "amount_paid_paise": g.amount_paid_paise,
        }
        for g in grants_rows
    ]

    lic_rows = (await db.execute(
        select(EnterpriseLicense)
        .where(EnterpriseLicense.client_id == client_id)
        .order_by(EnterpriseLicense.created_at.desc())
    )).scalars().all()
    licenses_history = [
        {
            "id": l.id,
            "from_date": l.from_date,
            "to_date": l.to_date,
            "status": l.status,
            "note": l.note,
            "granted_by_user_id": l.granted_by_user_id,
            "created_at": l.created_at,
        }
        for l in lic_rows
    ]

    return {
        "client_id": client_id,
        "client_status": (
            client.status.value if hasattr(client.status, "value")
            else str(client.status)
        ),
        "pool_totals": {
            "purchased_total": int(purchased_total),
            "consumed_total": int(consumed_total),
            "unallocated_balance": int(unallocated),
            "unlimited": el_active,
        },
        "active_license": active_license,
        "grants_history": grants_history,
        "licenses_history": licenses_history,
    }


# ── CA Admin: subscription cleanup (test-data clearance) ──────────────────────
#
# Time-sensitive surface added 2026-06-28: clients begin training in the
# next 5-7 days and need a way to clear practice subscriptions they
# create during training before going live. Decision (yesterday's memo):
# - Soft delete only — `deleted_at` flag, no scheduled purge.
# - Bound to CA Admin's `client_id` — no cross-tenant reach.
# - User accounts left alone (platform-wide identity).
# - "Hide everywhere" handled by the soft-delete event listener +
#   manual sweep of direct-subscription_id queries on cascade tables.
# - DPDP-triggered user erasure is a separate future workflow.


async def _assert_ca_admin_or_cm_for_client(
    db: AsyncSession, user_id: str, client_id: str,
) -> None:
    """Gate the cleanup endpoints to CA-role (or CM-EDIT impersonating
    the CA via the login-as flow). 403 with stable `ca_admin_only`."""
    cus = (await db.execute(
        select(ClientUser).where(
            ClientUser.user_id == user_id,
            ClientUser.client_id == client_id,
            ClientUser.status == StatusEnum.ACTIVE,
        )
    )).scalars().all()
    if any(cu.role == ClientUserRole.CA for cu in cus):
        return
    cm = (await db.execute(
        select(CMClientAssignment.id).where(
            CMClientAssignment.cm_user_id == user_id,
            CMClientAssignment.client_id == client_id,
            CMClientAssignment.status == StatusEnum.ACTIVE,
            CMClientAssignment.rights == CMRights.EDIT,
        ).limit(1)
    )).scalar_one_or_none()
    if cm is not None:
        return
    raise HTTPException(status_code=403, detail={
        "code": "ca_admin_only",
        "message": (
            "Only the CA Admin (or a CM with edit rights for this "
            "company) can clear subscriptions."
        ),
    })


@router.get("/admin/client/{client_id}/subscriptions/cleanup")
async def list_subscriptions_for_cleanup(
    client_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Rich subscription list for the CA Admin's cleanup screen.

    Shows ALL lifecycle states (ACTIVE / LAPSED / CANCELLED /
    UNSUBSCRIBED / WAITLISTED / SUSPENDED) — already-soft-deleted rows
    are excluded by the soft-delete event listener so the list is
    always "the live set the CA could clear next." Per-row columns:
    reference_number, package_name, crop_name, acreage-or-plants
    (formatted by measure), district/state, farmer name + phone,
    created_at, in-flight counts (orders + queries).

    Auth: CA (or CM-EDIT impersonator). Tenant-scoped to client_id.
    """
    await _assert_ca_admin_or_cm_for_client(db, current_user.id, client_id)

    # 1. Pull subscriptions for this client.
    subs = (await db.execute(
        select(Subscription)
        .where(Subscription.client_id == client_id)
        .order_by(Subscription.created_at.desc())
    )).scalars().all()
    if not subs:
        return []

    sub_ids = [s.id for s in subs]
    user_ids = list({s.farmer_user_id for s in subs})
    package_ids = list({s.package_id for s in subs})

    # 2. Farmer User rows (name + phone + location for state/district).
    users = (await db.execute(
        select(User).where(User.id.in_(user_ids))
    )).scalars().all()
    user_by_id = {u.id: u for u in users}

    # 3. Package rows for the package name (+ crop reference).
    pkgs = (await db.execute(
        select(Package).where(Package.id.in_(package_ids))
    )).scalars().all()
    pkg_by_id = {p.id: p for p in pkgs}

    # 4. Resolve crop names + farmer state/district via Cosh. Packages
    # reference crop_cosh_id; users reference state_cosh_id /
    # district_cosh_id. Localise per the caller's language. Falls back
    # to the cosh_id on missing translation so the column never reads
    # blank.
    from app.services.i18n_cosh import resolve_names_by_cosh_id
    cosh_ids_to_resolve: set[str] = set()
    for p in pkgs:
        cid = getattr(p, "crop_cosh_id", None)
        if cid:
            cosh_ids_to_resolve.add(cid)
    for u in users:
        for fld in ("state_cosh_id", "district_cosh_id"):
            cid = getattr(u, fld, None)
            if cid:
                cosh_ids_to_resolve.add(cid)
    lang = current_user.language_code or "en"
    cosh_loc = await resolve_names_by_cosh_id(
        db, cosh_ids_to_resolve, lang,
    ) if cosh_ids_to_resolve else {}

    # 5. In-flight order counts (active items, not archived). Cheap
    # group-by per subscription.
    from app.modules.orders.models import Order, OrderItem, OrderItemStatus
    from sqlalchemy import func as sa_func
    in_flight_states = (
        OrderItemStatus.PENDING, OrderItemStatus.AVAILABLE,
        OrderItemStatus.POSTPONED, OrderItemStatus.SENT_FOR_APPROVAL,
        OrderItemStatus.NOT_AVAILABLE,
    )
    order_counts_rows = (await db.execute(
        select(Order.subscription_id, sa_func.count(OrderItem.id))
        .join(OrderItem, OrderItem.order_id == Order.id)
        .where(
            Order.subscription_id.in_(sub_ids),
            OrderItem.status.in_(in_flight_states),
            OrderItem.archived_at.is_(None),
        )
        .group_by(Order.subscription_id)
    )).all()
    orders_by_sub = {sid: n for sid, n in order_counts_rows}

    # 6. Open pundit queries per subscription. NEW / FORWARDED /
    # RETURNED are "still in flight" from the farmer's POV; responded /
    # closed don't need surfacing here.
    from app.modules.farmpundit.models import Query as PunditQuery, QueryStatus
    open_query_states = (
        QueryStatus.NEW.value, QueryStatus.FORWARDED.value,
        QueryStatus.RETURNED.value,
    )
    query_counts_rows = (await db.execute(
        select(PunditQuery.subscription_id, sa_func.count())
        .where(
            PunditQuery.subscription_id.in_(sub_ids),
            PunditQuery.status.in_(open_query_states),
        )
        .group_by(PunditQuery.subscription_id)
    )).all()
    queries_by_sub = {sid: n for sid, n in query_counts_rows}

    # 7. Compose rows.
    out: list[dict] = []
    for sub in subs:
        farmer = user_by_id.get(sub.farmer_user_id)
        pkg = pkg_by_id.get(sub.package_id)
        # Package may be deleted/published variant — fall back to name
        # only if present.
        package_name = getattr(pkg, "name", None) if pkg else None
        crop_cosh = getattr(pkg, "crop_cosh_id", None) if pkg else None
        crop_name = (cosh_loc.get(crop_cosh) if crop_cosh else None) or crop_cosh
        # Plant-wise vs area-wise scale label.
        if sub.number_of_plants is not None:
            scale_label = f"{sub.number_of_plants} plants"
            if sub.planting_year:
                scale_label += f" · planted {sub.planting_year}"
        elif sub.farm_area_acres is not None:
            unit = sub.area_unit or "acres"
            scale_label = f"{float(sub.farm_area_acres):g} {unit}"
        else:
            scale_label = None
        # Farmer location (district / state) lives on User as Cosh
        # ids. Resolve via the localised map; fall back to the raw
        # cosh_id then to None.
        district_cid = getattr(farmer, "district_cosh_id", None) if farmer else None
        state_cid = getattr(farmer, "state_cosh_id", None) if farmer else None
        district = (cosh_loc.get(district_cid) if district_cid else None) or district_cid
        state = (cosh_loc.get(state_cid) if state_cid else None) or state_cid
        location_parts = [p for p in (district, state) if p]
        location = ", ".join(location_parts) if location_parts else None

        out.append({
            "subscription_id": sub.id,
            "reference_number": sub.reference_number,
            "status": sub.status.value if hasattr(sub.status, "value") else sub.status,
            "package_id": sub.package_id,
            "package_name": package_name,
            "crop_name": crop_name,
            "scale_label": scale_label,
            "location": location,
            "farmer_user_id": sub.farmer_user_id,
            "farmer_name": getattr(farmer, "name", None) if farmer else None,
            "farmer_phone": getattr(farmer, "phone", None) if farmer else None,
            "created_at": sub.created_at,
            "in_flight_orders": orders_by_sub.get(sub.id, 0),
            "in_flight_queries": queries_by_sub.get(sub.id, 0),
        })
    return out


class BulkSoftDeleteRequest(BaseModel):
    subscription_ids: list[str]
    reason: Optional[str] = None


@router.post("/admin/client/{client_id}/subscriptions/bulk-soft-delete")
async def bulk_soft_delete_subscriptions(
    client_id: str,
    request: BulkSoftDeleteRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Stamp `deleted_at` + `deleted_by_user_id` on the given
    subscription rows. Validates that every requested id belongs to
    `client_id` — silently skips any that don't to prevent cross-tenant
    leak (and reports the skipped count so the UI can flag it).

    Idempotent on already-soft-deleted rows (no double-stamping;
    counted as "skipped"). Returns a summary the UI can confirm.

    Cascade strategy: we soft-delete ONLY the Subscription row. The
    session-level event listener + the hot-read-path sweep makes the
    cascade invisible without touching individual order / query / ack
    rows. No purge job is scheduled — flag-and-stay per yesterday's
    decision.
    """
    await _assert_ca_admin_or_cm_for_client(db, current_user.id, client_id)

    if not request.subscription_ids:
        raise HTTPException(status_code=422, detail={
            "code": "no_subscriptions_selected",
            "message": "Pick at least one subscription to clear.",
        })

    # Fetch only the subscriptions in this client's tenant. Cross-
    # tenant ids silently miss the filter and surface as `skipped`.
    # Opt into `include_deleted=True` so the listener doesn't hide
    # already-soft-deleted rows from us — we want to report them as
    # "already deleted" rather than as cross-tenant skips.
    subs = (await db.execute(
        select(Subscription).where(
            Subscription.id.in_(request.subscription_ids),
            Subscription.client_id == client_id,
        )
        .execution_options(include_deleted=True)
    )).scalars().all()
    found_ids = {s.id for s in subs}
    skipped_cross_tenant = [
        sid for sid in request.subscription_ids if sid not in found_ids
    ]

    now = datetime.now(timezone.utc)
    soft_deleted: list[str] = []
    already_deleted: list[str] = []
    for sub in subs:
        if sub.deleted_at is not None:
            already_deleted.append(sub.id)
            continue
        sub.deleted_at = now
        sub.deleted_by_user_id = current_user.id
        soft_deleted.append(sub.id)

    await db.commit()
    return {
        "soft_deleted_count": len(soft_deleted),
        "soft_deleted_ids": soft_deleted,
        "already_deleted_count": len(already_deleted),
        "already_deleted_ids": already_deleted,
        "skipped_cross_tenant_count": len(skipped_cross_tenant),
        "skipped_cross_tenant_ids": skipped_cross_tenant,
        "deleted_by_user_id": current_user.id,
        "deleted_at": now.isoformat(),
        "reason": request.reason,
    }
