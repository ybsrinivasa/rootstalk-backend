"""BL-14 — Farmer Approval Flow (pure functions, no DB).

Spec rules:
- After dealer submits, approval ALWAYS goes to the FARMER, never the
  facilitator. Enforced at routing layer (no facilitator approval
  endpoint exists; live router only exposes
  /farmer/orders/{id}/items/{id}/approve|reject|approve-all).
- Facilitator gets an FCM alert ("Your farmer needs to approve").
  TODO state today — wiring blocked on Firebase key (rootstalk-2caa0),
  same dependency as the BL-09 / BL-12 FCM TODOs.
- **Brand revealed to farmer for the FIRST TIME at this step.** The
  helper below encodes that rule so `get_farmer_order_detail` and any
  future approval-step views show the canonical brand_name (canonical
  per BL-07) starting at SENT_FOR_APPROVAL — not earlier (farmer
  shouldn't know the dealer's preferred brand before the dealer
  commits to it) and not later (farmer can't approve blind).
"""
from __future__ import annotations


# Statuses at which the farmer may see the canonical brand_name on an
# OrderItem. Pre-SENT_FOR_APPROVAL the dealer is still working out
# brand selection; post-SENT_FOR_APPROVAL the farmer needs the brand
# to make their approval decision (and it stays visible after they
# approve, for the purchased-items view).
_BRAND_VISIBLE_STATUSES: frozenset[str] = frozenset({
    "SENT_FOR_APPROVAL", "APPROVED",
})


def is_brand_visible_to_farmer(item_status: str) -> bool:
    """True iff the farmer's view of an OrderItem should include the
    canonical brand_name. Encodes the BL-14 spec rule that brand is
    revealed at the approval step and stays visible afterwards."""
    return item_status in _BRAND_VISIBLE_STATUSES
