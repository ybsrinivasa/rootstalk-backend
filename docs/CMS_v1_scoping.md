# Credit Management System (CMS) — v1 Scoping

**Author**: yb + Claude
**Draft date**: 2026-09-14 (v2 — decisions locked)
**Status**: SCOPE LOCKED — ready for implementation kickoff.

---

## 1. Purpose

Give **dealers** and **farmers** a shared, dual-confirmed running account of credit sales and payments. Both parties see the same balance; either side can propose entries; the other side confirms or disputes. Automatic reminders on both sides; a behavioural trust score for the dealer's own reference.

**Why now**: dealer stickiness (evening reconciliation habit) + farmer install pressure (dealer records credit here → farmer must install to see it) → this is the module most likely to pull both sides into daily use.

## 2. Design principles (locked)

Three principles that shape every decision in this doc:

1. **We record; we don't route.** RootsTalk shows the context of an owed amount and helps the farmer copy the dealer's UPI ID. We do **not** generate UPI intent links, deep-link to UPI apps, verify payment status, receive PSP webhooks, or handle any actual money movement. This keeps us out of PSP licensing, refund liability, and chargeback risk.
2. **The dealer and farmer agree; we don't interpose.** Due dates, amounts, and terms are set by the dealer, agreed by the farmer. RootsTalk doesn't set defaults, doesn't suggest fairness, doesn't judge. Reminders and the trust score both measure adherence to the **agreed** terms — not to any standard we've invented.
3. **Confirmed entries are contracts.** Once both sides sign an entry, it's immutable — amount, due date, notes, everything. Any correction goes through a paired-void flow (both sign a new entry). This makes CONFIRMED a real commitment, not just a checkpoint.

## 3. Non-goals (v1)

- **Interest / late-fee calculation** — leave to dealer's own discretion.
- **Actual money movement** beyond the Copy-UPI-ID convenience (see §7.3).
- **Facilitator as a party** in the account (see §12).
- **Multi-currency** — INR only.
- **Recurring auto-entries** — every credit / payment is manually recorded.
- **Legal contracts / e-signatures** — statement PDF is for dealer's use, not legally binding.
- **Group / aggregate credit** — one dealer ↔ one farmer per account; no bulk operations across farmers.

## 4. Actors

| Actor | Role in CMS |
|---|---|
| **Dealer** | Records credit sales, records receipts, confirms farmer-initiated payments, disputes wrong entries. Sees per-farmer + portfolio views + trust scores. |
| **Farmer** | Confirms dealer-recorded credit, records own payments, disputes wrong entries. Sees per-dealer view. Does NOT see own trust score. |
| **Facilitator** | **Not a party in v1.** Can informally help either side by looking over their shoulder — no in-app role. See §12. |
| **SA / CM** | Read-only dispute mediation on request (via existing SA tools). No dedicated UI in v1. |

## 5. Mental model

The unit of work is a **Credit Account**: one per (dealer_user, farmer_user) pair. It is a shared ledger of entries. Each entry has a type, an amount, and (for credits) a due date.

**Lifecycle**: entries are `PROPOSED` (editable by initiator) → `CONFIRMED` (immutable contract) → optionally `VOIDED` (paired-void). `DISPUTED` is a side state that resolves back to withdrawn / counter-proposed / mediated.

**Running balance** = sum of CONFIRMED entries, weighted by direction (credits + adjustments-up increase debt; payments + adjustments-down decrease it).

Two amounts always visible to both parties:
- **Confirmed balance** — authoritative; used for reminders, statements, trust score.
- **Pending your confirmation** — entries the other party added, waiting on you.

This is deliberately **not a transactional system** — no money moves inside RootsTalk. It's a shared record.

## 6. Data model

### 6.1 CreditAccount

```
CreditAccount
├── id                  UUID PK
├── dealer_user_id      FK → User(id)
├── farmer_user_id      FK → User(id)
├── opened_at           timestamp
├── opened_by           enum(DEALER, FARMER)
├── is_active           bool
├── closed_at           timestamp nullable
├── dealer_notes        text nullable    — dealer-private (not visible to farmer)
└── UNIQUE(dealer_user_id, farmer_user_id)
```

Auto-created on first credit entry — dealer doesn't explicitly "open an account."

### 6.2 CreditEntry

