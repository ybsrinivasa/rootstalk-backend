# Add Sale v2 — Ledger multi-item + farmer-scoped entry point

**Author**: yb + Claude
**Draft date**: 2026-09-14
**Status**: DRAFT — awaiting user sign-off on open questions.

---

## 1. Purpose

Current flow (Farmer Ledger v1, live prod 2026-09-09) forces the dealer to re-enter the farmer's phone number for **every single item** they record. A farmer buying 3 items = 3 full round-trips through phone lookup + farmer preview + one-item form. Field feedback: this is tedious enough that dealers avoid recording sales, undermining the whole ledger.

Two changes fix the majority of the pain:

1. **Farmer-scoped entry point**: launch Add Sale directly from a farmer's ledger page — phone lookup skipped entirely.
2. **Multi-item form**: after the farmer is known, let the dealer add multiple items in one go and save the batch as one action.

## 2. Non-goals (v2)

- **Sale line editing after save** — existing single-sale edit flow (`PATCH /dealer/ledger/manual-sale/{id}`) covers this. Multi-item batch is create-only.
- **Different farmers per batch** — one farmer per batch. Splitting across farmers isn't a real use case.
- **Rich per-item detail** — no per-item photo, GST breakdown, or discount fields. Items are still {category, product, brand, qty, unit, price, notes}.
- **On-credit checkbox** — deferred per user 2026-09-14 (bigger Add Sale rework pending).
- **Reordering items** — items save in the order they were entered.

## 3. Actors

Dealer only. Farmer sees the new sales via their existing Ledger view (unchanged).

## 4. Two flows (side-by-side)

### 4.1 Existing flow (single item, unknown-farmer start)

Unchanged for v2 — the walk-in-with-phone case.
Dealer taps top-level **Add Sale** button → Phone lookup → farmer preview or new-farmer form → single item → Save → redirects to farmer detail page.

### 4.2 New flow A — from the farmer's ledger page

Dealer is already looking at `/dealer/ledger/[farmerId]`. Taps a prominent new **+ Add sale** button. Routes to `/dealer/ledger/add-sale?farmer_user_id=<id>`.

- Skips Step 1 (phone lookup) and Step 2 (farmer preview) entirely.
- Shows a small confirmation card at the top: *"Recording sale for **{farmer.name}** — Change farmer?"*, with **Change farmer** link that resets to the phone-lookup flow.
- Straight to Step 3 (item list).

### 4.3 New flow B — multi-item entry (both flows share this)

Step 3 is now a list of item cards:

```
┌─────────────────────────┐
│ 📅  Sale date: [today] │   ← shared across items
├─────────────────────────┤
│ Item 1        [🗑 remove]│
│ Category: [Seed | Pest | Fert]
│ Product name: ____
│ Brand: ____   Manufacturer: ____
│ Qty: __  Unit: __   Price: __
│ Notes: ____
├─────────────────────────┤
│ Item 2        [🗑 remove]│
│ ...
├─────────────────────────┤
│ + Add another item      │
└─────────────────────────┘

[ Save 2 items ]  ← button label reflects count
```

- **Sale date** is a single field at the top of the item list — shared across all items in the batch. Defaults to today. Rationale: farmer walks into shop once = all items have the same date. Simplifies the form. (See §9 open question 1 for a per-item override discussion.)
- **+ Add another item** appears below the last item card. Explicit tap — no auto-appearing new card, so the single-item flow feels unchanged.
- **Save button** shows the count: "Save 1 item" / "Save 3 items".
- **Remove item** allowed on any card except the last (form must have at least 1 item).
- **Validation** per item — same as v1 (product/qty/unit required). Save button disabled until all items are valid.

## 5. Backend

### 5.1 New endpoint

```
POST /dealer/ledger/manual-sales-batch
Content-Type: application/json

{
  // Exactly one of farmer_user_id / new_farmer required.
  "farmer_user_id"?: string,
  "new_farmer"?: {
    "phone": string,
    "name": string,
    "state_cosh_id": string,
    "district_cosh_id": string,
    "sub_district"?: string
  },
  "sale_date": "2026-09-14",  // shared across all sales
  "sales": [
    {
      "category": "SEED" | "PESTICIDE" | "FERTILIZER",
      "product_name": string,
      "brand"?: string,
      "manufacturer"?: string,
      "qty": number,
      "unit": string,
      "price"?: number,
      "notes"?: string
    },
    ...
  ]
}

Response:
{
  "farmer_user_id": string,
  "sale_ids": [ string, string, ... ]   // in the order submitted
}
```

