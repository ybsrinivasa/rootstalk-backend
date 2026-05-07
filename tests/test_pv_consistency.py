"""Pure-function tests for `find_consistency_violations`.

Integration coverage of the API surface (set_package_variables,
set_package_locations, publish_package returning 422 with the
right error code + body) lives in
`tests/test_phase_cca_step2_integration.py`.
"""
from __future__ import annotations

from app.services.pv_consistency import (
    PVConsistencyViolation, find_consistency_violations,
)


def _sib(id_: str, name: str, param_ids: set[str], districts: list[tuple[str, str]]):
    return (id_, name, frozenset(param_ids), set(districts))


# ── No-violation cases ───────────────────────────────────────────────────────

def test_no_siblings_no_violations():
    assert find_consistency_violations(
        this_parameter_ids=frozenset({"P1"}),
        this_districts={("S1", "D1")},
        siblings=[],
    ) == []


def test_same_set_same_district_no_violation():
    """The whole point: equal parameter sets in shared districts
    are exactly what guided elimination needs."""
    out = find_consistency_violations(
        this_parameter_ids=frozenset({"P1", "P2"}),
        this_districts={("S1", "D1")},
        siblings=[_sib("sib", "Sib", {"P1", "P2"}, [("S1", "D1")])],
    )
    assert out == []


def test_different_set_no_shared_district_no_violation():
    """Spec: parameters must be consistent within a district but
    can vary across districts. Sibling in a different district can
    use a different set."""
    out = find_consistency_violations(
        this_parameter_ids=frozenset({"P1"}),
        this_districts={("S1", "D1")},
        siblings=[_sib("sib", "Sib", {"P1", "P2", "P3"}, [("S1", "D2")])],
    )
    assert out == []


def test_both_empty_sets_in_shared_district_no_violation():
    """`set() == set()` is True — Batch 2D's uniqueness check is
    what catches this case (both fingerprints `{}` are equal). 2E
    sees them as consistent."""
    out = find_consistency_violations(
        this_parameter_ids=frozenset(),
        this_districts={("S1", "D1")},
        siblings=[_sib("sib", "Sib", set(), [("S1", "D1")])],
    )
    assert out == []


# ── Violation cases ──────────────────────────────────────────────────────────

def test_subset_in_shared_district_is_violation():
    """{P1} vs {P1, P2} in same district — Batch 2D lets this through
    (different fingerprints), 2E catches it (different parameter sets).
    Without this check, the farmer might be asked P2 when their actual
    PoP doesn't use P2."""
    out = find_consistency_violations(
        this_parameter_ids=frozenset({"P1"}),
        this_districts={("S1", "D1")},
        siblings=[_sib("sib", "Bigger Sib", {"P1", "P2"}, [("S1", "D1")])],
    )
    assert len(out) == 1
    assert out[0].sibling_package_name == "Bigger Sib"
    assert set(out[0].this_parameter_ids) == {"P1"}
    assert set(out[0].sibling_parameter_ids) == {"P1", "P2"}
    assert out[0].shared_districts == (("S1", "D1"),)


def test_superset_in_shared_district_is_violation():
    out = find_consistency_violations(
        this_parameter_ids=frozenset({"P1", "P2", "P3"}),
        this_districts={("S1", "D1")},
        siblings=[_sib("sib", "Smaller Sib", {"P1"}, [("S1", "D1")])],
    )
    assert len(out) == 1


def test_disjoint_sets_in_shared_district_is_violation():
    """No parameters in common at all."""
    out = find_consistency_violations(
        this_parameter_ids=frozenset({"P1", "P2"}),
        this_districts={("S1", "D1")},
        siblings=[_sib("sib", "Sib", {"P3", "P4"}, [("S1", "D1")])],
    )
    assert len(out) == 1


def test_one_empty_one_non_empty_in_shared_district_is_violation():
    """The `2D won't catch but 2E will` case from the matrix:
    asymmetric empties. Spec §4.2: "Parameters not mandatory for
    first PoP. Becomes mandatory for ALL PoPs when a second PoP is
    added for same crop and location" — exactly this."""
    out = find_consistency_violations(
        this_parameter_ids=frozenset(),
        this_districts={("S1", "D1")},
        siblings=[_sib("sib", "Has Params", {"P1"}, [("S1", "D1")])],
    )
    assert len(out) == 1


def test_multiple_violations_returned():
    """Two siblings each cause a violation — return both so the
    portal can render a complete picture."""
    out = find_consistency_violations(
        this_parameter_ids=frozenset({"P1"}),
        this_districts={("S1", "D1")},
        siblings=[
            _sib("a", "Sib A", {"P1", "P2"}, [("S1", "D1")]),
            _sib("b", "Sib B", {"P1", "P3"}, [("S1", "D1")]),
        ],
    )
    assert {v.sibling_package_id for v in out} == {"a", "b"}


def test_multiple_shared_districts_listed():
    out = find_consistency_violations(
        this_parameter_ids=frozenset({"P1"}),
        this_districts={("S1", "D1"), ("S1", "D2"), ("S1", "D3")},
        siblings=[_sib(
            "sib", "Big Sib", {"P1", "P2"},
            [("S1", "D1"), ("S1", "D2")],
        )],
    )
    assert len(out) == 1
    assert out[0].shared_districts == (("S1", "D1"), ("S1", "D2"))


def test_this_pkg_with_no_districts_no_violation():
    out = find_consistency_violations(
        this_parameter_ids=frozenset({"P1"}),
        this_districts=set(),
        siblings=[_sib("sib", "Sib", {"P1", "P2"}, [("S1", "D1")])],
    )
    assert out == []
