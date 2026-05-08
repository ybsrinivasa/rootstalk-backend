"""Standard Q&A library — L4 of the audit (2026-05-09).

Spec §14.9. Subject Experts curate question/answer pairs for their
company; FarmPundits pick from the library when responding to
farmer queries.

V1 surface (this batch):
- GET    /client/{cid}/standard-responses     (CA-portal list)
- POST   /client/{cid}/standard-responses     (create)
- PUT    /client/{cid}/standard-responses/{id} (edit)
- DELETE /client/{cid}/standard-responses/{id}
- GET    /pundit/standard-responses           (Pundit search; existed)

Answer body for V1 is text + media (JSON list). Timelines /
Practices integration deferred to V1.1 — see audit memory.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.modules.farmpundit.models import StandardResponse
from app.modules.farmpundit.router import (
    create_standard_response, delete_standard_response,
    list_standard_responses, search_standard_responses,
    update_standard_response,
)
from tests.conftest import requires_docker
from tests.factories import make_client, make_client_user, make_user


async def _se_for(db, *, client):
    """Subject Expert (any portal member, in V1 the gate is
    membership-only) for the standard-responses endpoints."""
    user = await make_user(db, name=f"SE-{client.short_name}")
    await make_client_user(db, user=user, client=client)
    return user


# ── create + list ───────────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_create_returns_full_payload(db):
    client = await make_client(db)
    se = await _se_for(db, client=client)
    await db.commit()

    out = await create_standard_response(
        client_id=client.id,
        data={
            "question_text": "Why are leaves yellowing in young paddy?",
            "answer_text": "Likely nitrogen deficiency. Apply urea at 40kg/ha.",
            "crop_cosh_id": "crop:paddy",
        },
        db=db, current_user=se,
    )
    assert out["question_text"].startswith("Why are leaves")
    assert out["answer_text"].startswith("Likely nitrogen")
    assert out["crop_cosh_id"] == "crop:paddy"
    assert out["created_by"] == se.id
    assert out["answer_media"] == []  # serialiser normalises None → []


@requires_docker
@pytest.mark.asyncio
async def test_create_strips_whitespace_and_empties(db):
    """Padding gets trimmed; an all-whitespace answer_text becomes
    None so the listing doesn't render empty quotes."""
    client = await make_client(db)
    se = await _se_for(db, client=client)
    await db.commit()

    out = await create_standard_response(
        client_id=client.id,
        data={
            "question_text": "  Q?  ",
            "answer_text": "   ",
        },
        db=db, current_user=se,
    )
    assert out["question_text"] == "Q?"
    assert out["answer_text"] is None


@requires_docker
@pytest.mark.asyncio
async def test_create_requires_question_text(db):
    client = await make_client(db)
    se = await _se_for(db, client=client)
    await db.commit()

    for bad in (None, "", "   "):
        with pytest.raises(HTTPException) as ei:
            await create_standard_response(
                client_id=client.id,
                data={"question_text": bad},
                db=db, current_user=se,
            )
        assert ei.value.status_code == 422
        assert "question_text" in ei.value.detail


@requires_docker
@pytest.mark.asyncio
async def test_create_validates_media_shape(db):
    """answer_media must be a list of dicts each with a `url`. Bad
    shapes are rejected with 422 instead of being silently persisted."""
    client = await make_client(db)
    se = await _se_for(db, client=client)
    await db.commit()

    # Not a list.
    with pytest.raises(HTTPException) as ei:
        await create_standard_response(
            client_id=client.id,
            data={
                "question_text": "Q?",
                "answer_media": {"url": "http://x"},
            },
            db=db, current_user=se,
        )
    assert ei.value.status_code == 422

    # List with a missing url.
    with pytest.raises(HTTPException) as ei:
        await create_standard_response(
            client_id=client.id,
            data={
                "question_text": "Q?",
                "answer_media": [{"media_type": "IMAGE"}],
            },
            db=db, current_user=se,
        )
    assert ei.value.status_code == 422


