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
    ConditionalAnswer, ConditionalQuestion, Element, PGElement, PGPractice,
    PGRecommendation, PGTimeline, Package, PackageAuthor, PackageLocation,
    PackageStatus, PackageType, PackageVariable, Parameter, ParameterSource,
    Practice, PracticeConditional, PracticeL0, Relation, RelationType,
    SPElement, SPPractice, SPRecommendation, SPTimeline, Timeline,
    TimelineFromType, Variable,
)
from app.modules.clients.models import (
    Client, ClientCrop, ClientUser, ClientUserRole,
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
    return u


async def make_crop_reference(
    db: AsyncSession, cosh_id: str, *,
    name: str = "Paddy", scientific_name: str | None = None,
    measure: str = "AREA_WISE", status: str = "active",
) -> tuple[CoshCoreItem, CropMeasure]:
    """Seed a Cosh biological_name + Crop classification + measure.

    Mirrors the post 2026-05-09 live-sync shape:
      • CoshCoreItem(core_type='biological_names', cosh_id=...)
      • CoshConnectRow(connect_type='biological_names_and_roles',
        endpoints=[{role: biological_names, cosh_id: <name>},
                   {role: roles_of_biological_names, cosh_id: CROP_UUID}])
      • CoshCoreItem(core_type='roles_of_biological_names',
        cosh_id=CROP_UUID)  — idempotent
      • CropMeasure(crop_cosh_id=..., measure=...)

    Required when a test exercises CCA Step 1 add_crop. Idempotent on
    cosh_id. The `scientific_name` parameter is accepted for back-
    compat with existing call sites but ignored — V1 doesn't source
    scientific names from Cosh until that Core's Connect ships.
    """
    from app.modules.sync.models import CoshConnectRow
    from app.services.cosh_constants import (
        COSH_BIOLOGICAL_NAMES_CORE, COSH_NAME_ROLE_CONNECT,
        COSH_ROLES_CORE, COSH_ROLE_CROP_UUID,
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

    # Seed the Crop role item — once per test session.
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

    existing_measure = (await db.execute(
        select(CropMeasure).where(CropMeasure.crop_cosh_id == cosh_id)
    )).scalar_one_or_none()
    if existing_measure is None:
        existing_measure = CropMeasure(crop_cosh_id=cosh_id, measure=measure)
        db.add(existing_measure)

    await db.flush()
    return existing_ref, existing_measure


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
    role: ClientUserRole = ClientUserRole.CA,
    status: StatusEnum = StatusEnum.ACTIVE,
) -> ClientUser:
    """Seed a ClientUser row so the user passes the FarmPundit
    module's membership gate (`_assert_portal_member`). Used by
    tests that exercise the FarmPundit-management endpoints.

    Default role is CA — change via `role=` for tests that need a
    specific role (e.g. SUBJECT_EXPERT for standard-response
    tests, FIELD_MANAGER for Promoter-Pundit tests)."""
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
) -> Practice:
    p = Practice(
        timeline_id=timeline.id, l0_type=l0, l1_type=l1, l2_type=l2,
        display_order=display_order,
        relation_id=relation.id if relation else None,
        relation_role=relation_role, is_special_input=is_special_input,
        frequency_days=frequency_days,
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
        application_type="GLOBAL",
    )
    db.add(pg)
    await db.flush()
    return pg


async def make_pg_timeline(
    db: AsyncSession, pg_rec: PGRecommendation, *,
    name: str = "PG-TL", from_value: int = 0, to_value: int = 7,
) -> PGTimeline:
    t = PGTimeline(
        pg_recommendation_id=pg_rec.id, name=_short(name + "_"),
        from_value=from_value, to_value=to_value,
    )
    db.add(t)
    await db.flush()
    return t


async def make_pg_practice(db: AsyncSession, tl: PGTimeline, **kw) -> PGPractice:
    p = PGPractice(
        timeline_id=tl.id, l0_type=kw.get("l0_type", "INPUT"),
        l1_type=kw.get("l1_type", "PESTICIDE"),
        display_order=kw.get("display_order", 0),
    )
    db.add(p)
    await db.flush()
    return p


async def make_pg_element(db: AsyncSession, prac: PGPractice, **kw) -> PGElement:
    e = PGElement(
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
        client_id=client.id, application_type="LOCAL",
    )
    db.add(sp)
    await db.flush()
    return sp


async def make_sp_timeline(
    db: AsyncSession, sp_rec: SPRecommendation, *,
    name: str = "SP-TL", from_value: int = 0, to_value: int = 7,
) -> SPTimeline:
    t = SPTimeline(
        sp_recommendation_id=sp_rec.id, name=_short(name + "_"),
        from_value=from_value, to_value=to_value,
    )
    db.add(t)
    await db.flush()
    return t


async def make_sp_practice(db: AsyncSession, tl: SPTimeline, **kw) -> SPPractice:
    p = SPPractice(
        timeline_id=tl.id, l0_type=kw.get("l0_type", "INPUT"),
        l1_type=kw.get("l1_type", "PESTICIDE"),
        display_order=kw.get("display_order", 0),
    )
    db.add(p)
    await db.flush()
    return p


async def make_sp_element(db: AsyncSession, prac: SPPractice, **kw) -> SPElement:
    e = SPElement(
        practice_id=prac.id, element_type=kw.get("element_type", "DOSAGE"),
        value=kw.get("value", "1"),
        cosh_ref=kw.get("cosh_ref"),
    )
    db.add(e)
    await db.flush()
    return e
