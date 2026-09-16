# Advisory-Only Mode — v1 Scoping (rev 4)

**Author**: yb + Claude
**Draft date**: 2026-09-16 (rev 4 — CHA/Q&A/Pundit surfaces folded in)
**Status**: DRAFT — ready for build.

---

## 1. Purpose

Support **university-type clients** who publish crop advisories but don't run a dealer network and don't want to appear biased toward any brand or dealer. Farmers on their subscriptions:

- See input details up front, no brand-lock
- Get a **Brands** button per fertiliser/pesticide → Cosh brand list + "not exhaustive / no endorsement" disclaimer
- Get a **Recommended Seed Varieties** tile on the crop dashboard → reuses existing farmer-facing seed-varieties page, read-only
- Optionally get a **Nearby Dealers** tile → 5 nearest onboarded dealers, informational (Call + Map, no Orders)
- Get a **date picker** on the advisory screen → look forward/backward across the crop timeline
- Buy inputs from any dealer of their choice — no in-app order flow

Universities that produce/sell their own seeds simply mark themselves as **Seed Company** (existing org type). Composes naturally with advisory-only mode — no special code path.

Subscription fee is **flat ₹99 per crop** (per subscription), applied uniformly to farmer-pays and company-pays channels; no bulk discount.

## 2. Design principles (locked)

1. Per-client, opt-in — two Booleans, both default off.
2. Snapshot on Subscription at create; immutable after.
3. Never read the flag globally — always from the specific subscription.
4. Byte-identical experience for non-opted-in clients.
5. Order flow entirely off for advisory-only. Farmer-to-dealer inventory model is a separate platform.
6. Composability with existing org types — university + seed-company = both hats, natural.
7. Pricing follows the same snapshot pattern — captured on Subscription at create, immutable after.

## 3. Non-goals

- Dealer inventory + farmer-to-dealer ordering
- Local-availability filter on brands
- University-curated brand lists
- Coaching sandbox parity
- CA-portal farmer-preview
- Order-lifecycle mechanics
- CA-side variety-authoring for non-Seed-Company universities
- Enterprise-License model for universities (EL is for very large clients negotiated offline; universities go through the standard FARMER_PAYS + COMPANY_PAYS pool with flat ₹99 pricing)

## 4. Two flags + pricing field

Three new nullable columns on Client, three matching snapshot columns on Subscription.

### 4.1 `Client.advisory_only_mode` (bool, default FALSE)

Main mode toggle. When TRUE, farmer PWA hides all order-related UI, forces FARMER_PAYS, shows Brands + Seed Varieties + date picker + Advisory Only chip.

### 4.2 `Client.dealer_list_enabled` (bool, default FALSE)

Optional add-on, meaningful only when `advisory_only_mode` is TRUE. When both TRUE, farmer's crop dashboard shows a Nearby Dealers tile → read-only 5-nearest-dealer list with Call + Map.

SA-portal UI: greyed out unless Advisory-only is checked. Backend: no cross-flag validation.

### 4.3 `Client.subscription_fee_paise` (int, nullable)

Per-unit subscription fee override. NULL → existing bulk-discount pricing logic applies (traditional clients). Non-null → flat multiplication `qty × subscription_fee_paise` on both FARMER_PAYS and COMPANY_PAYS pool top-up flows; bulk-discount table skipped.

SA-portal default when Advisory-only is ticked: **9900** (₹99). Editable per client so a university with a negotiated different price can be set individually. SA can revise anytime; existing subscriptions retain their snapshotted value.

## 5. Data model

```sql
ALTER TABLE clients ADD COLUMN advisory_only_mode      BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE clients ADD COLUMN dealer_list_enabled     BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE clients ADD COLUMN subscription_fee_paise  INTEGER;  -- NULL = use existing bulk-discount logic

ALTER TABLE subscriptions ADD COLUMN advisory_only_mode      BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE subscriptions ADD COLUMN dealer_list_enabled     BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE subscriptions ADD COLUMN subscription_fee_paise  INTEGER;  -- snapshotted at create
```

