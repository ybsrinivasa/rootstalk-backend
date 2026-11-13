from datetime import date, datetime, timedelta, timezone
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from pydantic import BaseModel
from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import or_, select, update
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
    get_client_by_token, create_ca_user,
    rotate_ca_admin, activate_ca_by_client_user_id,
    send_ca_reassignment_email,
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
        # Advisory-Only Mode (2026-09-16). Defaults to False; the fee
        # override is None (traditional bulk-discount pricing). SA can
        # tick + set at initiate time OR later via ClientEdit.
        advisory_only_mode=request.advisory_only_mode,
        dealer_list_enabled=request.dealer_list_enabled,
        subscription_fee_paise=request.subscription_fee_paise,
        input_alert_lead_days=request.input_alert_lead_days,
        in_app_orders_enabled=request.in_app_orders_enabled,
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

    # 2026-07-06 — CA-admin rotation trigger. If the SA is changing
    # ca_email AND the client is past approval (has an actual CA user
    # backing the login), we need to rotate the User account, not just
    # relabel the Client row. Detect the change BEFORE we mutate
    # client.ca_email in the setattr loop below. See `rotate_ca_admin`
    # in `service.py` for the workflow.
    old_ca_email = (client.ca_email or "").lower()
    incoming_ca_email = data.get("ca_email")
    ca_email_will_change = (
        incoming_ca_email is not None
        and str(incoming_ca_email).lower() != old_ca_email
    )

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

    # 2026-07-06 — Rotate the CA user if the SA changed ca_email and
    # the client is past approval. For PENDING_REVIEW clients the CA
    # User doesn't exist yet — the approve endpoint (`create_ca_user`)
    # will pick up whatever ca_email is on the Client row at that
    # time. Idempotent guard also inside `rotate_ca_admin`.
    if ca_email_will_change and client.status == ClientStatus.ACTIVE:
        from app.modules.clients.service import (
            rotate_ca_admin, send_ca_credentials_email,
            send_ca_reassignment_email,
        )
        created_new, plain_password, reused_phone = await rotate_ca_admin(db, client)
        await db.commit()
        if settings.email_smtp_user:
            login_url = f"{_base_url()}/login/{client.short_name}"
            if created_new and plain_password:
                # Fresh User created — send full credentials.
                await send_ca_credentials_email(
                    client.ca_email, client.ca_name, login_url, plain_password,
                )
            else:
                # Existing User re-assigned. No password to rotate
                # (would break their existing sign-in flow); notify
                # them of the appointment and lead with OTP as the
                # primary path since we can't verify their password
                # is set up.
                await send_ca_reassignment_email(
                    client.ca_email,
                    client.ca_name,
                    client.display_name or client.full_name,
                    login_url,
                    ca_phone=reused_phone,
                )

    return await _client_to_out(db, client)


# ── Per-client CA table (2026-07-07) ────────────────────────────────────────
#
# SA-facing surface for managing the CA history of a single client.
# The old flow (SA edits `ca_email` on the client-edit modal) still
# works and rotates the CA correctly, but this table view makes the
# history visible and lets the SA reactivate a previously-CA user
# without re-typing their email. Backend model didn't change — every
# rotate has always left the old CA row as INACTIVE; these endpoints
# just surface + curate that history.


class NewCAIn(BaseModel):
    ca_email: str
    ca_name: str


async def _serialise_ca_rows(db: AsyncSession, client_id: str) -> list[dict]:
    """List every CA `ClientUser` row for the given client, joined with
    the backing User so the SA sees name/email/phone alongside status
    and timestamps. Sorted: ACTIVE first, then most-recent
    deactivated_at, then falling back to created_at."""
    rows = (await db.execute(
        select(ClientUser, User)
        .join(User, User.id == ClientUser.user_id)
        .where(
            ClientUser.client_id == client_id,
            ClientUser.role == ClientUserRole.CA,
        )
    )).all()
    out = []
    for cu, u in rows:
        out.append({
            "id": cu.id,
            "user_id": u.id,
            "name": u.name,
            "email": u.email,
            "phone": u.phone,
            "status": cu.status.value if hasattr(cu.status, "value") else str(cu.status),
            "activated_at": cu.created_at,
            "deactivated_at": cu.deactivated_at,
        })
    out.sort(key=lambda r: (
        0 if r["status"] == "ACTIVE" else 1,
        # For inactive rows, most-recent-deactivation first. Fall
        # back to activated_at when deactivated_at is NULL (pre-
        # migration rows that were already inactive).
        -(r["deactivated_at"] or r["activated_at"]).timestamp()
        if r["status"] != "ACTIVE" else 0,
    ))
    return out


@router.get("/admin/clients/{client_id}/cas")
async def list_client_cas(
    client_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_sa(current_user)
    client = (await db.execute(
        select(Client).where(Client.id == client_id)
    )).scalar_one_or_none()
    if client is None:
        raise HTTPException(status_code=404, detail="Client not found")
    return await _serialise_ca_rows(db, client_id)


@router.post("/admin/clients/{client_id}/cas", status_code=201)
async def add_client_ca(
    client_id: str,
    payload: NewCAIn,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Add a new CA to a client. Mirrors what the edit-client modal
    does when SA changes `ca_email`, but exposed as an explicit
    action on the CA table. Deactivates the current CA (if any),
    reuses or creates the new CA's User row, and sends credentials
    (fresh User) or a reassignment email (reused User)."""
    _require_sa(current_user)
    client = (await db.execute(
        select(Client).where(Client.id == client_id)
    )).scalar_one_or_none()
    if client is None:
        raise HTTPException(status_code=404, detail="Client not found")
    if client.status != ClientStatus.ACTIVE:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "client_not_active",
                "message": (
                    "CA table is only editable on ACTIVE clients. "
                    "Approve the client first."
                ),
            },
        )

    new_email = (payload.ca_email or "").strip()
    new_name = (payload.ca_name or "").strip()
    if not new_email or not new_name:
        raise HTTPException(
            status_code=422,
            detail="ca_email and ca_name are required",
        )

    # Mirror the ca_email / ca_name onto the Client row first so
    # `rotate_ca_admin` (which reads from client) picks up the new
    # values. `edit_client` uses the same shape.
    client.ca_email = new_email
    client.ca_name = new_name
    await db.commit()

    created_new, plain_password, reused_phone = await rotate_ca_admin(db, client)
    await db.commit()

    if settings.email_smtp_user:
        login_url = f"{_base_url()}/login/{client.short_name}"
        if created_new and plain_password:
            await send_ca_credentials_email(
                client.ca_email, client.ca_name, login_url, plain_password,
            )
        else:
            await send_ca_reassignment_email(
                client.ca_email,
                client.ca_name,
                client.display_name or client.full_name,
                login_url,
                ca_phone=reused_phone,
            )

    return await _serialise_ca_rows(db, client_id)


@router.post("/admin/clients/{client_id}/cas/{client_user_id}/activate")
async def activate_client_ca(
    client_id: str,
    client_user_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Flip a specific past CA (identified by ClientUser.id) back to
    ACTIVE. Deactivates whoever is currently the active CA and
    updates the Client row's mirrored `ca_email` / `ca_name` to the
    reactivated user's values."""
    _require_sa(current_user)
    client = (await db.execute(
        select(Client).where(Client.id == client_id)
    )).scalar_one_or_none()
    if client is None:
        raise HTTPException(status_code=404, detail="Client not found")
    if client.status != ClientStatus.ACTIVE:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "client_not_active",
                "message": "CA table is only editable on ACTIVE clients.",
            },
        )

    new_active_user, previous_user = await activate_ca_by_client_user_id(
        db, client, client_user_id,
    )

    # Sync mirror on Client row so downstream reads (login page,
    # branding, etc.) match the active CA.
    client.ca_email = new_active_user.email
    client.ca_name = new_active_user.name
    await db.commit()

    if (
        settings.email_smtp_user
        and previous_user is not None
        and previous_user.id != new_active_user.id
    ):
        login_url = f"{_base_url()}/login/{client.short_name}"
        await send_ca_reassignment_email(
            new_active_user.email,
            new_active_user.name,
            client.display_name or client.full_name,
            login_url,
            ca_phone=new_active_user.phone,
        )

    return await _serialise_ca_rows(db, client_id)


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


@router.get("/admin/clients/{client_id}/manufacturers")
async def list_client_cosh_manufacturers(
    client_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """2026-10-17 (Phase 6a / Decision U) — list the Cosh Manufacturers
    this Client is linked to for the Soil Nutrient module. Returns
    `[{cosh_id, name}]` — same shape as `/admin/cosh/manufacturers`,
    enriched with the manufacturer display name from Cosh.

    Independent of `clients.cosh_manufacturer_id` (single value used
    by QR Product Authentication). This endpoint reads the M:N table.
    """
    _require_sa(current_user)
    from app.modules.clients.models import ClientCoshManufacturer
    from app.modules.sync.models import CoshCoreItem

    linked = (await db.execute(
        select(ClientCoshManufacturer.cosh_manufacturer_id)
        .where(ClientCoshManufacturer.client_id == client_id)
    )).scalars().all()
    if not linked:
        return []
    rows = (await db.execute(
        select(CoshCoreItem.cosh_id, CoshCoreItem.translations).where(
            CoshCoreItem.core_type == "input_manufacturers",
            CoshCoreItem.cosh_id.in_(linked),
        )
    )).all()
    name_by_id: dict[str, str] = {}
    for cosh_id, translations in rows:
        name = None
        if isinstance(translations, dict):
            name = translations.get("en") or next(
                (v for v in translations.values() if v), None,
            )
        name_by_id[cosh_id] = name or cosh_id
    out = [
        {"cosh_id": cid, "name": name_by_id.get(cid, cid)}
        for cid in linked
    ]
    out.sort(key=lambda r: (r["name"] or "").lower())
    return out


@router.put("/admin/clients/{client_id}/manufacturers")
async def replace_client_cosh_manufacturers(
    client_id: str,
    data: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """2026-10-17 (Phase 6a / Decision U) — replace the full set of
    linked Cosh Manufacturers for this Client. Payload: `{"cosh_ids":
    [...]}`. Replace-all semantics: existing rows deleted, new rows
    inserted. Idempotent on repeat.

    Does NOT touch `clients.cosh_manufacturer_id` (QR flow owns that
    single column independently). SA may consider syncing one entry
    from the multi-select back into the QR column via the Client edit
    modal if they want both to agree, but that's an explicit action,
    not an automatic one.
    """
    _require_sa(current_user)
    cosh_ids = data.get("cosh_ids") or []
    if not isinstance(cosh_ids, list):
        raise HTTPException(status_code=422, detail="cosh_ids must be a list")

    client = (await db.execute(
        select(Client).where(Client.id == client_id)
    )).scalar_one_or_none()
    if client is None:
        raise HTTPException(status_code=404, detail="Client not found")

    from app.modules.clients.models import ClientCoshManufacturer
    # Validate every cosh_id against Cosh master so we don't accept
    # typos / stale IDs.
    if cosh_ids:
        from app.modules.sync.models import CoshCoreItem
        valid = (await db.execute(
            select(CoshCoreItem.cosh_id).where(
                CoshCoreItem.core_type == "input_manufacturers",
                CoshCoreItem.status == "active",
                CoshCoreItem.cosh_id.in_(cosh_ids),
            )
        )).scalars().all()
        valid_set = set(valid)
        invalid = [c for c in cosh_ids if c not in valid_set]
        if invalid:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "invalid_manufacturer_ids",
                    "message": f"Unknown Cosh manufacturer IDs: {invalid}",
                },
            )

    # Replace-all: delete existing, insert new.
    await db.execute(
        ClientCoshManufacturer.__table__.delete().where(
            ClientCoshManufacturer.client_id == client_id,
        )
    )
    for cid in cosh_ids:
        db.add(ClientCoshManufacturer(
            client_id=client_id,
            cosh_manufacturer_id=cid,
            created_by=current_user.id,
        ))
    await db.commit()
    return {"client_id": client_id, "cosh_ids": cosh_ids}


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