```
CreditEntry
├── id                       UUID PK
├── account_id               FK → CreditAccount(id)
├── entry_type               enum(OPENING_BALANCE, CREDIT_ADVANCED, PAYMENT_MADE,
│                                  ADJUSTMENT_UP, ADJUSTMENT_DOWN, VOID)
├── amount_paise             bigint  — always positive; direction implied by entry_type
├── entry_date               date    — see per-type rules below
├── due_date                 date nullable  — only for CREDIT_ADVANCED; the agreed settlement deadline
├── initiated_by             enum(DEALER, FARMER)
├── initiator_user_id        FK → User(id)
├── status                   enum(PROPOSED, CONFIRMED, DISPUTED, VOIDED)
├── created_at               timestamp
├── related_sale_id          FK → FarmerLedgerSale(id) nullable
├── payment_method           enum(CASH, UPI, BANK, CHEQUE, OTHER) nullable  — PAYMENT_MADE only
├── payment_ref              text nullable   — UPI txn id, cheque no., etc.
├── receipt_media_id         FK → Media(id) nullable
├── initiator_note           text nullable
├── confirmer_note           text nullable
├── dispute_reason           text nullable
├── confirmed_at             timestamp nullable
├── confirmer_user_id        FK → User(id) nullable
└── voided_at                timestamp nullable
```

**Per-type entry_date rules**:
- `CREDIT_ADVANCED`: auto-set to today; NOT editable. Prevents backdating games.
- `OPENING_BALANCE`: the "as of" date; editable by dealer while PROPOSED.
- `PAYMENT_MADE`: when the payment happened; editable by initiator while PROPOSED.
- `ADJUSTMENT_*` / `VOID`: today, not editable.

**Editability rule**: while `PROPOSED`, the initiator can edit amount, due_date (for CREDIT_ADVANCED), entry_date (per rules above), and notes. On `CONFIRMED`, all fields are immutable. Any subsequent correction requires the paired-void flow: initiator proposes a new adjustment entry; other party signs. The original entry stays intact in history.

**Void semantics**: no hard deletes on CONFIRMED entries. Ever. VOIDED is a status flag with grey styling; the entry stays visible for audit.

**Freshness marker**: farmer's view of a PROPOSED entry shows `Last updated HH:MM` so a mid-flight dealer edit is visible. Farmer's confirmation always applies to the current state of the entry — no separate "you agreed to version X" tracking.

### 6.3 CreditReminderPref

```
CreditReminderPref
├── user_id                     FK → User(id) PK
├── daily_summary_enabled       bool default true   — dealer only
├── weekly_summary_enabled      bool default true
├── new_entry_push_enabled      bool default true
├── quiet_hours_start           time nullable
├── quiet_hours_end             time nullable
└── updated_at                  timestamp
```

Per-user, uniform across all their accounts.

### 6.4 Relation to Farmer Ledger

Farmer Ledger v1 (live prod 2026-09-09) already tracks per-farmer purchase history. CMS **extends** it: a sale can now be marked "on credit," auto-creating a `CreditEntry(CREDIT_ADVANCED, PROPOSED)` linked via `related_sale_id`. Farmer Ledger stays the source of truth for what was sold; CMS is the source of truth for what is owed.

## 7. Key flows

### 7.1 Dealer records a new credit sale

1. Dealer opens **Farmer Ledger → farmer detail → Add Sale** (existing flow).
2. New checkbox: **"On credit"** (default off). When checked, a follow-on section appears:
   - **Credit amount** (defaults to sale total; editable — some dealers may take partial cash + partial credit)
   - **Settle by** (date picker — dealer must pick; no default)
   - **Notes** (optional)
3. On Save: sale row saved as usual + `CreditEntry(CREDIT_ADVANCED, PROPOSED, entry_date=today, due_date=picked)` created, linked to the sale.
4. Push to farmer: *"Ramesh Kirana added ₹5,000 to your credit account. Settle by 30 Sept. Tap to confirm."*
5. Dealer's own view: entry shows *"Awaiting Rukmini's confirmation"* with edit-in-place until she confirms.

### 7.2 Farmer confirms credit

