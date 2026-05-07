"""Pure-function tests for `find_pv_conflicts`.

Integration coverage of the API surface (set_package_variables,
set_package_locations, publish_package returning 422 with the
right error code + body) lives in
`tests/test_phase_cca_step2_integration.py`.
"""
from __future__ import annotations

from app.services.pv_uniqueness import PVConflict, find_pv_conflicts


def _sib(id_: str, name: str, fp: dict, districts: list[tuple[str, str]]):
    """Compact tuple builder for sibling rows in tests."""
    return (id_, name, fp, set(districts))


# ── No-conflict cases ─────────────────────────────────────────────────────────

def test_no_siblings_no_conflicts():
    assert find_pv_conflicts(
        this_fingerprint={"P1": "V1"},
        this_districts={("S1", "D1")},
        siblings=[],
    ) == []


def test_no_shared_district_no_conflicts():
    """Same fingerprint but different districts — fine. Spec only
    requires uniqueness within a SHARED district."""
    out = find_pv_conflicts(
        this_fingerprint={"P1": "V1"},
        this_districts={("S1", "D1")},
        siblings=[_sib("sib", "Sib PoP", {"P1": "V1"}, [("S1", "D2")])],
    )
    assert out == []


def test_shared_district_but_different_fingerprint_no_conflict():
    """Shared district but fingerprints differ — exactly the spec-
    compliant case where guided elimination resolves correctly."""
    out = find_pv_conflicts(
        this_fingerprint={"P1": "V1"},
        this_districts={("S1", "D1")},
        siblings=[_sib("sib", "Sib PoP", {"P1": "V2"}, [("S1", "D1")])],
    )
    assert out == []


def test_partial_district_overlap_with_different_fingerprint_no_conflict():
    out = find_pv_conflicts(
        this_fingerprint={"P1": "V1", "P2": "V2"},
        this_districts={("S1", "D1"), ("S1", "D2")},
        siblings=[_sib(
            "sib", "Sib PoP", {"P1": "V1", "P2": "V3"},
            [("S1", "D1"), ("S1", "D5")],
        )],
    )
    assert out == []


# ── Conflict cases ────────────────────────────────────────────────────────────

def test_shared_district_and_equal_fingerprint_conflicts():
    """The headline rule: shared district + identical fingerprint =
    non-deterministic guided elimination."""
    out = find_pv_conflicts(
        this_fingerprint={"P1": "V1", "P2": "V2"},
        this_districts={("S1", "D1")},
        siblings=[_sib(
            "sib", "Sib PoP", {"P1": "V1", "P2": "V2"}, [("S1", "D1")],
        )],
    )
    assert len(out) == 1
    assert out[0].sibling_package_id == "sib"
    assert out[0].sibling_package_name == "Sib PoP"
    assert out[0].shared_districts == (("S1", "D1"),)


def test_both_empty_fingerprints_with_shared_district_conflicts():
    """Spec §4.2: a single PoP can have empty P/V, but the moment a
    second PoP exists with shared coverage, that exemption is gone.
    `{} == {}` returns True so the algorithm catches this naturally."""
    out = find_pv_conflicts(
        this_fingerprint={},
        this_districts={("S1", "D1")},
        siblings=[_sib("sib", "Empty Sib", {}, [("S1", "D1")])],
    )
    assert len(out) == 1
    assert out[0].sibling_package_id == "sib"


def test_multiple_shared_districts_all_listed():
    """A sibling overlapping in multiple districts — surface ALL
    of them so the CA portal can render a precise corrective hint."""
    out = find_pv_conflicts(
        this_fingerprint={"P1": "V1"},
        this_districts={("S1", "D1"), ("S1", "D2"), ("S1", "D3")},
        siblings=[_sib(
            "sib", "Big Sib", {"P1": "V1"},
            [("S1", "D1"), ("S1", "D2")],
        )],
    )
    assert len(out) == 1
    # Sorted for determinism in tests
    assert out[0].shared_districts == (("S1", "D1"), ("S1", "D2"))


def test_multiple_siblings_only_conflicting_returned():
    """Two siblings: one shares a district AND has equal fingerprint
    (conflict), the other shares a district but different fingerprint
    (fine). Only the conflict surfaces."""
    out = find_pv_conflicts(
        this_fingerprint={"P1": "V1"},
        this_districts={("S1", "D1")},
        siblings=[
            _sib("conflict_sib", "Bad Sib", {"P1": "V1"}, [("S1", "D1")]),
            _sib("ok_sib", "Good Sib", {"P1": "V2"}, [("S1", "D1")]),
        ],
    )
    assert len(out) == 1
    assert out[0].sibling_package_id == "conflict_sib"


def test_two_siblings_both_conflict_both_returned():
    """If multiple siblings each cause a conflict, return them all
    so the portal can surface a complete picture."""
    out = find_pv_conflicts(
        this_fingerprint={"P1": "V1"},
        this_districts={("S1", "D1")},
        siblings=[
            _sib("a", "Sib A", {"P1": "V1"}, [("S1", "D1")]),
            _sib("b", "Sib B", {"P1": "V1"}, [("S1", "D1")]),
        ],
    )
    assert {c.sibling_package_id for c in out} == {"a", "b"}


# ── Subtle cases ──────────────────────────────────────────────────────────────

def test_subset_fingerprint_does_not_conflict():
    """A's fingerprint is a strict subset of B's — Python equality
    fails (`{P1: V1} != {P1: V1, P2: V2}`), so no Batch 2D conflict.
    The subset-vs-superset case is a parameter-set inconsistency
    that Batch 2E will catch separately (different parameter sets
    in the same district)."""
    out = find_pv_conflicts(
        this_fingerprint={"P1": "V1"},
        this_districts={("S1", "D1")},
        siblings=[_sib(
            "sib", "Bigger Sib", {"P1": "V1", "P2": "V2"}, [("S1", "D1")],
        )],
    )
    assert out == []


def test_this_pkg_with_no_districts_no_conflict():
    """This PoP hasn't been given any locations yet → no shared-
    district scenario possible. Returns empty list. (The async
    wrapper short-circuits before this is reached, but the pure
    function also handles it correctly.)"""
    out = find_pv_conflicts(
        this_fingerprint={"P1": "V1"},
        this_districts=set(),
        siblings=[_sib("sib", "Sib", {"P1": "V1"}, [("S1", "D1")])],
    )
    assert out == []
