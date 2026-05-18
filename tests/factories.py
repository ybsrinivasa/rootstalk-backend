"""Test data factories.

Tiny helpers to create the minimal parent rows needed for snapshot
integration tests. Kept deliberately small — each factory creates only
what's necessary to satisfy FK constraints and exercises the SUT.

These are NOT a general-purpose fixture library. Production-grade
factories for the wider test suite can grow on top of this if/when
the codebase grows more integration tests.
"""
from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.advisory.models import (
    ConditionalAnswer, ConditionalQuestion, Element,
    PGRecommendation, Package, PackageAuthor, PackageLocation,
    PackageStatus, PackageType, PackageVariable, Parameter, ParameterSource,
    Practice, PracticeConditional, PracticeL0, Relation, RelationType,
    SPRecommendation, Timeline,
    TimelineFromType, Variable,
)
from app.modules.clients.models import (
    Client, ClientCrop, ClientLocation, ClientUser, ClientUserRole,
    CMClientAssignment, CMRights, PaymentModel,
)
from app.modules.platform.models import StatusEnum, User
from app.modules.subscriptions.models import (
    Subscription, SubscriptionStatus, SubscriptionType,
)
from app.modules.sync.models import CoshCoreItem, CropMeasure


def _short(prefix: str) -> str:
    return prefix + uuid.uuid4().hex[:6]


async def make_user(db: AsyncSession, **kw) -> User:
    u = User(phone=_short("+91"), name=kw.get("name", "Test Farmer"))
    db.add(u)
    await db.flush()
    if not kw.get("skip_auto_link"):
        await _auto_link_user_to_existing_clients(db, u)
    if not kw.get("skip_auto_cm"):
        await _auto_grant_cm_role(db, u)
    return u


async def _auto_grant_cm_role(db: AsyncSession, user: User) -> None:
    """2026-05-17 test compat: auto-grant CONTENT_MANAGER UserRole so
    the new SA-Portal-side guard (`_assert_sa_or_cm`) passes by
    default on Global write endpoints. Mirrors
    `_auto_link_user_to_existing_clients` (Batch 39S) in pattern.

    Tests that want to exercise rejection (e.g. test_phase_global_
    role_guard) pass `skip_auto_cm=True` to make_user. Tests that
    want a non-CM Portal role (RM, BM) should also use skip_auto_cm
    and add the UserRole explicitly.

    Real production never auto-grants; this only fires in the test
    factories."""
    from app.modules.platform.models import RoleType, StatusEnum, UserRole

    db.add(UserRole(
        user_id=user.id,
        role_type=RoleType.CONTENT_MANAGER,
        status=StatusEnum.ACTIVE,
    ))
    await db.flush()


async def _auto_link_user_to_existing_clients(db: AsyncSession, user: User) -> None:
    """Batch 39S (2026-05-17) test compat: auto-link new test Users to
    every existing test Client so the CCA-write guard
    (`_assert_can_edit_client_advisory`) passes by default.

    Role updated 2026-05-18 (Batch J): SUBJECT_EXPERT. Previously
    REPORT_USER, but the guard now refuses non-SE roles. Make_client_user
    DELETEs any prior (client, user) row before inserting an explicit
    role, so tests that override the role still work.

    Real production never auto-links; this only fires in the test
    factories."""
    from app.modules.clients.models import (
        Client, ClientUser, ClientUserRole,
    )
    from app.modules.platform.models import StatusEnum

    clients = (await db.execute(select(Client))).scalars().all()
    if not clients:
        return
    added = False
    for c in clients:
        # Tolerate tests that already wired a ClientUser row for
        # (user, client) via direct db.add or make_client_user — the
        # auto-link is a fallback, not authoritative. Skip when any
        # row exists for the pair (regardless of role).
        existing = (await db.execute(
            select(ClientUser.id).where(
                ClientUser.user_id == user.id,
                ClientUser.client_id == c.id,
            ).limit(1)
        )).scalar_one_or_none()
        if existing is not None:
            continue
        db.add(ClientUser(
            user_id=user.id, client_id=c.id,
            role=ClientUserRole.SUBJECT_EXPERT,
            status=StatusEnum.ACTIVE,
        ))
        added = True
    if added:
        await db.flush()


