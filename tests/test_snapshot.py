"""
Per-Subscription Content Versioning — Phase 1 tests.

Phase 1 is library-only. Existing test suite is pure-function (no DB fixtures
exist in this codebase). To stay consistent with that pattern, these tests
focus on:
  - deserialise_timeline (pure function — full coverage, no DB)
  - take_snapshot input validation (pure-validation paths — no DB)
  - serialise_timeline / take_snapshot / get_snapshot / has_snapshot
    integration tests are marked `integration` and skipped by default;
    they will run once a DB fixture is wired (Phase 2).

See: app/services/snapshot.py
"""
from __future__ import annotations

import pytest

from app.services.snapshot import (
    VALID_LOCK_TRIGGERS,
    VALID_SOURCES,
    SCHEMA_VERSION,
    deserialise_timeline,
    take_snapshot,
)


# ── deserialise_timeline (pure function — fully tested) ─────────────────────

def _sample_content() -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "source": "CCA",
        "timeline": {
            "id": "tl_1",
            "package_id": "pkg_1",
            "name": "Vegetative",
            "from_type": "DAS",
            "from_value": 0,
            "to_value": 30,
            "display_order": 0,
        },
        "practices": [
            {
                "id": "p1",
                "l0_type": "INPUT",
                "l1_type": "FERTILIZER",
                "l2_type": "UREA",
                "display_order": 0,
                "relation_id": "r1",
                "relation_role": "PART_1__OPT_1__POS_1",
                "is_special_input": False,
                "common_name_cosh_id": None,
                "frequency_days": None,
                "elements": [
                    {
                        "id": "e1",
                        "element_type": "DOSAGE",
                        "cosh_ref": None,
                        "value": "50",
                        "unit_cosh_id": "kg_per_acre",
                        "display_order": 0,
                    }
                ],
            },
            {
                "id": "p2",
                "l0_type": "NON_INPUT",
                "l1_type": "IRRIGATION",
                "l2_type": None,
                "display_order": 1,
                "relation_id": None,
                "relation_role": None,
                "is_special_input": False,
                "common_name_cosh_id": None,
                "frequency_days": 7,
                "elements": [],
            },
        ],
        "relations": [
            {"id": "r1", "relation_type": "AND", "expression": "p1 AND p2"}
        ],
        "conditional_questions": [
            {"id": "q1", "question_text": "Is rainfall expected?", "display_order": 0}
        ],
        "conditional_links": [
            {"practice_id": "p1", "question_id": "q1", "answer": "NO"},
            {"practice_id": "p2", "question_id": "q1", "answer": "YES"},
        ],
    }


def test_deserialise_adds_indexes():
    """deserialise_timeline must populate all four convenience indexes."""
    content = _sample_content()
    out = deserialise_timeline(content)

    # Original keys preserved (pass-through)
    assert out["schema_version"] == SCHEMA_VERSION
    assert out["source"] == "CCA"
    assert out["timeline"]["id"] == "tl_1"
    assert len(out["practices"]) == 2

    # Convenience indexes
    assert set(out["practices_by_id"].keys()) == {"p1", "p2"}
    assert out["practices_by_id"]["p1"]["l1_type"] == "FERTILIZER"

    assert set(out["relations_by_id"].keys()) == {"r1"}
    assert out["relations_by_id"]["r1"]["relation_type"] == "AND"

    assert set(out["questions_by_id"].keys()) == {"q1"}

    assert set(out["links_by_practice"].keys()) == {"p1", "p2"}
    assert out["links_by_practice"]["p1"][0]["answer"] == "NO"
    assert out["links_by_practice"]["p2"][0]["answer"] == "YES"


def test_deserialise_handles_empty_collections():
    """Missing or empty practices/relations/questions/links should yield empty indexes."""
    content = {
        "schema_version": SCHEMA_VERSION,
        "source": "PG",
        "timeline": {"id": "tl_pg_1", "name": "Foo"},
        "practices": [],
        "relations": [],
        "conditional_questions": [],
        "conditional_links": [],
    }
    out = deserialise_timeline(content)
    assert out["practices_by_id"] == {}
    assert out["relations_by_id"] == {}
    assert out["questions_by_id"] == {}
    assert out["links_by_practice"] == {}


