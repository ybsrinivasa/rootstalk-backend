"""soil_nutrient_phase1_seed

Revision ID: d2a0f574e215
Revises: d1a0f574e215
Create Date: 2026-10-15

Seeds the Soil Nutrient module's platform-scoped tables:
  - 22 platform_soil_parameters (ICL Narayanswamy canon, Decision N)
  - 2 product_categories (Decision L) with their field schemas
  - 12 preset_soil_profiles (Decision P + MQ5-B) for Mock input

Rows are referenced by their unique `name` column throughout the
application; UUIDs are generated at upgrade() time. Downgrade
truncates all four tables (does NOT drop them; that belongs to the
schema migration d1a0f574e215).
"""
import json
import uuid
from datetime import datetime, timezone
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd2a0f574e215'
down_revision: Union[str, Sequence[str], None] = 'd1a0f574e215'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# ── 22 parameters modeled on ICL Narayanswamy report (2026-01-23) ───────────
# Tuple shape: (name, unit, category_group, formula, method_note, display_order)
PARAMETERS = [
    # Base soil chemistry (4)
    ("pH",               None,     "BASE",      None, "1:2.5 water extraction",                 1),
    ("EC",               "dSm-1",  "BASE",      None, "1:2.5 water extraction",                 2),
    ("Free CaCO3",       "%",      "BASE",      None, "Titration",                              3),
    ("Organic Carbon",   "%",      "BASE",      None, "Walkley-Black",                          4),
    # Primary nutrients (3)
    ("Available Nitrogen",    "kg/ha", "PRIMARY", None, "Alkaline KMnO4",                       10),
    ("Available Phosphorous", "kg/ha", "PRIMARY", None, "Olsen (alkaline) / Bray (acidic)",     11),
    ("Available Potash",      "kg/ha", "PRIMARY", None, "Neutral NH4OAc",                       12),
    # Secondary nutrients (3)
    ("Available Calcium",     "%",   "SECONDARY", None, "NH4OAc extraction",                    20),
    ("Available Magnesium",   "ppm", "SECONDARY", None, "NH4OAc extraction",                    21),
    ("Available Sulphur",     "ppm", "SECONDARY", None, "CaCl2 or monocalcium phosphate",       22),
    # Micronutrients (5) — all DTPA + AAS unless noted
    ("Available Iron",        "ppm", "MICRO", None, "DTPA extraction + AAS detection",          30),
    ("Available Manganese",   "ppm", "MICRO", None, "DTPA extraction + AAS detection",          31),
    ("Available Zinc",        "ppm", "MICRO", None, "DTPA extraction + AAS detection",          32),
    ("Available Copper",      "ppm", "MICRO", None, "DTPA extraction + AAS detection",          33),
    ("Available Boron",       "ppm", "MICRO", None, "Hot water extraction",                     34),
    # Other (1)
    ("Extractable Sodium",    "ppm", "OTHER", None, "NH4OAc extraction",                        40),
    # Derived ratios (6)
    ("Ca/Mg Ratio", None, "RATIO", "Available Calcium / Available Magnesium",   "Computed from raw values", 50),
    ("Ca/K Ratio",  None, "RATIO", "Available Calcium / Available Potash",      "Computed from raw values", 51),
    ("Mg/K Ratio",  None, "RATIO", "Available Magnesium / Available Potash",    "Computed from raw values", 52),
    ("P/Zn Ratio",  None, "RATIO", "Available Phosphorous / Available Zinc",    "Computed from raw values", 53),
    ("Fe/Mn Ratio", None, "RATIO", "Available Iron / Available Manganese",      "Computed from raw values", 54),
    ("K/Na Ratio",  None, "RATIO", "Available Potash / Extractable Sodium",     "Computed from raw values", 55),
]


# ── Category field schema shared between both v1 categories ─────────────────
# Tuple shape: (field_key, field_label, field_type, required, unit, enum_options, display_order)
# `formulation` and `application_methods` are specified per-category below.
COMMON_FIELDS = [
    ("name",         "Name",                        "TEXT",   True,  None, None,  1),
    ("manufacturer", "Manufacturer",                "TEXT",   True,  None, None,  2),
    ("grade",        "Grade (e.g., 10:26:26)",      "TEXT",   False, None, None,  3),
    ("n_pct",        "N %",                         "NUMBER", False, "%",  None, 10),
    ("p2o5_pct",     "P2O5 %",                      "NUMBER", False, "%",  None, 11),
    ("k2o_pct",      "K2O %",                       "NUMBER", False, "%",  None, 12),
    ("ca_pct",       "Ca %",                        "NUMBER", False, "%",  None, 13),
    ("mg_pct",       "Mg %",                        "NUMBER", False, "%",  None, 14),
    ("s_pct",        "S %",                         "NUMBER", False, "%",  None, 15),
    ("zn_pct",       "Zn %",                        "NUMBER", False, "%",  None, 16),
    ("fe_pct",       "Fe %",                        "NUMBER", False, "%",  None, 17),
    ("mn_pct",       "Mn %",                        "NUMBER", False, "%",  None, 18),
    ("cu_pct",       "Cu %",                        "NUMBER", False, "%",  None, 19),
    ("b_pct",        "B %",                         "NUMBER", False, "%",  None, 20),
    ("notes",        "Notes",                       "TEXT",   False, None, None, 90),
]