@requires_docker
@pytest.mark.asyncio
async def test_create_persists_media_list(db):
    client = await make_client(db)
    se = await _se_for(db, client=client)
    await db.commit()

    media = [
        {"media_type": "IMAGE", "url": "https://cdn/a.jpg", "caption": "Diagnosis"},
        {"media_type": "HYPERLINK", "url": "https://kvk.example/paddy"},
    ]
    out = await create_standard_response(
        client_id=client.id,
        data={"question_text": "Q?", "answer_media": media},
        db=db, current_user=se,
    )
    assert len(out["answer_media"]) == 2
    assert out["answer_media"][0]["caption"] == "Diagnosis"


# ── list filters ────────────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_list_returns_client_scoped_only(db):
    """Client A's list never leaks Client B's entries — each client
    has its own library."""
    client_a = await make_client(db)
    client_b = await make_client(db)
    se_a = await _se_for(db, client=client_a)
    se_b = await _se_for(db, client=client_b)
    await db.commit()

    await create_standard_response(
        client_id=client_a.id,
        data={"question_text": "A's question"},
        db=db, current_user=se_a,
    )
    await create_standard_response(
        client_id=client_b.id,
        data={"question_text": "B's question"},
        db=db, current_user=se_b,
    )

    out = await list_standard_responses(
        client_id=client_a.id, db=db, current_user=se_a,
    )
    assert {r["question_text"] for r in out} == {"A's question"}


@requires_docker
@pytest.mark.asyncio
async def test_list_filters_by_crop_or_agnostic(db):
    """`crop_cosh_id=AGNOSTIC` returns only entries with no crop set;
    a real crop_cosh_id filters to that crop; omitted = no filter."""
    client = await make_client(db)
    se = await _se_for(db, client=client)
    await db.commit()

    await create_standard_response(
        client_id=client.id,
        data={"question_text": "Paddy Q", "crop_cosh_id": "crop:paddy"},
        db=db, current_user=se,
    )
    await create_standard_response(
        client_id=client.id,
        data={"question_text": "Tomato Q", "crop_cosh_id": "crop:tomato"},
        db=db, current_user=se,
    )
    await create_standard_response(
        client_id=client.id,
        data={"question_text": "Generic Q"},  # no crop
        db=db, current_user=se,
    )

    paddy = await list_standard_responses(
        client_id=client.id, crop_cosh_id="crop:paddy",
        db=db, current_user=se,
    )
    assert {r["question_text"] for r in paddy} == {"Paddy Q"}

    agnostic = await list_standard_responses(
        client_id=client.id, crop_cosh_id="AGNOSTIC",
        db=db, current_user=se,
    )
    assert {r["question_text"] for r in agnostic} == {"Generic Q"}

    everything = await list_standard_responses(
        client_id=client.id, db=db, current_user=se,
    )
    assert {r["question_text"] for r in everything} == {"Paddy Q", "Tomato Q", "Generic Q"}


@requires_docker
@pytest.mark.asyncio
async def test_list_filters_by_search(db):
    client = await make_client(db)
    se = await _se_for(db, client=client)
    await db.commit()

    await create_standard_response(
        client_id=client.id,
        data={"question_text": "Yellow leaves on paddy"},
        db=db, current_user=se,
    )
    await create_standard_response(
        client_id=client.id,
        data={"question_text": "Brown spots on tomato"},
        db=db, current_user=se,
    )

    out = await list_standard_responses(
        client_id=client.id, search="yellow",
        db=db, current_user=se,
    )
    assert {r["question_text"] for r in out} == {"Yellow leaves on paddy"}


