"""Training Sandbox lifecycle endpoints — Commit B of the 2026-07-24
build plan.

Three endpoints, all scoped to a real client_id:
    POST /client/{cid}/training/start    → creates the shadow child.
    GET  /client/{cid}/training/current  → returns the active child, or null.
    POST /client/{cid}/training/end      → force-ends the active child now.

Rules enforced here (not just in the UI):
- Parent client must be real (is_training=False). No training-of-
  training; the shadow-of-shadow shape would confuse cascade delete.
- Parent client status must be ACTIVE. A PENDING_REVIEW or INACTIVE
  parent shouldn't host a training session.
- One active session per parent. Belt-and-braces to the DB partial
  unique index — surfaced as a friendly 409 with a specific code
  rather than an IntegrityError leak.
- start / end are CA-only (or SA / CM-EDIT via the standard
  project convention). The GET is CA + FM view-permitted so a
  Field Manager can see "is there a training right now?" from
  their own screen too.

Money and allocation invariants (Razorpay bypass, PromoterAllocation
bypass, discovery filter) live in Commits D and E. This commit just
births the training client and lets the CA end it. Everything else
downstream (Package inheritance, promoter invites, expiry sweep)
still needs the following commits.
"""
from datetime import datetime, timedelta, timezone
from secrets import token_hex
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.dependencies import get_current_user
from app.modules.clients.models import (
    CMClientAssignment, CMRights, Client, ClientPromoter, ClientStatus,
    ClientUser, ClientUserRole, PaymentModel,
)
from app.modules.platform.models import StatusEnum, User


router = APIRouter(tags=["Training Sandbox"])


# Session length. 12 days per user 2026-07-24; extending is
# deliberate — CA has to explicitly start a fresh session, they
# can't drift the clock.
TRAINING_SESSION_DAYS = 12
# After training_ends_at, the child sits in WINDING_DOWN for this
# window so in-flight orders/queries can complete. The expiry
# sweep (Commit F) hard-deletes anything still standing after this.
TRAINING_WIND_DOWN_HOURS = 24


# ── Role gates ────────────────────────────────────────────────────────────────

async def _assert_ca(db: AsyncSession, user: User, client_id: str) -> None:
    """CA-only for start / end. Same shape as _assert_ca_or_field_manager
    (see clients/router.py line 1931 comment for the full rationale)
    minus FIELD_MANAGER — starting/ending a training session is a CA
    decision, not an FM one. CM(EDIT) still allowed per project
    convention ("CM has all privileges inside the client")."""
    if bool(settings.sa_email) and user.email == settings.sa_email:
        return
    role_row = (await db.execute(
        select(ClientUser.role).where(
            ClientUser.client_id == client_id,
            ClientUser.user_id == user.id,
            ClientUser.status == StatusEnum.ACTIVE,
            ClientUser.role == ClientUserRole.CA,
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
            "code": "ca_role_required",
            "message": (
                "Only the Customer Admin can start or end a training "
                "session. Ask your CA to open the Training Sandbox."
            ),
        },
    )


async def _assert_ca_or_fm(db: AsyncSession, user: User, client_id: str) -> None:
    """View gate for GET /training/current — either CA or FM can peek."""
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
            "code": "client_membership_required",
            "message": (
                "Only portal users enrolled at this client can view "
                "training-session state."
            ),
        },
    )


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _load_parent_client(db: AsyncSession, client_id: str) -> Client:
    parent = (await db.execute(
        select(Client).where(Client.id == client_id)
    )).scalar_one_or_none()
    if parent is None:
        raise HTTPException(status_code=404, detail="Client not found")
    if parent.is_training:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "training_of_training_forbidden",
                "message": (
                    "Cannot start a training session under an existing "
                    "training client. Use the real parent client."
                ),
            },
        )
    if parent.status != ClientStatus.ACTIVE:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "parent_client_not_active",
                "message": (
                    "The parent client must be ACTIVE to host a training "
                    f"session (currently {parent.status.value if hasattr(parent.status, 'value') else parent.status})."
                ),
            },
        )
    return parent


async def _current_training_child(db: AsyncSession, parent_id: str) -> Client | None:
    """The one training child that's still 'live' — either ACTIVE or
    WINDING_DOWN. Cleaned-up children are hard-deleted, so a NULL
    result means the parent has no session in flight and can start
    a fresh one."""
    return (await db.execute(
        select(Client).where(
            Client.parent_client_id == parent_id,
            Client.is_training == True,  # noqa: E712
            Client.training_status.in_(["ACTIVE", "WINDING_DOWN"]),
        ).limit(1)
    )).scalar_one_or_none()


