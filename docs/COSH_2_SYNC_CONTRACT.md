# Cosh 2.0 → RootsTalk Sync Contract (V1)

What RootsTalk expects to receive from Cosh 2.0's sync emitter. This
document is the alignment reference — Cosh's sync emit and RootsTalk's
sync ingest must both build to it.

Owned by: RootsTalk team. Last updated: 2026-05-08.

Data flow direction: **Cosh → RootsTalk only.** RootsTalk never echoes
edits back to Cosh.

---

## 1. Wire format — payload envelope

Cosh POSTs a JSON payload to `POST /sync/cosh` (auth: `X-Cosh-Api-Key`
header). One sync run = one POST.

```json
{
  "sync_id":      "<opaque string, ideally a uuid or timestamp>",
  "initiated_by": "<optional — Cosh user/admin who triggered the sync>",
  "sync_mode":    "incremental" | "full",
  "entity_batches": [
    /* one batch per Cosh entity type to deliver */
  ]
}
```

- `sync_mode = "full"` ⇒ rows of any `entity_type` present in the
  payload but absent from this run are **inactivated** in RootsTalk.
- `sync_mode = "incremental"` ⇒ payload-rows are upserted; absent rows
  are left untouched.

---

## 2. Batch — header and items

Each entry in `entity_batches` represents one Cosh entity type's worth
of rows.

### 2.1 Connect batch

```json
{
  "entity_type":  "<canonical_label_set_in_cosh_designer>",
  "connect_id":   "<uuid of the Connect definition in Cosh>",
  "connect_name": "<human display name, e.g. 'Pest Diagnosis Chain'>",
  "schema": [
    { "position_number": 1,
      "node_type": "CORE" | "CONNECT",
      "entity_type": "<target Core/Connect canonical label>",
      "relationship_to_next":      "<edge label, or null on last>",
      "relationship_display_name": "<human edge label, or null>" },
    ...
  ],
  "items": [ /* see §3.2 */ ]
}
```

### 2.2 Core batch

```json
{
  "entity_type": "<canonical_label_set_in_cosh_designer>",
  "items":       [ /* see §3.1 */ ]
}
```

The `schema` array is omitted on Core batches (or empty).

---

## 3. Item shapes — what each row in `items[]` looks like

### 3.1 Core item

```json
{
  "cosh_id":      "<uuid>",
  "entity_type":  "<canonical_label, must match the batch>",
  "status":       "active" | "inactive",
  "translations": { "en": "<English name — REQUIRED>",
                    "kn": "<optional>", "hi": "<optional>", ... },
  "parent_cosh_id": "<uuid or null — for hierarchical Cores like
                     specific_problem→problem_group, brand→common_name,
                     district→state>",
  "metadata":     { /* optional — entity-type-specific extras like
                       scientific_name on crop, manufacturer_name on
                       brand, S3 path on media */ }
}
```

### 3.2 Connect item

```json
{
  "cosh_id":     "<uuid of this Connect Data row>",
  "entity_type": "<must match the batch's entity_type>",
  "status":      "active" | "inactive",
  "positions":   {
    "1": { "cosh_id": "<uuid>", "entity_type": "<target's canonical label>" },
    "2": { "cosh_id": "<uuid>", "entity_type": "<target's canonical label>" },
    ...
  }
  /* plus any Connect-specific scalar attributes — see §6 */
}
```

The `positions` keys are stringified position numbers ("1", "2", …).
Each value pairs the linked entity's `cosh_id` with its `entity_type`
label (the same label declared in the batch's `schema[i].entity_type`
at `position_number = i`).

---

## 4. Classifying Connect vs Core in RootsTalk

RootsTalk's sync handler decides routing **by item shape**, not by a
hardcoded entity_type list:

| Signal on the item | Classification |
|---|---|
| has `positions` dict | Connect → `cosh_connect_rows` |
| has `translations` (and no `positions`) | Core → `cosh_core_items` |

This means Cosh can introduce new Connect or Core entity_types over
time — adding a new crop's image Connect, adding a new Core for ITKs,
etc. — and RootsTalk handles them with **zero backend code change**.

The only requirement: each item carries its shape signal correctly.