# ── update + delete ─────────────────────────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_update_persists_new_values(db):
    client = await make_client(db)
    se = await _se_for(db, client=client)
    await db.commit()

    created = await create_standard_response(
        client_id=client.id,
        data={"question_text": "Old Q", "answer_text": "Old A"},
        db=db, current_user=se,
    )

    updated = await update_standard_response(
        client_id=client.id, sr_id=created["id"],
        data={
            "question_text": "New Q",
            "answer_text": "New A",
            "crop_cosh_id": "crop:wheat",
        },
        db=db, current_user=se,
    )
    assert updated["question_text"] == "New Q"
    assert updated["answer_text"] == "New A"
    assert updated["crop_cosh_id"] == "crop:wheat"


@requires_docker
@pytest.mark.asyncio
async def test_update_404_for_other_clients_response(db):
    """Cannot edit another client's response by guessing the id."""
    client_a = await make_client(db)
    client_b = await make_client(db)
    se_a = await _se_for(db, client=client_a)
    se_b = await _se_for(db, client=client_b)
    await db.commit()

    sr_b = await create_standard_response(
        client_id=client_b.id,
        data={"question_text": "B's question"},
        db=db, current_user=se_b,
    )

    with pytest.raises(HTTPException) as ei:
        await update_standard_response(
            client_id=client_a.id, sr_id=sr_b["id"],
            data={"question_text": "Hijacked"},
            db=db, current_user=se_a,
        )
    assert ei.value.status_code == 404


@requires_docker
@pytest.mark.asyncio
async def test_delete_removes_row(db):
    client = await make_client(db)
    se = await _se_for(db, client=client)
    await db.commit()

    sr = await create_standard_response(
        client_id=client.id,
        data={"question_text": "Q?"},
        db=db, current_user=se,
    )

    await delete_standard_response(
        client_id=client.id, sr_id=sr["id"],
        db=db, current_user=se,
    )

    leftover = (await db.execute(
        select(StandardResponse).where(StandardResponse.id == sr["id"])
    )).scalar_one_or_none()
    assert leftover is None


@requires_docker
@pytest.mark.asyncio
async def test_delete_404_for_other_clients_response(db):
    """Same cross-client guard as update."""
    client_a = await make_client(db)
    client_b = await make_client(db)
    se_a = await _se_for(db, client=client_a)
    se_b = await _se_for(db, client=client_b)
    await db.commit()

    sr_b = await create_standard_response(
        client_id=client_b.id,
        data={"question_text": "B's"},
        db=db, current_user=se_b,
    )

    with pytest.raises(HTTPException) as ei:
        await delete_standard_response(
            client_id=client_a.id, sr_id=sr_b["id"],
            db=db, current_user=se_a,
        )
    assert ei.value.status_code == 404


# ── membership gate (M7 still applies to all CA-side endpoints) ─────────────

@requires_docker
@pytest.mark.asyncio
async def test_list_rejects_non_member(db):
    client = await make_client(db)
    outsider = await make_user(db, name="Outsider")
    await db.commit()

    with pytest.raises(HTTPException) as ei:
        await list_standard_responses(
            client_id=client.id, db=db, current_user=outsider,
        )
    assert ei.value.status_code == 403


# ── pundit-side search backward-compat ──────────────────────────────────────

@requires_docker
@pytest.mark.asyncio
async def test_pundit_search_unchanged_shape_includes_answer_body(db):
    """The Pundit-side search now returns the full payload (was
    raw rows pre-fix). The Pundit's response screen needs the
    answer_text + answer_media to render the standard answer."""
    client = await make_client(db)
    se = await _se_for(db, client=client)
    pundit = await make_user(db, name="Pundit")
    await db.commit()

    await create_standard_response(
        client_id=client.id,
        data={
            "question_text": "How to control aphids?",
            "answer_text": "Spray neem oil 5ml/L weekly.",
        },
        db=db, current_user=se,
    )

    out = await search_standard_responses(
        client_id=client.id, search="aphids",
        db=db, current_user=pundit,
    )
    assert len(out) == 1
    assert out[0]["answer_text"] == "Spray neem oil 5ml/L weekly."