def _training_child_short_name() -> str:
    """Short-name generator for training children. Not user-visible
    (CAs read display_name), so a stable 'TR<hex>' pattern is enough
    and keeps well under the 12-char column limit. Random-suffix
    dodges collisions across the parent's lifetime."""
    return f"TR{token_hex(4).upper()}"


def _serialise_training(child: Client) -> dict:
    """Response shape shared across start / current. Session-role
    slots (`training_expert_user_id`, `training_dealer_user_id`) are
    returned as ids only here; the CA-portal panel resolves names in
    a separate fetch (Expert dropdown for expert; `_hydrate_training_dealer_info`
    for dealer, since the dealer is entered by phone with no dropdown)."""
    return {
        "id": child.id,
        "parent_client_id": child.parent_client_id,
        "display_name": child.display_name,
        "primary_colour": child.primary_colour,
        "logo_url": child.logo_url,
        "training_started_at": child.training_started_at,
        "training_ends_at": child.training_ends_at,
        "training_status": child.training_status,
        "training_expert_user_id": child.training_expert_user_id,
        "training_dealer_user_id": child.training_dealer_user_id,
    }


async def _hydrate_training_dealer_info(
    db: AsyncSession, dealer_user_id: str | None,
) -> dict | None:
    """Fetch the assigned training dealer's display info so the CA
    portal panel can show name + phone + shop instead of an opaque
    "Assigned" placeholder. Called by the session-read endpoints
    only (start / current); the write endpoints don't need it — the
    read that follows will pick it up. None when no dealer is set."""
    if not dealer_user_id:
        return None
    from app.modules.orders.models import DealerProfile
    user_row = (await db.execute(
        select(User.id, User.name, User.phone).where(User.id == dealer_user_id)
    )).one_or_none()
    if not user_row:
        return None
    profile_row = (await db.execute(
        select(DealerProfile.shop_name, DealerProfile.shop_address)
        .where(DealerProfile.user_id == dealer_user_id)
    )).one_or_none()
    return {
        "user_id": user_row.id,
        "name": user_row.name,
        "phone": user_row.phone,
        "shop_name": profile_row.shop_name if profile_row else None,
        "shop_address": profile_row.shop_address if profile_row else None,
    }


