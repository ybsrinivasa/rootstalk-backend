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

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.advisory.models import (
    ConditionalAnswer, ConditionalQuestion, Element, PGElement, PGPractice,
    PGRecommendation, PGTimeline, Package, PackageLocation, PackageStatus,
    PackageType, PackageVariable, Parameter, ParameterSource,
    Practice, PracticeConditional, PracticeL0, Relation, RelationType,
    SPElement, SPPractice, SPRecommendation, SPTimeline, Timeline,
    TimelineFromType, Variable,
)
from app.modules.clients.models import Client
from app.modules.platform.models import User
from app.modules.subscriptions.models import (
    Subscription, SubscriptionStatus, SubscriptionType,
)


def _short(prefix: str) -> str:
    return prefix + uuid.uuid4().hex[:6]


async def make_user(db: AsyncSession, **kw) -> User:
    u = User(phone=_short("+91"), name=kw.get("name", "Test Farmer"))
    db.add(u)
    await db.flush()
    return u


async def make_client(db: AsyncSession, **kw) -> Client:
    c = Client(
        full_name=kw.get("full_name", "Test Client"),
        short_name=_short("c"),
        ca_name=kw.get("ca_name", "Test CA"),
        ca_phone=kw.get("ca_phone", _short("+91")),
        ca_email=kw.get("ca_email", _short("ca") + "@test.local"),
    )
    db.add(c)
    await db.flush()
    return c


async def make_package(db: AsyncSession, client: Client, **kw) -> Package:
    p = Package(
        client_id=client.id,
        crop_cosh_id=kw.get("crop_cosh_id", "crop:test"),
        name=kw.get("name", "Test PoP"),
        package_type=PackageType.ANNUAL,
        duration_days=120,
        status=PackageStatus.ACTIVE,
    )
    db.add(p)
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
    )
    db.add(e)
    await db.flush()
    return e
