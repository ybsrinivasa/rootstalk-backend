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


# ── Integration tests (require a live test DB — skipped by default) ─────────
#
# These cover the round-trip + immutability + double-snapshot + CHA paths.
# They will be enabled in Phase 2 when a DB fixture is wired into conftest.py.

@pytest.mark.skip(reason="Requires DB fixture — wire in Phase 2")
@pytest.mark.asyncio
async def test_serialise_round_trip():
    """Create a Timeline + 2 Practices + 1 Relation + Elements + 1 ConditionalQuestion
    + 1 PracticeConditional. Serialise. Assert all fields present with correct values.
    """
    pass


@pytest.mark.skip(reason="Requires DB fixture — wire in Phase 2")
@pytest.mark.asyncio
async def test_take_snapshot_stores_content():
    """take_snapshot writes the row, get_snapshot returns it, has_snapshot is True."""
    pass


@pytest.mark.skip(reason="Requires DB fixture — wire in Phase 2")
@pytest.mark.asyncio
async def test_take_snapshot_immutable_under_master_edit():
    """After snapshot is taken, edits to master Practice rows do NOT change the
    snapshot's content. Re-calling take_snapshot for the same (sub, tl, source)
    returns the SAME existing row (Rules 1 & 2)."""
    pass


@pytest.mark.skip(reason="Requires DB fixture — wire in Phase 2")
@pytest.mark.asyncio
async def test_get_missing_snapshot_returns_none():
    """get_snapshot for a non-existent (sub, tl, source) returns None;
    has_snapshot returns False."""
    pass


@pytest.mark.skip(reason="Requires DB fixture — wire in Phase 2")
@pytest.mark.asyncio
async def test_cha_serialise_pg_timeline():
    """serialise_cha_timeline for a PGTimeline returns source='PG' and includes
    its practices + elements."""
    pass


@pytest.mark.skip(reason="Requires DB fixture — wire in Phase 2")
@pytest.mark.asyncio
async def test_cha_serialise_sp_timeline():
    """serialise_cha_timeline for an SPTimeline returns source='SP'."""
    pass