1. Farmer receives push → **/credit** → dealer card shows the pending entry at the top with amount + settle-by date + optional dealer note.
2. Two buttons: **Confirm** / **Dispute**.
3. **Confirm** → status `CONFIRMED`, entry becomes immutable, balance updates on both sides. Push to dealer: *"Rukmini confirmed the ₹5,000 credit."*
4. **Dispute** → mandatory `dispute_reason` (short text or preset chips: *"Wrong amount", "Wrong date", "Wrong due date", "I didn't take this credit"*). Status `DISPUTED`, push back to dealer.

### 7.3 Farmer records a payment

1. Farmer opens **/credit → dealer card → Record payment**.
2. Amount + date + payment method + optional receipt photo + optional note.
3. If method = UPI + dealer has a VPA on their profile:
   - Card shows: **Dealer's UPI ID: `dealer@icici`** with a big **Copy** button.
   - Once farmer copies → they open their own UPI app manually, paste, pay.
   - Come back to RootsTalk → enter the UPI txn ID they got from their bank app in `payment_ref`.
   - **We do not deep-link, generate intent URIs, or verify anything.** Design principle §2.1.
4. On Save: `CreditEntry(PAYMENT_MADE, PROPOSED)` created.
5. Push to dealer: *"Rukmini recorded a ₹2,000 payment. Tap to confirm."*

### 7.4 Dealer records a payment received (farmer paid in cash / other channel)

Same as 7.3 but initiated by dealer, no UPI helper needed (they already have the money). Farmer confirms. Symmetric.

### 7.5 Opening balance (bootstrapping a dealer's existing book)

Locked decision: **lump sum only**.

- On an empty account, dealer sees **"Enter opening balance"** button.
- Enters single amount + "as of" date + brief note.
- Creates `CreditEntry(OPENING_BALANCE, PROPOSED)`. Farmer confirms once → entire pre-RootsTalk history collapses into that one confirmed number.
- Once opening balance is confirmed on an account, the option to add another is hidden forever (one per account).

### 7.6 Dispute resolution

- Disputed entries surface in both parties' **Dispute inbox** with a badge.
- Two resolution paths:
  - **Initiator withdraws** → entry deleted (no clutter for typos).
  - **Counter-proposal** → initiator creates a corrected entry; disputed one is voided.
- Third-party mediation (SA) is manual for v1: either party can tap **"Get help"** → creates a support-inbox ticket. No in-app SA UI.

## 8. UI surfaces (per portal)

### 8.1 Dealer PWA

- **Bottom-nav entry**: 💳 Credit (badge = pending-confirmations count)
- **/dealer/credit** — Portfolio home
  - Total outstanding across all farmers (big number)
  - Overdue buckets: 0-30 / 31-60 / 61-90 / 90+ days past due (tap to filter list)
  - Sort: recency / amount / days-overdue / name
  - Per-farmer row: name + phone + amount owed + oldest overdue days + trust badge (small) + Call button
- **/dealer/credit/[farmerId]** — Per-farmer account
  - Confirmed balance (big) + Pending confirmation (small) + Trust score chip
  - Reverse-chrono entry list — each row shows type, amount, entry_date, due_date (if credit), status pill, initiator/confirmer
  - Actions: **Add credit entry** / **Record payment received** / **Generate statement (PDF)** / **Export CSV**
- **Integration point**: "On credit" checkbox inside Add Sale (§7.1) — the primary entry path.
- **/dealer/credit/reminders** — push preferences.

### 8.2 Farmer PWA

- **New tile on farmer home**: 💳 My Credit (badge = pending confirmations + amount owed if any)
- **/farmer/credit** — Portfolio home
  - Total you owe (big number, sum across dealers)
  - Per-dealer card: dealer name + shop location + amount owed + oldest overdue + Call button
  - Pending confirmations pinned at top with red badge.
- **/farmer/credit/[dealerId]** — Per-dealer account
  - Confirmed balance + Pending. No trust score shown.
  - Reverse-chrono entry list — read-only for dealer's entries, editable for farmer's own PROPOSED entries.
  - Actions: **Record payment** / **Confirm / Dispute** pending entries.
- **/farmer/credit/reminders** — push preferences.

### 8.3 CA portal

Deferred. If clients want visibility into their dealer network's credit health, that's a v2 report. Not needed to ship v1.

## 9. Notifications

**Locked cadence**:

