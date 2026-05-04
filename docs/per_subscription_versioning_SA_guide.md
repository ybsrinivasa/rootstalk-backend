# Per-Subscription Content Versioning — SA Support Guide

A guide for the **Super Admin / support team** to investigate and explain
incidents related to the per-subscription versioning system.

If a farmer says "I'm seeing old practice information" or a dealer says
"the brand is locked but the SE just changed it" — this is the document
that tells you whether that is **expected behaviour** or a **bug**.

---

## 1. What snapshots are, in one paragraph

Every farmer's subscription gets a **frozen photograph** of each timeline
at the moment they first see it or order against it. From that moment
onward, that farmer's view of that timeline reads from the photograph,
**not** from the live master tables that the SE edits. Two farmers on the
same Package of Practices can therefore see different versions, depending
on when each first locked their reality. Dealers see the photograph too,
for the order in front of them.

**Why we did this:** climate change makes mid-season SE edits inevitable,
but a farmer with money on the line cannot have the recipe change on them
without warning. The snapshot is the contract.

---

## 2. The five rules (plain words)

| # | Rule | Plain words |
|---|---|---|
| 1 | Lock stays forever after order | Once a farmer places an order against a timeline, the photo of that timeline stays frozen for that farmer permanently. Even after the order is delivered. |
| 2 | Lock does NOT release on cancellation | If the farmer cancels the order, the photo still stays. We do not "unfreeze" — too risky when items might already have been partially fulfilled. |
| 3 | Snapshot dates are relative, not absolute | The window dates inside the snapshot are stored as offsets ("day 7 after sowing"), not as calendar dates. So if the farmer corrects their sowing date, the schedule shifts naturally. |
| 4 | SE never sees snapshots | The Client Portal hides snapshots completely. The SE edits master tables freely; the system handles "who has a frozen version of what" invisibly. |
| 5 | Dealers read from the snapshot too | When a farmer's order reaches a dealer, the dealer sees the practice details (brand, dosage, application instructions) from the photograph frozen at order time. |

---

## 3. Lock triggers — why was a snapshot taken?

Every snapshot row records *why* it was taken in the `lock_trigger` field.
Three values, each meaning a different real-world event:

| `lock_trigger` | Meaning |
|---|---|
| `PURCHASE_ORDER` | Snapshot was taken because the farmer placed an order. This is the strongest lock — money is on the line. |
| `VIEWED` | Snapshot was taken because the timeline was rendered on the farmer's "Today" screen for the first time, OR by the nightly defensive sweep filling in any missed snapshots. |
| `BACKFILL` | Snapshot was taken by the one-time backfill script for orders placed BEFORE the versioning system was rolled out. Effectively the same as `PURCHASE_ORDER` for those legacy orders. |

---

## 4. Two admin endpoints for investigation

Both require Super Admin authentication (your email must match
`SA_EMAIL` in the environment config).

### 4.1 List all snapshots for a subscription

```
GET /admin/subscriptions/{subscription_id}/snapshots
```

Returns one row per snapshot, sorted oldest first. Example:

```json
[
  {
    "id": "5ab1...",
    "subscription_id": "sub_42",
    "timeline_id": "tl_vegetative",
    "source": "CCA",
    "lock_trigger": "VIEWED",
    "locked_at": "2026-04-12T08:31:04Z",
    "schema_version": 1,
    "practice_count": 14
  },
  {
    "id": "9cf3...",
    "subscription_id": "sub_42",
    "timeline_id": "tl_flowering",
    "source": "CCA",
    "lock_trigger": "PURCHASE_ORDER",
    "locked_at": "2026-04-26T11:02:18Z",
    "schema_version": 1,
    "practice_count": 8
  }
]
```

This tells you: **what is locked, when it was locked, and why**.

### 4.2 Full content of one snapshot

```
GET /admin/snapshots/{snapshot_id}
```

Returns the entire frozen content payload — every practice, every element,
every conditional question, every relation, exactly as it stood when the
snapshot was taken. Use this to compare against the *current* master
state if you need to confirm the difference.

---

## 5. Common incidents — what to do

### 5.1 "The farmer says they're seeing old practice information"

This is **almost always expected behaviour.** Walk through:

