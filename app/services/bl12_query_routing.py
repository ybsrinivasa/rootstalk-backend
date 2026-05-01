"""
BL-12a — FarmPundit Query Routing Algorithm
Pure function service. No database access.
Spec: RootsTalk_Dev_BusinessLogic.pdf §BL-12a

Priority order:
1. Farmer has a specific FarmPundit preference set for this subscription.
2. Subscription was assigned by a Promoter-Pundit.
3. Default: sequential round-robin among PRIMARY experts (ordered by onboarded_at).
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class ExpertSlot:
    """One row from client_farm_pundits."""
    pundit_id: str
    role: str              # PRIMARY | PANEL
    status: str            # ACTIVE | INACTIVE
    round_robin_sequence: int
    is_promoter_pundit: bool
    onboarded_at: datetime


@dataclass
class RoutingResult:
    pundit_id: Optional[str]
    reason: str            # "PREFERENCE" | "PROMOTER_PUNDIT" | "ROUND_ROBIN" | "NO_ACTIVE_EXPERTS"


def route_query(
    experts: list[ExpertSlot],           # all client_farm_pundits for this client
    farmer_preferred_pundit_id: Optional[str],   # from farm_pundit_preferences
    promoter_pundit_id: Optional[str],   # facilitator marked as Promoter-Pundit who assigned this sub
    last_received_pundit_id: Optional[str],      # current_holder_id of the last NEW query
) -> RoutingResult:
    """
    BL-12a: Determine which FarmPundit receives the new query.

    Priority 1: Farmer preference
    Priority 2: Promoter-Pundit who assigned the subscription
    Priority 3: Round-robin among PRIMARY ACTIVE experts

    Round-robin: ordered by onboarded_at (ascending). Wraps to first after last.
    Inactive experts are skipped.
    """
    # Priority 1: Farmer preference
    if farmer_preferred_pundit_id:
        # Verify the preferred pundit is ACTIVE for this company
        match = next(
            (e for e in experts if e.pundit_id == farmer_preferred_pundit_id and e.status == "ACTIVE"),
            None,
        )
        if match:
            return RoutingResult(pundit_id=farmer_preferred_pundit_id, reason="PREFERENCE")

    # Priority 2: Promoter-Pundit
    if promoter_pundit_id:
        match = next(
            (e for e in experts
             if e.pundit_id == promoter_pundit_id and e.status == "ACTIVE" and e.is_promoter_pundit),
            None,
        )
        if match:
            return RoutingResult(pundit_id=promoter_pundit_id, reason="PROMOTER_PUNDIT")

    # Priority 3: Round-robin among PRIMARY ACTIVE experts
    primaries = sorted(
        [e for e in experts if e.role == "PRIMARY" and e.status == "ACTIVE"],
        key=lambda e: (e.onboarded_at, e.round_robin_sequence or 0),
    )

    if not primaries:
        return RoutingResult(pundit_id=None, reason="NO_ACTIVE_EXPERTS")

    if last_received_pundit_id is None:
        return RoutingResult(pundit_id=primaries[0].pundit_id, reason="ROUND_ROBIN")

    # Find the last recipient's position and advance to next (wrapping)
    idx = next(
        (i for i, e in enumerate(primaries) if e.pundit_id == last_received_pundit_id),
        None,
    )

    if idx is None:
        # Last recipient is no longer active — start from beginning
        return RoutingResult(pundit_id=primaries[0].pundit_id, reason="ROUND_ROBIN")

    next_idx = (idx + 1) % len(primaries)
    return RoutingResult(pundit_id=primaries[next_idx].pundit_id, reason="ROUND_ROBIN")