---

## 5. "BlankBox" sentinel — handling unfilled positions

When a Connect's schema has a position the designer can't leave empty
(Cosh-side constraint), Cosh fills it with the sentinel value chosen
for this contract. RootsTalk's adapter treats sentinel-valued positions
as **absent** — equivalent to the position not being filled at all.

**Sentinel value to use** (TBD — pin one of these and lock in this doc):

- `"BlankBox"` — single token, no space.
- `"Blank Box"` — two tokens with space.

Whichever Cosh 2.0 uses, both Cosh and RootsTalk reference this doc to
stay aligned. Once locked, the adapter's filter strips matching
`positions[i].cosh_id` (and `positions[i].entity_type` if also
sentinel-valued) so downstream consumers see the position as missing.

---

## 6. Optional scalar attributes per Connect

Some Connects carry **scalar (non-cosh_id) attributes** that aren't
positions. Today only one is needed:

### 6.1 `pest_diagnosis_chain` — `priority_rank`

`priority_rank` is an integer, expert-curated. Used by RootsTalk's BL-08
diagnosis algorithm to demote problems whose top-priority symptom
wasn't reported.

Cosh emits it as a row-level attribute alongside `cosh_id` /
`status` / `positions`:

```json
{
  "cosh_id": "...",
  "entity_type": "pest_diagnosis_chain",
  "status": "active",
  "priority_rank": 1,
  "positions": { ... }
}
```

If absent, RootsTalk treats the row as unranked. (Mixing ranked and
unranked rows of the same pest is supported — see BL-08 spec.)

---

## 7. Translations

- **English (`en`) is mandatory** on every Core item. RootsTalk rejects
  Cores without it.
- Other languages (`kn`, `hi`, `ta`, `te`, `mr`, ...) are optional.
- Connect items don't carry translations; their display name comes from
  the linked Core(s).

---

## 8. Required Cosh entities for V1

Cosh designer must tag the following Connects and Cores to the
**`rootstalk` product** and set `entity_type_label` to the canonical
labels listed below.

### 8.1 Cores

| `entity_type_label` | Purpose | Notes |
|---|---|---|
| `crop` | Crop reference (paddy, tomato, ...) | `metadata.scientific_name` recommended |
| `crop_stage` | Phenological stage (vegetative, flowering, ...) | Independent of crop age |
| `pest` | Diagnosable pest / disease / disorder | |
| `pest_stage` | Infestation severity stage (early / moderate / severe) | Affects which Symptoms apply |
| `part` | Plant part (leaf, stem, fruit, root, ...) | |
| `sub_part` | Sub-part (under-surface, tip, mid-rib, ...) | |
| `symptom` | Observable symptom (spots, wilting, lesions, ...) | |
| `sub_symptom` | Symptom variant (white spots vs. yellow spots, ...) | |
| `media` | Image / audio / video reference | `metadata.s3_path` carries the asset URL; `metadata.media_type` ∈ {image, audio, video, hyperlink} |
| `common_name` | Pesticide / fertilizer common name (CNI) | |
| `application_method` | How an input is applied (foliar spray, drench, ...) | |
| `dosage_unit` | Unit for dosage (ml/L, kg/acre, ...) | |
| `formulation` | Product formulation (SC, EC, WP, ...) | |
| `volume_unit` | Unit for volume per plant | |
| `distance_unit` | Unit for spacing (cm, m) | |
| `temperature_unit` | Unit for seed-treatment temperatures | |
| `time_unit` | Unit for seed-treatment durations | |
| `irrigation_unit` | Unit for irrigation duration | Distinct from `time_unit` |
| `planting_material` | Planting material type | |
| `number_unit` | Unit for plant counts | |
| `itk_name` | ITK (indigenous technical knowledge) name | |
| `maturity_index` | Harvest-stage maturity indicator | |
| `problem_group` | PG cluster of related Pests | `parent_cosh_id` empty |
| `specific_problem` | SP under a PG | `parent_cosh_id` = the PG's cosh_id |
| `brand` | Trade name / brand | `parent_cosh_id` = CNI's cosh_id; `metadata.manufacturer_name` carries the manufacturer string |