async def _auto_link_client_to_existing_users(db: AsyncSession, client: Client) -> None:
    """Mirror of `_auto_link_user_to_existing_clients` for the
    make_client → make_user ordering. Uses SUBJECT_EXPERT
    (Batch J, 2026-05-18) for the same guard-compat reason."""
    from app.modules.clients.models import ClientUser, ClientUserRole
    from app.modules.platform.models import StatusEnum

    users = (await db.execute(select(User))).scalars().all()
    if not users:
        return
    added = False
    for u in users:
        existing = (await db.execute(
            select(ClientUser.id).where(
                ClientUser.user_id == u.id,
                ClientUser.client_id == client.id,
            ).limit(1)
        )).scalar_one_or_none()
        if existing is not None:
            continue
        db.add(ClientUser(
            user_id=u.id, client_id=client.id,
            role=ClientUserRole.SUBJECT_EXPERT,
            status=StatusEnum.ACTIVE,
        ))
        added = True
    if added:
        await db.flush()


async def make_crop_reference(
    db: AsyncSession, cosh_id: str, *,
    name: str = "Paddy", scientific_name: str | None = None,
    measure: str = "AREA_WISE", status: str = "active",
) -> CoshCoreItem:
    """Seed a Cosh biological_name + Crop classification + Area/Plant typing.

    Mirrors the post 2026-05-09 live-sync shape (Round 1 + Round 3):
      • CoshCoreItem(core_type='biological_names', cosh_id=...)
      • CoshConnectRow(connect_type='biological_names_and_roles',
        endpoints=[<name>, CROP_UUID])
      • CoshCoreItem(core_type='roles_of_biological_names',
        cosh_id=CROP_UUID) — idempotent
      • CoshCoreItem(core_type='area_plant_wise', AREA/PLANT UUIDs) —
        idempotent
      • CoshConnectRow(connect_type='crop_area_plant_wise',
        endpoints=[<name>, AREA_WISE_UUID|PLANT_WISE_UUID])

    Required when a test exercises CCA Step 1 add_crop. Idempotent on
    cosh_id. `scientific_name` accepted for back-compat with existing
    call sites but ignored — V1 doesn't source scientific names from
    Cosh until that Core's Connect ships.

    `measure=None` skips the area_plant_wise wiring — useful for tests
    that need a Crop-classified-but-untyped name (mirrors the 27 of
    144 V1 crops still pending Cosh classification at first sync).
    """
    from app.modules.sync.models import CoshConnectRow
    from app.services.cosh_constants import (
        COSH_AREA_PLANT_WISE_CORE, COSH_AREA_WISE_UUID,
        COSH_BIOLOGICAL_NAMES_CORE, COSH_CROP_AREA_PLANT_CONNECT,
        COSH_NAME_ROLE_CONNECT, COSH_PLANT_WISE_UUID, COSH_ROLES_CORE,
        COSH_ROLE_CROP_UUID,
    )

    existing_ref = (await db.execute(
        select(CoshCoreItem).where(
            CoshCoreItem.cosh_id == cosh_id,
            CoshCoreItem.core_type == COSH_BIOLOGICAL_NAMES_CORE,
        )
    )).scalar_one_or_none()
    if existing_ref is None:
        existing_ref = CoshCoreItem(
            cosh_id=cosh_id, core_type=COSH_BIOLOGICAL_NAMES_CORE,
            status=status, translations={"en": name},
        )
        db.add(existing_ref)

    # Seed the Crop role item once per test session.
    crop_role = (await db.execute(
        select(CoshCoreItem).where(
            CoshCoreItem.cosh_id == COSH_ROLE_CROP_UUID,
            CoshCoreItem.core_type == COSH_ROLES_CORE,
        )
    )).scalar_one_or_none()
    if crop_role is None:
        db.add(CoshCoreItem(
            cosh_id=COSH_ROLE_CROP_UUID, core_type=COSH_ROLES_CORE,
            status="active", translations={"en": "Crop"},
        ))

    # Connect this name to the Crop role.
    connect_id = f"connect:{cosh_id}:crop"
    existing_connect = (await db.execute(
        select(CoshConnectRow).where(
            CoshConnectRow.connect_id == connect_id,
            CoshConnectRow.connect_type == COSH_NAME_ROLE_CONNECT,
        )
    )).scalar_one_or_none()
    if existing_connect is None:
        db.add(CoshConnectRow(
            connect_id=connect_id, connect_type=COSH_NAME_ROLE_CONNECT,
            status="active",
            endpoints=[
                {"role": COSH_BIOLOGICAL_NAMES_CORE,
                 "cosh_id": cosh_id, "position": 1},
                {"role": COSH_ROLES_CORE,
                 "cosh_id": COSH_ROLE_CROP_UUID, "position": 2},
            ],
        ))

    # Area/Plant typing — Round 3.
    if measure is not None:
        measure_uuid = (
            COSH_AREA_WISE_UUID if measure == "AREA_WISE"
            else COSH_PLANT_WISE_UUID if measure == "PLANT_WISE"
            else None
        )
        if measure_uuid is None:
            raise ValueError(
                f"measure must be 'AREA_WISE' / 'PLANT_WISE' / None, got {measure!r}"
            )
        # Seed the measure Core item idempotently.
        ap_row = (await db.execute(
            select(CoshCoreItem).where(
                CoshCoreItem.cosh_id == measure_uuid,
                CoshCoreItem.core_type == COSH_AREA_PLANT_WISE_CORE,
            )
        )).scalar_one_or_none()
        if ap_row is None:
            db.add(CoshCoreItem(
                cosh_id=measure_uuid, core_type=COSH_AREA_PLANT_WISE_CORE,
                status="active",
                translations={"en": (
                    "Area-wise" if measure == "AREA_WISE" else "Plant-wise"
                )},
            ))
        # Connect this name to the measure.
        ap_connect_id = f"connect:{cosh_id}:measure"
        existing_ap = (await db.execute(
            select(CoshConnectRow).where(
                CoshConnectRow.connect_id == ap_connect_id,
                CoshConnectRow.connect_type == COSH_CROP_AREA_PLANT_CONNECT,
            )
        )).scalar_one_or_none()
        if existing_ap is None:
            db.add(CoshConnectRow(
                connect_id=ap_connect_id,
                connect_type=COSH_CROP_AREA_PLANT_CONNECT,
                status="active",
                endpoints=[
                    {"role": COSH_BIOLOGICAL_NAMES_CORE,
                     "cosh_id": cosh_id, "position": 1},
                    {"role": COSH_AREA_PLANT_WISE_CORE,
                     "cosh_id": measure_uuid, "position": 2},
                ],
            ))

    await db.flush()
    return existing_ref