async def _training_counts(db: AsyncSession, training_client_id: str) -> dict:
    """Summary counts for the CA portal training page — one query per
    entity. Cheap (all indexed by client_id) and CAs open this page
    once a session; no need for a materialised view yet."""
    from sqlalchemy import func
    from app.modules.subscriptions.models import (
        PromoterAssignment, Subscription, SubscriptionStatus,
    )
    from app.modules.orders.models import Order
    from app.modules.farmpundit.models import Query
    subs_total = (await db.execute(
        select(func.count(Subscription.id))
        .where(Subscription.client_id == training_client_id)
    )).scalar() or 0
    subs_active = (await db.execute(
        select(func.count(Subscription.id))
        .where(
            Subscription.client_id == training_client_id,
            Subscription.status == SubscriptionStatus.ACTIVE,
        )
    )).scalar() or 0
    farmers = (await db.execute(
        select(func.count(func.distinct(Subscription.farmer_user_id)))
        .where(Subscription.client_id == training_client_id)
    )).scalar() or 0
    promoters = (await db.execute(
        select(func.count(func.distinct(PromoterAssignment.promoter_user_id)))
        .join(Subscription, Subscription.id == PromoterAssignment.subscription_id)
        .where(Subscription.client_id == training_client_id)
    )).scalar() or 0
    orders_total = (await db.execute(
        select(func.count(Order.id))
        .where(Order.client_id == training_client_id)
    )).scalar() or 0
    queries_total = (await db.execute(
        select(func.count(Query.id))
        .where(Query.client_id == training_client_id)
    )).scalar() or 0
    return {
        "subscriptions_total": int(subs_total),
        "subscriptions_active": int(subs_active),
        "farmers": int(farmers),
        "promoters": int(promoters),
        "orders_total": int(orders_total),
        "queries_total": int(queries_total),
    }


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post("/client/{client_id}/training/start", status_code=201)
async def start_training_session(
    client_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """CA starts a fresh 12-day training session under this client.

    Refusals (409):
    - `training_of_training_forbidden` — caller passed a training id.
    - `parent_client_not_active` — parent isn't ACTIVE.
    - `training_session_already_active` — there's already an
      ACTIVE/WINDING_DOWN child. The DB partial unique index would
      raise IntegrityError otherwise; we check first for a clean
      error message.
    """
    await _assert_ca(db, current_user, client_id)
    parent = await _load_parent_client(db, client_id)

    existing = await _current_training_child(db, parent.id)
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "training_session_already_active",
                "message": (
                    "A training session is already in progress for this "
                    "client. End it first, or wait for it to finish, "
                    "before starting a new one."
                ),
                "current_training_id": existing.id,
                "training_status": existing.training_status,
                "training_ends_at": (
                    existing.training_ends_at.isoformat()
                    if existing.training_ends_at else None
                ),
            },
        )

    now = datetime.now(timezone.utc)
    ends_at = now + timedelta(days=TRAINING_SESSION_DAYS)

    # display_name: parent's display + " · Training" so the CA and
    # every downstream PWA immediately reads it as a training tile.
    # Falls back to full_name when the parent has no display_name
    # (they're not required to have one).
    parent_display = parent.display_name or parent.full_name
    display_name = f"{parent_display} · Training"[:255]
    full_name = f"{parent.full_name} (Training)"[:500]

    child = Client(
        full_name=full_name,
        short_name=_training_child_short_name(),
        display_name=display_name,
        # Brand carry-over so the trainee sees the familiar look.
        tagline=parent.tagline,
        logo_url=parent.logo_url,
        primary_colour=parent.primary_colour,
        secondary_colour=parent.secondary_colour,
        # Contact fields — inherit so downstream code that reads
        # them doesn't hit NOT NULL violations. CAs never see these
        # on the training child; they're only ever surfaced on the
        # real client.
        ca_name=parent.ca_name,
        ca_phone=parent.ca_phone,
        ca_email=parent.ca_email,
        # 2026-07-24 — Training subs always bypass Razorpay + real
        # promoter allocations (see Commit D). COMPANY_PAYS is the
        # safest payment_model to carry so any legacy path reading
        # it during a training flow doesn't try to bill the farmer.
        payment_model=PaymentModel.COMPANY_PAYS,
        # Never surface a training client on the farmer discovery
        # drawer or nearby-dealers — trainees enter via Promoter
        # invitation only. The discovery-filter sweep (Commit E)
        # is the belt on top of this suspender.
        hidden_from_discovery=True,
        # ACTIVE so downstream endpoints that already filter on
        # status treat the training child as live (Package lookups,
        # etc.). The training_status field is the training-specific
        # lifecycle marker.
        status=ClientStatus.ACTIVE,
        # Training-lifecycle fields.
        is_training=True,
        parent_client_id=parent.id,
        training_started_at=now,
        training_ends_at=ends_at,
        training_status="ACTIVE",
    )
    db.add(child)
    try:
        await db.commit()
    except IntegrityError:
        # Race — someone else created a training child between our
        # existence check and the insert. Surface the same 409.
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail={
                "code": "training_session_already_active",
                "message": (
                    "A training session was just started by someone "
                    "else. Refresh and try again."
                ),
            },
        )
    await db.refresh(child)
    return _serialise_training(child)