Rules:
- Client columns: SA-editable, mutable. Flipping later takes effect on **new** subscriptions only.
- Subscription columns: **snapshot at row creation**. Immutable after (like `Subscription.package_id`).
- Backfill: existing rows → FALSE / NULL. No functional change.
- Every subscription-scoped API response includes all three fields so the PWA can render + price correctly without extra calls.

## 6. SA-portal — two-checkbox section + fee field

Client onboarding + edit form adds:

```
▢ Advisory-only mode
    Farmer sees input details up front and buys from any dealer.
    In-app ordering, dealer/facilitator payment routing, and brand-lock
    are hidden. This flag can be flipped anytime — existing subscriptions
    keep the mode captured when they were created; only new subscriptions
    pick up the current setting.

  ▢ Show nearby-dealers list  [greyed out unless above is checked]
      Include a read-only list of the 5 nearest onboarded dealers on the
      farmer's crop dashboard, with search-by-location and map view.
      No orders can be placed from this list.

  Subscription fee per crop:  [₹  99  ]    [defaults to 99 when Advisory-only ticked]
      Applies uniformly to farmer-pays and company-pays channels.
      Bulk discounts do not apply. Leave empty for standard pricing
      (only meaningful for non-advisory-only clients).
```

## 7. Farmer PWA — surfaces

All rendering gated on `subscription.advisory_only_mode` (and `dealer_list_enabled` where relevant), read from the specific subscription's snapshot.

### 7.1 Advisory screen

- Reveal brand-family up front (no brand-lock)
- **Brands** button per fertiliser/pesticide input → Brands screen (§7.2)
- Date picker at top: `◀ Fri 15 Sep ▶ · Today` — bounded to `[crop_start_date, crop_end_date]`. Backend adds `?date=YYYY-MM-DD` on existing advisory endpoint.
- Hide order-status pills, "Place order" CTA, postpone/approve actions

### 7.2 Brands screen (new — fertilisers/pesticides only)

- Header: **Some brands available in the market**
- Body: brand + manufacturer, one per row (Cosh catalog, reuses dealer-side query)
- Disclaimer: *"This list is not exhaustive. {ClientName} does not endorse the purchase of any brand."*
- No actions

### 7.3 Recommended Seed Varieties tile + page

- Tile on crop dashboard
- Detail: reuse existing `/subscribe/seed-varieties/[subscriptionId]` — read-only variant via conditional-inside-route (hide order/purchase buttons when advisory-only)
- Content: all Cosh varieties matching the crop, each entry naturally labelled with its authoring seed company (including university's own if they're Seed Company)
- **"How to buy" info** on each variety entry belonging to a client: surface existing contact fields — phone (native `tel:`) + address / website
- Disclaimer: *"This list is not exhaustive. {ClientName} does not endorse the purchase of varieties from any other company."*

### 7.4 Crop dashboard

- Hide Orders tile, QR share, Received items tab
- Show **Advisory Only** chip in header
- Show **Recommended Seed Varieties** tile
- Show **Nearby Dealers** tile ONLY when `dealer_list_enabled` also TRUE

### 7.5 Nearby Dealers screen (new, when `dealer_list_enabled`)

- 5 nearest onboarded dealers (reuses existing "nearby dealers" backend query — same as subscribe-flow dealer picker)
- Row: shop name + address + categories + Call button (native `tel:`)
- Search-by-current-location + view-on-map (existing primitives)
- Empty state: *"No onboarded dealers within range."*
- No Orders / Payment / anything actionable beyond Call + Map

### 7.6 Subscribe flow

- **Advisory Only** chip next to opted-in client in the picker
- **Price line shows ₹99** (or the snapshotted `Client.subscription_fee_paise` value) prominently, with a one-liner: *"Flat ₹99 per crop — no bulk discount."*
- Force FARMER_PAYS channel; hide dealer/facilitator payment routing

