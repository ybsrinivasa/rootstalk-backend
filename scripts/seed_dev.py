"""
Development seed data — loads complete demo dataset from RootsTalk_SeedData_Document.pdf §Part 4.
Run with: python scripts/seed_dev.py
Only for development/demo environments. Never run in staging or production.
"""
import asyncio
import secrets
import sys
import os
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy import select, text

from app.config import settings
from app.modules.platform.models import User, UserRole, RoleType, StatusEnum
from app.modules.clients.models import (
    Client, ClientUser, ClientUserRole, ClientStatus,
    ClientLocation, ClientCrop,
)
from app.modules.advisory.models import (
    Package, Timeline, Practice, Element,
    PackageStatus, PackageType, TimelineFromType, PracticeL0,
)
from app.modules.subscriptions.models import (
    Subscription, SubscriptionStatus, SubscriptionType, FarmerSubscriptionHistory,
)
from app.modules.auth.service import hash_password


def utcnow():
    return datetime.now(timezone.utc)


def new_uuid():
    import uuid
    return str(uuid.uuid4())


async def seed():
    engine = create_async_engine(settings.database_url)
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with async_session() as db:
        print("🌱  RootsTalk dev seed starting…")

        # ── Check if already seeded ──────────────────────────────────────────
        existing = (await db.execute(
            select(User).where(User.phone == "+919900000001")
        )).scalar_one_or_none()
        if existing:
            print("⚠️   Seed data already exists. Run with --force to re-seed.")
            if "--force" not in sys.argv:
                return
            print("🔄  --force flag detected. Clearing existing seed data…")
            await db.execute(text("DELETE FROM farmer_subscriptions_history"))
            await db.execute(text("DELETE FROM subscriptions"))
            await db.execute(text("DELETE FROM elements"))
            await db.execute(text("DELETE FROM practices"))
            await db.execute(text("DELETE FROM timelines"))
            await db.execute(text("DELETE FROM packages WHERE client_id IN (SELECT id FROM clients WHERE short_name='padmashali')"))
            await db.execute(text("DELETE FROM client_crops WHERE client_id IN (SELECT id FROM clients WHERE short_name='padmashali')"))
            await db.execute(text("DELETE FROM client_locations WHERE client_id IN (SELECT id FROM clients WHERE short_name='padmashali')"))
            await db.execute(text("DELETE FROM client_users WHERE client_id IN (SELECT id FROM clients WHERE short_name='padmashali')"))
            await db.execute(text("DELETE FROM clients WHERE short_name='padmashali'"))
            await db.execute(text("DELETE FROM user_roles WHERE user_id IN (SELECT id FROM users WHERE phone LIKE '+9199000000%')"))
            await db.execute(text("DELETE FROM users WHERE phone LIKE '+9199000000%'"))
            await db.commit()

        # ── Section 4.1: 5 Test Users ────────────────────────────────────────
        print("  Creating 5 test users…")

        users_data = [
            ("+919900000001", "Ramu Krishnaswamy",       RoleType.FARMER,      None),
            ("+919900000002", "Suresh Kumar Agro Store", RoleType.DEALER,      None),
            ("+919900000003", "Anitha Nagaraj",          RoleType.FACILITATOR, None),
            ("+919900000004", "Dr. Venkatesh Rao",       RoleType.FARM_PUNDIT, None),
            ("+919900000005", "Padmashali Seeds CA",     RoleType.FARMER,      "padmashali-ca@padmashali-seeds.in"),
        ]

        user_objs: dict[str, User] = {}
        for phone, name, role, email in users_data:
            user = User(
                id=new_uuid(), phone=phone, name=name, email=email,
                language_code="kn" if role == RoleType.FARMER else "en",
            )
            db.add(user)
            await db.flush()
            db.add(UserRole(id=new_uuid(), user_id=user.id, role_type=RoleType.FARMER))
            if role != RoleType.FARMER:
                db.add(UserRole(id=new_uuid(), user_id=user.id, role_type=role))
            user_objs[phone] = user

        farmer = user_objs["+919900000001"]
        ca_user = user_objs["+919900000005"]

        print("  ✓ 5 test users created")

        # ── Section 4.2: Test Client Company ──────────────────────────────────
        print("  Creating Padmashali Seeds company…")

        client_id = new_uuid()
        client = Client(
            id=client_id,
            full_name="Padmashali Seeds and Agro Private Limited",
            short_name="padmashali",
            display_name="Padmashali Seeds",
            tagline="Growing trust, field by field",
            primary_colour="#1B4332",
            secondary_colour="#854F0B",
            hq_address="42, Industrial Area, Mysuru, Karnataka 570010",
            website="https://padmashali-seeds.in",
            gst_number="29ABCDE1234F1Z5",
            pan_number="ABCDE1234F",
            is_manufacturer=True,
            status=ClientStatus.ACTIVE,
            ca_name="Padmashali Seeds CA",
            ca_phone="+919900000005",
            ca_email="padmashali-ca@padmashali-seeds.in",
            approved_at=utcnow(),
            approved_by=None,
        )
        db.add(client)

        # CA user → ClientUser role
        db.add(ClientUser(
            id=new_uuid(), client_id=client_id, user_id=ca_user.id,
            role=ClientUserRole.CA, status=StatusEnum.ACTIVE,
        ))

        # Location: Karnataka / Mysuru
        db.add(ClientLocation(
            id=new_uuid(), client_id=client_id,
            state_cosh_id="state_karnataka",
            district_cosh_id="district_mysuru",
        ))

        # Crop: Paddy
        db.add(ClientCrop(
            id=new_uuid(), client_id=client_id,
            crop_cosh_id="crop_paddy",
        ))

        await db.flush()
        print("  ✓ Padmashali Seeds company created")

        # ── Section 4.4: Test Package of Practices ────────────────────────────
        print("  Creating Paddy Kharif Season test package…")

        package_id = new_uuid()
        package = Package(
            id=package_id,
            client_id=client_id,
            crop_cosh_id="crop_paddy",
            name="Paddy — Kharif Season (Test Package)",
            package_type=PackageType.ANNUAL,
            duration_days=120,
            start_date_label_cosh_id="sdl_sowing_date",
            description="Standard Package of Practices for Kharif Paddy cultivation in Karnataka. Created for development testing.",
            status=PackageStatus.ACTIVE,
            version=1,
            published_at=utcnow(),
            published_by=ca_user.id,
            created_by=ca_user.id,
        )
        db.add(package)
        await db.flush()

        # Timeline 1: Pre-Sowing Preparation (DBS Day 15 → Day 8)
        tl1_id = new_uuid()
        tl1 = Timeline(
            id=tl1_id,
            package_id=package_id,
            name="Pre-Sowing Preparation",
            from_type=TimelineFromType.DBS,
            from_value=15,
            to_value=8,
            display_order=1,
        )
        db.add(tl1)
        await db.flush()

        p1 = Practice(
            id=new_uuid(), timeline_id=tl1_id,
            l0_type=PracticeL0.INSTRUCTION, l1_type="general_instructions",
            display_order=1, is_special_input=False,
        )
        db.add(p1)
        await db.flush()
        db.add(Element(
            id=new_uuid(), practice_id=p1.id,
            element_type="TITLE", value="Soil Test and Preparation", display_order=1,
        ))
        db.add(Element(
            id=new_uuid(), practice_id=p1.id,
            element_type="INSTRUCTIONS",
            value="Conduct a soil test 15 days before sowing. Apply recommended lime or gypsum based on soil pH results. Deep plough to 20 cm depth. Form raised beds in waterlogged areas.",
            display_order=2,
        ))

        # Timeline 2: Early Vegetative Stage (DAS Day 20 → Day 35)
        tl2_id = new_uuid()
        tl2 = Timeline(
            id=tl2_id,
            package_id=package_id,
            name="Early Vegetative Stage",
            from_type=TimelineFromType.DAS,
            from_value=20,
            to_value=35,
            display_order=2,
        )
        db.add(tl2)
        await db.flush()

        # Input practice: Chlorpyrifos
        p2 = Practice(
            id=new_uuid(), timeline_id=tl2_id,
            l0_type=PracticeL0.INPUT, l1_type="pesticides", l2_type="chemical_pesticides",
            display_order=1, is_special_input=False,
        )
        db.add(p2)
        await db.flush()
        db.add(Element(
            id=new_uuid(), practice_id=p2.id, element_type="COMMON_NAME",
            cosh_ref="cosh_chlorpyrifos", value="Chlorpyrifos", display_order=1,
        ))
        db.add(Element(
            id=new_uuid(), practice_id=p2.id, element_type="APPLICATION_METHOD",
            value="Foliar Spray", display_order=2,
        ))
        db.add(Element(
            id=new_uuid(), practice_id=p2.id, element_type="DOSAGE",
            value="2.5", unit_cosh_id="ml_per_L", display_order=3,
        ))
        db.add(Element(
            id=new_uuid(), practice_id=p2.id, element_type="INSTRUCTIONS",
            value="Apply during early morning or evening. Avoid spraying during flowering. Ensure complete coverage of leaf surface.",
            display_order=4,
        ))

        # Instruction: Field Monitoring
        p3 = Practice(
            id=new_uuid(), timeline_id=tl2_id,
            l0_type=PracticeL0.INSTRUCTION, l1_type="general_instructions",
            display_order=2, is_special_input=False,
        )
        db.add(p3)
        await db.flush()
        db.add(Element(
            id=new_uuid(), practice_id=p3.id, element_type="TITLE",
            value="Field Monitoring", display_order=1,
        ))
        db.add(Element(
            id=new_uuid(), practice_id=p3.id, element_type="INSTRUCTIONS",
            value="Walk through the field every 3 days. Check the lower surface of leaves for egg masses and early instar larvae. Record observations.",
            display_order=2,
        ))

        await db.flush()
        print("  ✓ Package with 2 timelines and practices created")

        # ── Section 4.5: Test Subscription ────────────────────────────────────
        print("  Creating test subscription for Ramu Krishnaswamy…")

        today = datetime.now(timezone.utc)
        crop_start = today + timedelta(days=10)  # DBS timelines visible immediately

        sub = Subscription(
            id=new_uuid(),
            reference_number="PDPD26000001",
            farmer_user_id=farmer.id,
            client_id=client_id,
            package_id=package_id,
            subscription_type=SubscriptionType.SELF,
            status=SubscriptionStatus.ACTIVE,
            crop_start_date=crop_start,
            subscription_date=today,
        )
        db.add(sub)
        await db.flush()

        db.add(FarmerSubscriptionHistory(
            id=new_uuid(),
            subscription_id=sub.id,
            parameter_variable_summary="Kharif • Irrigated",
        ))

        await db.commit()
        print("  ✓ Test subscription created (crop starts in 10 days)")

        print()
        print("✅  Dev seed complete!")
        print()
        print("Test accounts (OTP bypass: 123456 — set BYPASS_OTP_PHONES in .env):")
        print("  Farmer:       +919900000001 — Ramu Krishnaswamy")
        print("  Dealer:       +919900000002 — Suresh Kumar Agro Store")
        print("  Facilitator:  +919900000003 — Anitha Nagaraj")
        print("  FarmPundit:   +919900000004 — Dr. Venkatesh Rao")
        print("  Client CA:    +919900000005 / padmashali-ca@padmashali-seeds.in")
        print()
        print("Client Portal login: short_name = padmashali")
        print("Company Primary Colour: #1B4332")
        print()


if __name__ == "__main__":
    asyncio.run(seed())