async def make_client(db: AsyncSession, **kw) -> Client:
    c = Client(
        full_name=kw.get("full_name", "Test Client"),
        short_name=_short("c"),
        ca_name=kw.get("ca_name", "Test CA"),
        ca_phone=kw.get("ca_phone", _short("+91")),
        ca_email=kw.get("ca_email", _short("ca") + "@test.local"),
        payment_model=kw.get("payment_model", PaymentModel.FARMER_PAYS),
    )
    db.add(c)
    await db.flush()
    if not kw.get("skip_auto_link"):
        await _auto_link_client_to_existing_users(db, c)
    return c


async def make_onboarded_dealer(
    db: AsyncSession, *, client: Client | None = None, name: str = "Dealer",
) -> User:
    """Seed a User + UserRole.DEALER + ACTIVE ClientPromoter row.

    Used by tests that operate on /dealer/* endpoints — V1.1 Item 5
    requires the caller to have at least one active Dealer onboarding.
    The ClientPromoter row is pinned to a fresh client by default;
    pass `client=` to attach to an existing one.
    """
    from app.modules.platform.models import RoleType, UserRole
    from app.modules.clients.models import ClientPromoter

    user = await make_user(db, name=name)
    db.add(UserRole(user_id=user.id, role_type=RoleType.DEALER))
    target_client = client if client is not None else await make_client(db)
    db.add(ClientPromoter(
        client_id=target_client.id, user_id=user.id,
        promoter_type="DEALER", status="ACTIVE",
    ))
    await db.flush()
    return user


