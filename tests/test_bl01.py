"""
BL-01 — PoP Guided Elimination Algorithm
All test cases from RootsTalk_Dev_TestCases.pdf §BL-01.
"""
import pytest
from app.services.bl01_guided_elimination import (
    run_elimination, PackageStub, ParameterOption, VariableOption, EliminationStep
)


# ── Helpers ────────────────────────────────────────────────────────────────────

def make_package(id: str, name: str, var_map: dict) -> PackageStub:
    return PackageStub(id=id, name=name, description=None, variable_map=var_map)

def make_param(id: str, name: str, order: int = 0) -> ParameterOption:
    return ParameterOption(id=id, name=name, display_order=order)

def make_var_names(*pairs) -> dict:
    """pairs: (var_id, name)"""
    return {v: n for v, n in pairs}


# ── TC-BL01-01: Single parameter, two variables ───────────────────────────────

def test_bl01_01_single_param_two_vars_selects_correct_package():
    """TC-BL01-01: Farmer selects Variable A. Pool of 2 packages. Should return Package 1."""
    pkg1 = make_package("pkg1", "Package 1", {"p1": "varA"})
    pkg2 = make_package("pkg2", "Package 2", {"p1": "varB"})
    params = [make_param("p1", "Season", 1)]
    var_names = {"varA": "Kharif", "varB": "Rabi"}

    result = run_elimination([pkg1, pkg2], params, {"p1": "varA"}, var_names)

    assert result.done is True
    assert result.package is not None
    assert result.package.id == "pkg1"
    assert result.error is None


# ── TC-BL01-02: Three parameters, sequential elimination ──────────────────────

def test_bl01_02_three_params_sequential_elimination():
    """TC-BL01-02: 6 packages, 3 params (Season, Duration, Soil). Pool reduces 6→3→2→1."""
    packages = [
        make_package("p1", "P1", {"season": "kharif", "duration": "short",  "soil": "clay"}),
        make_package("p2", "P2", {"season": "kharif", "duration": "short",  "soil": "sandy"}),
        make_package("p3", "P3", {"season": "kharif", "duration": "long",   "soil": "clay"}),
        make_package("p4", "P4", {"season": "kharif", "duration": "long",   "soil": "sandy"}),
        make_package("p5", "P5", {"season": "rabi",   "duration": "short",  "soil": "clay"}),
        make_package("p6", "P6", {"season": "rabi",   "duration": "long",   "soil": "loam"}),
    ]
    params = [make_param("season", "Season", 1), make_param("duration", "Duration", 2), make_param("soil", "Soil", 3)]
    var_names = {"kharif": "Kharif", "rabi": "Rabi", "short": "Short Duration", "long": "Long Duration", "clay": "Clay", "sandy": "Sandy", "loam": "Loam"}

    # After: season=kharif (4 remain), duration=short (2 remain), soil=clay (1 remains)
    result = run_elimination(packages, params, {"season": "kharif", "duration": "short", "soil": "clay"}, var_names)

    assert result.done is True
    assert result.package.id == "p1"
    assert "Kharif" in result.summary
    assert "Short Duration" in result.summary
    assert "Clay" in result.summary


# ── TC-BL01-03: Single-option parameter is auto-skipped ───────────────────────

def test_bl01_03_single_option_param_auto_skipped():
    """TC-BL01-03: Parameter 1 has only 1 variable matching pool. Auto-selected. Farmer sees only Param 2."""
    pkg1 = make_package("pkg1", "Package 1", {"p1": "common_only", "p2": "varA"})
    pkg2 = make_package("pkg2", "Package 2", {"p1": "common_only", "p2": "varB"})
    params = [make_param("p1", "Common Feature", 1), make_param("p2", "Season", 2)]
    var_names = {"common_only": "Common", "varA": "Kharif", "varB": "Rabi"}

    # No answers yet — should auto-skip p1 and ask p2
    result = run_elimination([pkg1, pkg2], params, {}, var_names)

    assert result.done is False
    assert result.parameter is not None
    assert result.parameter.id == "p2"
    assert result.auto_selected is True
    assert len(result.variables) == 2


# ── TC-BL01-04: Dead end is structurally impossible ───────────────────────────

def test_bl01_04_dead_end_structurally_impossible():
    """TC-BL01-04: Remaining pool = 2 packages, both require same Variable for Param 3. Only one shown."""
    pkg1 = make_package("pkg1", "P1", {"p1": "a", "p2": "x", "p3": "same"})
    pkg2 = make_package("pkg2", "P2", {"p1": "a", "p2": "y", "p3": "same"})
    params = [make_param("p1", "P1", 1), make_param("p2", "P2", 2), make_param("p3", "P3", 3)]
    var_names = {"a": "A", "x": "X", "y": "Y", "same": "Common"}

    # After answering p1=a, p2=x → pool=[pkg1]; p3 has only one option "same" → auto-select
    result = run_elimination([pkg1, pkg2], params, {"p1": "a", "p2": "x"}, var_names)

    assert result.done is True
    assert result.package.id == "pkg1"