@router.get("/client/{client_id}/training/current")
async def get_current_training_session(
    client_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Returns the parent's active training child, or `{}` if none.
    CA + FM view-permitted (FM sees the session running for
    situational awareness, but can't start/end it — that's the CA)."""
    await _assert_ca_or_fm(db, current_user, client_id)
    parent = await _load_parent_client(db, client_id)
    child = await _current_training_child(db, parent.id)
    if child is None:
        return {}
    out = _serialise_training(child)
    out["counts"] = await _training_counts(db, child.id)
    # 2026-08-19 — Hydrate training dealer info (name + phone + shop)
    # so the CA panel can render more than an opaque "Assigned" badge.
    # Expert already resolves via the /onboarded-experts dropdown; the
    # dealer has no such dropdown (entered by phone), so surface it
    # here on the session read.
    out["training_dealer_info"] = await _hydrate_training_dealer_info(
        db, child.training_dealer_user_id,
    )
    return out


@router.post("/client/{client_id}/training/end")
async def end_training_session(
    client_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """CA force-ends the active session — hard close.

    2026-07-25 — Switched from soft-close (WINDING_DOWN + 24h grace)
    to hard-close per user's team feedback. Soft-close was creating
    confusion because the `uq_one_active_training_per_parent` DB
    index covers both ACTIVE and WINDING_DOWN rows, so a CA who
    ended a session couldn't start a fresh one for ~25h until the
    hourly sweep cascade-deleted the winding row. Hard-close runs
    the same cascade helper synchronously so `POST /training/start`
    is unblocked within seconds of tap.

    In-flight orders / queries / subscriptions under the training
    session vanish mid-transaction. That's the deliberate tradeoff
    the team accepted — training data is throwaway, and the visual
    friction of soft-close outweighed the value of the 24h grace.

    The 12-day auto-expiry (`app/tasks/training_expiry.py`) still
    routes ACTIVE → WINDING_DOWN → 24h grace → cascade. Only the
    user-initiated end path is now synchronous; farmers + promoters
    keep the grace window when the session times out naturally.

    404 if there's no active training session for this client.
    """
    from app.tasks.training_expiry import _cascade_delete_training_child

    await _assert_ca(db, current_user, client_id)
    parent = await _load_parent_client(db, client_id)
    child = await _current_training_child(db, parent.id)
    if child is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "no_active_training_session",
                "message": "No training session is currently active for this client.",
            },
        )
    # Snapshot the id + label BEFORE cascade so we can return a
    # meaningful confirmation payload after the row is gone.
    ended_id = child.id
    ended_label = child.display_name or child.full_name
    await _cascade_delete_training_child(db, child)
    await db.commit()
    return {
        "closed": True,
        "id": ended_id,
        "display_name": ended_label,
    }


# ── Promoter: list active training sessions the caller can join ──────────────

@router.get("/promoter/training/available-clients")
async def list_available_training_clients(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Every ACTIVE training session where the caller has an ACTIVE
    ClientPromoter binding on the PARENT client. Used by the dealer
    / facilitator PWA to render the "Training Session" tile (only
    when this returns non-empty) and to populate the picker on the
    invite-farmer form.

    F-P sees at most one training session (they're locked to a
    single parent per §11.2); D-P may see several.

    WINDING_DOWN sessions are excluded — the invite endpoint
    refuses them anyway (Commit G training_not_active), so no
    point showing them in the picker.
    """
    parent_ids = (await db.execute(
        select(ClientPromoter.client_id).where(
            ClientPromoter.user_id == current_user.id,
            ClientPromoter.is_promoter.is_(True),
            ClientPromoter.status == "ACTIVE",
        )
    )).scalars().all()
    if not parent_ids:
        return []
    rows = (await db.execute(
        select(Client, Client.parent_client_id).where(
            Client.is_training == True,  # noqa: E712
            Client.training_status == "ACTIVE",
            Client.parent_client_id.in_(list(set(parent_ids))),
        )
    )).all()
    # Join in each parent's display_name for the picker label.
    parent_names: dict[str, str] = {}
    if rows:
        parents = (await db.execute(
            select(Client.id, Client.display_name, Client.full_name)
            .where(Client.id.in_([r[1] for r in rows]))
        )).all()
        for pid, pdisp, pfull in parents:
            parent_names[pid] = pdisp or pfull or ""
    return [
        {
            **_serialise_training(child),
            "parent_display_name": parent_names.get(parent_client_id, ""),
        }
        for child, parent_client_id in rows
    ]


# ── Promoter: invite a farmer into a training session ────────────────────────

class TrainingInviteRequest(BaseModel):
    """Farmer + package the promoter is inviting into training. Same
    shape as the real PromoterAssignRequest in subscriptions/router.py
    minus client_id (derived from the URL) and minus the P-V measure
    fields (training subs never need the volume-calc context — the
    farmer's real measures still live on their real subs). If we ever
    want farmers to practise the plant-count / area-wise flow inside
    training, add the fields here and mirror the write logic."""
    farmer_phone: str
    package_id: str
    promoter_type: str = "DEALER"  # DEALER or FACILITATOR


async def _assert_promoter_at_parent(
    db: AsyncSession, user: User, parent_client_id: str, promoter_type: str,
) -> ClientPromoter:
    """Verify caller has an ACTIVE ClientPromoter binding at the
    PARENT client (not the training child — training children carry
    no promoter rows of their own). Real F-P is single-parent per
    §11.2 exclusivity; D-P is multi-parent. Either way, the check
    is the same shape: ACTIVE binding on the parent with is_promoter
    True and the requested role.
    """
    row = (await db.execute(
        select(ClientPromoter).where(
            ClientPromoter.user_id == user.id,
            ClientPromoter.client_id == parent_client_id,
            ClientPromoter.promoter_type == promoter_type.upper(),
            ClientPromoter.is_promoter.is_(True),
            ClientPromoter.status == "ACTIVE",
        ).limit(1)
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "not_a_promoter_at_parent",
                "message": (
                    f"You don't have an active {promoter_type.title()}-"
                    f"Promoter role at this company's parent client. "
                    f"Only real promoters can invite farmers into "
                    f"training."
                ),
            },
        )
    return row


@router.post(
    "/promoter/training/{training_client_id}/invite-farmer",
    status_code=201,
)
async def invite_farmer_to_training(
    training_client_id: str,
    request: TrainingInviteRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Promoter invites a real farmer into a training session.

    The farmer receives a normal PromoterAssignment PENDING_FARMER_APPROVAL
    (with a [Training]-prefixed FCM), accepts or rejects via the
    standard flow, and the Subscription lands under the training
    child's client_id — carrying the training marker to every
    downstream Order / Query.

    Money invariants (Commit D): consume_for_assignment is a no-op
    for training clients, so the promoter's real allocation kitty
    is untouched by this invite. Symmetric refund_to_promoter on
    farmer reject is also a no-op — nothing was consumed, nothing
    to refund.

    Refusals:
    - training_client_not_found  (404) — id doesn't resolve.
    - not_a_training_client      (409) — id is a real client.
    - training_not_active        (409) — training is WINDING_DOWN
      or the row's state is invalid. New invites blocked once the
      12-day clock has passed; in-flight subs can still complete.
    - farmer_not_registered      (404) — phone has no User row.
      Farmer must self-register in the PWA first.
    - not_a_promoter_at_parent   (403) — see helper docstring.
    - package_not_in_parent      (409) — package_id belongs to
      a different client. Training children borrow the parent's
      Package catalogue (Commit C); packages from other companies
      are not eligible even by URL manipulation.
    - farmer_already_in_training (409) — dedupe: the same farmer
      already holds an ACTIVE / PENDING training sub under this
      training client. User can end the earlier sub or wait for
      it to complete first.
    """
    # Load the training child + its parent in one hop.
    child = (await db.execute(
        select(Client).where(Client.id == training_client_id)
    )).scalar_one_or_none()
    if child is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "training_client_not_found",
                    "message": "Training client not found."},
        )
    if not child.is_training or not child.parent_client_id:
        raise HTTPException(
            status_code=409,
            detail={"code": "not_a_training_client",
                    "message": "This client is not a training sandbox."},
        )
    if child.training_status != "ACTIVE":
        raise HTTPException(
            status_code=409,
            detail={
                "code": "training_not_active",
                "message": (
                    "This training session is no longer accepting new "
                    "invitations. It has either ended or is winding down."
                ),
                "training_status": child.training_status,
            },
        )

    # Caller must be a real promoter at the PARENT.
    await _assert_promoter_at_parent(
        db, current_user, child.parent_client_id,
        request.promoter_type,
    )

    # Farmer must be a registered user.
    from app.modules.auth.service import get_user_by_phone
    farmer = await get_user_by_phone(db, request.farmer_phone)
    if farmer is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "farmer_not_registered",
                "message": (
                    "That phone number isn't registered on RootsTalk. "
                    "Ask the farmer to install and open the app first, "
                    "then try again."
                ),
            },
        )

    # Package must belong to the PARENT (training children borrow
    # the parent's Package catalogue — see Commit C).
    from app.modules.advisory.models import Package
    pkg_client_id = (await db.execute(
        select(Package.client_id).where(Package.id == request.package_id)
    )).scalar_one_or_none()
    if pkg_client_id is None:
        raise HTTPException(status_code=404, detail="Package not found.")
    if pkg_client_id != child.parent_client_id:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "package_not_in_parent",
                "message": (
                    "That package belongs to a different company. Pick "
                    "a package authored by this company's parent client."
                ),
            },
        )

    # Dedupe — one live training sub per farmer per training client.
    from app.modules.subscriptions.models import (
        AssignmentStatus, PromoterAssignment, Subscription,
        SubscriptionStatus, SubscriptionType,
    )
    existing = (await db.execute(
        select(Subscription.id).where(
            Subscription.client_id == training_client_id,
            Subscription.farmer_user_id == farmer.id,
            Subscription.status.in_([
                SubscriptionStatus.ACTIVE,
                SubscriptionStatus.WAITLISTED,
            ]),
        ).limit(1)
    )).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "farmer_already_in_training",
                "message": (
                    "This farmer already has a live subscription in "
                    "this training session."
                ),
                "existing_subscription_id": existing,
            },
        )

    # ── Create the Sub + PromoterAssignment. ──────────────────────
    # consume_for_assignment auto-bypasses for training clients
    # (Commit D), so the promoter's real kitty is untouched. Kept
    # in the call chain for parity with the real initiate_assignment
    # flow so any future audit sees the same code path.
    from app.services.promoter_pool import consume_for_assignment
    try:
        await consume_for_assignment(
            db,
            client_id=training_client_id,
            promoter_user_id=current_user.id,
        )
    except ValueError:
        # Shouldn't happen for training (bypass returns None), but
        # if it ever does, surface the underlying kitty error.
        raise HTTPException(
            status_code=422,
            detail={
                "code": "consume_failed",
                "message": "Unable to open a training slot right now.",
            },
        )

    now = datetime.now(timezone.utc)
    sub = Subscription(
        farmer_user_id=farmer.id,
        client_id=training_client_id,
        package_id=request.package_id,
        promoter_user_id=current_user.id,
        subscription_type=SubscriptionType.ASSIGNED,
        status=SubscriptionStatus.ACTIVE,
        subscription_date=now,
    )
    db.add(sub)
    await db.flush()
    # reference_number generation reuses the real helper — the
    # training sub gets a normal reference so all downstream code
    # that renders it works unchanged.
    from app.modules.subscriptions.router import _generate_reference_for_sub
    sub.reference_number = await _generate_reference_for_sub(
        db, sub.client_id,
    )

    assignment = PromoterAssignment(
        subscription_id=sub.id,
        promoter_user_id=current_user.id,
        promoter_type=request.promoter_type.upper(),
        status=AssignmentStatus.PENDING_FARMER_APPROVAL,
    )
    db.add(assignment)
    await db.commit()

    # [Training]-prefixed FCM to the farmer. Same shape as the real
    # PROMOTER_ASSIGNMENT_RECEIVED push so the PWA's existing
    # handler can render it — the training marker travels via
    # data.is_training so the accept-screen banner reads it.
    if farmer.fcm_token:
        try:
            from app.services.fcm_service import send_fcm
            parent = (await db.execute(
                select(Client).where(Client.id == child.parent_client_id)
            )).scalar_one_or_none()
            parent_name = (
                parent.display_name or parent.full_name
                if parent else "a company"
            )
            promoter_label = (
                "Dealer" if request.promoter_type.upper() == "DEALER"
                else "Facilitator"
            )
            await send_fcm(
                token=farmer.fcm_token,
                title=f"[Training] Invitation from {parent_name}",
                body=(
                    f"{promoter_label} {current_user.name or 'a promoter'} "
                    f"is running a training session and has invited you "
                    f"to practise. Open the app to accept or decline."
                ),
                data={
                    "type": "PROMOTER_ASSIGNMENT_RECEIVED",
                    "subscription_id": sub.id,
                    "assignment_id": assignment.id,
                    "is_training": "true",
                },
            )
        except Exception:
            pass

    return {
        "subscription_id": sub.id,
        "assignment_id": assignment.id,
        "status": "Awaiting farmer approval",
    }


# ── Session Roles — Training Expert + Training Dealer ──────────────────────
#
# Both are per-session assignment slots on the training-child row. Set +
# cleared by the CA from the /training panel. Cleared implicitly on
# session end via the existing hard-close cascade — no separate teardown.


class _SessionRoleUserBody(BaseModel):
    user_id: str


class _SessionRolePhoneBody(BaseModel):
    phone: str


async def _load_active_training_child(
    db: AsyncSession, parent_client_id: str,
) -> Client:
    """Fetch the parent's active training child or 404."""
    await _load_parent_client(db, parent_client_id)
    child = await _current_training_child(db, parent_client_id)
    if child is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "no_active_training_session",
                "message": "No training session is currently active for this client.",
            },
        )
    return child


