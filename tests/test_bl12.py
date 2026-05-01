"""
BL-12a — FarmPundit Query Routing Algorithm
Test cases from RootsTalk_Dev_TestCases.pdf §BL-12 plus additional routing edge cases.
"""
import pytest
from datetime import datetime, timezone, timedelta
from app.services.bl12_query_routing import route_query, ExpertSlot, RoutingResult


# ── Helpers ────────────────────────────────────────────────────────────────────

def expert(pundit_id: str, role: str = "PRIMARY", status: str = "ACTIVE",
           seq: int = 0, promoter: bool = False, days_ago: int = 0) -> ExpertSlot:
    return ExpertSlot(
        pundit_id=pundit_id,
        role=role,
        status=status,
        round_robin_sequence=seq,
        is_promoter_pundit=promoter,
        onboarded_at=datetime.now(timezone.utc) - timedelta(days=days_ago),
    )


# ── TC-BL12-01: Round-robin distributes across 3 experts ──────────────────────

def test_bl12_01_round_robin_three_experts():
    """Queries 1, 2, 3 → E1, E2, E3 in order."""
    experts = [expert("E1", days_ago=30, seq=1), expert("E2", days_ago=20, seq=2), expert("E3", days_ago=10, seq=3)]

    r1 = route_query(experts, None, None, None)        # No previous → E1
    r2 = route_query(experts, None, None, "E1")        # Last was E1 → E2
    r3 = route_query(experts, None, None, "E2")        # Last was E2 → E3

    assert r1.pundit_id == "E1" and r1.reason == "ROUND_ROBIN"
    assert r2.pundit_id == "E2" and r2.reason == "ROUND_ROBIN"
    assert r3.pundit_id == "E3" and r3.reason == "ROUND_ROBIN"


# ── TC-BL12-02: Round-robin wraps to beginning ────────────────────────────────

def test_bl12_02_round_robin_wraps():
    """After E3, next query → E1."""
    experts = [expert("E1", days_ago=30, seq=1), expert("E2", days_ago=20, seq=2), expert("E3", days_ago=10, seq=3)]

    result = route_query(experts, None, None, "E3")  # Last was E3 → wraps to E1

    assert result.pundit_id == "E1"
    assert result.reason == "ROUND_ROBIN"


# ── TC-BL12-03: Inactive expert skipped ──────────────────────────────────────

def test_bl12_03_inactive_expert_skipped():
    """E1 ACTIVE, E2 INACTIVE, E3 ACTIVE. Last was E1 → skip E2 → E3."""
    experts = [
        expert("E1", days_ago=30, seq=1, status="ACTIVE"),
        expert("E2", days_ago=20, seq=2, status="INACTIVE"),
        expert("E3", days_ago=10, seq=3, status="ACTIVE"),
    ]

    result = route_query(experts, None, None, "E1")

    assert result.pundit_id == "E3"   # E2 is skipped
    assert result.reason == "ROUND_ROBIN"


# ── TC-BL12-04: Panel Expert cannot receive round-robin queries ───────────────

def test_bl12_04_panel_expert_not_in_round_robin():
    """Panel Experts are never included in the round-robin pool."""
    experts = [
        expert("E1", role="PRIMARY", days_ago=30, seq=1),
        expert("P1", role="PANEL",   days_ago=20),  # Panel
    ]

    r1 = route_query(experts, None, None, None)
    r2 = route_query(experts, None, None, "E1")  # After E1, wraps — P1 not included

    assert r1.pundit_id == "E1"
    assert r2.pundit_id == "E1"  # Only one PRIMARY → wraps back to E1


# ── TC-BL12-05: Farmer preference overrides round-robin ──────────────────────

def test_bl12_05_farmer_preference_wins():
    """TC-BL12-07: Farmer set specific FarmPundit → round-robin ignored."""
    experts = [
        expert("E1", days_ago=30, seq=1),
        expert("E2", days_ago=20, seq=2),
    ]

    # Farmer prefers E2 explicitly
    result = route_query(experts, farmer_preferred_pundit_id="E2", promoter_pundit_id=None, last_received_pundit_id="E2")

    assert result.pundit_id == "E2"
    assert result.reason == "PREFERENCE"