async def _resolve_coaching_access(
    client: Client, db: AsyncSession,
) -> dict:
    """Resolve the GRACE / ARCHIVED banner data for the Client Portal.
    Empty dict for real (non-coaching) workspaces so the frontend
    hides the banner."""
    if not client.is_coaching or not client.parent_session_id:
        return {}
    from app.modules.coaching.models import (
        CoachingSession, CoachingSessionAccessState,
    )
    session = await db.get(CoachingSession, client.parent_session_id)
    if session is None:
        return {}
    state = session.access_state()
    grace_ends_at = session.grace_ends_at()
    return {
        "coaching_access": {
            "state": state.value,
            "closed_at": session.closed_at.isoformat() if session.closed_at else None,
            "grace_ends_at": grace_ends_at.isoformat() if grace_ends_at else None,
        },
    }


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
        # 2026-10-16 — Soil Nutrient module flags. CA portal sidebar
        # hides /soil-nutrient/* entries when False. SA toggles the
        # flags from the SA Portal Client-detail panel; cached
        # CPClient on localStorage auto-refreshes on next layout mount
        # (see (portal)/layout.tsx refresh effect).
        "soil_nutrient_module_enabled": bool(client.soil_nutrient_module_enabled),
        "has_soil_testing_lab": bool(client.has_soil_testing_lab),
        # 2026-10-16 — Coaching workspace access state (hybrid-access
        # model). For is_coaching=true workspaces the frontend reads
        # this to render the GRACE / ARCHIVED banner. Absent for real
        # (non-coaching) clients so the frontend hides the banner.
        **(await _resolve_coaching_access(client, db)),
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
        # 2026-07-24 — Training Sandbox marker. Farmer PWA reads this
        # on /home/[clientId] to render the training banner + suppress
        # any "real world" affordances.
        "is_training": bool(client.is_training),
        # 2026-09-16 — Advisory-Only Mode marker. Farmer PWA reads this
        # on the client picker (subscribe flow) to render the
        # "Advisory Only" chip. Per-subscription behaviour is driven by
        # Subscription.advisory_only_mode (snapshot), not this field.
        "advisory_only_mode": bool(client.advisory_only_mode),
        "dealer_list_enabled": bool(client.dealer_list_enabled),
        "subscription_fee_paise": client.subscription_fee_paise,
        "input_alert_lead_days": client.input_alert_lead_days,
        "in_app_orders_enabled": bool(client.in_app_orders_enabled) if client.in_app_orders_enabled is not None else False,
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
    from app.modules.coaching.service import guard_coaching_workspace_onboarding

    client = (await db.execute(
        select(Client).where(Client.id == client_id)
    )).scalar_one_or_none()
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")

    # Coaching Sandbox — refuse to invite anyone but the student
    # themselves into an is_coaching workspace. Prevents unsolicited
    # invite emails to real people and prevents attaching real users'
    # identities to the practice workspace. No-op for real clients.
    await guard_coaching_workspace_onboarding(
        db, client_id, target_email=request.email,
    )

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

    # 2026-09-24: also email the existing-user case (a new role added
    # on top of an existing account). Previously only the new-user
    # branch was wired, so an SE promoted to FM (or any similar
    # role-add) had no email trail — they only found out on their next
    # login, if at all. Password NOT included in the role-added email
    # since the user's already got one.
    if settings.email_smtp_user:
        login_url = f"{_base_url()}/login/{client.short_name}"
        if is_new_user:
            await send_portal_user_welcome_email(
                email=user.email,
                name=user.name,
                company_name=client.full_name,
                login_url=login_url,
                password=request.password,
                role_value=cu.role.value,
            )
        else:
            from app.modules.clients.service import send_portal_role_added_email
            await send_portal_role_added_email(
                email=user.email,
                name=user.name,
                company_name=client.full_name,
                login_url=login_url,
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
    """Deactivate / reactivate ALL of a user's ClientUser rows at
    this client in one call. 2026-07-28 — updated to scalars().all()
    so multi-role users (Batch K) flip cleanly; the earlier
    scalar_one_or_none() raised MultipleResultsFound as soon as a
    user held more than one role."""
    new_status = data.get("status")  # "ACTIVE" or "INACTIVE"
    if new_status not in ("ACTIVE", "INACTIVE"):
        raise HTTPException(status_code=422, detail="status must be ACTIVE or INACTIVE")
    rows = (await db.execute(
        select(ClientUser).where(
            ClientUser.client_id == client_id, ClientUser.user_id == user_id,
        )
    )).scalars().all()
    if not rows:
        raise HTTPException(status_code=404, detail="User not found")
    # CA-exclusivity — CA rows are SA-managed only (rotation via
    # client-edit / Manage CAs). Skip them here so a CA-portal toggle
    # can never (a) accidentally re-activate a historical CA row and
    # produce a dual-role state, or (b) deactivate the active CA from
    # under SA's feet.
    non_ca_rows = [cu for cu in rows if cu.role != ClientUserRole.CA]
    if not non_ca_rows:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "ca_role_status_sa_only",
                "message": (
                    "This user only holds the Company Admin role. CA "
                    "activation / deactivation is managed by the Super "
                    "Admin through the client-edit or Manage CAs flow."
                ),
            },
        )
    target = StatusEnum.ACTIVE if new_status == "ACTIVE" else StatusEnum.INACTIVE
    for cu in non_ca_rows:
        cu.status = target
        if target == StatusEnum.INACTIVE:
            cu.deactivated_at = utcnow()
        else:
            cu.deactivated_at = None
    await db.commit()
    return {"detail": f"User status set to {new_status}"}