def test_deserialise_handles_missing_keys():
    """Snapshot with no relations/questions/links keys still yields empty indexes."""
    content = {
        "schema_version": SCHEMA_VERSION,
        "source": "CCA",
        "timeline": {"id": "tl_1"},
        "practices": [
            {"id": "px", "l0_type": "INPUT", "elements": []},
        ],
    }
    out = deserialise_timeline(content)
    assert set(out["practices_by_id"].keys()) == {"px"}
    assert out["relations_by_id"] == {}
    assert out["questions_by_id"] == {}
    assert out["links_by_practice"] == {}


def test_deserialise_rejects_non_dict():
    with pytest.raises(ValueError, match="must be a dict"):
        deserialise_timeline([])  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="must be a dict"):
        deserialise_timeline("not a dict")  # type: ignore[arg-type]


def test_deserialise_skips_links_without_practice_id():
    """Defensive: a malformed link missing practice_id should be skipped, not crash."""
    content = {
        "schema_version": SCHEMA_VERSION,
        "source": "CCA",
        "timeline": {"id": "tl_1"},
        "practices": [{"id": "p1", "elements": []}],
        "relations": [],
        "conditional_questions": [],
        "conditional_links": [
            {"question_id": "q1", "answer": "YES"},  # no practice_id
            {"practice_id": "p1", "question_id": "q1", "answer": "NO"},
        ],
    }
    out = deserialise_timeline(content)
    assert "p1" in out["links_by_practice"]
    assert len(out["links_by_practice"]["p1"]) == 1


# ── take_snapshot input validation (no DB needed) ───────────────────────────

class _NullSession:
    """Minimal stand-in: take_snapshot raises before touching the session."""
    def add(self, *_a, **_k):
        raise AssertionError("session.add must not be called when validation fails")
    async def commit(self):
        raise AssertionError("session.commit must not be called when validation fails")
    async def refresh(self, *_a, **_k):
        raise AssertionError("session.refresh must not be called when validation fails")
    async def execute(self, *_a, **_k):
        raise AssertionError("session.execute must not be called when validation fails")


@pytest.mark.asyncio
async def test_take_snapshot_rejects_invalid_lock_trigger():
    sess = _NullSession()
    with pytest.raises(ValueError, match="lock_trigger"):
        await take_snapshot(sess, "sub_1", "tl_1", "FOO")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_take_snapshot_rejects_invalid_source():
    sess = _NullSession()
    with pytest.raises(ValueError, match="source"):
        await take_snapshot(
            sess, "sub_1", "tl_1", "PURCHASE_ORDER", source="BOGUS"  # type: ignore[arg-type]
        )


def test_valid_lock_triggers_constant():
    assert VALID_LOCK_TRIGGERS == {"PURCHASE_ORDER", "VIEWED", "BACKFILL"}


def test_valid_sources_constant():
    assert VALID_SOURCES == {"CCA", "PG", "SP"}


# ── Integration tests (DB-backed — enabled in Phase 3) ──────────────────────
#
# These cover the round-trip + immutability + double-snapshot + CHA paths.
# Use the Postgres testcontainer fixture from tests/conftest.py.

from app.services.snapshot import (
    get_snapshot, has_snapshot, serialise_timeline, serialise_cha_timeline,
)
from tests.conftest import requires_docker
from tests.factories import (
    make_client, make_conditional_question, make_element, make_package,
    make_pg_element, make_pg_practice, make_pg_recommendation, make_pg_timeline,
    make_practice, make_practice_conditional, make_relation, make_sp_element,
    make_sp_practice, make_sp_recommendation, make_sp_timeline,
    make_subscription, make_timeline, make_user,
)
from app.modules.advisory.models import (
    ConditionalAnswer, PracticeL0, RelationType, TimelineFromType,
)


async def _seed_subscription(db):
    """Common parent rows for every integration test."""
    user = await make_user(db)
    client = await make_client(db)
    package = await make_package(db, client)
    sub = await make_subscription(db, farmer=user, client=client, package=package)
    return sub, package