# ── TC-BL12-06: Promoter-Pundit routing (priority 2) ─────────────────────────

def test_bl12_06_promoter_pundit_overrides_round_robin():
    """When subscription was assigned by a Promoter-Pundit, queries go to them (not round-robin)."""
    experts = [
        expert("E1", days_ago=30, seq=1),
        expert("PP1", days_ago=20, seq=2, promoter=True),  # Promoter-Pundit
    ]

    result = route_query(experts, farmer_preferred_pundit_id=None, promoter_pundit_id="PP1", last_received_pundit_id=None)

    assert result.pundit_id == "PP1"
    assert result.reason == "PROMOTER_PUNDIT"


# ── TC-BL12-07: Farmer preference wins over Promoter-Pundit ──────────────────

def test_bl12_07_preference_beats_promoter_pundit():
    """Priority 1 (farmer preference) beats Priority 2 (Promoter-Pundit)."""
    experts = [
        expert("E1", days_ago=30, seq=1),
        expert("PP1", days_ago=20, seq=2, promoter=True),
        expert("E3", days_ago=10, seq=3),
    ]

    result = route_query(
        experts,
        farmer_preferred_pundit_id="E3",   # Farmer explicitly chose E3
        promoter_pundit_id="PP1",           # Promoter-Pundit is PP1
        last_received_pundit_id=None,
    )

    assert result.pundit_id == "E3"
    assert result.reason == "PREFERENCE"


# ── TC-BL12-08: No active primary experts → error state ──────────────────────

def test_bl12_08_no_active_experts():
    """TC-BL12-08 variant: If no PRIMARY ACTIVE experts → NO_ACTIVE_EXPERTS error."""
    experts = [
        expert("E1", status="INACTIVE"),
        expert("P1", role="PANEL"),
    ]

    result = route_query(experts, None, None, None)

    assert result.pundit_id is None
    assert result.reason == "NO_ACTIVE_EXPERTS"


# ── TC-BL12-09: Last recipient no longer active → restart from beginning ──────

def test_bl12_09_last_recipient_deactivated():
    """Last recipient became INACTIVE since last query → restart from first ACTIVE expert."""
    experts = [
        expert("E1", days_ago=30, seq=1, status="ACTIVE"),
        expert("E2", days_ago=20, seq=2, status="INACTIVE"),  # was last, now inactive
        expert("E3", days_ago=10, seq=3, status="ACTIVE"),
    ]

    # E2 was last recipient but is now INACTIVE → restart at E1
    result = route_query(experts, None, None, "E2")

    assert result.pundit_id == "E1"
    assert result.reason == "ROUND_ROBIN"


# ── TC-BL12-10: Single expert always receives ────────────────────────────────

def test_bl12_10_single_expert_always_receives():
    """With one PRIMARY ACTIVE expert, every query goes to them."""
    experts = [expert("E1", seq=1)]

    r1 = route_query(experts, None, None, None)
    r2 = route_query(experts, None, None, "E1")
    r3 = route_query(experts, None, None, "E1")

    assert all(r.pundit_id == "E1" for r in [r1, r2, r3])


# ── TC-BL12-11: Preference for inactive pundit falls through to round-robin ───

def test_bl12_11_inactive_preferred_pundit_falls_to_round_robin():
    """Farmer preference set for an inactive pundit → falls through to round-robin."""
    experts = [
        expert("E1", days_ago=30, seq=1),
        expert("PREF", days_ago=20, status="INACTIVE"),  # Preferred but inactive
    ]

    result = route_query(experts, farmer_preferred_pundit_id="PREF", promoter_pundit_id=None, last_received_pundit_id=None)

    assert result.pundit_id == "E1"   # Falls through to round-robin
    assert result.reason == "ROUND_ROBIN"