async def make_onboarded_facilitator(
    db: AsyncSession, *, client: Client | None = None, name: str = "Facilitator",
) -> User:
    """Mirror of `make_onboarded_dealer` for the Facilitator side."""
    from app.modules.platform.models import RoleType, UserRole
    from app.modules.clients.models import ClientPromoter

    user = await make_user(db, name=name)
    db.add(UserRole(user_id=user.id, role_type=RoleType.FACILITATOR))
    target_client = client if client is not None else await make_client(db)
    db.add(ClientPromoter(
        client_id=target_client.id, user_id=user.id,
        promoter_type="FACILITATOR", status="ACTIVE",
    ))
    await db.flush()
    return user


async def make_self_registered_user(
    db: AsyncSession, *, phone: str, role: str, name: str = "Pwa User",
) -> User:
    """Seed a User with phone + a UserRole entry — i.e. someone who
    has self-registered as a Dealer or Facilitator on the PWA.

    Used by tests that exercise the FM `register_promoter` flow:
    post-V1.1 Item 3, the FM endpoint refuses to create users itself
    and 422s if the user hasn't self-claimed the role first. Tests
    that previously called `register_promoter` with a fresh phone
    must now pre-seed via this helper.

    `role` is a string ("DEALER" / "FACILITATOR" / etc) — looked up
    in RoleType to keep the call sites readable."""
    from app.modules.platform.models import RoleType, UserRole

    user = await make_user(db, name=name)
    user.phone = phone
    db.add(UserRole(user_id=user.id, role_type=RoleType[role]))
    await db.flush()
    return user


async def make_client_user(
    db: AsyncSession, *, user: User, client: Client,
    role: ClientUserRole = ClientUserRole.SUBJECT_EXPERT,
    status: StatusEnum = StatusEnum.ACTIVE,
) -> ClientUser:
    """Seed a ClientUser row so the user passes the FarmPundit
    module's membership gate (`_assert_portal_member`). Used by
    tests that exercise the FarmPundit-management endpoints.

    Default role is SUBJECT_EXPERT (Batch J, 2026-05-18) — most
    advisory-authoring tests need an SE to pass
    `_assert_can_edit_client_advisory`. Tests that explicitly need
    a non-SE role (CA / FIELD_MANAGER / REPORT_USER) pass `role=`
    tests, FIELD_MANAGER for Promoter-Pundit tests).

    Batch 39S (2026-05-17): drop any auto-linked REPORT_USER row
    for this (user, client) pair so callers that need a SINGLE
    ClientUser row for the user (e.g. tests asserting on
    `scalar_one_or_none()` semantics in the FarmPundit module) get
    exactly one row, not the auto-linked REPORT_USER plus the
    explicit role they're adding."""
    await db.execute(
        ClientUser.__table__.delete().where(
            ClientUser.client_id == client.id,
            ClientUser.user_id == user.id,
        )
    )
    cu = ClientUser(
        client_id=client.id, user_id=user.id, role=role, status=status,
    )
    db.add(cu)
    await db.flush()
    return cu


async def make_cm_assignment(
    db: AsyncSession, *, user: User, client: Client,
    rights: CMRights = CMRights.EDIT,
    status: StatusEnum = StatusEnum.ACTIVE,
) -> CMClientAssignment:
    """Seed an active CMClientAssignment so the user passes
    `_assert_cm_can_edit_client` for `client`. Used by tests that
    exercise the Global → Local import endpoints (fork_global_package,
    import_global_pg, import_pg_into_sp)."""
    assignment = CMClientAssignment(
        cm_user_id=user.id, client_id=client.id,
        rights=rights, status=status,
    )
    db.add(assignment)
    await db.flush()
    return assignment


