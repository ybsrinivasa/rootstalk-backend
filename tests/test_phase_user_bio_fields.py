"""Batch D+E (2026-05-18) — author bio fields move from PackageAuthor
to User.

Per user 2026-05-18: "designation and professional profile is created
only for the SE role" — these fields are captured in User Management
on the CA Portal and persist on User (not per-package). The Authors
panel joins them in at read time so the farmer PWA always sees the
same bio for the same SE regardless of which package surfaced them.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.modules.advisory.router import (
    list_package_authors, set_package_authors,
)
from app.modules.advisory.schemas import PackageAuthorIn
from app.modules.clients.models import (
    ClientUser, ClientUserRole,
)
from app.modules.clients.router import (
    add_portal_user, list_portal_users, update_portal_user,
)
from app.modules.clients.schemas import (
    PortalUserCreate, PortalUserUpdate,
)
from app.modules.platform.models import StatusEnum, User
from tests.conftest import requires_docker
from tests.factories import make_client, make_client_user, make_user


# ── User Management: create + update with bio fields ────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_add_portal_user_persists_bio_fields(db):
    client = await make_client(db)
    ca = await make_user(db, name="CA", skip_auto_link=True)
    await db.commit()

    out = await add_portal_user(
        client_id=client.id,
        request=PortalUserCreate(
            email="se-new@kingcorp.example.com",
            name="Dr Suresh",
            password="initial_pw",
            role=ClientUserRole.SUBJECT_EXPERT,
            designation="Senior Agronomist",
            professional_profile="25 years in Karnataka rice cultivation",
        ),
        db=db, current_user=ca,
    )
    assert out.designation == "Senior Agronomist"
    assert out.professional_profile == "25 years in Karnataka rice cultivation"

    # And the row landed on User, not PackageAuthor.
    user = (await db.execute(
        select(User).where(User.email == "se-new@kingcorp.example.com")
    )).scalar_one()
    assert user.designation == "Senior Agronomist"
    assert user.professional_profile == "25 years in Karnataka rice cultivation"


@requires_docker
@pytest.mark.asyncio
async def test_update_portal_user_edits_bio_fields(db):
    client = await make_client(db)
    ca = await make_user(db, name="CA", skip_auto_link=True)
    target = await make_user(db, name="SE", skip_auto_link=True)
    target.email = "se-target@kingcorp.example.com"
    await make_client_user(
        db, user=target, client=client, role=ClientUserRole.SUBJECT_EXPERT,
    )
    await db.commit()

    out = await update_portal_user(
        client_id=client.id, user_id=target.id,
        request=PortalUserUpdate(
            designation="Lead Scientist",
            professional_profile="Author of 12 peer-reviewed papers on IPM",
        ),
        db=db, current_user=ca,
    )
    assert out.designation == "Lead Scientist"
    assert out.professional_profile == "Author of 12 peer-reviewed papers on IPM"


@requires_docker
@pytest.mark.asyncio
async def test_update_portal_user_404_non_member(db):
    """Update endpoint refuses cross-client edits — user must be an
    ACTIVE member of the path's client."""
    client_a = await make_client(db)
    client_b = await make_client(db)
    ca_a = await make_user(db, name="CA-A", skip_auto_link=True)
    target = await make_user(db, name="SE-B", skip_auto_link=True)
    await make_client_user(
        db, user=target, client=client_b, role=ClientUserRole.SUBJECT_EXPERT,
    )
    await db.commit()
    with pytest.raises(HTTPException) as exc:
        await update_portal_user(
            client_id=client_a.id, user_id=target.id,
            request=PortalUserUpdate(designation="X"),
            db=db, current_user=ca_a,
        )
    assert exc.value.status_code == 404


@requires_docker
@pytest.mark.asyncio
async def test_list_portal_users_returns_bio_fields(db):
    client = await make_client(db)
    ca = await make_user(db, name="CA", skip_auto_link=True)
    se = await make_user(db, name="SE", skip_auto_link=True)
    se.email = "se-listed@kingcorp.example.com"
    se.designation = "Specialist"
    se.professional_profile = "Mango grafting expert"
    await make_client_user(
        db, user=se, client=client, role=ClientUserRole.SUBJECT_EXPERT,
    )
    await db.commit()
    out = await list_portal_users(
        client_id=client.id, db=db, current_user=ca,
    )
    found = [u for u in out if u.id == se.id]
    assert len(found) == 1
    assert found[0].designation == "Specialist"
    assert found[0].professional_profile == "Mango grafting expert"


# ── PackageAuthor: bio fields join from User ────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_list_package_authors_joins_bio_from_user(db):
    """PackageAuthorOut surfaces designation + professional_profile
    from User.* — not from the package_authors table (those columns
    were dropped in Batch E)."""
    from tests.test_phase_cca_step2_integration import (
        _create_test_package, _seed_paddy_on_belt, _make_subject_expert,
    )

    client = await make_client(db)
    ca = await make_user(db, name="CA")
    await _seed_paddy_on_belt(db, client, ca)
    pkg = await _create_test_package(db, client=client, user=ca)
    se = await _make_subject_expert(
        db, client=client, name="Dr A",
        designation="Lead Scientist",
        professional_profile="20 years in pest management",
    )
    await db.commit()

    await set_package_authors(
        client_id=client.id, package_id=pkg.id,
        authors=[PackageAuthorIn(user_id=se.id, display_order=0)],
        db=db, current_user=ca,
    )
    listed = await list_package_authors(
        client_id=client.id, package_id=pkg.id, db=db, current_user=ca,
    )
    assert len(listed) == 1
    # Bio surfaces from the User row, not the PackageAuthor row.
    assert listed[0].designation == "Lead Scientist"
    assert listed[0].professional_profile == "20 years in pest management"


@requires_docker
@pytest.mark.asyncio
async def test_changing_user_bio_updates_existing_authors(db):
    """Because PackageAuthor.* is now a join-shape, editing User
    bio fields automatically refreshes EVERY package's display of
    that author — without per-package re-save."""
    from tests.test_phase_cca_step2_integration import (
        _create_test_package, _seed_paddy_on_belt, _make_subject_expert,
    )

    client = await make_client(db)
    ca = await make_user(db, name="CA")
    await _seed_paddy_on_belt(db, client, ca)
    pkg = await _create_test_package(db, client=client, user=ca)
    se = await _make_subject_expert(
        db, client=client, name="Dr A", designation="Old Title",
    )
    await db.commit()
    await set_package_authors(
        client_id=client.id, package_id=pkg.id,
        authors=[PackageAuthorIn(user_id=se.id, display_order=0)],
        db=db, current_user=ca,
    )

    # Update bio via User Management endpoint.
    await update_portal_user(
        client_id=client.id, user_id=se.id,
        request=PortalUserUpdate(designation="New Title"),
        db=db, current_user=ca,
    )
    listed = await list_package_authors(
        client_id=client.id, package_id=pkg.id, db=db, current_user=ca,
    )
    # Already-set author row picks up the new designation
    # without a re-save of the Authors list.
    assert listed[0].designation == "New Title"