def _normalise_phone(phone: str) -> str:
    """Match the app's phone-key convention: +91 + last 10 digits."""
    digits = "".join(c for c in (phone or "") if c.isdigit())
    if len(digits) < 10:
        return ""
    return "+91" + digits[-10:]


# ── Training Expert ─────────────────────────────────────────────────────

@router.post("/client/{client_id}/training/expert")
async def set_training_expert(
    client_id: str,
    body: _SessionRoleUserBody,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Mark one ACTIVE onboarded Primary Expert (Pundit) of the parent
    as the go-to expert for this training session. All training-farmer
    queries route directly to their device instead of the round-robin
    queue while this is set.

    Validation:
      - Parent has an active training child (else 404 no_active_training_session).
      - Target user is an ACTIVE onboarded PE of the parent (via
        ClientPromoter with promoter_type='FARM_PUNDIT' and status='ACTIVE').

    409 `not_active_primary_expert` if the target isn't currently active on
    the parent's onboarded PE list.
    """
    await _assert_ca(db, current_user, client_id)
    child = await _load_active_training_child(db, client_id)

    # Primary Experts live in ClientFarmPundit joined through
    # FarmPunditProfile — NOT in ClientPromoter (which is
    # DEALER/FACILITATOR only).
    from app.modules.farmpundit.models import (
        ClientFarmPundit, FarmPunditProfile, PunditRole,
    )
    pe_row = (await db.execute(
        select(ClientFarmPundit)
        .join(FarmPunditProfile, FarmPunditProfile.id == ClientFarmPundit.pundit_id)
        .where(
            ClientFarmPundit.client_id == child.parent_client_id,
            FarmPunditProfile.user_id == body.user_id,
            ClientFarmPundit.role == PunditRole.PRIMARY,
            ClientFarmPundit.status == "ACTIVE",
        )
    )).scalar_one_or_none()
    if pe_row is None:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "not_active_primary_expert",
                "message": (
                    "This user is not an active Primary Expert onboarded by "
                    "the parent client. Only currently-active PEs can be "
                    "marked as the Training Expert."
                ),
            },
        )

    child.training_expert_user_id = body.user_id
    await db.commit()
    await db.refresh(child)
    return _serialise_training(child)


@router.delete("/client/{client_id}/training/expert")
async def clear_training_expert(
    client_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Clear the Training Expert assignment; queries revert to today's
    round-robin behaviour immediately."""
    await _assert_ca(db, current_user, client_id)
    child = await _load_active_training_child(db, client_id)
    child.training_expert_user_id = None
    await db.commit()
    await db.refresh(child)
    return _serialise_training(child)


# ── Training Dealer ─────────────────────────────────────────────────────

async def _validate_training_dealer_candidate(
    db: AsyncSession, phone: str, client_id: str,
) -> tuple[User, dict]:
    """Look up a phone and validate it can be the Training Dealer.

    Returns (user, info-dict). Info-dict carries the shape needed by
    both the preflight /lookup-dealer endpoint and the POST — one
    query path, two callers.

    Raises HTTPException with a specific code on failure:
      - `phone_not_registered` — no User for this phone.
      - `dealer_profile_missing` — user hasn't set up their shop.
      - `user_inactive` — user's DEALER role isn't ACTIVE.
      - `already_onboarded_here` — user is onboarded to THIS client
        as an active DEALER promoter. Scoped to this client only
        (2026-08-09) — a dealer onboarded to some OTHER client can
        still be a Training Dealer here because they won't appear
        in this client's real-orders dealer picker.
    """
    from app.modules.orders.models import DealerProfile
    from app.modules.platform.models import RoleType, UserRole

    normalised = _normalise_phone(phone)
    if not normalised:
        raise HTTPException(status_code=422, detail="Enter a 10-digit phone number.")

    user = (await db.execute(
        select(User).where(User.phone == normalised)
    )).scalar_one_or_none()
    if user is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "phone_not_registered",
                "message": "No RootsTalk account found for this phone.",
            },
        )

    dealer_role = (await db.execute(
        select(UserRole).where(
            UserRole.user_id == user.id,
            UserRole.role_type == RoleType.DEALER,
        )
    )).scalar_one_or_none()
    if dealer_role is None or dealer_role.status != StatusEnum.ACTIVE:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "user_inactive",
                "message": "This user isn't an active Dealer on RootsTalk.",
            },
        )

    profile = (await db.execute(
        select(DealerProfile).where(DealerProfile.user_id == user.id)
    )).scalar_one_or_none()
    if profile is None:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "dealer_profile_missing",
                "message": (
                    "This dealer hasn't set up their shop yet. Ask them to "
                    "complete the shop profile in the PWA first."
                ),
            },
        )

    # Scoped to THIS client — a dealer onboarded to a different
    # client is fine to serve as Training Dealer here. Preventing
    # that would kill a legitimate design: the CA wants an
    # "exclusive dummy" for training that doesn't overlap with THIS
    # client's real dealers; some other company's real dealer is
    # invisible to THIS client's real-orders picker so no bleed.
    already = (await db.execute(
        select(ClientPromoter).where(
            ClientPromoter.user_id == user.id,
            ClientPromoter.client_id == client_id,
            ClientPromoter.promoter_type == "DEALER",
            ClientPromoter.status == "ACTIVE",
        )
    )).scalars().first()
    if already is not None:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "already_onboarded_here",
                "message": (
                    "This dealer is already onboarded to your company as a "
                    "real dealer. Pick a phone number that isn't one of your "
                    "onboarded dealers so training stays isolated from real orders."
                ),
            },
        )

    return user, {
        "user_id": user.id,
        "name": user.name,
        "phone": user.phone,
        "shop_name": profile.shop_name,
        "shop_address": profile.shop_address,
    }