### 8.2 Connects

| `entity_type_label` | Schema (positions) | Row attributes |
|---|---|---|
| `pest_diagnosis_chain` | `[1: crop, 2: crop_stage, 3: pest, 4: pest_stage, 5: part, 6: sub_part, 7: symptom, 8: sub_symptom]` — all CORE node_type | `priority_rank: int` |
| `<crop>_pest_images` (one per crop, e.g. `tomato_pest_images`, `paddy_pest_images`, ...) | `[1: pest_diagnosis_chain (CONNECT), 2: media (CORE)]` | — |

For image Connects, one row pairs one `pest_diagnosis_chain` row with
exactly one `media` Core item. Multiple images for the same diagnosis
row = multiple rows in the image Connect.

---

## 9. Sync modes — incremental vs. full

### Incremental

Default. Each item upserts into its target table. Items absent from
this payload are unaffected.

### Full

Per-`entity_type` reset. After upserting all items in a batch, any
existing row of the same `entity_type` whose `cosh_id` is **not** in
the batch's `items[]` is marked `status='inactive'`.

The Cosh admin chooses incremental vs. full per sync run.

---

## 10. Update semantics — append-only with tombstoning

Per the architectural rule confirmed 2026-05-07:

- Cosh rows are **never updated in place semantically**. When a row's
  meaning changes, the old row is set `status='inactive'` and a new row
  with a new `cosh_id` is added.
- Translations are the one exception — Indian-language translations
  may be edited in place, since they don't carry their own ID.
- RootsTalk respects this: an upsert of an existing `(cosh_id,
  entity_type)` updates fields in place, but Cosh is expected to use
  this only for translation refresh and similar non-semantic edits.

---

## 11. Worked examples

### 11.1 Core batch (`crop`)

```json
{
  "entity_type": "crop",
  "items": [
    {
      "cosh_id": "9a6f4c10-1234-...",
      "entity_type": "crop",
      "status": "active",
      "translations": {
        "en": "Paddy", "kn": "ಭತ್ತ", "hi": "धान"
      },
      "metadata": { "scientific_name": "Oryza sativa" }
    }
  ]
}
```

### 11.2 Connect batch (`pest_diagnosis_chain`)

```json
{
  "entity_type":  "pest_diagnosis_chain",
  "connect_id":   "0db911c3-5223-46e2-9269-8aef4da86490",
  "connect_name": "Pest Diagnosis Chain",
  "schema": [
    { "position_number": 1, "node_type": "CORE",
      "entity_type": "crop",
      "relationship_to_next": "AT_STAGE",
      "relationship_display_name": "At Stage" },
    { "position_number": 2, "node_type": "CORE",
      "entity_type": "crop_stage",
      "relationship_to_next": "AFFECTED_BY",
      "relationship_display_name": "Affected By" },
    { "position_number": 3, "node_type": "CORE",
      "entity_type": "pest",
      "relationship_to_next": "AT_INFESTATION",
      "relationship_display_name": "At Infestation Stage" },
    { "position_number": 4, "node_type": "CORE",
      "entity_type": "pest_stage",
      "relationship_to_next": "OBSERVED_ON",
      "relationship_display_name": "Observed On" },
    { "position_number": 5, "node_type": "CORE",
      "entity_type": "part",
      "relationship_to_next": "WITHIN",
      "relationship_display_name": "Within" },
    { "position_number": 6, "node_type": "CORE",
      "entity_type": "sub_part",
      "relationship_to_next": "EXPRESSING",
      "relationship_display_name": "Expressing" },
    { "position_number": 7, "node_type": "CORE",
      "entity_type": "symptom",
      "relationship_to_next": "SPECIFICALLY",
      "relationship_display_name": "Specifically" },
    { "position_number": 8, "node_type": "CORE",
      "entity_type": "sub_symptom",
      "relationship_to_next": null,
      "relationship_display_name": null }
  ],
  "items": [
    {
      "cosh_id": "row-uuid-1",
      "entity_type": "pest_diagnosis_chain",
      "status": "active",
      "priority_rank": 1,
      "positions": {
        "1": { "cosh_id": "<paddy uuid>",         "entity_type": "crop" },
        "2": { "cosh_id": "<tillering uuid>",     "entity_type": "crop_stage" },
        "3": { "cosh_id": "<stem_borer uuid>",    "entity_type": "pest" },
        "4": { "cosh_id": "<early uuid>",         "entity_type": "pest_stage" },
        "5": { "cosh_id": "<stem uuid>",          "entity_type": "part" },
        "6": { "cosh_id": "<BlankBox sentinel>",  "entity_type": "sub_part" },
        "7": { "cosh_id": "<bored_holes uuid>",   "entity_type": "symptom" },
        "8": { "cosh_id": "<BlankBox sentinel>",  "entity_type": "sub_symptom" }
      }
    }
  ]
}
```