@router.put("/client/{client_id}/users/{user_id}/roles")
async def reconcile_portal_user_roles(
    client_id: str, user_id: str,
    data: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Reconcile a user's ACTIVE ClientUser roles at this client to
    exactly the given set. Adds missing, soft-deactivates extra.

    Body: ``{"roles": ["SUBJECT_EXPERT", "REPORT_USER", ...]}``.

    Rules:
    - Target set must contain at least one role. Removing a user
      entirely goes through the Deactivate button, not this endpoint.
    - Target set MUST NOT contain CA. CA is exclusive and managed
      by SA via the client-edit flow; refusing here keeps this
      endpoint out of CA-assignment territory (409 ``ca_not_managed_here``).
    - If the user is currently CA, refuse the reconcile entirely
      (409 ``ca_role_exclusive``) — they can't hold non-CA roles.
    - Roles already ACTIVE stay put; roles present but INACTIVE flip
      back to ACTIVE (re-add). Roles absent from target flip to
      INACTIVE with a fresh ``deactivated_at`` stamp.
    """
    from app.modules.clients.models import ClientUser, ClientUserRole
    from app.modules.platform.models import StatusEnum

    target_raw = data.get("roles")
    if not isinstance(target_raw, list) or len(target_raw) == 0:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "roles_required",
                "message": (
                    "Pick at least one role. To remove the user "
                    "entirely, use the Deactivate button."
                ),
            },
        )
    try:
        target_set = {ClientUserRole(r) for r in target_raw}
    except ValueError:
        raise HTTPException(status_code=422, detail="Unknown role in payload")

    if ClientUserRole.CA in target_set:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "ca_not_managed_here",
                "message": (
                    "CA is exclusive and assigned separately by SA. "
                    "This endpoint reconciles non-CA roles only."
                ),
            },
        )

    existing_rows = (await db.execute(
        select(ClientUser).where(
            ClientUser.client_id == client_id, ClientUser.user_id == user_id,
        )
    )).scalars().all()
    if not existing_rows:
        raise HTTPException(status_code=404, detail="User not found at this client")

    # If the user is currently CA, refuse the whole reconcile.
    if any(r.role == ClientUserRole.CA and r.status == StatusEnum.ACTIVE
           for r in existing_rows):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "ca_role_exclusive",
                "message": (
                    "This user is the CA. CA is a single exclusive role — "
                    "they cannot also hold other roles."
                ),
            },
        )

    existing_by_role = {r.role: r for r in existing_rows}
    now = datetime.now(timezone.utc)

    # Add or re-activate the roles in target.
    for role in target_set:
        if role in existing_by_role:
            r = existing_by_role[role]
            if r.status != StatusEnum.ACTIVE:
                r.status = StatusEnum.ACTIVE
                r.deactivated_at = None
        else:
            db.add(ClientUser(
                client_id=client_id, user_id=user_id,
                role=role, status=StatusEnum.ACTIVE,
            ))

    # Deactivate the roles that are ACTIVE-but-not-in-target.
    for r in existing_rows:
        if r.role in target_set:
            continue
        if r.role == ClientUserRole.CA:
            continue  # already refused above; belt-and-braces
        if r.status == StatusEnum.ACTIVE:
            r.status = StatusEnum.INACTIVE
            r.deactivated_at = now

    await db.commit()
    return {"detail": "roles reconciled", "roles": sorted(r.value for r in target_set)}


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

async def _assert_ca_or_field_manager(
    db: AsyncSession, user: User, client_id: str,
) -> None:
    """Role gate on every /client/{cid}/field-manager/* endpoint.

    2026-07-20 — Before this, the FM endpoints had NO auth check at
    all: any authenticated user (PM, SE, REPORT_USER, even users
    not enrolled on this client) could hit them if they knew the
    URL. Caught during the "PM has FM privileges too" audit.

    Allowed:
      - SA (global email match) — always.
      - CA of this client — always; supersedes every other role.
      - FIELD_MANAGER of this client — the intended role.
      - CM with EDIT rights on this client — per project convention
        "CM has all privileges inside the client (CA, SE, and all
        other roles)"; same shape as the QR gate.

    Rejected (403 field_manager_role_required):
      - PRODUCT_MANAGER, SUBJECT_EXPERT, CLIENT_RM, REPORT_USER,
        SEED_DATA_MANAGER (legacy), anyone not enrolled on this
        client. CLIENT_RM was scoped to Alerts only per user
        direction 2026-07-20 — they no longer manage dealers.
    """
    if bool(settings.sa_email) and user.email == settings.sa_email:
        return
    role_row = (await db.execute(
        select(ClientUser.role).where(
            ClientUser.client_id == client_id,
            ClientUser.user_id == user.id,
            ClientUser.status == StatusEnum.ACTIVE,
            ClientUser.role.in_([
                ClientUserRole.CA, ClientUserRole.FIELD_MANAGER,
            ]),
        ).limit(1)
    )).scalar_one_or_none()
    if role_row is not None:
        return
    cm_edit = (await db.execute(
        select(CMClientAssignment.id).where(
            CMClientAssignment.cm_user_id == user.id,
            CMClientAssignment.client_id == client_id,
            CMClientAssignment.status == StatusEnum.ACTIVE,
            CMClientAssignment.rights == CMRights.EDIT,
        ).limit(1)
    )).scalar_one_or_none()
    if cm_edit is not None:
        return
    raise HTTPException(
        status_code=403,
        detail={
            "code": "field_manager_role_required",
            "message": (
                "Dealer and Facilitator management is restricted to "
                "Customer Admin, Field Manager, or CM (edit). Ask "
                "your CA to assign one of these roles at this client."
            ),
        },
    )


@router.get("/client/{client_id}/field-manager/promoters")
async def list_promoters(
    client_id: str,
    promoter_type: str = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _assert_ca_or_field_manager(db, current_user, client_id)
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
        # 2026-08-10 — enrich stepdown-requested rows with the pending
        # counts so the FM sees "who's still holding what" at a glance
        # (same three numbers the notification email carries). Only
        # computed for the requested rows so non-request rows stay
        # cheap.
        row["pending_stepdown"] = None
        if cp.promoter_request_status == "STEPDOWN_REQUESTED":
            from app.modules.orders.router import _pending_stepdown_counts
            row["pending_stepdown"] = await _pending_stepdown_counts(
                db, user.id, client_id,
            )
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
    await _assert_ca_or_field_manager(db, current_user, client_id)
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

    # Coaching Sandbox — inside an is_coaching workspace, only the
    # student's own approved phone can be onboarded as
    # dealer/facilitator. No-op for real clients.
    from app.modules.coaching.service import guard_coaching_workspace_onboarding
    await guard_coaching_workspace_onboarding(
        db, client_id, target_phone=phone,
    )

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
    await _assert_ca_or_field_manager(db, current_user, client_id)
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
    # 2026-07-14: same cascade for Promoter-Pundit — PP requires being
    # an active Promoter (enforced at assign-time by the
    # `promoter_pundit_requires_facilitator_promoter` gate). If we
    # leave PP=True while Promoter=False, the CA Promoters list keeps
    # showing the person as a functional Pundit even though queries
    # can no longer route to them, and the flag would silently
    # re-activate on re-onboarding.
    cp.is_promoter_pundit = False
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
    await _assert_ca_or_field_manager(db, current_user, client_id)
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
    await _assert_ca_or_field_manager(db, current_user, client_id)
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
    await _assert_ca_or_field_manager(db, current_user, client_id)
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
    await _assert_ca_or_field_manager(db, current_user, client_id)
    cp = (await db.execute(
        select(ClientPromoter).where(
            ClientPromoter.id == promoter_id,
            ClientPromoter.client_id == client_id,
        )
    )).scalar_one_or_none()
    if not cp:
        raise HTTPException(status_code=404, detail="Promoter row not found")

    # 2026-07-14: auto-reclaim any un-consumed units before flipping
    # is_promoter=False. Before this, the promoter's PromoterAllocation
    # row was left untouched — the row kept subtracting from company
    # unallocated balance while the promoter could no longer assign,
    # stranding units. Staging sweep 2026-07-14 found 7,334 stranded
    # units across 11 rows on 6 clients from this exact pattern.
    from app.modules.subscriptions.promoter_allocation_models import (
        PromoterAllocation,
    )
    from app.services.promoter_pool import reclaim_from_promoter
    alloc = (await db.execute(
        select(PromoterAllocation).where(
            PromoterAllocation.client_id == client_id,
            PromoterAllocation.promoter_user_id == cp.user_id,
        )
    )).scalar_one_or_none()
    if alloc is not None and alloc.units_balance > 0:
        await reclaim_from_promoter(
            db,
            client_id=client_id,
            promoter_user_id=cp.user_id,
            units=int(alloc.units_balance),
        )

    # 2026-08-10 — snapshot for the FCM push below (whether this
    # revoke is the approval-completion of a stepdown request).
    was_stepdown_request = cp.promoter_request_status == "STEPDOWN_REQUESTED"

    cp.is_promoter = False
    # 2026-07-14: PP requires being an active Promoter, so cascade the
    # flag off here too. Otherwise the CA Promoters list keeps showing
    # the person as a functional Pundit even though the Promoter role
    # (its prerequisite) is gone.
    cp.is_promoter_pundit = False
    cp.promoter_request_status = "NONE"
    cp.promoter_request_responded_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(cp)

    # 2026-08-10 — Push the promoter an FCM so they know the CA has
    # acted. Post-commit + wrapped so a delivery failure doesn't
    # roll back the revoke. The copy differs based on whether this
    # was the promoter's own stepdown being approved vs a
    # company-initiated revoke — the wording matters for trust.
    try:
        from app.services.fcm_service import send_fcm
        from app.modules.platform.models import User as _User
        promoter_user = (await db.execute(
            select(_User).where(_User.id == cp.user_id)
        )).scalar_one_or_none()
        client_row = (await db.execute(
            select(Client).where(Client.id == client_id)
        )).scalar_one_or_none()
        if promoter_user and promoter_user.fcm_token:
            company_name = (
                (client_row.display_name or client_row.full_name)
                if client_row else "the company"
            )
            if was_stepdown_request:
                title = "Your stepdown has been approved"
                body = f"You've been cleared from the Promoter role at {company_name}. Thank you for your service."
            else:
                title = "Promoter role ended"
                body = f"Your Promoter role at {company_name} has been ended by the company."
            await send_fcm(
                token=promoter_user.fcm_token,
                title=title,
                body=body,
                data={
                    "type": "PROMOTER_ROLE_ENDED",
                    "client_id": client_id,
                    "click_action": "/",
                },
            )
    except Exception as exc:   # noqa: BLE001
        import logging
        logging.getLogger(__name__).warning(
            f"revoke_promoter FCM push failed for cp={cp.id}: {exc}"
        )

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
    await _assert_ca_or_field_manager(db, current_user, client_id)
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

async def _bulk_resolve_enrichment(
    db: AsyncSession, subs: list, users: list, lang: str,
) -> dict:
    """Batched Cosh lookups for the enrichment pass. Returns dicts
    keyed by cosh_id for state/district/crop names + a measure map
    keyed by crop_cosh_id, plus a `crop_by_sub` map keyed by
    subscription id → crop_cosh_id (crop lives on Package, not
    Subscription — bulk-loaded here so the callsites stay clean).
    Every alert row uses these; batching keeps the whole page at a
    small fixed number of queries."""
    from app.services.i18n_cosh import resolve_names_by_cosh_id
    from app.services.cosh_crop_view import get_measure_for_biological_name
    from app.modules.advisory.models import Package

    package_ids = {s.package_id for s in subs if s.package_id}
    pkg_crop_by_id: dict[str, str] = {}
    if package_ids:
        pkg_rows = (await db.execute(
            select(Package.id, Package.crop_cosh_id)
            .where(Package.id.in_(package_ids))
        )).all()
        pkg_crop_by_id = {pid: cid for pid, cid in pkg_rows if cid}
    crop_by_sub: dict[str, str | None] = {
        s.id: pkg_crop_by_id.get(s.package_id) for s in subs
    }

    state_ids = {u.state_cosh_id for u in users if u.state_cosh_id}
    district_ids = {u.district_cosh_id for u in users if u.district_cosh_id}
    crop_ids = {c for c in crop_by_sub.values() if c}
    name_by_id = await resolve_names_by_cosh_id(
        db, state_ids | district_ids | crop_ids, lang,
    ) if (state_ids or district_ids or crop_ids) else {}
    measure_by_crop: dict[str, str] = {}
    for cid in crop_ids:
        m = await get_measure_for_biological_name(db, cid)
        measure_by_crop[cid] = m or "AREA_WISE"
    return {
        "name_by_id": name_by_id,
        "measure_by_crop": measure_by_crop,
        "crop_by_sub": crop_by_sub,
    }


def _base_alert_row(sub, user, enrich: dict) -> dict:
    """Shared row shape for both alert types — enrichment plumbed
    identically across pending-start-dates and overdue-inputs so the
    CA UI can render location + crop context without branching."""
    name_by_id = enrich["name_by_id"]
    measure_by_crop = enrich["measure_by_crop"]
    crop_by_sub = enrich["crop_by_sub"]
    crop_cosh_id = crop_by_sub.get(sub.id)
    crop_measure = (
        measure_by_crop.get(crop_cosh_id) if crop_cosh_id else None
    )
    return {
        "subscription_id": sub.id,
        "subscription_ref": sub.reference_number,
        # 2026-11-10 — Added farmer_user_id so the CA Portal Farmer
        # Support list can hard-match alert rows to farmer rows by id
        # instead of the (name + phone) soft key Phase 2 shipped with.
        "farmer_user_id": user.id,
        "farmer_name": user.name,
        "farmer_phone": user.phone,
        "state_cosh_id": user.state_cosh_id,
        "state_name": (
            name_by_id.get(user.state_cosh_id) if user.state_cosh_id else None
        ),
        "district_cosh_id": user.district_cosh_id,
        "district_name": (
            name_by_id.get(user.district_cosh_id) if user.district_cosh_id else None
        ),
        "crop_cosh_id": crop_cosh_id,
        "crop_name": (
            name_by_id.get(crop_cosh_id) if crop_cosh_id else None
        ),
        "crop_measure": crop_measure,
        "farm_area_acres": (
            float(sub.farm_area_acres) if sub.farm_area_acres else None
        ),
        "number_of_plants": (
            int(sub.number_of_plants) if sub.number_of_plants else None
        ),
    }


@router.get("/client/{client_id}/alerts/pending-start-dates")
async def get_pending_start_dates(
    client_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Farmers with ACTIVE subscriptions but no crop start date set.

    2026-07-08 — Auth added (was open to any authenticated user across
    tenants). Enrichment: state / district / crop / area or plants /
    subscription_ref plumbed for the CA-portal Alerts screen.

    Soft-deleted subscriptions are auto-filtered by the session-level
    listener in `app/modules/subscriptions/soft_delete.py`."""
    from app.modules.farmpundit.router import _assert_portal_member
    from app.modules.subscriptions.models import Subscription, SubscriptionStatus
    await _assert_portal_member(db, current_user.id, client_id)

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
    rows = result.all()
    subs = [s for s, _u in rows]
    users = [u for _s, u in rows]
    lang = current_user.language_code or "en"
    enrich = await _bulk_resolve_enrichment(db, subs, users, lang)
    return [
        {
            **_base_alert_row(sub, user, enrich),
            "subscribed_at": sub.subscription_date,
        }
        for sub, user in rows
    ]


@router.get("/client/{client_id}/alerts/overdue-inputs")
async def get_overdue_inputs(
    client_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Farmers whose input practices are due today but have no active
    order **for that specific timeline**.

    2026-07-08 audit + rewrite:
    - Auth added (was open across tenants).
    - Order-active check is now per-timeline. Pre-fix, ANY non-
      cancelled order on the subscription suppressed alerts for ALL
      of its timelines, hiding real work. Now checks
      `order_items.timeline_id == tl.id` so only orders touching this
      timeline count.
    - CALENDAR from_type handled (day-of-year window vs
      crop_start_date year). Pre-fix, CALENDAR timelines never fired.
    - Enrichment: state / district / crop / area or plants / crop age /
      subscription_ref.
    - Kept "one entry per subscription" behaviour (first overdue
      timeline wins) to keep the CA alerts table compact — user will
      call whether to surface all overdue timelines separately.

    Soft-delete filtering: auto via session listener.
    """
    from app.modules.farmpundit.router import _assert_portal_member
    from app.modules.subscriptions.models import Subscription, SubscriptionStatus
    from app.modules.orders.models import Order, OrderItem
    from app.modules.advisory.models import Timeline, Practice, PracticeL0
    from app.modules.subscriptions.router import _compute_crop_age
    from datetime import date, timedelta
    await _assert_portal_member(db, current_user.id, client_id)

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
    subs = [s for s, _u in rows]
    users = [u for _s, u in rows]
    lang = current_user.language_code or "en"
    enrich = await _bulk_resolve_enrichment(db, subs, users, lang)

    overdue = []
    for sub, user in rows:
        crop_start = (
            sub.crop_start_date.date()
            if hasattr(sub.crop_start_date, "date") else sub.crop_start_date
        )
        day_offset = (today - crop_start).days

        tl_result = await db.execute(
            select(Timeline).where(Timeline.package_id == sub.package_id)
        )
        for tl in tl_result.scalars().all():
            from_type = tl.from_type.value if hasattr(tl.from_type, "value") else str(tl.from_type)
            active = False
            if from_type == "DAS" and tl.from_value <= day_offset <= tl.to_value:
                active = True
            elif from_type == "DBS" and -tl.to_value <= day_offset <= -tl.from_value:
                active = True
            elif from_type == "CALENDAR" and tl.from_value is not None and tl.to_value is not None:
                # DOY window resolved against crop_start's year (mirror of
                # order_bundle._timeline_window). Wrap-around windows
                # (from > to) are V1-unsupported; skip cleanly.
                if int(tl.from_value) <= int(tl.to_value):
                    year_start = date(crop_start.year, 1, 1)
                    win_from = year_start + timedelta(days=int(tl.from_value) - 1)
                    win_to = year_start + timedelta(days=int(tl.to_value) - 1)
                    if win_from <= today <= win_to:
                        active = True

            if not active:
                continue

            p_result = await db.execute(
                select(Practice.id).where(
                    Practice.timeline_id == tl.id,
                    Practice.l0_type == PracticeL0.INPUT,
                )
            )
            if not p_result.scalars().first():
                continue

            # Per-timeline order check: any live OrderItem tied to
            # THIS timeline on THIS subscription short-circuits the
            # alert. Pre-fix this was subscription-wide.
            item_row = (await db.execute(
                select(OrderItem.id)
                .join(Order, Order.id == OrderItem.order_id)
                .where(
                    Order.subscription_id == sub.id,
                    Order.status.notin_(["CANCELLED", "EXPIRED"]),
                    OrderItem.timeline_id == tl.id,
                    OrderItem.archived_at.is_(None),
                )
            )).scalars().first()
            if item_row is not None:
                continue

            crop_cosh_id = enrich["crop_by_sub"].get(sub.id)
            crop_measure = enrich["measure_by_crop"].get(crop_cosh_id) if crop_cosh_id else None
            computed_crop_age = (
                _compute_crop_age(sub, crop_measure) if crop_measure else None
            )
            overdue.append({
                **_base_alert_row(sub, user, enrich),
                "crop_start_date": crop_start,
                "computed_crop_age": computed_crop_age,
                "day_offset": day_offset,
                "timeline_name": tl.name,
            })
            break  # One entry per subscription — first overdue tl wins

    return overdue


# ═══════════════════════════════════════════════════════════════════════════════
# Farmer Support View (2026-11-10) — RM-facing CRM-style farmer list +
# three new alert endpoints. Design in
# `~/Downloads/RootsTalk_ClientPortal_FarmerSupport.docx`.
#
# Core principle: read and remind. The CLIENT_RM role gets rich visibility
# across every farmer in this client but can NEVER mutate farmer state —
# that boundary is held by the farmer-ownership checks on every
# `/farmer/*` mutation endpoint (verified 2026-11-10 audit; e.g.
# `_upsert_practice_ack` at subscriptions/router.py:5774, orders POST at
# orders/router.py:234, queries POST at farmpundit/router.py:854 all
# gate on `current_user.id == farmer_user_id`).
# ═══════════════════════════════════════════════════════════════════════════════


async def _assert_ca_or_rm(
    db: AsyncSession, user: User, client_id: str,
) -> None:
    """Role gate on every /client/{cid}/farmer-support/* endpoint.

    Allowed:
      - SA (global email match).
      - CA of this client.
      - CLIENT_RM of this client (the Relationship Manager role, the
        primary audience for Farmer Support).
      - CM with EDIT rights on this client (project convention — CM
        inherits every in-client role).

    Rejected (403 farmer_support_role_required):
      - SUBJECT_EXPERT, FIELD_MANAGER, REPORT_USER, PRODUCT_MANAGER,
        anyone not enrolled on this client. Farmer Support is a
        deliberate scope cut — the doc (Section 2) restricts it to
        RM + CA. SE/FM can hold the RM role too; they just need it
        explicitly assigned.
    """
    if bool(settings.sa_email) and user.email == settings.sa_email:
        return
    role_row = (await db.execute(
        select(ClientUser.role).where(
            ClientUser.client_id == client_id,
            ClientUser.user_id == user.id,
            ClientUser.status == StatusEnum.ACTIVE,
            ClientUser.role.in_([
                ClientUserRole.CA, ClientUserRole.CLIENT_RM,
            ]),
        ).limit(1)
    )).scalar_one_or_none()
    if role_row is not None:
        return
    cm_edit = (await db.execute(
        select(CMClientAssignment.id).where(
            CMClientAssignment.cm_user_id == user.id,
            CMClientAssignment.client_id == client_id,
            CMClientAssignment.status == StatusEnum.ACTIVE,
            CMClientAssignment.rights == CMRights.EDIT,
        ).limit(1)
    )).scalar_one_or_none()
    if cm_edit is not None:
        return
    raise HTTPException(
        status_code=403,
        detail={
            "code": "farmer_support_role_required",
            "message": (
                "Farmer Support is restricted to Customer Admin and "
                "Relationship Manager roles. Ask your CA to assign the "
                "RM role to your account at this client."
            ),
        },
    )


@router.get("/client/{client_id}/farmer-support/farmers")
async def list_farmer_support_farmers(
    client_id: str,
    # Location cascade (user-side fields)
    state_cosh_id: Optional[str] = None,
    district_cosh_id: Optional[str] = None,
    sub_district_cosh_id: Optional[str] = None,
    # Subscription-side filters
    promoter_user_id: Optional[str] = None,
    crop_cosh_id: Optional[str] = None,
    subscription_status: Optional[str] = None,  # ACTIVE | COMPLETED | NOT_STARTED
    start_date_from: Optional[str] = None,       # ISO date
    start_date_to: Optional[str] = None,         # ISO date
    # Subscription origin — self-subscribed vs promoter-assigned
    origin: Optional[str] = None,                # SELF | PROMOTER
    # Search (farmer name OR phone, partial)
    search: Optional[str] = None,
    # Sort + pagination
    sort_by: str = "name",                       # name | start_date | district
    sort_dir: str = "asc",                       # asc | desc
    page: int = 1,
    page_size: int = 25,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Paginated farmer list for Farmer Support (CLIENT_RM + CA).

    One row per farmer (deduped). Each row carries the farmer's
    identity, location, their current / most-recent subscription
    summary (crop, status, start date, promoter name), and a
    pre-computed 'active_subscription_count'. Alert signals are NOT
    computed here — the UI calls the per-type alert endpoints
    separately and surfaces them as tiles / badges.

    Design principle (2026-11-10): keep this endpoint cheap +
    predictable; richer per-farmer data lives behind `GET
    /client/{cid}/farmer-support/farmers/{uid}` (Phase 3). Cap
    page_size at 100 to defend against runaway queries; the UI
    default is 25 per the user's 2026-11-10 call.
    """
    from app.modules.subscriptions.models import (
        PromoterAssignment, Subscription, SubscriptionStatus,
    )
    await _assert_ca_or_rm(db, current_user, client_id)
    if page < 1:
        page = 1
    if page_size < 1:
        page_size = 25
    if page_size > 100:
        page_size = 100
    sort_by = (sort_by or "name").strip().lower()
    sort_dir = (sort_dir or "asc").strip().lower()
    if sort_by not in ("name", "start_date", "district"):
        raise HTTPException(status_code=422, detail="sort_by must be name|start_date|district.")
    if sort_dir not in ("asc", "desc"):
        raise HTTPException(status_code=422, detail="sort_dir must be asc|desc.")

    # Base: every subscription for this client + its farmer. We dedupe
    # by farmer below so a farmer with multiple subs appears once.
    q = (
        select(Subscription, User)
        .join(User, User.id == Subscription.farmer_user_id)
        .where(Subscription.client_id == client_id)
    )
    if state_cosh_id:
        q = q.where(User.state_cosh_id == state_cosh_id)
    if district_cosh_id:
        q = q.where(User.district_cosh_id == district_cosh_id)
    if sub_district_cosh_id:
        q = q.where(User.sub_district_cosh_id == sub_district_cosh_id)
    if crop_cosh_id:
        # crop lives on Package; join lazily to keep the common case
        # (no crop filter) cheap.
        from app.modules.advisory.models import Package
        q = q.join(Package, Package.id == Subscription.package_id).where(
            Package.crop_cosh_id == crop_cosh_id
        )
    if subscription_status:
        status_upper = subscription_status.strip().upper()
        if status_upper == "NOT_STARTED":
            q = q.where(
                Subscription.status == SubscriptionStatus.ACTIVE,
                Subscription.crop_start_date.is_(None),
            )
        elif status_upper in ("ACTIVE", "COMPLETED"):
            q = q.where(Subscription.status == SubscriptionStatus(status_upper))
        else:
            raise HTTPException(
                status_code=422,
                detail="subscription_status must be ACTIVE|COMPLETED|NOT_STARTED.",
            )
    if start_date_from:
        from datetime import date as _date
        try:
            d_from = _date.fromisoformat(start_date_from)
        except ValueError:
            raise HTTPException(status_code=422, detail="start_date_from must be YYYY-MM-DD.")
        q = q.where(Subscription.crop_start_date >= d_from)
    if start_date_to:
        from datetime import date as _date
        try:
            d_to = _date.fromisoformat(start_date_to)
        except ValueError:
            raise HTTPException(status_code=422, detail="start_date_to must be YYYY-MM-DD.")
        q = q.where(Subscription.crop_start_date <= d_to)
    if promoter_user_id:
        # Promoter is stored on PromoterAssignment, not directly on
        # Subscription. Filter via EXISTS so the dedupe still works.
        q = q.where(
            select(PromoterAssignment.id).where(
                PromoterAssignment.subscription_id == Subscription.id,
                PromoterAssignment.promoter_user_id == promoter_user_id,
            ).exists()
        )
    if origin:
        origin_upper = origin.strip().upper()
        has_promoter = select(PromoterAssignment.id).where(
            PromoterAssignment.subscription_id == Subscription.id,
        ).exists()
        if origin_upper == "SELF":
            q = q.where(~has_promoter)
        elif origin_upper == "PROMOTER":
            q = q.where(has_promoter)
        else:
            raise HTTPException(
                status_code=422, detail="origin must be SELF|PROMOTER.",
            )
    if search:
        needle = f"%{search.strip()}%"
        q = q.where(or_(User.name.ilike(needle), User.phone.ilike(needle)))

    rows = (await db.execute(q)).all()

    # Dedupe by farmer_user_id, keeping the most-recent subscription
    # as the representative row (so start_date sort surfaces the
    # latest crop start, matching the design doc's "Latest Start Date"
    # column).
    by_farmer: dict[str, tuple] = {}
    for sub, user in rows:
        existing = by_farmer.get(user.id)
        if existing is None:
            by_farmer[user.id] = (sub, user)
            continue
        prev_sub, _ = existing
        prev_start = prev_sub.crop_start_date
        cur_start = sub.crop_start_date
        # Prefer the subscription with the later crop_start_date; fall
        # back to created_at when start_date is NULL on either side.
        if cur_start and (prev_start is None or cur_start > prev_start):
            by_farmer[user.id] = (sub, user)
        elif cur_start is None and prev_start is None:
            if sub.created_at and prev_sub.created_at and sub.created_at > prev_sub.created_at:
                by_farmer[user.id] = (sub, user)

    # Second pass: active-subscription count per farmer (visible on
    # the list row). Separate lightweight query to avoid inflating the
    # join above with aggregate grouping.
    farmer_ids = list(by_farmer.keys())
    active_counts: dict[str, int] = {}
    if farmer_ids:
        from sqlalchemy import func as _func
        count_rows = (await db.execute(
            select(
                Subscription.farmer_user_id,
                _func.count(Subscription.id),
            )
            .where(
                Subscription.client_id == client_id,
                Subscription.farmer_user_id.in_(farmer_ids),
                Subscription.status == SubscriptionStatus.ACTIVE,
            )
            .group_by(Subscription.farmer_user_id)
        )).all()
        active_counts = {fid: int(cnt) for fid, cnt in count_rows}

    # Promoter name resolution — one lookup for the set of promoter
    # user_ids appearing on the representative subs.
    promoter_name_by_sub: dict[str, str | None] = {}
    sub_ids_for_promoter = [s.id for s, _ in by_farmer.values()]
    if sub_ids_for_promoter:
        pa_rows = (await db.execute(
            select(PromoterAssignment.subscription_id, User.name)
            .join(User, User.id == PromoterAssignment.promoter_user_id)
            .where(PromoterAssignment.subscription_id.in_(sub_ids_for_promoter))
        )).all()
        for sid, pname in pa_rows:
            promoter_name_by_sub.setdefault(sid, pname)

    # 2026-11-11 — resolve state/district names once per page so the
    # UI's filter dropdowns can show readable labels instead of
    # truncated cosh_ids (first live RM report: "the dropdowns show
    # 3b9f56d0… not 'Karnataka'"). Skipping sub_district here since
    # the list UI doesn't render sub-district as a filter option.
    cosh_ids_to_resolve: set[str] = set()
    for sub, user in by_farmer.values():
        if user.state_cosh_id: cosh_ids_to_resolve.add(user.state_cosh_id)
        if user.district_cosh_id: cosh_ids_to_resolve.add(user.district_cosh_id)
    lang = current_user.language_code or "en"
    from app.services.i18n_cosh import resolve_names_by_cosh_id
    name_by_cosh_id = (
        await resolve_names_by_cosh_id(db, cosh_ids_to_resolve, lang)
        if cosh_ids_to_resolve else {}
    )

    # Build row objects
    out_rows: list[dict] = []
    for sub, user in by_farmer.values():
        out_rows.append({
            "user_id": user.id,
            "name": user.name,
            "phone": user.phone,
            "state_cosh_id": user.state_cosh_id,
            "state_name": name_by_cosh_id.get(user.state_cosh_id) if user.state_cosh_id else None,
            "district_cosh_id": user.district_cosh_id,
            "district_name": name_by_cosh_id.get(user.district_cosh_id) if user.district_cosh_id else None,
            "sub_district_cosh_id": user.sub_district_cosh_id,
            "latest_subscription_id": sub.id,
            "latest_subscription_status": (
                sub.status.value if hasattr(sub.status, "value") else sub.status
            ),
            # Normalise datetime → date-only ISO so the CP sort / badge
            # UI doesn't see mixed shapes (2026-11-11 fix — some
            # Subscription rows store this as datetime, others as date).
            "latest_crop_start_date": (
                sub.crop_start_date.date().isoformat()
                if hasattr(sub.crop_start_date, "date")
                else (sub.crop_start_date.isoformat() if sub.crop_start_date else None)
            ),
            "active_subscription_count": active_counts.get(user.id, 0),
            "promoter_name": promoter_name_by_sub.get(sub.id),
        })

    # Sort in Python — dataset size is bounded by the farmer-per-
    # client count, which is small enough that in-memory sort beats
    # pushing another ORDER BY through the dedupe.
    def sort_key(r: dict):
        if sort_by == "name":
            return (r["name"] or "").lower()
        if sort_by == "start_date":
            # NULL start dates land at the end regardless of sort_dir —
            # the SA's intent on sorting by date is usually to see
            # cohorts, and NULL-at-end keeps them from polluting both
            # extremes.
            return r["latest_crop_start_date"] or ""
        if sort_by == "district":
            return (r["district_cosh_id"] or "")
        return (r["name"] or "").lower()
    out_rows.sort(key=sort_key, reverse=(sort_dir == "desc"))

    total = len(out_rows)
    start = (page - 1) * page_size
    end = start + page_size
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "rows": out_rows[start:end],
    }


@router.get("/client/{client_id}/alerts/missed-inputs")
async def get_missed_inputs(
    client_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Farmers whose input practice window has FULLY passed with no
    order and no purchase recorded. Distinct from overdue-inputs
    (where the window is still open today).

    v1 definition: `today > timeline.to_date` for a timeline on an
    ACTIVE subscription, with no order_item on that timeline AND no
    PracticeAcknowledgement.purchased_at for any practice in that
    timeline. Tightening pass in Phase 3 may add per-practice
    granularity; this is the aggregate signal the tile needs.
    """
    from app.modules.farmpundit.router import _assert_portal_member
    from app.modules.subscriptions.models import Subscription, SubscriptionStatus
    from app.modules.orders.models import Order, OrderItem, OrderStatus
    from app.modules.advisory.models import (
        PracticeAcknowledgement, Timeline,
    )
    from datetime import date, timedelta
    await _assert_portal_member(db, current_user.id, client_id)

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
    subs = [s for s, _u in rows]
    users = [u for _s, u in rows]
    lang = current_user.language_code or "en"
    enrich = await _bulk_resolve_enrichment(db, subs, users, lang)

    missed = []
    for sub, user in rows:
        crop_start = (
            sub.crop_start_date.date()
            if hasattr(sub.crop_start_date, "date") else sub.crop_start_date
        )
        tls = (await db.execute(
            select(Timeline).where(Timeline.package_id == sub.package_id)
        )).scalars().all()
        for tl in tls:
            from_type = tl.from_type.value if hasattr(tl.from_type, "value") else str(tl.from_type)
            win_end: date | None = None
            if from_type == "DAS":
                win_end = crop_start + timedelta(days=int(tl.to_value))
            elif from_type == "DBS":
                win_end = crop_start - timedelta(days=int(tl.from_value))
            elif from_type == "CALENDAR":
                if tl.from_value is not None and tl.to_value is not None \
                        and int(tl.from_value) <= int(tl.to_value):
                    year_start = date(crop_start.year, 1, 1)
                    win_end = year_start + timedelta(days=int(tl.to_value) - 1)
            if win_end is None or today <= win_end:
                continue
            # Window has fully passed. Confirm no active order AND no
            # purchase_ack on any practice in this timeline.
            # 2026-11-13 — tightened:
            #   (a) scope to THIS subscription (OrderItem.timeline_id
            #       is shared across farmers when they subscribe to
            #       the same package; without the Order.subscription_id
            #       filter, any farmer's order on the shared timeline
            #       suppressed everyone else's missed-input alert).
            #   (b) filter CANCELLED + EXPIRED Orders so a farmer
            #       whose order was cancelled still surfaces as missed
            #       (they genuinely missed the input).
            #   (c) filter archived OrderItems so Orders V2 lineage
            #       husks (REROUTED items) don't suppress real misses.
            had_order = (await db.execute(
                select(OrderItem.id)
                .join(Order, Order.id == OrderItem.order_id)
                .where(
                    Order.subscription_id == sub.id,
                    Order.status.notin_([OrderStatus.CANCELLED, OrderStatus.EXPIRED]),
                    OrderItem.timeline_id == tl.id,
                    OrderItem.archived_at.is_(None),
                )
                .limit(1)
            )).scalar_one_or_none()
            if had_order:
                continue
            had_purchase = (await db.execute(
                select(PracticeAcknowledgement.id)
                .where(
                    PracticeAcknowledgement.subscription_id == sub.id,
                    PracticeAcknowledgement.timeline_lineage_id == tl.lineage_id,
                    PracticeAcknowledgement.purchased_at.isnot(None),
                )
                .limit(1)
            )).scalar_one_or_none()
            if had_purchase:
                continue
            missed.append({
                **_base_alert_row(sub, user, enrich),
                "timeline_name": tl.name,
                "window_end": win_end.isoformat(),
            })
            break  # One entry per subscription — first missed wins
    return missed


@router.get("/client/{client_id}/alerts/overdue-expert-queries")
async def get_overdue_expert_queries(
    client_id: str,
    sla_hours: int = 48,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Farmers with expert queries past SLA (default 48h) that haven't
    reached a terminal status (RESPONDED, REJECTED, EXPIRED). Tuneable
    via `sla_hours` query param so the SA can sweep for either
    strictly-overdue or nearing-overdue cases."""
    from app.modules.farmpundit.router import _assert_portal_member
    from app.modules.farmpundit.models import Query as FarmerQuery, QueryStatus
    from app.modules.subscriptions.models import Subscription
    from datetime import datetime, timedelta, timezone
    await _assert_portal_member(db, current_user.id, client_id)

    sla_cutoff = datetime.now(timezone.utc) - timedelta(hours=max(1, sla_hours))

    rows = (await db.execute(
        select(FarmerQuery, Subscription, User)
        .join(Subscription, Subscription.id == FarmerQuery.subscription_id)
        .join(User, User.id == FarmerQuery.farmer_user_id)
        .where(
            FarmerQuery.client_id == client_id,
            FarmerQuery.status.notin_([
                QueryStatus.RESPONDED,
                QueryStatus.REJECTED,
                QueryStatus.EXPIRED,
            ]),
            FarmerQuery.created_at <= sla_cutoff,
        )
        .order_by(FarmerQuery.created_at.asc())
    )).all()
    subs = [s for _q, s, _u in rows]
    users = [u for _q, _s, u in rows]
    lang = current_user.language_code or "en"
    enrich = await _bulk_resolve_enrichment(db, subs, users, lang)

    out = []
    for q, sub, user in rows:
        out.append({
            **_base_alert_row(sub, user, enrich),
            "query_id": q.id,
            "query_title": q.title,
            "query_status": (q.status.value if hasattr(q.status, "value") else q.status),
            "query_created_at": q.created_at.isoformat() if q.created_at else None,
            "sla_hours": sla_hours,
        })
    return out


@router.get("/client/{client_id}/alerts/no-active-subscription")
async def get_no_active_subscription(
    client_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Farmers who have EVER subscribed with this client but have no
    ACTIVE subscription right now. Proactive-outreach signal: the
    farmer finished a prior crop, time to invite them back for the
    next season."""
    from app.modules.farmpundit.router import _assert_portal_member
    from app.modules.subscriptions.models import Subscription, SubscriptionStatus
    await _assert_portal_member(db, current_user.id, client_id)

    # All farmer_user_ids who ever subscribed with this client.
    ever_rows = (await db.execute(
        select(Subscription.farmer_user_id)
        .where(Subscription.client_id == client_id)
        .distinct()
    )).scalars().all()
    if not ever_rows:
        return []

    # Those who have at least one ACTIVE subscription right now.
    active_rows = (await db.execute(
        select(Subscription.farmer_user_id)
        .where(
            Subscription.client_id == client_id,
            Subscription.status == SubscriptionStatus.ACTIVE,
        )
        .distinct()
    )).scalars().all()
    active_ids = set(active_rows)
    inactive_ids = [fid for fid in ever_rows if fid not in active_ids]
    if not inactive_ids:
        return []

    # Pull the most-recent (any-status) subscription per farmer for
    # context on the alert row (what was their last crop / when did
    # it finish).
    last_rows = (await db.execute(
        select(Subscription, User)
        .join(User, User.id == Subscription.farmer_user_id)
        .where(
            Subscription.client_id == client_id,
            Subscription.farmer_user_id.in_(inactive_ids),
        )
        .order_by(Subscription.created_at.desc())
    )).all()
    seen: set[str] = set()
    picked: list[tuple] = []
    for sub, user in last_rows:
        if user.id in seen:
            continue
        seen.add(user.id)
        picked.append((sub, user))
    subs = [s for s, _ in picked]
    users = [u for _, u in picked]
    lang = current_user.language_code or "en"
    enrich = await _bulk_resolve_enrichment(db, subs, users, lang)
    return [
        {
            **_base_alert_row(sub, user, enrich),
            "last_subscription_status": (
                sub.status.value if hasattr(sub.status, "value") else sub.status
            ),
            "last_subscription_created_at": (
                sub.created_at.isoformat() if sub.created_at else None
            ),
        }
        for sub, user in picked
    ]


# ═══════════════════════════════════════════════════════════════════════════════
# Farmer Support 360° (Phase 3, 2026-11-10) — per-farmer read endpoints
# for a client's RM to open any farmer and see everything.
# ═══════════════════════════════════════════════════════════════════════════════


async def _assert_farmer_belongs_to_client(
    db: AsyncSession, client_id: str, farmer_user_id: str,
) -> None:
    """Prevent enumeration via guessed user_ids — the RM can only
    open farmers who actually subscribe with this client. Raises 404
    (not 403) so the response is indistinguishable from "user doesn't
    exist at all" — standard anti-enumeration shape."""
    from app.modules.subscriptions.models import Subscription
    row = (await db.execute(
        select(Subscription.id)
        .where(
            Subscription.client_id == client_id,
            Subscription.farmer_user_id == farmer_user_id,
        )
        .limit(1)
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Farmer not found")


def _timeline_window(tl, crop_start: "date") -> tuple["date | None", "date | None"]:
    """Compute (window_start, window_end) for a Timeline against a
    crop_start_date. Mirrors the /overdue-inputs + /missed-inputs
    pattern; returns (None, None) when the window can't be resolved
    (unsupported from_type or wrap-around CALENDAR)."""
    from datetime import date as _date, timedelta
    from_type = tl.from_type.value if hasattr(tl.from_type, "value") else str(tl.from_type)
    if from_type == "DAS":
        return (crop_start + timedelta(days=int(tl.from_value)),
                crop_start + timedelta(days=int(tl.to_value)))
    if from_type == "DBS":
        return (crop_start - timedelta(days=int(tl.to_value)),
                crop_start - timedelta(days=int(tl.from_value)))
    if from_type == "CALENDAR":
        if tl.from_value is not None and tl.to_value is not None \
                and int(tl.from_value) <= int(tl.to_value):
            year_start = _date(crop_start.year, 1, 1)
            return (year_start + timedelta(days=int(tl.from_value) - 1),
                    year_start + timedelta(days=int(tl.to_value) - 1))
    return (None, None)


@router.get("/client/{client_id}/farmer-support/farmers/{farmer_user_id}")
async def get_farmer_support_profile(
    client_id: str,
    farmer_user_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Farmer header for the 360° view — persistent across all tabs.
    Lightweight; name / phone / location / registered crops
    (deduped)."""
    from app.modules.subscriptions.models import Subscription
    from app.modules.advisory.models import Package
    await _assert_ca_or_rm(db, current_user, client_id)
    await _assert_farmer_belongs_to_client(db, client_id, farmer_user_id)
    user = await db.get(User, farmer_user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="Farmer not found")
    # Crops registered by this farmer with this client — one per
    # Package.crop_cosh_id across all their subs.
    crop_rows = (await db.execute(
        select(Package.crop_cosh_id)
        .join(Subscription, Subscription.package_id == Package.id)
        .where(
            Subscription.client_id == client_id,
            Subscription.farmer_user_id == farmer_user_id,
            Package.crop_cosh_id.isnot(None),
        )
        .distinct()
    )).scalars().all()
    lang = current_user.language_code or "en"
    from app.services.i18n_cosh import resolve_names_by_cosh_id
    cosh_ids = {c for c in crop_rows if c}
    if user.state_cosh_id:
        cosh_ids.add(user.state_cosh_id)
    if user.district_cosh_id:
        cosh_ids.add(user.district_cosh_id)
    if user.sub_district_cosh_id:
        cosh_ids.add(user.sub_district_cosh_id)
    names = await resolve_names_by_cosh_id(db, cosh_ids, lang) if cosh_ids else {}
    return {
        "user_id": user.id,
        "name": user.name,
        "phone": user.phone,
        "state_cosh_id": user.state_cosh_id,
        "state_name": names.get(user.state_cosh_id) if user.state_cosh_id else None,
        "district_cosh_id": user.district_cosh_id,
        "district_name": names.get(user.district_cosh_id) if user.district_cosh_id else None,
        "sub_district_cosh_id": user.sub_district_cosh_id,
        "sub_district_name": names.get(user.sub_district_cosh_id) if user.sub_district_cosh_id else None,
        "crops": [
            {"crop_cosh_id": c, "crop_name": names.get(c)}
            for c in crop_rows
        ],
    }


@router.get("/client/{client_id}/farmer-support/farmers/{farmer_user_id}/subscriptions")
async def list_farmer_subscriptions(
    client_id: str,
    farmer_user_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """All subscriptions this farmer has with this client — active
    first, past in reverse-chrono. One row per subscription with the
    display fields for the 360° Subscription Summary tab bar."""
    from app.modules.subscriptions.models import (
        PromoterAssignment, Subscription, SubscriptionStatus,
    )
    from app.modules.advisory.models import Package
    await _assert_ca_or_rm(db, current_user, client_id)
    await _assert_farmer_belongs_to_client(db, client_id, farmer_user_id)

    rows = (await db.execute(
        select(Subscription, Package)
        .join(Package, Package.id == Subscription.package_id)
        .where(
            Subscription.client_id == client_id,
            Subscription.farmer_user_id == farmer_user_id,
        )
    )).all()
    if not rows:
        return []

    # Promoter name + user_id per subscription — one query then map.
    # Keeping user_id too (2026-11-12) so we can resolve Promoter-Pundit
    # status for each sub's assigned promoter below.
    sub_ids = [s.id for s, _ in rows]
    pa_rows = (await db.execute(
        select(
            PromoterAssignment.subscription_id,
            PromoterAssignment.promoter_user_id,
            User.name,
        )
        .join(User, User.id == PromoterAssignment.promoter_user_id)
        .where(PromoterAssignment.subscription_id.in_(sub_ids))
    )).all()
    promoter_name_by_sub: dict[str, str | None] = {}
    promoter_user_id_by_sub: dict[str, str | None] = {}
    for sid, puid, pname in pa_rows:
        promoter_name_by_sub.setdefault(sid, pname)
        promoter_user_id_by_sub.setdefault(sid, puid)

    # Crop name resolution.
    lang = current_user.language_code or "en"
    from app.services.i18n_cosh import resolve_names_by_cosh_id
    crop_ids = {pkg.crop_cosh_id for _, pkg in rows if pkg.crop_cosh_id}
    name_by_id = await resolve_names_by_cosh_id(db, crop_ids, lang) if crop_ids else {}

    # 2026-11-12 — per-sub activity counts for the tab badges + alert-
    # receiver / promoter-pundit resolution for the "Who follows up"
    # strip. All lookups are batched (one query each for N subs).
    from app.modules.diagnosis.models import DiagnosisSession
    from app.modules.farmpundit.models import Query as FarmerQuery
    from sqlalchemy import func as _func

    # Count of DIAGNOSED + OUTSIDE_LIST diagnosis sessions per sub.
    # Mirrors the filter on get_subscription_diagnoses so badge count
    # matches what the RM will see in the detail panel.
    diag_count_rows = (await db.execute(
        select(DiagnosisSession.subscription_id, _func.count(DiagnosisSession.id))
        .where(
            DiagnosisSession.subscription_id.in_(sub_ids),
            DiagnosisSession.status.in_(["DIAGNOSED", "OUTSIDE_LIST"]),
        )
        .group_by(DiagnosisSession.subscription_id)
    )).all()
    diag_count_by_sub = {sid: cnt for sid, cnt in diag_count_rows}

    # Count of UNANSWERED queries per sub (NEW/FORWARDED/RETURNED).
    # Terminal statuses (RESPONDED/REJECTED/EXPIRED) are excluded —
    # the badge's purpose is to flag queues that need follow-up.
    query_count_rows = (await db.execute(
        select(FarmerQuery.subscription_id, _func.count(FarmerQuery.id))
        .where(
            FarmerQuery.subscription_id.in_(sub_ids),
            FarmerQuery.status.in_(["NEW", "FORWARDED", "RETURNED"]),
        )
        .group_by(FarmerQuery.subscription_id)
    )).all()
    query_count_by_sub = {sid: cnt for sid, cnt in query_count_rows}

    # Alert receiver = Subscription.extra_alert_user_id → User (the
    # FK that drives alert delivery, per the model comment). Falls
    # back to the subscription's primary promoter when no extra_alert
    # recipient is set — that's who gets alerts in the default case.
    alert_user_ids: set[str] = set()
    for sub, _pkg in rows:
        if sub.extra_alert_user_id:
            alert_user_ids.add(sub.extra_alert_user_id)
        elif sub.promoter_user_id:
            alert_user_ids.add(sub.promoter_user_id)
        else:
            # Fall back to PromoterAssignment-resolved promoter.
            pa_puid = promoter_user_id_by_sub.get(sub.id)
            if pa_puid:
                alert_user_ids.add(pa_puid)
    alert_users_by_id: dict[str, User] = {}
    if alert_user_ids:
        au_rows = (await db.execute(
            select(User).where(User.id.in_(alert_user_ids))
        )).scalars().all()
        alert_users_by_id = {u.id: u for u in au_rows}

    # Promoter-Pundit resolution: for each sub's promoter_user_id,
    # check the ClientPromoter row at this client. If
    # is_promoter_pundit=True AND status=ACTIVE, they're the PP.
    candidate_pp_ids = {
        promoter_user_id_by_sub.get(sub.id) or sub.promoter_user_id
        for sub, _pkg in rows
    }
    candidate_pp_ids.discard(None)
    pp_user_ids_set: set[str] = set()
    if candidate_pp_ids:
        pp_rows = (await db.execute(
            select(ClientPromoter.user_id).where(
                ClientPromoter.client_id == client_id,
                ClientPromoter.user_id.in_(candidate_pp_ids),
                ClientPromoter.status == StatusEnum.ACTIVE,
                ClientPromoter.is_promoter_pundit.is_(True),
            )
        )).scalars().all()
        pp_user_ids_set = set(pp_rows)
    pp_users_by_id: dict[str, User] = {}
    if pp_user_ids_set:
        pp_user_rows = (await db.execute(
            select(User).where(User.id.in_(pp_user_ids_set))
        )).scalars().all()
        pp_users_by_id = {u.id: u for u in pp_user_rows}

    # 2026-11-11 — Subscription.crop_start_date is stored as a
    # datetime on some rows, date on others (legacy inconsistency).
    # Normalise to date-only ISO at serialisation time so the API
    # shape is predictable and the sort key below never has to parse
    # a full datetime string. Earlier crash (`ValueError: invalid
    # literal for int() with base 10: '20260928T00:00:00+00:00'`) was
    # exactly this.
    def _date_iso(v) -> str | None:
        if v is None:
            return None
        if hasattr(v, "date"):
            return v.date().isoformat()
        return v.isoformat()

    out = []
    for sub, pkg in rows:
        # Resolve alert receiver for this sub (extra_alert_user_id →
        # promoter_user_id → PromoterAssignment-resolved, same order
        # as the alert_user_ids collection above).
        alert_uid = (
            sub.extra_alert_user_id
            or sub.promoter_user_id
            or promoter_user_id_by_sub.get(sub.id)
        )
        alert_receiver = None
        if alert_uid and alert_uid in alert_users_by_id:
            au = alert_users_by_id[alert_uid]
            alert_receiver = {
                "user_id": au.id,
                "name": au.name,
                "phone": au.phone,
                "gps_lat": float(au.gps_lat) if au.gps_lat is not None else None,
                "gps_lng": float(au.gps_lng) if au.gps_lng is not None else None,
            }

        # Resolve Promoter-Pundit (if the sub's promoter holds that
        # role at this client).
        pp = None
        pp_candidate = promoter_user_id_by_sub.get(sub.id) or sub.promoter_user_id
        if pp_candidate and pp_candidate in pp_users_by_id:
            ppu = pp_users_by_id[pp_candidate]
            pp = {
                "user_id": ppu.id,
                "name": ppu.name,
                "phone": ppu.phone,
            }

        out.append({
            "subscription_id": sub.id,
            "status": sub.status.value if hasattr(sub.status, "value") else sub.status,
            "is_active": sub.status == SubscriptionStatus.ACTIVE,
            "package_id": pkg.id,
            "package_name": pkg.name,
            "crop_cosh_id": pkg.crop_cosh_id,
            "crop_name": name_by_id.get(pkg.crop_cosh_id) if pkg.crop_cosh_id else None,
            "crop_start_date": _date_iso(sub.crop_start_date),
            "created_at": sub.created_at.isoformat() if sub.created_at else None,
            "promoter_name": promoter_name_by_sub.get(sub.id),
            "farm_area_acres": (
                float(sub.farm_area_acres) if sub.farm_area_acres else None
            ),
            "number_of_plants": (
                int(sub.number_of_plants) if sub.number_of_plants else None
            ),
            # 2026-11-12 — tab-badge counts + follow-up contacts.
            "diagnoses_count": int(diag_count_by_sub.get(sub.id, 0)),
            "unanswered_queries_count": int(query_count_by_sub.get(sub.id, 0)),
            "alert_receiver": alert_receiver,
            "promoter_pundit": pp,
        })
    # Active first, then by crop_start_date desc (nulls last), then
    # created_at desc as tie-breaker. crop_start_date is always
    # YYYY-MM-DD after normalisation; created_at is full ISO so split
    # off the date portion before int-parsing.
    out.sort(key=lambda r: (
        0 if r["is_active"] else 1,
        -(int(r["crop_start_date"].replace("-", "")) if r["crop_start_date"] else 0),
        -(int(r["created_at"].replace("-", "").split("T")[0]) if r["created_at"] else 0),
    ))
    return out


@router.get("/client/{client_id}/farmer-support/farmers/{farmer_user_id}/subscriptions/{subscription_id}/practices")
async def get_subscription_practice_timeline(
    client_id: str,
    farmer_user_id: str,
    subscription_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Practice timeline for one subscription — past (with status),
    current, upcoming. Read-only; the RM never acts on these.

    Status derivation per practice:
      - 'Completed' if PracticeAcknowledgement.marked_at is set
      - 'Skipped'   if PracticeAcknowledgement.hidden_at is set
      - 'Missed'    if window has passed and no ack
      - 'Current'   if today is within the window
      - 'Upcoming'  otherwise (window in future)

    For Phase 3 v1 we return a flat list sorted by timeline window
    start date; the UI buckets them into past/current/upcoming.
    """
    from app.modules.subscriptions.models import Subscription
    from app.modules.advisory.models import (
        PracticeAcknowledgement, Timeline, Practice,
    )
    from datetime import date
    await _assert_ca_or_rm(db, current_user, client_id)
    await _assert_farmer_belongs_to_client(db, client_id, farmer_user_id)
    sub = (await db.execute(
        select(Subscription).where(
            Subscription.id == subscription_id,
            Subscription.client_id == client_id,
            Subscription.farmer_user_id == farmer_user_id,
        )
    )).scalar_one_or_none()
    if sub is None:
        raise HTTPException(status_code=404, detail="Subscription not found")
    today = date.today()
    crop_start = None
    if sub.crop_start_date:
        crop_start = (
            sub.crop_start_date.date()
            if hasattr(sub.crop_start_date, "date") else sub.crop_start_date
        )

    tls = (await db.execute(
        select(Timeline).where(Timeline.package_id == sub.package_id)
    )).scalars().all()
    tl_ids = [tl.id for tl in tls]
    practices = (await db.execute(
        select(Practice).where(Practice.timeline_id.in_(tl_ids))
    )).scalars().all() if tl_ids else []
    acks = (await db.execute(
        select(PracticeAcknowledgement).where(
            PracticeAcknowledgement.subscription_id == subscription_id,
        )
    )).scalars().all()
    ack_by_key: dict[tuple[str, str], object] = {}
    for a in acks:
        ack_by_key[(a.practice_id, a.timeline_lineage_id)] = a

    # 2026-11-11 — Practice has no `name` column; the user-facing
    # practice name is derived from l2_type via the Cosh-translated
    # map in subscriptions/router.py. Reuse that helper so the name
    # matches what farmers see in the PWA advisory view.
    from app.modules.subscriptions.router import _l2_name_loc_map
    lang = current_user.language_code or "en"
    l2_name_loc = await _l2_name_loc_map(db, lang)

    tl_by_id = {tl.id: tl for tl in tls}
    out = []
    for p in practices:
        tl = tl_by_id.get(p.timeline_id)
        if tl is None or crop_start is None:
            continue
        win_start, win_end = _timeline_window(tl, crop_start)
        ack = ack_by_key.get((p.id, tl.lineage_id))
        if ack and ack.marked_at:
            status = "COMPLETED"
        elif ack and ack.hidden_at:
            status = "SKIPPED"
        elif win_end is not None and today > win_end:
            status = "MISSED"
        elif win_start is not None and win_end is not None and win_start <= today <= win_end:
            status = "CURRENT"
        else:
            status = "UPCOMING"
        out.append({
            "practice_id": p.id,
            # Localised L2 name, falling back to the enum value so the
            # UI never shows a blank name even if a Cosh mapping is
            # missing for a legacy practice type.
            "practice_name": l2_name_loc.get(p.l2_type) or p.l2_type,
            "l0_type": (p.l0_type.value if hasattr(p.l0_type, "value") else p.l0_type),
            "l1_type": p.l1_type,
            "l2_type": p.l2_type,
            "timeline_id": tl.id,
            "timeline_name": tl.name,
            "window_start": win_start.isoformat() if win_start else None,
            "window_end": win_end.isoformat() if win_end else None,
            "status": status,
            "marked_at": ack.marked_at.isoformat() if ack and ack.marked_at else None,
            "hidden_at": ack.hidden_at.isoformat() if ack and ack.hidden_at else None,
        })
    # Sort by window_start asc so past → current → upcoming reads
    # naturally. Timelines without a resolved window land first (deal
    # with the garbage).
    out.sort(key=lambda r: (r["window_start"] or ""))
    return out


@router.get("/client/{client_id}/farmer-support/farmers/{farmer_user_id}/subscriptions/{subscription_id}/diagnoses")
async def get_subscription_diagnoses(
    client_id: str,
    farmer_user_id: str,
    subscription_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Pest diagnoses the farmer raised under this subscription.

    2026-11-11 — widened the status filter: now returns both
    DIAGNOSED (farmer matched a Cosh problem — recommendations
    flowed) AND OUTSIDE_LIST (farmer completed the flow but the
    problem wasn't in the catalog — farmer was pointed at the
    expert path). The OUTSIDE_LIST signal is the RM's clearest
    "farmer has a problem we couldn't help with" cue. Still
    hiding ACTIVE (in-flight, incomplete) and ABORTED (user
    bailed mid-flow) — those are noise for the RM's follow-up.
    """
    from app.modules.subscriptions.models import Subscription
    from app.modules.diagnosis.models import DiagnosisSession
    await _assert_ca_or_rm(db, current_user, client_id)
    await _assert_farmer_belongs_to_client(db, client_id, farmer_user_id)
    sub = (await db.execute(
        select(Subscription.id).where(
            Subscription.id == subscription_id,
            Subscription.client_id == client_id,
            Subscription.farmer_user_id == farmer_user_id,
        )
    )).scalar_one_or_none()
    if sub is None:
        raise HTTPException(status_code=404, detail="Subscription not found")

    rows = (await db.execute(
        select(DiagnosisSession)
        .where(
            DiagnosisSession.subscription_id == subscription_id,
            DiagnosisSession.status.in_(["DIAGNOSED", "OUTSIDE_LIST"]),
        )
        .order_by(DiagnosisSession.created_at.desc())
    )).scalars().all()
    lang = current_user.language_code or "en"
    from app.services.i18n_cosh import resolve_names_by_cosh_id
    cosh_ids = set()
    for r in rows:
        if getattr(r, "diagnosed_problem_cosh_id", None):
            cosh_ids.add(r.diagnosed_problem_cosh_id)
        if getattr(r, "initial_plant_part_cosh_id", None):
            cosh_ids.add(r.initial_plant_part_cosh_id)
    names = await resolve_names_by_cosh_id(db, cosh_ids, lang) if cosh_ids else {}
    return [
        {
            "session_id": r.id,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "diagnosed_problem_cosh_id": getattr(r, "diagnosed_problem_cosh_id", None),
            "diagnosed_problem_name": names.get(getattr(r, "diagnosed_problem_cosh_id", None)),
            "plant_part_cosh_id": getattr(r, "initial_plant_part_cosh_id", None),
            "plant_part_name": names.get(getattr(r, "initial_plant_part_cosh_id", None)),
            "status": r.status,
        }
        for r in rows
    ]


@router.get("/client/{client_id}/farmer-support/farmers/{farmer_user_id}/subscriptions/{subscription_id}/queries")
async def get_subscription_queries(
    client_id: str,
    farmer_user_id: str,
    subscription_id: str,
    sla_hours: int = 48,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Expert queries the farmer raised under this subscription.
    Each row carries the derived ui_status (Answered / Pending /
    Overdue) so the front-end doesn't have to re-apply the SLA logic."""
    from app.modules.subscriptions.models import Subscription
    from app.modules.farmpundit.models import (
        FarmPunditProfile, Query as FarmerQuery, QueryResponse, QueryStatus,
    )
    from datetime import datetime, timedelta, timezone
    await _assert_ca_or_rm(db, current_user, client_id)
    await _assert_farmer_belongs_to_client(db, client_id, farmer_user_id)
    sub = (await db.execute(
        select(Subscription.id).where(
            Subscription.id == subscription_id,
            Subscription.client_id == client_id,
            Subscription.farmer_user_id == farmer_user_id,
        )
    )).scalar_one_or_none()
    if sub is None:
        raise HTTPException(status_code=404, detail="Subscription not found")

    sla_cutoff = datetime.now(timezone.utc) - timedelta(hours=max(1, sla_hours))
    rows = (await db.execute(
        select(FarmerQuery)
        .where(FarmerQuery.subscription_id == subscription_id)
        .order_by(FarmerQuery.created_at.desc())
    )).scalars().all()

    # Pre-fetch responses + pundit names in bulk.
    query_ids = [q.id for q in rows]
    response_by_q: dict[str, QueryResponse] = {}
    pundit_name_by_profile: dict[str, str | None] = {}
    if query_ids:
        resp_rows = (await db.execute(
            select(QueryResponse).where(QueryResponse.query_id.in_(query_ids))
        )).scalars().all()
        for r in resp_rows:
            response_by_q[r.query_id] = r
        profile_ids = {q.current_holder_id for q in rows if q.current_holder_id}
        if profile_ids:
            pundit_rows = (await db.execute(
                select(FarmPunditProfile.id, User.name)
                .join(User, User.id == FarmPunditProfile.user_id)
                .where(FarmPunditProfile.id.in_(profile_ids))
            )).all()
            pundit_name_by_profile = {pid: nm for pid, nm in pundit_rows}

    def derive_ui_status(q: FarmerQuery) -> str:
        if q.status == QueryStatus.RESPONDED:
            return "ANSWERED"
        if q.status in (QueryStatus.REJECTED, QueryStatus.EXPIRED):
            return "CLOSED"
        # NEW / FORWARDED / RETURNED — pending or overdue
        if q.created_at and q.created_at <= sla_cutoff:
            return "OVERDUE"
        return "PENDING"

    out = []
    for q in rows:
        resp = response_by_q.get(q.id)
        out.append({
            "query_id": q.id,
            "title": q.title,
            "severity": q.severity,
            "status": q.status.value if hasattr(q.status, "value") else q.status,
            "ui_status": derive_ui_status(q),
            "created_at": q.created_at.isoformat() if q.created_at else None,
            "assigned_to": pundit_name_by_profile.get(q.current_holder_id) if q.current_holder_id else None,
            "response_text": resp.text if resp else None,
            "response_at": resp.created_at.isoformat() if resp and resp.created_at else None,
            "sla_hours": sla_hours,
        })
    return out


@router.get("/client/{client_id}/farmer-support/farmers/{farmer_user_id}/subscriptions/{subscription_id}/inputs")
async def get_subscription_inputs(
    client_id: str,
    farmer_user_id: str,
    subscription_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Input tracking for one subscription. Each input-type practice
    (l0_type = 'INPUT') gets a row with one of six statuses:

      PURCHASED    — any of:
                     (a) PracticeAcknowledgement.purchased_at is set
                         (farmer tapped "I've purchased this" in PWA)
                     (b) an OrderItem on this timeline reached
                         status=APPROVED with final_confirmed_at set
                         (farmer accepted dealer's final offer in-app)
                     Surfaces `picked_up_at` when the corresponding
                     PackingList has `farmer_received_at` set
                     (physical pickup confirmed).
      ORDERED      — an uncancelled OrderItem exists for this timeline
                     but hasn't reached APPROVED+final_confirmed yet
      DUE_NOW      — today within window, no order, no purchase
      UPCOMING     — today before window, starts within 7 days
      MISSED       — today past window end, no order, no purchase
      NOT_REQUIRED — PracticeAcknowledgement.hidden_at is set

    Response groups 'due_now' separately so the UI can render its
    highlighted priority panel without re-filtering.

    2026-11-13 fix: previously `has_order → ORDERED` was terminal in
    the no-ack path, so an item stayed ORDERED even after the farmer
    picked it up via the in-app order flow (no code writes
    PracticeAcknowledgement.purchased_at as a side effect of order
    completion). Now any APPROVED+final-confirmed OrderItem flips the
    row to PURCHASED, and `picked_up_at` carries the actual pickup
    date when a PackingList farmer_received_at exists.
    """
    from app.modules.subscriptions.models import Subscription
    from app.modules.advisory.models import (
        PracticeAcknowledgement, Timeline, Practice,
    )
    from app.modules.orders.models import (
        Order, OrderItem, OrderStatus, OrderItemStatus, PackingList,
    )
    from datetime import date, timedelta
    from sqlalchemy import func as _func
    await _assert_ca_or_rm(db, current_user, client_id)
    await _assert_farmer_belongs_to_client(db, client_id, farmer_user_id)
    sub = (await db.execute(
        select(Subscription).where(
            Subscription.id == subscription_id,
            Subscription.client_id == client_id,
            Subscription.farmer_user_id == farmer_user_id,
        )
    )).scalar_one_or_none()
    if sub is None:
        raise HTTPException(status_code=404, detail="Subscription not found")
    today = date.today()
    crop_start = None
    if sub.crop_start_date:
        crop_start = (
            sub.crop_start_date.date()
            if hasattr(sub.crop_start_date, "date") else sub.crop_start_date
        )

    # Pull only INPUT practices.
    tls = (await db.execute(
        select(Timeline).where(Timeline.package_id == sub.package_id)
    )).scalars().all()
    tl_ids = [tl.id for tl in tls]
    practices = (await db.execute(
        select(Practice).where(
            Practice.timeline_id.in_(tl_ids),
            Practice.l0_type == "INPUT",
        )
    )).scalars().all() if tl_ids else []
    if not practices:
        return {"due_now": [], "all": []}

    acks = (await db.execute(
        select(PracticeAcknowledgement).where(
            PracticeAcknowledgement.subscription_id == subscription_id,
        )
    )).scalars().all()
    ack_by_key: dict[tuple[str, str], object] = {}
    for a in acks:
        ack_by_key[(a.practice_id, a.timeline_lineage_id)] = a

    # Pull all non-cancelled OrderItems once; derive the three
    # timeline-level booleans we need (active / approved / picked-up)
    # + a best `picked_up_at` per timeline. Batched so the per-practice
    # loop below stays O(n) in practice count, not per-row round-trips.
    oi_rows = (await db.execute(
        select(
            OrderItem.timeline_id,
            OrderItem.status,
            OrderItem.final_confirmed_at,
            OrderItem.order_id,
        )
        .join(Order, Order.id == OrderItem.order_id)
        .where(
            Order.subscription_id == subscription_id,
            Order.status.notin_([OrderStatus.CANCELLED, OrderStatus.EXPIRED]),
            OrderItem.archived_at.is_(None),
        )
    )).all()
    active_order_tls: set[str] = set()
    approved_tls: set[str] = set()
    order_ids_for_approved: set[str] = set()
    for tl_id, item_status, final_at, order_id in oi_rows:
        active_order_tls.add(tl_id)
        if item_status == OrderItemStatus.APPROVED and final_at is not None:
            approved_tls.add(tl_id)
            order_ids_for_approved.add(order_id)

    # For each approved-containing order, map order_id → earliest
    # packing-list `farmer_received_at` (the authoritative "farmer has
    # physically received the goods" marker). Multiple PL rows per
    # order are possible (per-batch pickup); the earliest "farmer has
    # it in hand" wins for the per-timeline pickup date.
    pickup_by_order: dict[str, object] = {}
    if order_ids_for_approved:
        pl_rows = (await db.execute(
            select(PackingList.order_id, PackingList.farmer_received_at)
            .where(
                PackingList.order_id.in_(order_ids_for_approved),
                PackingList.farmer_received_at.isnot(None),
            )
        )).all()
        for oid, rec_at in pl_rows:
            existing = pickup_by_order.get(oid)
            if existing is None or (rec_at and rec_at < existing):
                pickup_by_order[oid] = rec_at
    # Collapse back to per-timeline: earliest pickup across any
    # approved OrderItem on that timeline wins.
    picked_up_by_tl: dict[str, object] = {}
    for tl_id, item_status, _final, order_id in oi_rows:
        if item_status != OrderItemStatus.APPROVED:
            continue
        rec_at = pickup_by_order.get(order_id)
        if rec_at is None:
            continue
        existing = picked_up_by_tl.get(tl_id)
        if existing is None or rec_at < existing:
            picked_up_by_tl[tl_id] = rec_at

    # 2026-11-11 — same Cosh-resolution pattern as the practices
    # endpoint; Practice has no `name` column, use l2_type via
    # _l2_name_loc_map.
    from app.modules.subscriptions.router import _l2_name_loc_map
    lang = current_user.language_code or "en"
    l2_name_loc = await _l2_name_loc_map(db, lang)

    tl_by_id = {tl.id: tl for tl in tls}
    rows: list[dict] = []
    for p in practices:
        tl = tl_by_id.get(p.timeline_id)
        if tl is None:
            continue
        win_start, win_end = (None, None) if crop_start is None \
            else _timeline_window(tl, crop_start)
        ack = ack_by_key.get((p.id, tl.lineage_id))
        has_order = tl.id in active_order_tls
        has_approved = tl.id in approved_tls
        picked_up_at = picked_up_by_tl.get(tl.id)

        # purchased_via tells the UI which signal fired so it can
        # label accordingly: "ack" (farmer tapped buy) /
        # "order_pickup" (full in-app pickup) / "order_approved"
        # (farmer accepted final offer but PackingList pickup not yet
        # recorded — still counts as purchased for RM purposes).
        purchased_via: str | None = None
        if ack and ack.purchased_at:
            status = "PURCHASED"
            purchased_via = "ack"
        elif has_approved:
            status = "PURCHASED"
            purchased_via = "order_pickup" if picked_up_at else "order_approved"
        elif ack and ack.hidden_at:
            status = "NOT_REQUIRED"
        elif has_order:
            status = "ORDERED"
        elif win_start is not None and win_end is not None:
            if today > win_end:
                status = "MISSED"
            elif win_start <= today <= win_end:
                status = "DUE_NOW"
            elif today < win_start and (win_start - today).days <= 7:
                status = "UPCOMING"
            else:
                status = "UPCOMING"  # further-future; UI may hide
        else:
            status = "UPCOMING"  # unresolved window

        rows.append({
            "practice_id": p.id,
            "practice_name": l2_name_loc.get(p.l2_type) or p.l2_type,
            "l1_type": p.l1_type,
            "l2_type": p.l2_type,
            "timeline_id": tl.id,
            "timeline_name": tl.name,
            "window_start": win_start.isoformat() if win_start else None,
            "window_end": win_end.isoformat() if win_end else None,
            "status": status,
            "purchased_via": purchased_via,
            "purchased_at": ack.purchased_at.isoformat() if ack and ack.purchased_at else None,
            "picked_up_at": picked_up_at.isoformat() if picked_up_at else None,
            "purchased_brand_text": (
                ack.purchased_brand_text if ack and getattr(ack, "purchased_brand_text", None) else None
            ),
            "purchased_brand_cosh_id": (
                ack.purchased_brand_cosh_id if ack and getattr(ack, "purchased_brand_cosh_id", None) else None
            ),
        })
    # Sort by window_start so the input list mirrors the practice
    # timeline ordering.
    rows.sort(key=lambda r: (r["window_start"] or ""))
    due_now = [r for r in rows if r["status"] == "DUE_NOW"]
    return {"due_now": due_now, "all": rows}


@router.get(
    "/client/{client_id}/farmer-support/farmers/{farmer_user_id}"
    "/subscriptions/{subscription_id}/nearby-facilitators"
)
async def get_subscription_nearby_facilitators(
    client_id: str,
    farmer_user_id: str,
    subscription_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """RM-facing variant of the PWA's nearby-facilitators endpoint.

    Returns the farmer's own GPS pin + every ONBOARDED facilitator at
    this subscription's effective client (training subs resolve to the
    parent client), each with distance from the farmer and a `role`
    tag so the CP map can colour-code pins (ALERT_RECEIVER, PROMOTER,
    PROMOTER_PUNDIT, FACILITATOR). All data is read-only — the RM
    uses this to see whether the active alert receiver is actually the
    closest/right person, not to re-assign anyone.
    """
    from app.modules.subscriptions.models import (
        Subscription, PromoterAssignment, AssignmentStatus,
    )
    from app.modules.subscriptions.router import _haversine_sub
    from app.services.training import resolve_package_client_id

    await _assert_ca_or_rm(db, current_user, client_id)
    await _assert_farmer_belongs_to_client(db, client_id, farmer_user_id)
    sub = (await db.execute(
        select(Subscription).where(
            Subscription.id == subscription_id,
            Subscription.client_id == client_id,
            Subscription.farmer_user_id == farmer_user_id,
        )
    )).scalar_one_or_none()
    if sub is None:
        raise HTTPException(status_code=404, detail="Subscription not found")

    farmer = (await db.execute(
        select(User).where(User.id == farmer_user_id)
    )).scalar_one_or_none()
    if farmer is None or farmer.gps_lat is None or farmer.gps_lng is None:
        # Map can't anchor without a farmer coord — surface the gap
        # so the CP can render an explanatory empty state instead of
        # a confusing world-centred map.
        return {
            "farmer": {
                "user_id": farmer_user_id,
                "name": farmer.name if farmer else None,
                "phone": farmer.phone if farmer else None,
                "gps_lat": None,
                "gps_lng": None,
            },
            "facilitators": [],
            "reason": "farmer_gps_missing" if farmer else "farmer_not_found",
        }

    farmer_lat = float(farmer.gps_lat)
    farmer_lng = float(farmer.gps_lng)

    # Resolve who the alert_receiver is for this sub so the role tag
    # lines up with the "Who follows up" strip (same fallback chain
    # as list_farmer_subscriptions).
    pa_row = (await db.execute(
        select(PromoterAssignment.promoter_user_id).where(
            PromoterAssignment.subscription_id == subscription_id,
            PromoterAssignment.status == AssignmentStatus.ACTIVE,
        ).order_by(PromoterAssignment.assigned_at.desc()).limit(1)
    )).scalar_one_or_none()
    alert_user_id = (
        sub.extra_alert_user_id or sub.promoter_user_id or pa_row
    )
    promoter_user_id = sub.promoter_user_id or pa_row

    # Facilitator pool = ACTIVE FACILITATOR ClientPromoter rows at
    # the effective (training-aware) client.
    effective_client_id = await resolve_package_client_id(db, sub.client_id)
    cp_rows = (await db.execute(
        select(ClientPromoter.user_id, ClientPromoter.is_promoter_pundit).where(
            ClientPromoter.client_id == effective_client_id,
            ClientPromoter.promoter_type == "FACILITATOR",
            ClientPromoter.status == StatusEnum.ACTIVE,
        )
    )).all()
    pundit_flag_by_uid = {uid: bool(pp) for uid, pp in cp_rows}
    fac_uids = set(pundit_flag_by_uid.keys())

    # Always include the alert_receiver + promoter even if they
    # weren't onboarded as FACILITATOR (e.g. a DEALER-pinned
    # promoter) — the RM needs to see them on the map regardless.
    for extra in (alert_user_id, promoter_user_id):
        if extra:
            fac_uids.add(extra)

    if not fac_uids:
        return {
            "farmer": {
                "user_id": farmer.id,
                "name": farmer.name,
                "phone": farmer.phone,
                "gps_lat": farmer_lat,
                "gps_lng": farmer_lng,
            },
            "facilitators": [],
            "reason": None,
        }

    fac_users = (await db.execute(
        select(User).where(User.id.in_(fac_uids))
    )).scalars().all()

    out = []
    for u in fac_users:
        gps_ok = u.gps_lat is not None and u.gps_lng is not None
        dist = (
            round(_haversine_sub(
                farmer_lat, farmer_lng,
                float(u.gps_lat), float(u.gps_lng),
            ), 1)
            if gps_ok else None
        )
        # Role precedence: a single pin carries the highest-signal
        # label (alert-receiver > promoter-pundit > promoter > plain).
        if u.id == alert_user_id:
            role = "ALERT_RECEIVER"
        elif pundit_flag_by_uid.get(u.id):
            role = "PROMOTER_PUNDIT"
        elif u.id == promoter_user_id:
            role = "PROMOTER"
        else:
            role = "FACILITATOR"
        out.append({
            "user_id": u.id,
            "name": u.name,
            "phone": u.phone,
            "gps_lat": float(u.gps_lat) if gps_ok else None,
            "gps_lng": float(u.gps_lng) if gps_ok else None,
            "distance_km": dist,
            "role": role,
            "is_alert_receiver": u.id == alert_user_id,
            "is_promoter": u.id == promoter_user_id,
            "is_promoter_pundit": pundit_flag_by_uid.get(u.id, False),
        })

    # Sort: alert receiver first (so the RM's primary follow-up
    # contact is always visible even on a crowded map), then by
    # distance; GPS-less facilitators sink to the bottom.
    out.sort(key=lambda r: (
        0 if r["is_alert_receiver"] else 1,
        r["distance_km"] if r["distance_km"] is not None else 1e9,
    ))

    return {
        "farmer": {
            "user_id": farmer.id,
            "name": farmer.name,
            "phone": farmer.phone,
            "gps_lat": farmer_lat,
            "gps_lng": farmer_lng,
        },
        "facilitators": out,
        "reason": None,
    }


# ── Call Log (Phase 4, 2026-11-10) ────────────────────────────────────────────


async def _assert_rm_for_call_logging(
    db: AsyncSession, user: User, client_id: str,
) -> None:
    """Narrow gate for POST /call-log. CA can VIEW the log but cannot
    LOG — per spec §5.5 ("CA can view the call log — read only"). This
    reflects RootsTalk's single-role-at-a-client model: logging a call
    is an RM action; a CA who also handles support should hold the RM
    role on a separate login per user direction 2026-11-10.

    Allowed:
      - SA (admin / testing).
      - CLIENT_RM at this client.
      - CM-EDIT at this client (CM inherits every in-client role per
        project convention).

    Rejected (403 call_log_rm_role_required):
      - CA, SUBJECT_EXPERT, FIELD_MANAGER, REPORT_USER, PRODUCT_MANAGER,
        anyone not enrolled. The CA case is called out in the error
        message because that's the most surprising reject.
    """
    if bool(settings.sa_email) and user.email == settings.sa_email:
        return
    role_row = (await db.execute(
        select(ClientUser.role).where(
            ClientUser.client_id == client_id,
            ClientUser.user_id == user.id,
            ClientUser.status == StatusEnum.ACTIVE,
            ClientUser.role == ClientUserRole.CLIENT_RM,
        ).limit(1)
    )).scalar_one_or_none()
    if role_row is not None:
        return
    cm_edit = (await db.execute(
        select(CMClientAssignment.id).where(
            CMClientAssignment.cm_user_id == user.id,
            CMClientAssignment.client_id == client_id,
            CMClientAssignment.status == StatusEnum.ACTIVE,
            CMClientAssignment.rights == CMRights.EDIT,
        ).limit(1)
    )).scalar_one_or_none()
    if cm_edit is not None:
        return
    raise HTTPException(
        status_code=403,
        detail={
            "code": "call_log_rm_role_required",
            "message": (
                "Only the Relationship Manager role can log calls. "
                "CA roles can view the log but not add entries — ask "
                "an RM to log on your behalf, or hold the RM role on "
                "a separate account."
            ),
        },
    )


class CallLogCreate(BaseModel):
    call_type: str                     # OUTBOUND | INBOUND
    call_at: Optional[str] = None      # ISO datetime; defaults to now()


@router.get("/client/{client_id}/farmer-support/farmers/{farmer_user_id}/call-log")
async def list_farmer_call_log(
    client_id: str,
    farmer_user_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Chronological list of calls (most recent first) for one farmer.
    Returns every entry with the logging RM's name resolved so the
    panel renders standalone."""
    from app.modules.clients.models import FarmerCallLog
    await _assert_ca_or_rm(db, current_user, client_id)
    await _assert_farmer_belongs_to_client(db, client_id, farmer_user_id)
    rows = (await db.execute(
        select(FarmerCallLog)
        .where(
            FarmerCallLog.client_id == client_id,
            FarmerCallLog.farmer_user_id == farmer_user_id,
        )
        .order_by(FarmerCallLog.call_at.desc())
    )).scalars().all()

    # Bulk-resolve logger names.
    logger_ids = {r.logged_by_user_id for r in rows}
    name_by_id: dict[str, str | None] = {}
    if logger_ids:
        user_rows = (await db.execute(
            select(User.id, User.name).where(User.id.in_(logger_ids))
        )).all()
        name_by_id = {uid: nm for uid, nm in user_rows}

    out = []
    outbound = 0
    inbound = 0
    for r in rows:
        ct = r.call_type.value if hasattr(r.call_type, "value") else r.call_type
        if ct == "OUTBOUND":
            outbound += 1
        elif ct == "INBOUND":
            inbound += 1
        out.append({
            "id": r.id,
            "call_type": ct,
            "call_at": r.call_at.isoformat() if r.call_at else None,
            "logged_by_user_id": r.logged_by_user_id,
            "logged_by_name": name_by_id.get(r.logged_by_user_id),
            "created_at": r.created_at.isoformat() if r.created_at else None,
        })
    return {
        "total": len(rows),
        "outbound_count": outbound,
        "inbound_count": inbound,
        "rows": out,
    }


@router.post(
    "/client/{client_id}/farmer-support/farmers/{farmer_user_id}/call-log",
    status_code=201,
)
async def create_farmer_call_log(
    client_id: str,
    farmer_user_id: str,
    body: CallLogCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """RM-only. Permanent entry — no edit or delete endpoint exists.
    `logged_by_user_id` is always the authenticated caller's id; the
    body cannot override it (the auto-fill is the whole provenance
    guarantee per spec §5.3)."""
    from app.modules.clients.models import CallType, FarmerCallLog
    from datetime import datetime, timezone
    await _assert_rm_for_call_logging(db, current_user, client_id)
    await _assert_farmer_belongs_to_client(db, client_id, farmer_user_id)

    ct_raw = (body.call_type or "").strip().upper()
    if ct_raw not in ("OUTBOUND", "INBOUND"):
        raise HTTPException(
            status_code=422,
            detail="call_type must be OUTBOUND or INBOUND.",
        )
    if body.call_at:
        try:
            # Accept both naive and tz-aware ISO strings; normalise to UTC.
            parsed = datetime.fromisoformat(body.call_at.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            call_at = parsed
        except ValueError:
            raise HTTPException(
                status_code=422,
                detail="call_at must be a valid ISO datetime string.",
            )
    else:
        call_at = datetime.now(timezone.utc)

    row = FarmerCallLog(
        client_id=client_id,
        farmer_user_id=farmer_user_id,
        call_type=CallType(ct_raw),
        call_at=call_at,
        logged_by_user_id=current_user.id,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return {
        "id": row.id,
        "call_type": ct_raw,
        "call_at": row.call_at.isoformat(),
        "logged_by_user_id": row.logged_by_user_id,
        "logged_by_name": current_user.name,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


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