# Category-specific enum options
FORMULATION_DIRECT = {"options": ["prilled", "granule", "powder", "paste"]}
APPLICATION_DIRECT = {"options": ["basal", "top-dress", "soil-incorporation"]}

FORMULATION_WATER = {"options": ["crystalline WSF", "liquid concentrate"]}
APPLICATION_WATER = {"options": ["fertigation", "foliar"]}


# ── 12 preset soil profiles for Mock input ──────────────────────────────────
# Values chosen loosely around common agronomic patterns. Purpose is
# Mock scenario coverage — the agronomy team will refine via SA Portal.

PRESETS = [
    (
        "Balanced optimum",
        "All parameters at optimum. Baseline sanity-check scenario.",
        {
            "pH": 6.8, "EC": 0.4, "Free CaCO3": 0.5, "Organic Carbon": 1.2,
            "Available Nitrogen": 200, "Available Phosphorous": 60, "Available Potash": 300,
            "Available Calcium": 0.08, "Available Magnesium": 350, "Available Sulphur": 25,
            "Available Iron": 3.5, "Available Manganese": 3.0, "Available Zinc": 2.5,
            "Available Copper": 0.7, "Available Boron": 0.35, "Extractable Sodium": 150,
        },
        1,
    ),
    (
        "Low NPK (deficient soil)",
        "N, P, K all below optimum. Classic input-depleted smallholder plot.",
        {
            "pH": 6.5, "EC": 0.3, "Free CaCO3": 0.4, "Organic Carbon": 0.6,
            "Available Nitrogen": 120, "Available Phosphorous": 25, "Available Potash": 140,
            "Available Calcium": 0.07, "Available Magnesium": 320, "Available Sulphur": 20,
            "Available Iron": 3.0, "Available Manganese": 2.8, "Available Zinc": 2.0,
            "Available Copper": 0.6, "Available Boron": 0.3, "Extractable Sodium": 200,
        },
        2,
    ),
    (
        "Alkaline soil with high Ca",
        "pH > 8, high Free CaCO3, high Ca — micros tend to be suppressed.",
        {
            "pH": 8.2, "EC": 0.6, "Free CaCO3": 4.5, "Organic Carbon": 0.9,
            "Available Nitrogen": 190, "Available Phosphorous": 55, "Available Potash": 260,
            "Available Calcium": 0.15, "Available Magnesium": 400, "Available Sulphur": 22,
            "Available Iron": 2.2, "Available Manganese": 1.8, "Available Zinc": 1.2,
            "Available Copper": 0.5, "Available Boron": 0.25, "Extractable Sodium": 400,
        },
        3,
    ),
    (
        "Acidic with low micros",
        "pH < 5.5, low Zn/Cu/B — classic acid-soil micronutrient deficiency.",
        {
            "pH": 5.2, "EC": 0.3, "Free CaCO3": 0.1, "Organic Carbon": 0.7,
            "Available Nitrogen": 180, "Available Phosphorous": 55, "Available Potash": 220,
            "Available Calcium": 0.04, "Available Magnesium": 180, "Available Sulphur": 15,
            "Available Iron": 1.5, "Available Manganese": 1.2, "Available Zinc": 0.4,
            "Available Copper": 0.2, "Available Boron": 0.1, "Extractable Sodium": 100,
        },
        4,
    ),
    (
        "Narayanswamy ICL anchor",
        "Actual Narayanswamy report values (Grape Red globe, 2026-01-23). "
        "Design anchor scenario — matches the ICL PDF the user shared.",
        {
            "pH": 6.5, "EC": 0.212, "Free CaCO3": 4.75, "Organic Carbon": 0.72,
            "Available Nitrogen": 112.90, "Available Phosphorous": 12.01,
            "Available Potash": 170.24,
            "Available Calcium": 900, "Available Magnesium": 780,
            "Available Sulphur": 12.40,
            "Available Iron": 4.43, "Available Manganese": 2.47,
            "Available Zinc": 0.25, "Available Copper": 0.29, "Available Boron": 0.78,
            "Extractable Sodium": 315,
        },
        5,
    ),
    (
        "Saline (high EC)",
        "EC > 2 dSm-1 + high Na. Salinity-stress scenario.",
        {
            "pH": 7.8, "EC": 2.5, "Free CaCO3": 1.2, "Organic Carbon": 0.5,
            "Available Nitrogen": 150, "Available Phosphorous": 40, "Available Potash": 180,
            "Available Calcium": 0.09, "Available Magnesium": 380, "Available Sulphur": 60,
            "Available Iron": 2.8, "Available Manganese": 2.0, "Available Zinc": 1.8,
            "Available Copper": 0.5, "Available Boron": 0.6, "Extractable Sodium": 1500,
        },
        6,
    ),
    (
        "Low N, high P, K optimum",
        "Common imbalance after years of P-heavy fertilization.",
        {
            "pH": 6.7, "EC": 0.3, "Free CaCO3": 0.5, "Organic Carbon": 1.0,
            "Available Nitrogen": 110, "Available Phosphorous": 110, "Available Potash": 280,
            "Available Calcium": 0.08, "Available Magnesium": 340, "Available Sulphur": 25,
            "Available Iron": 3.2, "Available Manganese": 2.8, "Available Zinc": 2.2,
            "Available Copper": 0.6, "Available Boron": 0.35, "Extractable Sodium": 180,
        },
        7,
    ),
    (
        "Low Zn, high P",
        "Classic P/Zn interaction — high P suppresses Zn uptake. "
        "Tests the P/Zn Ratio parameter.",
        {
            "pH": 7.5, "EC": 0.4, "Free CaCO3": 2.0, "Organic Carbon": 0.85,
            "Available Nitrogen": 185, "Available Phosphorous": 95, "Available Potash": 240,
            "Available Calcium": 0.09, "Available Magnesium": 320, "Available Sulphur": 22,
            "Available Iron": 2.5, "Available Manganese": 2.2, "Available Zinc": 0.4,
            "Available Copper": 0.5, "Available Boron": 0.3, "Extractable Sodium": 220,
        },
        8,
    ),
    (
        "Low Mg, high K",
        "Classic Mg/K interaction — high K suppresses Mg uptake. "
        "Tests the Mg/K Ratio parameter.",
        {
            "pH": 6.8, "EC": 0.4, "Free CaCO3": 0.6, "Organic Carbon": 1.0,
            "Available Nitrogen": 190, "Available Phosphorous": 55, "Available Potash": 650,
            "Available Calcium": 0.07, "Available Magnesium": 150, "Available Sulphur": 20,
            "Available Iron": 3.0, "Available Manganese": 2.5, "Available Zinc": 2.0,
            "Available Copper": 0.6, "Available Boron": 0.3, "Extractable Sodium": 180,
        },
        9,
    ),
    (
        "Micro-deficient (macros fine)",
        "All macros optimum, all micros low. Tests tier-2 fallback to "
        "micronutrient products.",
        {
            "pH": 7.0, "EC": 0.4, "Free CaCO3": 0.8, "Organic Carbon": 1.0,
            "Available Nitrogen": 200, "Available Phosphorous": 60, "Available Potash": 280,
            "Available Calcium": 0.08, "Available Magnesium": 350, "Available Sulphur": 20,
            "Available Iron": 1.8, "Available Manganese": 1.5, "Available Zinc": 0.6,
            "Available Copper": 0.3, "Available Boron": 0.15, "Extractable Sodium": 180,
        },
        10,
    ),
    (
        "High organic matter",
        "Rich soil, high OC. Tests upper-bound behavior.",
        {
            "pH": 6.8, "EC": 0.5, "Free CaCO3": 0.4, "Organic Carbon": 1.9,
            "Available Nitrogen": 240, "Available Phosphorous": 80, "Available Potash": 380,
            "Available Calcium": 0.09, "Available Magnesium": 400, "Available Sulphur": 35,
            "Available Iron": 4.0, "Available Manganese": 3.5, "Available Zinc": 3.0,
            "Available Copper": 0.9, "Available Boron": 0.4, "Extractable Sodium": 220,
        },
        11,
    ),
    (
        "Sandy, nutrient-leached",
        "Low everything. Typical rainfed sandy-soil scenario.",
        {
            "pH": 5.8, "EC": 0.2, "Free CaCO3": 0.1, "Organic Carbon": 0.4,
            "Available Nitrogen": 90, "Available Phosphorous": 20, "Available Potash": 100,
            "Available Calcium": 0.03, "Available Magnesium": 120, "Available Sulphur": 10,
            "Available Iron": 1.5, "Available Manganese": 1.2, "Available Zinc": 0.8,
            "Available Copper": 0.25, "Available Boron": 0.12, "Extractable Sodium": 80,
        },
        12,
    ),
]