### 7.7 My Subscriptions + Home + /orders retrospective

- Subscription card carries **Advisory Only** chip
- Cross-crop tiles that assume orders render empty for advisory-only subs

### 7.8 CHA / Q&A / Farm Pundit surfaces — the "any input recommendation" rule

The advisory-only mode rules apply uniformly to **every surface where an input is recommended to the farmer**, not just the CCA advisory screen. This includes:

- **CHA — Problem Group advisory** — input recommendations reveal upfront, Brands button per fertiliser/pesticide, order buttons hidden.
- **CHA — Specific Problem advisory** — same rules.
- **Q&A — Standard Response** rendered to the farmer — if it references a structured input, apply the same rules to that reference.
- **Q&A — Farm Pundit reply** — same rules for any structured input reference. Free-form prose in the reply is displayed as-is; the rules apply only to structured input attachments.
- **Future — Soil Analysis Advisory (SAAS)** and any subsequent module emitting input recommendations. Same principle.

**Implementation pattern**: funnel every input-recommendation render through a shared component / hook that reads `subscription.advisory_only_mode` from the sub context and renders accordingly. Every surface above already has subscription context available (all farmer-facing content is scoped to a specific crop = subscription), so no context threading is needed beyond passing the sub.

If the shared component doesn't exist today, build it once and every current + future surface benefits. Estimated additional effort: ~0.5 day.

## 8. CA-portal — pool top-up UI