@router.get("/client/{client_id}/training/lookup-dealer")
async def lookup_training_dealer(
    client_id: str,
    phone: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """CA preflight — validate a phone before committing it as the
    Training Dealer. Returns the resolved user + shop metadata when
    eligible; raises with a specific code otherwise so the CA sees
    exactly why a candidate was rejected."""
    await _assert_ca(db, current_user, client_id)
    await _load_active_training_child(db, client_id)
    _, info = await _validate_training_dealer_candidate(db, phone, client_id)
    return info


@router.post("/client/{client_id}/training/dealer")
async def set_training_dealer(
    client_id: str,
    body: _SessionRolePhoneBody,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Mark one phone number as the Training Dealer for this session.
    The user must satisfy every check in _validate_training_dealer_candidate."""
    await _assert_ca(db, current_user, client_id)
    child = await _load_active_training_child(db, client_id)
    user, _ = await _validate_training_dealer_candidate(db, body.phone, client_id)
    child.training_dealer_user_id = user.id
    await db.commit()
    await db.refresh(child)
    return _serialise_training(child)


@router.delete("/client/{client_id}/training/dealer")
async def clear_training_dealer(
    client_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Clear the Training Dealer slot. Recipient picker reverts to
    just the parent's onboarded dealers immediately."""
    await _assert_ca(db, current_user, client_id)
    child = await _load_active_training_child(db, client_id)
    child.training_dealer_user_id = None
    await db.commit()
    await db.refresh(child)
    return _serialise_training(child)


# ── Onboarded PE lookup for the Training Expert dropdown ───────────────

@router.get("/client/{client_id}/training/onboarded-experts")
async def list_onboarded_primary_experts(
    client_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List the parent's currently-ACTIVE Primary Experts so the CA
    can pick one for the Training Expert dropdown. Same table that
    real query routing reads from (ClientFarmPundit joined via
    FarmPunditProfile) so the dropdown never surfaces someone who
    couldn't have received a live query."""
    from app.modules.farmpundit.models import (
        ClientFarmPundit, FarmPunditProfile, PunditRole,
    )
    await _assert_ca(db, current_user, client_id)
    child = await _load_active_training_child(db, client_id)

    rows = (await db.execute(
        select(User)
        .join(FarmPunditProfile, FarmPunditProfile.user_id == User.id)
        .join(ClientFarmPundit, ClientFarmPundit.pundit_id == FarmPunditProfile.id)
        .where(
            ClientFarmPundit.client_id == child.parent_client_id,
            ClientFarmPundit.role == PunditRole.PRIMARY,
            ClientFarmPundit.status == "ACTIVE",
        )
        .order_by(User.name)
    )).scalars().all()
    return [
        {"user_id": u.id, "name": u.name, "phone": u.phone}
        for u in rows
    ]