def upgrade() -> None:
    now = datetime.now(timezone.utc)
    conn = op.get_bind()

    # ── platform_soil_parameters ──────────────────────────────────────
    for (name, unit, group, formula, method, order) in PARAMETERS:
        conn.execute(
            sa.text("""
                INSERT INTO platform_soil_parameters
                    (id, name, unit, category_group, formula,
                     measurement_method_note, display_order, status,
                     created_at, updated_at)
                VALUES
                    (:id, :name, :unit, :group, :formula, :method,
                     :order, 'ACTIVE', :now, :now)
            """),
            {
                "id": str(uuid.uuid4()),
                "name": name, "unit": unit, "group": group,
                "formula": formula, "method": method, "order": order,
                "now": now,
            },
        )

    # ── product_categories ────────────────────────────────────────────
    cat_direct_id = str(uuid.uuid4())
    cat_water_id = str(uuid.uuid4())

    conn.execute(
        sa.text("""
            INSERT INTO product_categories
                (id, name, scope, display_order, status,
                 created_at, updated_at)
            VALUES
                (:id, :name, 'GLOBAL', :order, 'ACTIVE', :now, :now)
        """),
        {"id": cat_direct_id, "name": "Direct Soil Application",
         "order": 1, "now": now},
    )
    conn.execute(
        sa.text("""
            INSERT INTO product_categories
                (id, name, scope, display_order, status,
                 created_at, updated_at)
            VALUES
                (:id, :name, 'GLOBAL', :order, 'ACTIVE', :now, :now)
        """),
        {"id": cat_water_id, "name": "Water Solubles",
         "order": 2, "now": now},
    )

    # ── product_category_fields ───────────────────────────────────────
    def insert_field(cid, fkey, label, ftype, required, unit,
                     enum_vals, order):
        conn.execute(
            sa.text("""
                INSERT INTO product_category_fields
                    (id, category_id, field_key, field_label,
                     field_type, required, unit, enum_values_json,
                     display_order, status, created_at)
                VALUES
                    (:id, :cid, :fkey, :label, :ftype, :req, :unit,
                     CAST(:enum AS JSON), :order, 'ACTIVE', :now)
            """),
            {
                "id": str(uuid.uuid4()),
                "cid": cid, "fkey": fkey, "label": label,
                "ftype": ftype, "req": required, "unit": unit,
                "enum": json.dumps(enum_vals) if enum_vals else None,
                "order": order, "now": now,
            },
        )

    # Direct Soil Application
    insert_field(cat_direct_id, "formulation", "Formulation", "ENUM",
                 True, None, FORMULATION_DIRECT, 4)
    insert_field(cat_direct_id, "application_methods", "Application Methods",
                 "MULTI_SELECT", True, None, APPLICATION_DIRECT, 5)
    for (fkey, label, ftype, required, unit, enum_vals, order) in COMMON_FIELDS:
        insert_field(cat_direct_id, fkey, label, ftype, required,
                     unit, enum_vals, order)

    # Water Solubles
    insert_field(cat_water_id, "formulation", "Formulation", "ENUM",
                 True, None, FORMULATION_WATER, 4)
    insert_field(cat_water_id, "application_methods", "Application Methods",
                 "MULTI_SELECT", True, None, APPLICATION_WATER, 5)
    for (fkey, label, ftype, required, unit, enum_vals, order) in COMMON_FIELDS:
        insert_field(cat_water_id, fkey, label, ftype, required,
                     unit, enum_vals, order)

    # ── preset_soil_profiles ──────────────────────────────────────────
    for (name, desc, values, order) in PRESETS:
        conn.execute(
            sa.text("""
                INSERT INTO preset_soil_profiles
                    (id, name, description, values_json, display_order,
                     status, created_at, updated_at)
                VALUES
                    (:id, :name, :desc, CAST(:values AS JSON), :order,
                     'ACTIVE', :now, :now)
            """),
            {
                "id": str(uuid.uuid4()),
                "name": name, "desc": desc,
                "values": json.dumps(values),
                "order": order, "now": now,
            },
        )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text("DELETE FROM preset_soil_profiles"))
    conn.execute(sa.text("DELETE FROM product_category_fields"))
    conn.execute(sa.text("DELETE FROM product_categories"))
    conn.execute(sa.text("DELETE FROM platform_soil_parameters"))