# ── TC-BL01-05: Pool reaches 0 — configuration error ─────────────────────────

def test_bl01_05_empty_pool_configuration_error():
    """TC-BL01-05: No package matches all selected variables → DATA_CONFIG_ERROR."""
    pkg1 = make_package("pkg1", "P1", {"p1": "a"})
    pkg2 = make_package("pkg2", "P2", {"p1": "b"})
    params = [make_param("p1", "Season", 1)]
    var_names = {"a": "A", "b": "B", "c": "C"}

    result = run_elimination([pkg1, pkg2], params, {"p1": "c"}, var_names)

    assert result.error == "DATA_CONFIG_ERROR"
    assert result.done is False
    assert result.package is None


# ── TC-BL01-06: Farmer declines confirmation — reset ─────────────────────────

def test_bl01_06_reset_after_decline():
    """TC-BL01-06: After guided elimination completes, if farmer declines, caller passes empty answers.
    Service returns first question again."""
    pkg1 = make_package("pkg1", "P1", {"p1": "a"})
    pkg2 = make_package("pkg2", "P2", {"p1": "b"})
    params = [make_param("p1", "Season", 1)]
    var_names = {"a": "Kharif", "b": "Rabi"}

    # With empty answers = reset state
    result = run_elimination([pkg1, pkg2], params, {}, var_names)

    assert result.done is False
    assert result.parameter is not None
    assert result.parameter.id == "p1"
    assert len(result.variables) == 2
    assert result.remaining_count == 2


# ── TC-BL01-07: Already at single package before first question ───────────────

def test_bl01_07_single_package_in_pool_no_questions():
    """Single package in district — skip directly to confirmation, no questions asked."""
    pkg1 = make_package("pkg1", "P1", {"p1": "a"})
    params = [make_param("p1", "Season", 1)]
    var_names = {"a": "Kharif"}

    result = run_elimination([pkg1], params, {}, var_names)

    assert result.done is True
    assert result.package.id == "pkg1"
    assert result.error is None


# ── TC-BL01-08: Confirmation summary lists only variable names ────────────────

def test_bl01_08_confirmation_summary_variable_names_only():
    """Summary shows variable display names, NOT parameter names or package name."""
    pkg1 = make_package("pkg1", "Paddy Kharif Package", {"season": "kharif", "duration": "short"})
    params = [make_param("season", "Season", 1), make_param("duration", "Duration", 2)]
    var_names = {"kharif": "Kharif", "short": "Short Duration (90 days)"}

    result = run_elimination([pkg1], params, {"season": "kharif", "duration": "short"}, var_names)

    assert result.done is True
    assert "Kharif" in result.summary
    assert "Short Duration (90 days)" in result.summary
    assert "Season" not in result.summary   # parameter name should NOT appear
    assert "Paddy Kharif Package" not in result.summary  # package name should NOT appear


# ── TC-BL01-09: Parameters checked in display_order, not alphabetical ─────────

def test_bl01_09_parameters_in_display_order():
    """a_first (display_order=1) must be asked before z_last (display_order=2).
    Both params have 2 distinct variables so neither auto-skips."""
    pkg1 = make_package("pkg1", "P1", {"z_last": "z_a", "a_first": "a_x"})
    pkg2 = make_package("pkg2", "P2", {"z_last": "z_b", "a_first": "a_y"})
    params = [make_param("a_first", "First Question", 1), make_param("z_last", "Last Question", 2)]
    var_names = {"z_a": "Z Alpha", "z_b": "Z Beta", "a_x": "A Alpha", "a_y": "A Beta"}

    result = run_elimination([pkg1, pkg2], params, {}, var_names)

    assert result.done is False
    assert result.parameter.id == "a_first"  # display_order=1 beats z_last (order=2)
    assert len(result.variables) == 2


# ── TC-BL01-10: Multiple auto-selections chain correctly ──────────────────────

def test_bl01_10_multiple_single_options_chain():
    """If first two parameters both have only one valid variable in pool, both auto-skip. Third is asked."""
    pkg1 = make_package("pkg1", "P1", {"p1": "only1", "p2": "only2", "p3": "varA"})
    pkg2 = make_package("pkg2", "P2", {"p1": "only1", "p2": "only2", "p3": "varB"})
    params = [make_param("p1", "P1", 1), make_param("p2", "P2", 2), make_param("p3", "P3", 3)]
    var_names = {"only1": "Only Option 1", "only2": "Only Option 2", "varA": "Kharif", "varB": "Rabi"}

    result = run_elimination([pkg1, pkg2], params, {}, var_names)

    assert result.done is False
    assert result.parameter.id == "p3"   # p1 and p2 were auto-skipped
    assert result.auto_selected is True
    assert len(result.variables) == 2