async def make_package(db: AsyncSession, client: Client, **kw) -> Package:
    """Create a Package row.

    Mirrors the production invariants enforced by the live router so
    tests don't have to opt in to each one:

    - Batch 1C: every Package has a matching ACTIVE ClientCrop row
      (idempotent insert).
    - Batch 2C: Package can publish — has at least one
      PackageLocation and at least one PackageAuthor. The factory
      auto-creates a default Subject Expert + ClientUser row + author
      link so the publish gate is satisfied out of the box.

    Tests that exercise the unset-fields edge cases (e.g. CCA Step 2
    Batch 2C tests for `no_locations` / `no_authors`) bypass this
    factory and create Packages via the API endpoints directly.
    """
    crop_cosh_id = kw.get("crop_cosh_id", "crop:test")
    existing_cc = (await db.execute(
        select(ClientCrop).where(
            ClientCrop.client_id == client.id,
            ClientCrop.crop_cosh_id == crop_cosh_id,
        )
    )).scalar_one_or_none()
    if existing_cc is None:
        db.add(ClientCrop(client_id=client.id, crop_cosh_id=crop_cosh_id))
        await db.flush()

    p = Package(
        client_id=client.id,
        crop_cosh_id=crop_cosh_id,
        name=kw.get("name", "Test PoP"),
        package_type=PackageType.ANNUAL,
        duration_days=120,
        start_date_label_cosh_id=kw.get(
            "start_date_label_cosh_id", "label:sowing_date",
        ),
        status=PackageStatus.ACTIVE,
    )
    db.add(p)
    await db.flush()

    # Unique district per Package so two factory-created Packages
    # under the same client don't trip the §4.2 shared-district rule
    # (Batch 2D / 2E / 2C). Tests that specifically need shared
    # districts set them explicitly via the API.
    db.add(PackageLocation(
        package_id=p.id,
        state_cosh_id="state:test",
        district_cosh_id=_short("district:test:"),
    ))

    se = User(phone=_short("+91"), name="Test SE")
    db.add(se)
    await db.flush()
    db.add(ClientUser(
        client_id=client.id, user_id=se.id,
        role=ClientUserRole.SUBJECT_EXPERT,
        status=StatusEnum.ACTIVE,
    ))
    db.add(PackageAuthor(package_id=p.id, user_id=se.id))
    await db.flush()
    return p


async def make_subscription(
    db: AsyncSession, *, farmer: User, client: Client, package: Package, **kw,
) -> Subscription:
    s = Subscription(
        farmer_user_id=farmer.id,
        client_id=client.id,
        package_id=package.id,
        subscription_type=SubscriptionType.SELF,
        status=SubscriptionStatus.ACTIVE,
    )
    db.add(s)
    await db.flush()
    return s


async def make_package_location(
    db: AsyncSession, package: Package, *,
    state_cosh_id: str = "state:test",
    district_cosh_id: str = "district:test",
) -> PackageLocation:
    pl = PackageLocation(
        package_id=package.id,
        state_cosh_id=state_cosh_id,
        district_cosh_id=district_cosh_id,
    )
    db.add(pl)
    await db.flush()
    return pl


async def make_parameter(
    db: AsyncSession, *,
    crop_cosh_id: str = "crop:test", name: str = "Param",
    display_order: int = 0,
) -> Parameter:
    p = Parameter(
        crop_cosh_id=crop_cosh_id,
        name=name,
        source=ParameterSource.COSH,
        display_order=display_order,
    )
    db.add(p)
    await db.flush()
    return p


async def make_variable(
    db: AsyncSession, parameter: Parameter, *, name: str = "Var",
) -> Variable:
    v = Variable(parameter_id=parameter.id, name=name)
    db.add(v)
    await db.flush()
    return v


async def make_package_variable(
    db: AsyncSession, package: Package, parameter: Parameter, variable: Variable,
) -> PackageVariable:
    pv = PackageVariable(
        package_id=package.id, parameter_id=parameter.id, variable_id=variable.id,
    )
    db.add(pv)
    await db.flush()
    return pv


async def make_timeline(
    db: AsyncSession, package: Package, *,
    name: str = "TL", from_type: TimelineFromType = TimelineFromType.DAS,
    from_value: int = 0, to_value: int = 30, display_order: int = 0,
) -> Timeline:
    t = Timeline(
        package_id=package.id, name=_short(name + "_"), from_type=from_type,
        from_value=from_value, to_value=to_value, display_order=display_order,
    )
    db.add(t)
    await db.flush()
    return t