The existing pool top-up flow (used by any client's CA for COMPANY_PAYS bulk purchases) needs one small addition: when the client is advisory-only, the pricing preview reads:

```
Units to buy:     100
Price per unit:   ₹99
Total:            ₹9,900
                  (No bulk discount — flat pricing)
```

vs the traditional flow which shows the bulk-discount tier breakdown.

Backend service: the price-calculation function branches on `Client.subscription_fee_paise` — if non-null, return `qty × subscription_fee_paise`; else use existing bulk-discount table.

## 9. Seeds — special handling

- **Universities recommending seeds**: farmer sees all Cosh varieties matching the crop
- **Universities producing/selling their own seeds**: mark themselves as **Seed Company** (existing org type, Cosh UUID `4b0847f9-…`). SDM privileges unlock; they author their own varieties in Cosh; those varieties appear naturally in the list labelled "By {University name}"
- **Implementation-time check**: verify Seed Company org type auto-provisions SDM role capability today, vs requiring separate role assignment
- **"How to buy"**: contact info surfaced on the client's own variety entries
- **No in-app seed order flow** — sold offline via university's existing channels (KVK, phone, Krishi Melas)

## 10. Payment channels + pricing (summary)

| Channel | Traditional | Advisory-only |
|---|---|---|
| FARMER_PAYS direct (Razorpay) | Bulk-discount lookup by qty | Flat: `qty × ₹99` |
| COMPANY_PAYS pool top-up (Razorpay) | Bulk-discount lookup by qty | Flat: `qty × ₹99` |
| Promoter-assign from pool | Deducts 1 unit | Deducts 1 unit (no pricing at assign time) |
| Enterprise License | Offline negotiated | **Not offered for advisory-only clients** |
| Dealer/facilitator share-link | Available | **Hidden** — farmer must pay directly |

## 11. Actors — who sees what

| Actor | Change |
|---|---|
| **SA** | New 2-checkbox + fee-field section on client onboarding + edit |
| **CA / SE / SDM** | No functional change to authoring; CA sees flat-pricing preview on pool top-up |
| **Dealer / Facilitator** | No code change. Don't receive orders from advisory-only subs. Farmer Ledger + CMS still work |
| **Farmer on opted-in client** | Six UI changes + Advisory Only chip + flat ₹99 pricing |
| **Farmer on non-opted-in client** | Zero change |
| **Farmer on both** | Each subscription card renders under its own mode + its own snapshotted price |

## 12. "Advisory Only" chip — five placements

1. Client picker in subscribe flow
2. My Subscriptions card header
3. Crop dashboard header
4. Any farmer-visible title showing the client name
5. Promoter-assign preview

## 13. Coaching + Training sandbox

- Coaching: OUT of scope; trainers cover verbally
- Training sandbox: aligned with parent client via existing snapshot logic; natural

## 14. Backend order-state audit (mandatory before UI hides)

Every consumer of order state needs one of: graceful empty-state, conditional short-circuit, or explicit hide.

- **BL-02 conditional-answer stickiness** — fallback: no orders → treat as "not yet purchased" → question stays on regular schedule
- **Timeline progression** — time-based only under advisory-only
- **START_DATE alert** — unchanged
- **INPUT alert** — CTA copy variant: *"input details are in RootsTalk — purchase from any local dealer"*
- **Purchased items retrospective** — empty, renders cleanly
- **QR crop-record verification** — hidden per §7.4
- **Reports / analytics** — university-client subs will have zero order data; empty tiles, no schema break
- **CHA → order create path** — short-circuit for advisory-only subs; no order created regardless of upstream trigger
- **Q&A Standard Response → order create path** (if any) — same short-circuit
- **Farm Pundit reply → order create path** (if any) — same short-circuit

**~4–6 hours grep-and-verify before writing UI hides.** Non-negotiable.

## 15. Rollout phases

| Phase | Scope | Est. |
|---|---|---|
| **v1.0** | Backend: 6 new columns (Client × 3 + Subscription × 3) + snapshot logic on subscribe + SA-portal 2-checkbox + fee-field section + backend order-state audit + date-param on advisory endpoint + pricing-service branch for flat-fee flow | ~1.5 days |
| **v1.1** | Farmer PWA UI hides + chip in 5 placements + date picker + INPUT alert CTA variant + payment routing hide + pricing display on subscribe flow | ~1.5 days |
| **v1.2** | Brands screen + Recommended Seed Varieties tile + read-only seed-varieties page variant + "how to buy" contact info + Nearby Dealers tile + Nearby Dealers screen + CA-portal pool top-up flat-pricing preview + shared input-recommendation component wrapping CHA / Q&A / Pundit surfaces | ~2 days |
| **v1.3** | i18n batched with the deferred hi/ta/kn translations project | ~0.5 day |

**Total v1**: ~5.5 engineering days + your QA cycles. First-cohort ship: **~1 week** from start.

## 16. Open items (none blocking)

- **Implementation-time verification**: does setting the Seed Company org type on a client auto-provision SDM role capability, or is it a separate role assignment? (Operational; not blocking scoping.)
- **First-university-onboarding review**: verify CA-portal advisory-authoring UI allows recommending a seed variety at the variety level (company-agnostic) when the recommending client isn't a Seed Company. Small enhancement possibly needed at first university onboarding — not blocking build.
- **Payment-fee absorption**: is ₹99 gross-to-farmer (we absorb ~₹2 Razorpay fee) or net-to-us (farmer charged ~₹101)? Same for pool top-up. Business decision — doesn't affect scoping (we just charge whatever the config says). Decide before first university goes live.

## 17. Deliberately not building

- University-curated brand lists
- Local-availability filter
- Farmer-to-dealer inventory-mediated ordering
- CA-portal Farmer preview
- Coaching sandbox integration
- Special order flow for seeds — even for Seed-Company universities. Contact info surfaces on their variety entries; farmer reaches out offline.
- Enterprise License for advisory-only clients — flat ₹99 through the standard payment channels covers this.
- Additional flag axes beyond the two + pricing field.

## 18. Companion documents

- CMS v1 scoping: `docs/CMS_v1_scoping.md`
- Add Sale v2 scoping: `docs/AddSale_v2_scoping.md`
- Feedback memory: `feedback_prefer_domain_labels_over_actor_action.md` — applies to the SA-portal helper text wording.