1. Pull the subscription_id from the farmer's profile.
2. Call `GET /admin/subscriptions/{subscription_id}/snapshots` and find
   the row(s) for the timeline they're complaining about.
3. Look at `locked_at` — that is the moment their reality was frozen.
4. Look at `lock_trigger` — was it `PURCHASE_ORDER` (they ordered),
   `VIEWED` (they saw it on Today), or `BACKFILL` (legacy migration)?
5. Pull the snapshot content with `GET /admin/snapshots/{id}` and
   confirm what they're seeing matches the snapshot.

**If the snapshot matches what the farmer is seeing**: the system is
working as designed. Explain Rule 1 to the farmer in plain words —
their version was locked on `<locked_at>` because of `<reason>`, and
that protects them from any later changes the company makes.

**If the snapshot does NOT match what the farmer is seeing**: this is
a bug. Escalate to engineering with the snapshot id and the farmer's
report.

### 5.2 "The dealer says the brand is locked but the SE just changed it"

Expected behaviour, per Rule 5. Dealers see the brand-lock state from
the snapshot frozen at order placement, not from current master.

1. Pull the order_id and find the order_item the dealer is looking at.
2. Note the `snapshot_id` on the order_item.
3. If `snapshot_id` is **not null**: call
   `GET /admin/snapshots/{snapshot_id}` and confirm the brand element
   was locked at order time. Explain to the dealer that this is the
   farmer's frozen recipe — they should fulfil it as locked.
4. If `snapshot_id` **is null**: this is a pre-Phase-3 legacy order
   that the backfill missed (or that pre-dates the backfill). Ask
   engineering to verify and run the backfill if needed.

### 5.3 "The SE updated a practice and is asking why their farmers are not seeing it"

Expected behaviour, per Rule 4 — and this is where the support team
needs to be **firm and clear** with the SE.

The system protects farmers from mid-season changes by design. If the
SE wants their edit to reach a farmer who is already locked, that is
**not possible** — and is the entire point of the architecture. Suggest
they:

- Wait for the next crop cycle (new subscription, new snapshots).
- For genuinely unsafe practices (e.g. a brand recall), engineering
  may need to write a one-off targeted intervention. Treat as an
  escalation, not a routine support request.

### 5.4 "Why does this farmer have so many snapshot rows?"

Normal. A farmer with an annual crop touching 5 timelines (e.g.
Pre-Sowing, Vegetative, Flowering, Fruiting, Harvest) and a couple of
diagnosis events (CHA SP/PG) can easily accumulate 7-10 snapshots over
the season. Each is small (a few KB of JSON). No action needed.

---

## 6. When to escalate to engineering

| Symptom | Action |
|---|---|
| Snapshot content doesn't match what the farmer/dealer is actually seeing | Escalate — possible bug in read path. |
| `snapshot_id` is NULL on an order placed AFTER Phase 3.2 rollout date | Escalate — synchronous trigger may have failed; the nightly sweep should have caught it. |
| Need to delete or modify a snapshot (Rule 1 says you can't) | Escalate — should never be needed in normal operation. |
| Backfill script fails or shows unexpected counts | Escalate — share the script output. |
| SE insists on pushing an edit to already-locked farmers (e.g. brand recall) | Escalate — needs an engineering-built one-off intervention. |

---

## 7. Glossary

- **Master tables** — the regular `practices`, `elements`, `relations`,
  `conditional_questions`, `practice_conditionals` tables that the SE
  edits via the Client Portal.
- **Snapshot** — a frozen copy of one timeline's full content, stored
  as JSON in the `locked_timeline_snapshots` table.
- **CCA** — Crop Cultivation Advisory (the main timeline source).
- **CHA — PG / SP** — Crop Health Advisory, Problem Group / Specific
  Problem (timelines triggered by diagnosis events).
- **BL-04 / BL-02 / BL-03 / BL-07** — internal engineering labels for
  windowing, conditional filtering, deduplication, and brand-options
  logic. You don't need to know what they are; the engineering team
  uses these labels in commit messages and tickets.

---

*This guide is owned by the engineering team. If a scenario isn't
covered here, raise it in the support channel and we will add it.*