async def make_relation(
    db: AsyncSession, timeline: Timeline, *,
    relation_type: RelationType = RelationType.AND,
) -> Relation:
    r = Relation(
        timeline_id=timeline.id, relation_type=relation_type,
        expression="p1 AND p2",
    )
    db.add(r)
    await db.flush()
    return r


async def make_practice(
    db: AsyncSession, timeline: Timeline, *,
    l0: PracticeL0 = PracticeL0.INPUT, l1: str = "FERTILIZER",
    l2: str | None = "UREA", display_order: int = 0,
    relation: Relation | None = None, relation_role: str | None = None,
    is_special_input: bool = False, frequency_days: int | None = None,
    is_brand_locked: bool = False,
) -> Practice:
    p = Practice(
        timeline_id=timeline.id, l0_type=l0, l1_type=l1, l2_type=l2,
        display_order=display_order,
        relation_id=relation.id if relation else None,
        relation_role=relation_role, is_special_input=is_special_input,
        frequency_days=frequency_days,
        is_brand_locked=is_brand_locked,
    )
    db.add(p)
    await db.flush()
    return p


async def make_element(
    db: AsyncSession, practice: Practice, *,
    element_type: str = "DOSAGE", value: str = "50",
    unit_cosh_id: str = "kg_per_acre", display_order: int = 0,
    cosh_ref: str | None = None,
) -> Element:
    e = Element(
        practice_id=practice.id, element_type=element_type, value=value,
        unit_cosh_id=unit_cosh_id, display_order=display_order,
        cosh_ref=cosh_ref,
    )
    db.add(e)
    await db.flush()
    return e


async def make_conditional_question(
    db: AsyncSession, timeline: Timeline, *,
    text: str = "Is rainfall expected?", display_order: int = 0,
) -> ConditionalQuestion:
    q = ConditionalQuestion(
        timeline_id=timeline.id, question_text=text,
        display_order=display_order,
    )
    db.add(q)
    await db.flush()
    return q


async def make_practice_conditional(
    db: AsyncSession, practice: Practice, question: ConditionalQuestion, *,
    answer: ConditionalAnswer = ConditionalAnswer.YES,
) -> PracticeConditional:
    pc = PracticeConditional(
        practice_id=practice.id, question_id=question.id, answer=answer,
    )
    db.add(pc)
    await db.flush()
    return pc


# ── CHA helpers ─────────────────────────────────────────────────────────────

async def make_pg_recommendation(db: AsyncSession, **kw) -> PGRecommendation:
    pg = PGRecommendation(
        problem_group_cosh_id=kw.get("problem_group_cosh_id", "pg:test"),
        client_id=kw.get("client_id"),
        area_or_plant=kw.get("area_or_plant", "AREA_WISE"),
    )
    db.add(pg)
    await db.flush()
    return pg


# Batch 39O (2026-05-16): PG/SP timelines + practices + elements live
# in the shared `Timeline` / `Practice` / `Element` tables, polymorphic
# on which parent FK is set. The factories below set `pg_recommendation_id`
# or `sp_recommendation_id` on Timeline and let Practice/Element flow
# through the same builders CCA uses.

async def make_pg_timeline(
    db: AsyncSession, pg_rec: PGRecommendation, *,
    name: str = "PG-TL", from_value: int = 0, to_value: int = 7,
    from_type: str = "DAYS_AFTER_DETECTION",
) -> Timeline:
    t = Timeline(
        pg_recommendation_id=pg_rec.id, name=_short(name + "_"),
        from_type=from_type, from_value=from_value, to_value=to_value,
    )
    db.add(t)
    await db.flush()
    return t


async def make_pg_practice(db: AsyncSession, tl: Timeline, **kw) -> Practice:
    p = Practice(
        timeline_id=tl.id, l0_type=kw.get("l0_type", "INPUT"),
        l1_type=kw.get("l1_type", "PESTICIDE"),
        display_order=kw.get("display_order", 0),
    )
    db.add(p)
    await db.flush()
    return p