**Semantics**:
- **All-or-nothing** — entire batch commits in one DB transaction. If any single item fails validation or write, the whole batch is rejected.
- **Farmer creation** runs first, then all sales. If new-farmer creation fails, no sales are attempted.
- **Sale count cap**: **20 items per batch**. Rejects with 422 if exceeded — protects against runaway payloads and keeps the transaction short.
- **Coaching sandbox isolation** — same guard as existing single-sale endpoint (already enforced at farmer_user_id level).

### 5.2 Existing single-sale endpoint stays

`POST /dealer/ledger/manual-sale` (single) is kept unchanged. Frontend uses the batch endpoint always; the single-sale endpoint stays for API stability + admin scripting.

### 5.3 Data model

**No schema changes**. Each item in the batch inserts one `DealerManualSale` row exactly as today. No batch grouping ID — dealer treats items in the batch as independent rows once saved (matches how they're viewed in the ledger).

## 6. UI details

### 6.1 Farmer ledger detail page — new entry point

Add a **+ Add sale for this farmer** button:
- Placement: prominent, near the top of the page, above the filter tabs (Active / Completed / All).
- Style: filled purple button matching dealer theme colour (`#7D4196`).
- On tap: `router.push('/dealer/ledger/add-sale?farmer_user_id=' + farmerId)`.

### 6.2 Add Sale page — query-param handling

```typescript
const searchParams = useSearchParams()
const preselectedFarmerId = searchParams.get('farmer_user_id')

useEffect(() => {
  if (preselectedFarmerId) {
    // Fetch farmer details via /dealer/ledger/farmers/{id} (existing endpoint)
    api.get(`/dealer/ledger/farmers/${preselectedFarmerId}`)
       .then(r => {
         setExistingUser({
           user_id: preselectedFarmerId,
           name: r.data.name,
           phone: r.data.phone,
           // etc.
         })
         setLookupState('found')
       })
  }
}, [preselectedFarmerId])
```

The rest of the page renders as if the dealer just did a successful phone lookup — Step 1 and Step 2 collapse to a single "Recording sale for X" card.

### 6.3 Item list state

Client-side array of item drafts:

```typescript
interface ItemDraft {
  id: string  // client-side UUID for React keys
  category: 'SEED' | 'PESTICIDE' | 'FERTILIZER'
  productName: string
  brand: string
  manufacturer: string
  qty: string
  unit: string
  price: string
  notes: string
}

const [items, setItems] = useState<ItemDraft[]>([blankItem()])
```

Add → `setItems(prev => [...prev, blankItem()])`
Remove → `setItems(prev => prev.filter(i => i.id !== id))`
Update → `setItems(prev => prev.map(i => i.id === id ? { ...i, ...patch } : i))`

Save button `disabled` when: `items.length === 0 || items.some(i => !isValid(i))`.

## 7. Error handling

- **Full-batch failure** (validation, backend rejection): show the error near the Save button. All items stay in the form — dealer fixes the issue and re-submits.
- **Partial success**: impossible by design — all-or-nothing transaction.
- **Network failure mid-save**: Save button shows spinner. On timeout or 5xx, show error + Retry button; nothing was committed.

## 8. Rollout

| Step | Scope | Est. |
|---|---|---|
| Backend | Batch endpoint + tests | ~1h |
| Frontend | Multi-item form + query-param handling + entry point button on farmer detail | ~3-4h |
| i18n | ~10 new keys | ~30m |
| QA + polish | | ~1h |

**Total**: ~half day of engineering + your QA cycle.

**No migration needed** — schema unchanged.

## 9. Open questions for you

Three calls to make before I build:

1. **Sale date — shared or per-item?**
   My recommendation: **shared** (one date field at the top of the batch, applies to all items). Rationale: farmer's visit is a single event; per-item dates almost never used in the real world. Simpler UI.
   Alternative: shared with a tiny "different date for this item" link revealing a per-item override — slightly more complex, covers the edge case.
   Or: per-item from the start — more flexibility but adds one required field per item.

2. **Item cap per batch**: proposed **20**. Too tight? Too generous? Rationale: 20 items is 3× the largest realistic single-visit purchase; keeps the DB transaction short and the form manageable. Adjustable.

3. **"+ Add another item" trigger**: proposed **explicit tap** (button always visible below the last item). Alternative: auto-append a blank card once the previous one has all required fields filled — feels magical but can feel over-eager and creates half-filled cards that dealer might submit by accident. My preference: explicit.

## 10. Related

- Farmer Ledger v1 scoping: `project_rootstalk_farmer_ledger_2026_09_09.md` (memory)
- On-credit checkbox integration: **deferred** per user 2026-09-14; will land in a separate Add Sale rework once this v2 is settled and any other add-sale changes have been designed.
