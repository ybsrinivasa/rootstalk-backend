"""Client.payment_model — Spec §11.1 enforcement tests.

Covers both layers landing in the same batch:

1. SA `init_onboarding` requires `payment_model` and persists it.
2. Subscription create-self gate: SELF subscriptions are rejected
   for COMPANY_PAYS clients (only ASSIGNED via Promoter is allowed),
   accepted for FARMER_PAYS clients.

Spec reference: Agriculture Team Document v5 §11.1 Subscription
Configurations.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.config import settings
from app.modules.clients.models import Client, ClientStatus, PaymentModel
from app.modules.clients.router import initiate_onboarding
from app.modules.clients.schemas import ClientInitiate
from app.modules.subscriptions.router import (
    SubscribeRequest, create_subscription,
)
from app.modules.advisory.models import Package, PackageType
from tests.conftest import requires_docker
from tests.factories import make_client, make_crop_reference, make_user


# ── init_onboarding requires payment_model ──────────────────────────────────

def test_client_initiate_schema_rejects_missing_payment_model():
    """The Pydantic schema makes the field mandatory — a request without
    it fails before reaching the route at all."""
    with pytest.raises(ValidationError) as ei:
        ClientInitiate(
            full_name="Test Co",
            short_name="testco",
            ca_name="Test CA",
            ca_phone="+919000000000",
            ca_email="test@example.com",
            is_manufacturer=False,
        )
    errs = ei.value.errors()
    assert any(e["loc"] == ("payment_model",) for e in errs)


@requires_docker
@pytest.mark.asyncio
async def test_init_onboarding_persists_company_pays(db, monkeypatch):
    """SA picks Company Pays — value lands on the Client row."""
    sa = await make_user(db, name="SA")
    sa.email = "yb@eywa.farm"
    monkeypatch.setattr(settings, "sa_email", "yb@eywa.farm")
    monkeypatch.setattr(settings, "frontend_base_url", "https://rstalk-ca.eywa.farm")
    monkeypatch.setattr(settings, "environment", "staging")
    monkeypatch.setattr(settings, "email_smtp_user", "")  # disables real email
    await db.commit()

    out = await initiate_onboarding(
        request=ClientInitiate(
            full_name="Company Pays Co",
            short_name="cpco",
            ca_name="CP CA",
            ca_phone="+919111111111",
            ca_email="cpco@example.com",
            is_manufacturer=False,
            payment_model=PaymentModel.COMPANY_PAYS,
        ),
        db=db, current_user=sa,
    )

    from sqlalchemy import select
    client = (await db.execute(
        select(Client).where(Client.id == out.client_id)
    )).scalar_one()
    assert client.payment_model == PaymentModel.COMPANY_PAYS


@requires_docker
@pytest.mark.asyncio
async def test_init_onboarding_persists_farmer_pays(db, monkeypatch):
    """Same but with Farmer Pays."""
    sa = await make_user(db, name="SA")
    sa.email = "yb@eywa.farm"
    monkeypatch.setattr(settings, "sa_email", "yb@eywa.farm")
    monkeypatch.setattr(settings, "frontend_base_url", "https://rstalk-ca.eywa.farm")
    monkeypatch.setattr(settings, "environment", "staging")
    monkeypatch.setattr(settings, "email_smtp_user", "")
    await db.commit()

    out = await initiate_onboarding(
        request=ClientInitiate(
            full_name="Farmer Pays Co",
            short_name="fpco",
            ca_name="FP CA",
            ca_phone="+919222222222",
            ca_email="fpco@example.com",
            is_manufacturer=False,
            payment_model=PaymentModel.FARMER_PAYS,
        ),
        db=db, current_user=sa,
    )

    from sqlalchemy import select
    client = (await db.execute(
        select(Client).where(Client.id == out.client_id)
    )).scalar_one()
    assert client.payment_model == PaymentModel.FARMER_PAYS


# ── Subscription gate ───────────────────────────────────────────────────────

async def _seed_package(db, client) -> Package:
    """Minimal Package row so SubscribeRequest can target a real id."""
    await make_crop_reference(db, "crop:test")
    pkg = Package(
        client_id=client.id,
        crop_cosh_id="crop:test",
        name="Test PoP",
        package_type=PackageType.ANNUAL,
        duration_days=120,
        start_date_label_cosh_id="label:sowing_date",
    )
    db.add(pkg)
    await db.flush()
    return pkg


@requires_docker
@pytest.mark.asyncio
async def test_self_subscribe_rejected_for_company_pays_client(db):
    """Spec §11.1: under Company Pays, farmers cannot self-subscribe.
    Only Promoters can assign on behalf of the company."""
    farmer = await make_user(db, name="Farmer")
    client = await make_client(db, payment_model=PaymentModel.COMPANY_PAYS)
    pkg = await _seed_package(db, client)
    await db.commit()

    with pytest.raises(HTTPException) as ei:
        await create_subscription(
            request=SubscribeRequest(
                package_id=pkg.id,
                client_id=client.id,
                subscription_type="SELF",
            ),
            db=db, current_user=farmer,
        )
    assert ei.value.status_code == 422
    body = ei.value.detail
    assert body["code"] == "self_subscribe_not_allowed"
    assert body["client_payment_model"] == "COMPANY_PAYS"


@requires_docker
@pytest.mark.asyncio
async def test_self_subscribe_allowed_for_farmer_pays_client(db):
    """Same payload, FARMER_PAYS client — gate passes, subscription
    created in WAITLISTED state pending Razorpay confirmation."""
    farmer = await make_user(db, name="Farmer")
    client = await make_client(db, payment_model=PaymentModel.FARMER_PAYS)
    pkg = await _seed_package(db, client)
    await db.commit()

    out = await create_subscription(
        request=SubscribeRequest(
            package_id=pkg.id,
            client_id=client.id,
            subscription_type="SELF",
        ),
        db=db, current_user=farmer,
    )
    assert out["status"] == "WAITLISTED"


@requires_docker
@pytest.mark.asyncio
async def test_self_subscribe_404_when_client_missing(db):
    """Defensive: bad client_id resolves to 404, not a confusing 422."""
    farmer = await make_user(db, name="Farmer")
    await db.commit()

    with pytest.raises(HTTPException) as ei:
        await create_subscription(
            request=SubscribeRequest(
                package_id="some-pkg",
                client_id="nonexistent",
                subscription_type="SELF",
            ),
            db=db, current_user=farmer,
        )
    assert ei.value.status_code == 404