async def make_pg_element(db: AsyncSession, prac: Practice, **kw) -> Element:
    e = Element(
        practice_id=prac.id, element_type=kw.get("element_type", "DOSAGE"),
        value=kw.get("value", "1"),
        cosh_ref=kw.get("cosh_ref"),
    )
    db.add(e)
    await db.flush()
    return e


async def make_sp_recommendation(
    db: AsyncSession, client: Client, **kw,
) -> SPRecommendation:
    sp = SPRecommendation(
        specific_problem_cosh_id=kw.get("specific_problem_cosh_id", "sp:test"),
        client_id=client.id,
        crop_cosh_id=kw.get("crop_cosh_id", "crop:test"),
    )
    db.add(sp)
    await db.flush()
    return sp


async def make_sp_timeline(
    db: AsyncSession, sp_rec: SPRecommendation, *,
    name: str = "SP-TL", from_value: int = 0, to_value: int = 7,
    from_type: str = "DAYS_AFTER_DETECTION",
) -> Timeline:
    t = Timeline(
        sp_recommendation_id=sp_rec.id, name=_short(name + "_"),
        from_type=from_type, from_value=from_value, to_value=to_value,
    )
    db.add(t)
    await db.flush()
    return t


async def make_sp_practice(db: AsyncSession, tl: Timeline, **kw) -> Practice:
    p = Practice(
        timeline_id=tl.id, l0_type=kw.get("l0_type", "INPUT"),
        l1_type=kw.get("l1_type", "PESTICIDE"),
        display_order=kw.get("display_order", 0),
    )
    db.add(p)
    await db.flush()
    return p


async def make_sp_element(db: AsyncSession, prac: Practice, **kw) -> Element:
    e = Element(
        practice_id=prac.id, element_type=kw.get("element_type", "DOSAGE"),
        value=kw.get("value", "1"),
        cosh_ref=kw.get("cosh_ref"),
    )
    db.add(e)
    await db.flush()
    return e


# ── Push scaffolding (Batch 39N-a, 2026-05-16) ────────────────────────
# The form-driven push gate validates name uniqueness, onboarded
# locations, catalogue PVs, and ACTIVE SE authors at request time, so
# every push test needs a populated client. This helper seeds the
# minimum scaffolding and returns the matching `PackagePushRequest`
# kwargs ready to pass into `push_global_package`.

async def make_push_request_body(
    db: AsyncSession, *,
    client: Client, src: Package,
    name: str = "Pushed PoP",
    description: str | None = None,
    start_date_label_cosh_id: str | None = None,
) -> dict:
    """Seed an ACTIVE ClientLocation, an ACTIVE SE ClientUser, and a
    catalogue Parameter+Variable for the src's crop, then return a
    dict shaped for `PackagePushRequest`. Each call returns fresh ids
    so tests can stack multiple pushes without scaffolding collision.
    """
    state = "state:test"
    district = _short("district:test:")
    db.add(ClientLocation(
        client_id=client.id, state_cosh_id=state, district_cosh_id=district,
        status=StatusEnum.ACTIVE,
    ))
    se_user = await make_user(db, name="Push SE", skip_auto_link=True)
    db.add(ClientUser(
        client_id=client.id, user_id=se_user.id,
        role=ClientUserRole.SUBJECT_EXPERT, status=StatusEnum.ACTIVE,
    ))
    param = await make_parameter(
        db, crop_cosh_id=src.crop_cosh_id, name=_short("Param "),
    )
    variable = await make_variable(db, param, name=_short("Var "))
    await db.flush()
    return {
        "name": name,
        "description": description,
        "start_date_label_cosh_id": (
            start_date_label_cosh_id or src.start_date_label_cosh_id
            or "label:sowing_date"
        ),
        "locations": [
            {"state_cosh_id": state, "district_cosh_id": district},
        ],
        "pv_assignments": [
            {"parameter_id": param.id, "variable_id": variable.id},
        ],
        "author_ids": [se_user.id],
    }