@requires_docker
@pytest.mark.asyncio
async def test_serialise_round_trip(db):
    """Create a Timeline + 2 Practices + 1 Relation + Elements + 1 ConditionalQuestion
    + 1 PracticeConditional. Serialise. Assert all fields present with correct values.
    """
    _sub, package = await _seed_subscription(db)
    tl = await make_timeline(
        db, package, from_type=TimelineFromType.DAS, from_value=0, to_value=30,
    )
    relation = await make_relation(db, tl, relation_type=RelationType.AND)
    p1 = await make_practice(
        db, tl, l0=PracticeL0.INPUT, l1="FERTILIZER", l2="UREA",
        display_order=0, relation=relation, relation_role="PART_1__OPT_1__POS_1",
    )
    p2 = await make_practice(
        db, tl, l0=PracticeL0.NON_INPUT, l1="IRRIGATION", l2=None,
        display_order=1, frequency_days=7,
    )
    await make_element(db, p1, element_type="DOSAGE", value="50",
                       unit_cosh_id="kg_per_acre")
    q = await make_conditional_question(db, tl, text="Rain expected?")
    await make_practice_conditional(db, p1, q, answer=ConditionalAnswer.NO)

    content = await serialise_timeline(db, tl.id)

    assert content["schema_version"] == SCHEMA_VERSION
    assert content["source"] == "CCA"
    assert content["timeline"]["id"] == tl.id
    assert content["timeline"]["from_type"] == "DAS"
    assert content["timeline"]["from_value"] == 0
    assert content["timeline"]["to_value"] == 30

    practice_ids = {p["id"] for p in content["practices"]}
    assert practice_ids == {p1.id, p2.id}

    p1_dict = next(p for p in content["practices"] if p["id"] == p1.id)
    assert p1_dict["l0_type"] == "INPUT"
    assert p1_dict["l1_type"] == "FERTILIZER"
    assert p1_dict["relation_id"] == relation.id
    assert p1_dict["relation_role"] == "PART_1__OPT_1__POS_1"
    assert len(p1_dict["elements"]) == 1
    assert p1_dict["elements"][0]["value"] == "50"

    p2_dict = next(p for p in content["practices"] if p["id"] == p2.id)
    assert p2_dict["frequency_days"] == 7
    assert p2_dict["elements"] == []

    assert len(content["relations"]) == 1
    assert content["relations"][0]["relation_type"] == "AND"

    assert len(content["conditional_questions"]) == 1
    assert content["conditional_questions"][0]["question_text"] == "Rain expected?"

    assert len(content["conditional_links"]) == 1
    link = content["conditional_links"][0]
    assert link["practice_id"] == p1.id
    assert link["question_id"] == q.id
    assert link["answer"] == "NO"


@requires_docker
@pytest.mark.asyncio
async def test_take_snapshot_stores_content(db):
    """take_snapshot writes the row, get_snapshot returns it, has_snapshot is True."""
    sub, package = await _seed_subscription(db)
    tl = await make_timeline(db, package)
    await make_practice(db, tl)

    assert await has_snapshot(db, sub.id, tl.id, "CCA") is False

    snap = await take_snapshot(
        db, sub.id, tl.id, lock_trigger="PURCHASE_ORDER", source="CCA",
    )
    assert snap.subscription_id == sub.id
    assert snap.timeline_id == tl.id
    assert snap.source == "CCA"
    assert snap.lock_trigger == "PURCHASE_ORDER"
    assert snap.content["timeline"]["id"] == tl.id

    fetched = await get_snapshot(db, sub.id, tl.id, "CCA")
    assert fetched is not None
    assert fetched.id == snap.id
    assert await has_snapshot(db, sub.id, tl.id, "CCA") is True