The `relationship_*` fields in `schema[]` are informational —
RootsTalk reads them once per Connect for display purposes (debug
tools, future graph-walk UIs). The actual data ingestion uses
`positions[i].cosh_id` and `positions[i].entity_type` only.

### 11.3 Connect batch (`tomato_pest_images`)

```json
{
  "entity_type":  "tomato_pest_images",
  "connect_id":   "<uuid>",
  "connect_name": "Tomato Pest Images",
  "schema": [
    { "position_number": 1, "node_type": "CONNECT",
      "entity_type": "pest_diagnosis_chain",
      "relationship_to_next": "ILLUSTRATED_BY",
      "relationship_display_name": "Illustrated By" },
    { "position_number": 2, "node_type": "CORE",
      "entity_type": "media",
      "relationship_to_next": null,
      "relationship_display_name": null }
  ],
  "items": [
    {
      "cosh_id": "tpi-row-uuid",
      "entity_type": "tomato_pest_images",
      "status": "active",
      "positions": {
        "1": { "cosh_id": "<pest_diagnosis_chain row uuid>",
               "entity_type": "pest_diagnosis_chain" },
        "2": { "cosh_id": "<media uuid>",
               "entity_type": "media" }
      }
    }
  ]
}
```

---

## 12. Out of scope for V1

The following are intentionally not part of this contract today.

- **Per-row `created_at` / `updated_at`** — not needed by RootsTalk's
  V1 logic. May be added later if a feature requires temporal ordering
  beyond `priority_rank`.
- **Per-row authorship** — who curated this row in Cosh.
- **Cores beyond §8.1's list** — RootsTalk ignores tags and labels for
  entities outside the V1 list. Cosh emitting them is harmless; they
  simply land in `cosh_core_items` and stay unread.
- **Reverse sync (RootsTalk → Cosh)** — not implemented and not
  planned for V1.
- **Compound Connects with intermediate non-Core positions** — V1's
  `pest_diagnosis_chain` has all-CORE positions per §8.2. If Cosh
  models nested Connects (a position whose `node_type` is `CONNECT`),
  RootsTalk for V1 only handles this for `<crop>_pest_images` (where
  position 1 is the diagnosis Connect). Other compound shapes are
  out of scope.

---

## 13. Versioning

This document is V1. Any changes — adding a Core, changing a Connect's
schema, changing the BlankBox sentinel, etc. — are coordinated between
Cosh and RootsTalk teams in advance and reflected here in a new
revision.

The contract is immutable per RootsTalk release. Mid-release changes
are forbidden — they break either the Cosh emit or the RootsTalk
ingest depending on which side ships first.

---

## 14. Open items (must close before V1 launch)

1. **BlankBox sentinel** — pin exact spelling (§5).
2. **Cosh designer**: tag all entities in §8 to the `rootstalk`
   product and set canonical `entity_type_label` per the table.
3. **Cosh emitter**: confirm `priority_rank` is added as a row
   attribute on `pest_diagnosis_chain` rows (§6.1). If Cosh adds
   `priority_rank` as a real column on `connect_data_items`, this
   document doesn't change — only the emit code does.
4. **`<crop>_pest_images`** — Cosh designer creates one Connect per
   crop covered in V1 (paddy, tomato, ...). Each follows the §8.2
   schema.
5. **Sample sync run** against the dev DB once §8 is configured —
   captured JSON becomes the conformance fixture both teams build to.
