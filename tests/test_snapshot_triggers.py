"""Phase 2 — snapshot_triggers tests.

Pure-function coverage for `cca_timeline_keys_from_items` plus async
coverage of `take_snapshots_for_keys` using a fake `take_snapshot` that
records calls and can be made to fail. No DB required.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pytest

from app.services import snapshot_triggers
from app.services.snapshot_triggers import (
    cca_timeline_keys_from_items,
    take_snapshots_for_keys,
)


@dataclass
class _Item:
    timeline_id: Optional[str]


# ── cca_timeline_keys_from_items ────────────────────────────────────────────

def test_keys_unique_and_preserve_first_seen_order():
    items = [_Item("tl_a"), _Item("tl_b"), _Item("tl_a"), _Item("tl_c")]
    assert cca_timeline_keys_from_items(items) == [
        ("tl_a", "CCA"),
        ("tl_b", "CCA"),
        ("tl_c", "CCA"),
    ]


def test_keys_skip_falsy_timeline_ids():
    items = [_Item(None), _Item(""), _Item("tl_x")]
    assert cca_timeline_keys_from_items(items) == [("tl_x", "CCA")]


def test_keys_accept_raw_strings():
    """For convenience the helper also accepts a plain iterable of timeline_ids."""
    assert cca_timeline_keys_from_items(["tl_1", "tl_1", "tl_2"]) == [
        ("tl_1", "CCA"),
        ("tl_2", "CCA"),
    ]


def test_keys_empty_input():
    assert cca_timeline_keys_from_items([]) == []


# ── take_snapshots_for_keys ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_take_snapshots_calls_through_for_each_key(monkeypatch):
    calls: list[tuple] = []

    async def fake_take_snapshot(db, sub_id, tl_id, trigger, source="CCA"):
        calls.append((sub_id, tl_id, trigger, source))
        return object()

    monkeypatch.setattr(snapshot_triggers, "take_snapshot", fake_take_snapshot)

    n = await take_snapshots_for_keys(
        db=None,  # not used by fake
        subscription_id="sub_1",
        keys=[("tl_a", "CCA"), ("tl_b", "PG")],
        lock_trigger="PURCHASE_ORDER",
    )
    assert n == 2
    assert calls == [
        ("sub_1", "tl_a", "PURCHASE_ORDER", "CCA"),
        ("sub_1", "tl_b", "PURCHASE_ORDER", "PG"),
    ]


@pytest.mark.asyncio
async def test_take_snapshots_swallows_per_key_failures(monkeypatch):
    """A failure on one key must not abort the rest. Successful count returned."""
    calls: list[str] = []

    async def fake_take_snapshot(db, sub_id, tl_id, trigger, source="CCA"):
        calls.append(tl_id)
        if tl_id == "tl_b":
            raise RuntimeError("boom")
        return object()

    monkeypatch.setattr(snapshot_triggers, "take_snapshot", fake_take_snapshot)

    n = await take_snapshots_for_keys(
        db=None,
        subscription_id="sub_1",
        keys=[("tl_a", "CCA"), ("tl_b", "CCA"), ("tl_c", "CCA")],
        lock_trigger="VIEWED",
    )
    assert n == 2
    assert calls == ["tl_a", "tl_b", "tl_c"]


@pytest.mark.asyncio
async def test_take_snapshots_empty_keys_is_zero(monkeypatch):
    """Empty key list — no calls, returns 0."""
    called = False

    async def fake_take_snapshot(*_a, **_k):
        nonlocal called
        called = True

    monkeypatch.setattr(snapshot_triggers, "take_snapshot", fake_take_snapshot)
    n = await take_snapshots_for_keys(None, "sub_1", [], "PURCHASE_ORDER")
    assert n == 0
    assert called is False