| Trigger | Recipient | Copy (illustrative) | Cadence |
|---|---|---|---|
| New PROPOSED entry needing your confirm | Counterparty | *"Ramesh Kirana recorded a ₹5,000 credit. Settle by 30 Sept. Tap to confirm."* | Immediate |
| Your PROPOSED entry was CONFIRMED | Initiator | *"Rukmini confirmed the ₹5,000 credit."* | Immediate |
| Your PROPOSED entry was DISPUTED | Initiator | *"Rukmini flagged the ₹5,000 credit. Tap to review."* | Immediate |
| Daily portfolio digest | **Dealer only** | *"Today: 3 payments received (₹8,500). 2 need your confirmation. 4 credits due tomorrow. 12 overdue."* | Daily 8pm (dealer's tz) |
| Weekly overdue nudge | Dealer | *"12 farmers owe you ₹1,20,000 total. 4 are past 60 days."* | Weekly Monday morning |
| Weekly credit summary | Farmer | *"You owe ₹15,000 across 2 dealers. ₹5,000 to Ramesh Kirana is due in 3 days. ₹10,000 to Suresh Traders is 12 days overdue."* | Weekly Sunday morning |

All controllable via `CreditReminderPref` + quiet hours. Copy is factual, not judgmental — respects design principle §2.2.

Localisation: all copy through i18n keys from day one (en + kn + hi + ta). Currency via `Intl.NumberFormat` (locale-aware ₹ placement).

## 10. Trust score

**Purpose**: dealer-side behavioural cue — *how well does this farmer adhere to what they've agreed to?* Never shown to the farmer; never shared with other dealers.

**Scoring window**: rolling **last 5 resolved credits OR last 12 months, whichever gives more data**. Prevents a low-volume farmer from being stuck on one old event forever; also reflects present-day behaviour honestly for high-volume relationships.

**Per-credit weight** (evaluated once a credit reaches resolution: fully paid OR past due_date):

| Outcome | Weight |
|---|---|
| Full settlement ON or BEFORE due date | **1.0** |
| Partial ≥ 50% by due date (rest may come later) | **0.5** |
| Full settlement AFTER due date, < 30 days late | **0.3** |
| Full settlement AFTER due date, ≥ 30 days late | **0.2** |
| < 50% by due date AND never fully settled | **0.0** |

**Trust score** = average of weights × 100 → percentage.

**Display** (dealer-side only, on farmer card + per-farmer account header):

| Score | Badge |
|---|---|
| 85–100% | 🟢 *"Reliable"* |
| 65–84% | 🟢 *"Usually on time"* |
| 45–64% | 🟡 *"Mixed record"* |
| 25–44% | 🟠 *"Often late"* |
| 0–24% | 🔴 *"Unreliable"* |
| < 2 resolved credits | *"New — not enough history"* |

**Currently-open overdue credits** are shown as a separate live warning above the score (*"1 credit ₹5,000 is 12 days past due"*) — they don't dilute the historical score but are impossible to miss.

**Not shown to farmers.** Ever. If we later add a farmer-side reputation surface (e.g. multi-dealer summary that helps them access better terms), it will be a separate opt-in feature with different privacy semantics.

**Thresholds and weights are tuneable.** After first cohort, we should look at real distributions and adjust so buckets aren't lopsided.

## 11. Sensitive edges

### 11.1 Coaching sandbox isolation
Same pattern as Farmer Ledger — `guard_coaching_workspace` at lookup + entry endpoints. Coaching students' credit play stays inside their workspace's User set.

### 11.2 Unclaimed farmers
Dealer opens account against a phone that isn't yet a self-registered User → entries accumulate silently. On first OTP-verify, farmer sees a **"Welcome to CMS"** intro listing every account. **Bulk-confirm gate**: farmer reviews and confirms (or disputes) per account. Prevents a rogue dealer from silently loading debt onto an unclaimed number.

### 11.3 Cross-dealer confidentiality
Farmer's per-dealer views strictly isolated — farmer never sees any cross-dealer balance from within a dealer's view. Dealer never sees any other dealer's records or trust scores. Mirrors existing anonymisation in Farmer Ledger.

### 11.4 Deletion / auditability
No hard deletes on CONFIRMED. VOIDED is a status. Every state transition writes actor + timestamp.

### 11.5 Numeric input
Currency needs Unicode digit normalisation (existing `digitsOnly()` helper). All amounts stored in paise internally; display via `Intl.NumberFormat`.

### 11.6 Concurrent edits
Rare but possible — mid-flight dealer edit while farmer is looking at PROPOSED entry. Handled by (a) freshness marker on farmer's view (§6.2), (b) farmer's confirmation always applies to current state, (c) append-only entry semantics — no shared mutable state between entries.

### 11.7 Language
All copy through i18n keys from day one. Dispute-reason chips need extra care — legal-sounding words don't translate well.

## 12. The facilitator question — excluded from v1

Keeping facilitators out of v1 entirely. Reasons in the earlier draft; short version: they're a helper role, not a party to the debt. Farmers wanting help can hand their phone over — no app change needed. If field feedback demands it, revisit as an **optional read-only witness** in v2.

## 13. Decisions locked (2026-09-14)

Recorded here for future reference — all previously-open questions resolved:

1. **Opening balance**: lump sum only (no per-entry historical backfill).
2. **Push cadence**: Dealer = daily evening digest + weekly overdue. Farmer = weekly only. Immediate on state changes for both.
3. **Statement PDF**: v1 must-have.
4. **UPI**: Copy-UPI-ID button only (never intent-link / deep-link / verify). Consistent with design principle §2.1 — we don't route money.
5. **Trust score**: v1 must-have. Due-date-anchored, rolling window (last 5 OR last 12 months). Dealer-only visibility.
6. **Weights + thresholds**: as tabulated in §10.
7. **Credit entry immutability**: PROPOSED editable by initiator; CONFIRMED fully immutable; corrections via paired-void.

## 14. Phased rollout

| Phase | Scope | Est. days |
|---|---|---|
| **v1.0** — data model + core flows + trust score | Tables + migrations; §7.1–§7.4 flows; per-farmer + per-dealer views; §7.5 opening balance; §10 trust score; §11.1–§11.4 isolation/audit; immediate-push triggers (§9 first three rows) | ~6 |
| **v1.1** — reminders + polish | Daily digest job (dealer) + weekly summary jobs (both sides); overdue buckets; ReminderPref UI; quiet hours | ~2 |
| **v1.2** — statement + Copy-UPI + CSV | Statement PDF; Copy-UPI-ID UI (behind dealer VPA field on profile); CSV export | ~2 |
| **v1.3** — coaching parity + hi/ta i18n | Sandbox parity audit; Hindi + Tamil translations for all keys; grep coaching/training for leak parity per feedback memory | ~2 |
| **v2** — CA reports, recurring, farmer-side reputation | Evaluate after v1 field feedback | TBD |

**Total v1**: ~12 engineering days + ~3 days QA / tuning cycles. Realistic first-cohort ship: **~3 weeks from start**.

## 15. Dependencies + prerequisites

- **Farmer Ledger v1** ✅ (live prod 2026-09-09) — data model dependency for `related_sale_id`.
- **Farmer OTP self-registration + `self_registered_at`** ✅ (live prod 2026-09-09) — unclaimed-farmer flow.
- **Unicode digit normalisation** ✅ (live prod 2026-08-28) — currency inputs.
- **Coaching sandbox guards** ✅ (live prod).
- **Dealer VPA field on profile** ⚠️ — currently optional/missing on most profiles; needed for §7.3 Copy-UPI. Small profile-edit addition in v1.2.
- **Media upload for receipt photos** ✅ (existing `/media/upload` handles image types).

No blockers.

---

## Appendix A — Alternatives considered

**Signed single amount instead of typed entries** — rejected: sign carries less meaning than a typed entry (opening balance ≠ credit advanced); harder queries; noisier UI copy.

**Extending Farmer Ledger `Sale` with `paid_bool` + `credit_balance`** — rejected: payments don't attach cleanly to a single sale (farmers often pay lump sums across multiple credits); opening balance has no sale to attach to; statement PDF wants a unified event stream.

**Absolute days-to-settle for trust score** — rejected in favour of due-date-anchored (§10). Absolute days punishes generous dealers who offer 90-day terms; anchoring to the agreed date measures adherence rather than raw speed. Fairer to both parties.

**UPI intent link / deep-link** — rejected per design principle §2.1. Copy-UPI-ID is the whole extent of RootsTalk's involvement in money movement.

**Facilitator as third confirmation party** — rejected per §12. Optional read-only witness may return in v2 if field-tested demand exists.