@requires_docker
@pytest.mark.asyncio
async def test_take_snapshot_immutable_under_master_edit(db):
    """After snapshot is taken, edits to master Practice rows do NOT change the
    snapshot's content. Re-calling take_snapshot for the same (sub, tl, source)
    returns the SAME existing row (Rules 1 & 2)."""
    sub, package = await _seed_subscription(db)
    tl = await make_timeline(db, package)
    p = await make_practice(db, tl, l1="FERTILIZER", l2="UREA")
    el = await make_element(db, p, value="50", unit_cosh_id="kg_per_acre")

    snap1 = await take_snapshot(db, sub.id, tl.id, "VIEWED", source="CCA")
    snap1_content_str = str(snap1.content)

    # SE edits master tables — practice and element values change.
    p.l1_type = "PESTICIDE"
    p.l2_type = "MANCOZEB"
    el.value = "999"
    el.unit_cosh_id = "ml_per_acre"
    await db.commit()

    # Re-call take_snapshot — must return the SAME row, content unchanged.
    snap2 = await take_snapshot(db, sub.id, tl.id, "PURCHASE_ORDER", source="CCA")
    assert snap2.id == snap1.id
    assert str(snap2.content) == snap1_content_str
    assert snap2.lock_trigger == "VIEWED"  # original trigger preserved

    # Master DID change — sanity check the master is not what the snapshot has.
    fresh = await serialise_timeline(db, tl.id)
    fresh_p = next(pr for pr in fresh["practices"] if pr["id"] == p.id)
    assert fresh_p["l1_type"] == "PESTICIDE"
    snap_p = next(pr for pr in snap1.content["practices"] if pr["id"] == p.id)
    assert snap_p["l1_type"] == "FERTILIZER"


@requires_docker
@pytest.mark.asyncio
async def test_get_missing_snapshot_returns_none(db):
    """get_snapshot for a non-existent (sub, tl, source) returns None;
    has_snapshot returns False."""
    sub, package = await _seed_subscription(db)
    tl = await make_timeline(db, package)

    assert await get_snapshot(db, sub.id, tl.id, "CCA") is None
    assert await has_snapshot(db, sub.id, tl.id, "CCA") is False
    # Different source — also missing.
    assert await get_snapshot(db, sub.id, tl.id, "PG") is None


@requires_docker
@pytest.mark.asyncio
async def test_cha_serialise_pg_timeline(db):
    """serialise_cha_timeline for a PGTimeline returns source='PG' and includes
    its practices + elements."""
    pg_rec = await make_pg_recommendation(db)
    pg_tl = await make_pg_timeline(db, pg_rec, from_value=0, to_value=14)
    p = await make_pg_practice(db, pg_tl, l0_type="INPUT", l1_type="PESTICIDE")
    await make_pg_element(db, p, value="2.5")

    content = await serialise_cha_timeline(db, pg_tl.id, "PG")

    assert content["source"] == "PG"
    assert content["timeline"]["id"] == pg_tl.id
    assert content["timeline"]["from_value"] == 0
    assert content["timeline"]["to_value"] == 14
    assert len(content["practices"]) == 1
    assert content["practices"][0]["l1_type"] == "PESTICIDE"
    assert content["practices"][0]["elements"][0]["value"] == "2.5"
    assert content["relations"] == []
    assert content["conditional_questions"] == []
    assert content["conditional_links"] == []


@requires_docker
@pytest.mark.asyncio
async def test_cha_serialise_sp_timeline(db):
    """serialise_cha_timeline for an SPTimeline returns source='SP'."""
    client = await make_client(db)
    sp_rec = await make_sp_recommendation(db, client)
    sp_tl = await make_sp_timeline(db, sp_rec, from_value=2, to_value=10)
    p = await make_sp_practice(db, sp_tl)
    await make_sp_element(db, p, value="100")

    content = await serialise_cha_timeline(db, sp_tl.id, "SP")
    assert content["source"] == "SP"
    assert content["timeline"]["id"] == sp_tl.id
    assert content["timeline"]["from_value"] == 2
    assert content["timeline"]["to_value"] == 10
    assert len(content["practices"]) == 1


@requires_docker
@pytest.mark.asyncio
async def test_serialise_cha_rejects_bad_source(db):
    """serialise_cha_timeline raises ValueError on unknown source."""
    with pytest.raises(ValueError, match="source"):
        await serialise_cha_timeline(db, "tl_x", "BOGUS")


@requires_docker
@pytest.mark.asyncio
async def test_serialise_timeline_missing_id_raises(db):
    """serialise_timeline raises ValueError when timeline doesn't exist."""
    with pytest.raises(ValueError, match="Timeline not found"):
        await serialise_timeline(db, "tl_does_not_exist")
