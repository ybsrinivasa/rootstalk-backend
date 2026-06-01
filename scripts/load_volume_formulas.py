"""One-shot loader for the 304 BL-06 volume formulas.

Source: RootsTalk_VolCalc_Reference.pdf (April 2026, CONFIDENTIAL).
Loaded 2026-06-01.

Lookup key: (measure, l2_practice, application_method, brand_unit,
dosage_unit). NPK rows use suffix-discriminated L2 strings:
CHEMICAL_FERTILIZERS_NPK_DOSAGES__N / __P / __K / __COMPLEX (and the
fertigation siblings) so a single practice.l2_type still differentiates
the per-nutrient formula at lookup time. The volume-estimate endpoint
needs to derive the correct suffix from the picked brand's nutrient
class before this layer can serve NPK estimates; non-NPK rows are
immediately usable.

Run via:
  cd ~/apps/rootstalk-backend && source venv/bin/activate
  python scripts/load_volume_formulas.py
"""
from __future__ import annotations

import asyncio
import sys

from app.database import AsyncSessionLocal
from app.modules.sync.models import VolumeFormula


# ── Source rows (304) ────────────────────────────────────────────────────────
# Format: (measure, l2_practice, application_method, brand_unit, dosage_unit, formula)
# Formulas keep the × character; bl06_volume_calc.evaluate_formula
# substitutes it for * at eval time (Finding 1 in
# project_rootstalk_volcalc_formulas.md).

ROWS: list[tuple[str, str, str, str, str, str]] = [

    # ── Area-wise · Chemical Pesticides (24) ──────────────────────────
    ("AREA_WISE", "CHEMICAL_PESTICIDES", "Soil Drenching",                "kg", "g/L",      "(Dosage × 200 × Total_area)/1000"),
    ("AREA_WISE", "CHEMICAL_PESTICIDES", "Soil Drenching",                "L",  "ml/L",     "(Dosage × 200 × Total_area)/1000"),
    ("AREA_WISE", "CHEMICAL_PESTICIDES", "Soil Drenching",                "g",  "g/L",      "Dosage × 200 × Total_area"),
    ("AREA_WISE", "CHEMICAL_PESTICIDES", "Soil Drenching",                "ml", "ml/L",     "Dosage × 200 × Total_area"),
    ("AREA_WISE", "CHEMICAL_PESTICIDES", "Direct Soil Application",       "kg", "kg/acre",  "Dosage × Total_area"),
    ("AREA_WISE", "CHEMICAL_PESTICIDES", "Root Zone Application",         "kg", "kg/acre",  "Dosage × Total_area"),
    ("AREA_WISE", "CHEMICAL_PESTICIDES", "Fumigation",                    "ml", "ml/acre",  "Dosage × Total_area"),
    ("AREA_WISE", "CHEMICAL_PESTICIDES", "Fumigation",                    "g",  "g/acre",   "Dosage × Total_area"),
    ("AREA_WISE", "CHEMICAL_PESTICIDES", "Foliar Spray",                  "kg", "g/L",      "(Dosage × 150 × Total_area)/1000"),
    ("AREA_WISE", "CHEMICAL_PESTICIDES", "Foliar Spray",                  "L",  "ml/L",     "(Dosage × 150 × Total_area)/1000"),
    ("AREA_WISE", "CHEMICAL_PESTICIDES", "Foliar Spray",                  "g",  "g/L",      "Dosage × 150 × Total_area"),
    ("AREA_WISE", "CHEMICAL_PESTICIDES", "Foliar Spray",                  "ml", "ml/L",     "Dosage × 150 × Total_area"),
    ("AREA_WISE", "CHEMICAL_PESTICIDES", "Apply through drip irrigation", "kg", "g/acre",   "(Dosage × Total_area)/1000"),
    ("AREA_WISE", "CHEMICAL_PESTICIDES", "Apply through drip irrigation", "L",  "ml/acre",  "(Dosage × Total_area)/1000"),
    ("AREA_WISE", "CHEMICAL_PESTICIDES", "Apply through drip irrigation", "L",  "L/acre",   "Dosage × Total_area"),
    ("AREA_WISE", "CHEMICAL_PESTICIDES", "Apply through drip irrigation", "kg", "kg/acre",  "Dosage × Total_area"),
    ("AREA_WISE", "CHEMICAL_PESTICIDES", "Leaf whorl application",        "kg", "kg/acre",  "Dosage × Total_area"),
    ("AREA_WISE", "CHEMICAL_PESTICIDES", "Spot Application",              "kg", "kg/acre",  "Dosage × Total_area"),
    ("AREA_WISE", "CHEMICAL_PESTICIDES", "Soil Incorporation",            "kg", "kg/acre",  "Dosage × Total_area"),
    ("AREA_WISE", "CHEMICAL_PESTICIDES", "Furrow Application",            "kg", "kg/acre",  "Dosage × Total_area"),
    ("AREA_WISE", "CHEMICAL_PESTICIDES", "Dusting",                       "kg", "kg/acre",  "Dosage × Total_area"),
    ("AREA_WISE", "CHEMICAL_PESTICIDES", "Broadcasting",                  "kg", "kg/acre",  "Dosage × Total_area"),
    ("AREA_WISE", "CHEMICAL_PESTICIDES", "Baiting",                       "g",  "g/acre",   "Dosage × Total_area"),
    ("AREA_WISE", "CHEMICAL_PESTICIDES", "Baiting",                       "ml", "ml/acre",  "Dosage × Total_area"),

    # ── Area-wise · Microbial Pesticides (17) ─────────────────────────
    ("AREA_WISE", "MICROBIAL_PESTICIDES", "Foliar Spray",                  "kg", "g/L",     "(Dosage × 150 × Total_area)/1000"),
    ("AREA_WISE", "MICROBIAL_PESTICIDES", "Foliar Spray",                  "L",  "ml/L",    "(Dosage × 150 × Total_area)/1000"),
    ("AREA_WISE", "MICROBIAL_PESTICIDES", "Foliar Spray",                  "g",  "g/L",     "Dosage × 150 × Total_area"),
    ("AREA_WISE", "MICROBIAL_PESTICIDES", "Foliar Spray",                  "ml", "ml/L",    "Dosage × 150 × Total_area"),
    ("AREA_WISE", "MICROBIAL_PESTICIDES", "Direct Soil Application",       "kg", "kg/acre", "Dosage × Total_area"),
    ("AREA_WISE", "MICROBIAL_PESTICIDES", "Soil Incorporation",            "kg", "kg/acre", "Dosage × Total_area"),
    ("AREA_WISE", "MICROBIAL_PESTICIDES", "Soil Drenching",                "kg", "g/L",     "(Dosage × 200 × Total_area)/1000"),
    ("AREA_WISE", "MICROBIAL_PESTICIDES", "Soil Drenching",                "L",  "ml/L",    "(Dosage × 200 × Total_area)/1000"),
    ("AREA_WISE", "MICROBIAL_PESTICIDES", "Soil Drenching",                "g",  "g/L",     "Dosage × 200 × Total_area"),
    ("AREA_WISE", "MICROBIAL_PESTICIDES", "Soil Drenching",                "ml", "ml/L",    "Dosage × 200 × Total_area"),
    ("AREA_WISE", "MICROBIAL_PESTICIDES", "Furrow Application",            "kg", "kg/acre", "Dosage × Total_area"),
    ("AREA_WISE", "MICROBIAL_PESTICIDES", "Apply through drip irrigation", "kg", "g/acre",  "(Dosage × Total_area)/1000"),
    ("AREA_WISE", "MICROBIAL_PESTICIDES", "Apply through drip irrigation", "L",  "ml/acre", "(Dosage × Total_area)/1000"),
    ("AREA_WISE", "MICROBIAL_PESTICIDES", "Apply through drip irrigation", "L",  "L/acre",  "Dosage × Total_area"),
    ("AREA_WISE", "MICROBIAL_PESTICIDES", "Apply through drip irrigation", "kg", "kg/acre", "Dosage × Total_area"),
    ("AREA_WISE", "MICROBIAL_PESTICIDES", "Baiting",                       "g",  "g/acre",  "Dosage × Total_area"),
    ("AREA_WISE", "MICROBIAL_PESTICIDES", "Baiting",                       "ml", "ml/acre", "Dosage × Total_area"),

    # ── Area-wise · Botanical Pesticides (7) ──────────────────────────
    ("AREA_WISE", "BOTANICAL_PESTICIDES", "Soil Drenching",                "L",  "ml/L",    "(Dosage × 200 × Total_area)/1000"),
    ("AREA_WISE", "BOTANICAL_PESTICIDES", "Soil Drenching",                "ml", "ml/L",    "Dosage × 200 × Total_area"),
    ("AREA_WISE", "BOTANICAL_PESTICIDES", "Foliar Spray",                  "L",  "ml/L",    "(Dosage × 150 × Total_area)/1000"),
    ("AREA_WISE", "BOTANICAL_PESTICIDES", "Foliar Spray",                  "ml", "ml/L",    "Dosage × 150 × Total_area"),
    ("AREA_WISE", "BOTANICAL_PESTICIDES", "Apply through drip irrigation", "L",  "ml/acre", "(Dosage × Total_area)/1000"),
    ("AREA_WISE", "BOTANICAL_PESTICIDES", "Apply through drip irrigation", "L",  "L/acre",  "Dosage × Total_area"),
    ("AREA_WISE", "BOTANICAL_PESTICIDES", "Apply Through Drip Irrigation", "L",  "ml/acre", "(Dosage × Total_area)/1000"),

    # ── Area-wise · Other Pesticides (6) ──────────────────────────────
    ("AREA_WISE", "OTHER_PESTICIDES", "Foliar Spray",   "g",  "g/L",  "Dosage × 150 × Total_area"),
    ("AREA_WISE", "OTHER_PESTICIDES", "Foliar Spray",   "ml", "ml/L", "Dosage × 150 × Total_area"),
    ("AREA_WISE", "OTHER_PESTICIDES", "Foliar Spray",   "kg", "g/L",  "(Dosage × 150 × Total_area)/1000"),
    ("AREA_WISE", "OTHER_PESTICIDES", "Foliar Spray",   "L",  "ml/L", "(Dosage × 150 × Total_area)/1000"),
    ("AREA_WISE", "OTHER_PESTICIDES", "Soil Drenching", "kg", "g/L",  "(Dosage × 200 × Total_area)/1000"),
    ("AREA_WISE", "OTHER_PESTICIDES", "Soil Drenching", "L",  "ml/L", "(Dosage × 200 × Total_area)/1000"),

    # ── Area-wise · Chemical Herbicides (9) ───────────────────────────
    ("AREA_WISE", "CHEMICAL_HERBICIDES", "Foliar Spray",            "kg", "g/L",          "(Dosage × 200 × Total_area)/1000"),
    ("AREA_WISE", "CHEMICAL_HERBICIDES", "Foliar Spray",            "L",  "ml/L",         "(Dosage × 200 × Total_area)/1000"),
    ("AREA_WISE", "CHEMICAL_HERBICIDES", "Foliar Spray",            "g",  "g/L",          "Dosage × 200 × Total_area"),
    ("AREA_WISE", "CHEMICAL_HERBICIDES", "Foliar Spray",            "ml", "ml/L",         "Dosage × 200 × Total_area"),
    ("AREA_WISE", "CHEMICAL_HERBICIDES", "Broadcasting",            "kg", "kg/acre",      "Dosage × Total_area"),
    ("AREA_WISE", "CHEMICAL_HERBICIDES", "Direct Soil Application", "kg", "g/L of water", "(Dosage × 200 × Total_area)/1000"),
    ("AREA_WISE", "CHEMICAL_HERBICIDES", "Direct Soil Application", "L",  "ml/L of water","(Dosage × 200 × Total_area)/1000"),
    ("AREA_WISE", "CHEMICAL_HERBICIDES", "Direct Soil Application", "g",  "g/L of water", "Dosage × 200 × Total_area"),
    ("AREA_WISE", "CHEMICAL_HERBICIDES", "Direct Soil Application", "ml", "ml/L of water","Dosage × 200 × Total_area"),

    # ── Area-wise · Adjuvants (2) ─────────────────────────────────────
    ("AREA_WISE", "ADJUVANTS", "Foliar Spray", "g",  "g/L",  "Dosage × 150 × Total_area"),
    ("AREA_WISE", "ADJUVANTS", "Foliar Spray", "ml", "ml/L", "Dosage × 150 × Total_area"),

    # ── Area-wise · Insect Biocontrol Agents (4) ──────────────────────
    ("AREA_WISE", "INSECT_BIOCONTROL_AGENTS", "Release of Adults",                          "numbers", "number of adults/acre",                 "Dosage × Total_area"),
    ("AREA_WISE", "INSECT_BIOCONTROL_AGENTS", "Release of Larvae",                          "numbers", "number of larvae/acre",                 "Dosage × Total_area"),
    ("AREA_WISE", "INSECT_BIOCONTROL_AGENTS", "Release of Eggs",                            "numbers", "number of eggs/acre",                   "Dosage × Total_area"),
    ("AREA_WISE", "INSECT_BIOCONTROL_AGENTS", "Release Of Infective Juveniles (IJs) nematodes", "numbers", "number of infective juveniles (IJs) /acre", "Dosage × Total_area"),

    # ── Area-wise · Insect Traps (1) ──────────────────────────────────
    ("AREA_WISE", "INSECT_TRAPS", "BLANK BOX", "numbers", "traps/acre", "Dosage × Total_area"),

    # ── Area-wise · Manures (7) ───────────────────────────────────────
    ("AREA_WISE", "MANURES", "Direct Soil Application",         "kg", "kg/acre", "Dosage × Total_area"),
    ("AREA_WISE", "MANURES", "Soil Incorporation",              "kg", "kg/acre", "Dosage × Total_area"),
    ("AREA_WISE", "MANURES", "Foliar Spray",                    "L",  "ml/L",    "(Dosage × 150 × Total_area)/1000"),
    ("AREA_WISE", "MANURES", "Broadcasting",                    "kg", "kg/acre", "Dosage × Total_area"),
    ("AREA_WISE", "MANURES", "Apply through drip irrigation",   "L",  "L/acre",  "Dosage × Total_area"),
    ("AREA_WISE", "MANURES", "Broadcasting with incorporation", "kg", "kg/acre", "Dosage × Total_area"),
    ("AREA_WISE", "MANURES", "Soil Drenching",                  "L",  "ml/L",    "(Dosage × 200 × Total_area)/1000"),

    # ── Area-wise · Chemical Fertilizer Products (12) ─────────────────
    ("AREA_WISE", "CHEMICAL_FERTILIZER_PRODUCTS", "Direct Soil Application",       "kg", "kg/acre", "Dosage × Total_area"),
    ("AREA_WISE", "CHEMICAL_FERTILIZER_PRODUCTS", "Soil Drenching",                "kg", "g/L",     "(Dosage × 200 × Total_area)/1000"),
    ("AREA_WISE", "CHEMICAL_FERTILIZER_PRODUCTS", "Soil Drenching",                "L",  "ml/L",    "(Dosage × 200 × Total_area)/1000"),
    ("AREA_WISE", "CHEMICAL_FERTILIZER_PRODUCTS", "Furrow Application",            "kg", "kg/acre", "Dosage × Total_area"),
    ("AREA_WISE", "CHEMICAL_FERTILIZER_PRODUCTS", "Foliar Spray",                  "kg", "g/L",     "(Dosage × 150 × Total_area)/1000"),
    ("AREA_WISE", "CHEMICAL_FERTILIZER_PRODUCTS", "Foliar Spray",                  "L",  "ml/L",    "(Dosage × 150 × Total_area)/1000"),
    ("AREA_WISE", "CHEMICAL_FERTILIZER_PRODUCTS", "Apply Through Drip Irrigation", "kg", "kg/acre", "Dosage × Total_area"),
    ("AREA_WISE", "CHEMICAL_FERTILIZER_PRODUCTS", "Apply Through Drip Irrigation", "L",  "L/acre",  "Dosage × Total_area"),
    ("AREA_WISE", "CHEMICAL_FERTILIZER_PRODUCTS", "Soil Incorporation",            "kg", "kg/acre", "Dosage × Total_area"),
    ("AREA_WISE", "CHEMICAL_FERTILIZER_PRODUCTS", "Dusting",                       "kg", "kg/acre", "Dosage × Total_area"),
    ("AREA_WISE", "CHEMICAL_FERTILIZER_PRODUCTS", "Broadcasting",                  "kg", "kg/acre", "Dosage × Total_area"),
    ("AREA_WISE", "CHEMICAL_FERTILIZER_PRODUCTS", "Band Placement",                "kg", "kg/acre", "Dosage × Total_area"),

    # ── Area-wise · Biofertilizers (11) ───────────────────────────────
    ("AREA_WISE", "BIOFERTILIZERS", "Soil Incorporation",            "kg", "kg/acre", "Dosage × Total_area"),
    ("AREA_WISE", "BIOFERTILIZERS", "Soil Drenching",                "L",  "ml/L",    "(Dosage × 200 × Total_area)/1000"),
    ("AREA_WISE", "BIOFERTILIZERS", "Soil Drenching",                "kg", "g/L",     "(Dosage × 200 × Total_area)/1000"),
    ("AREA_WISE", "BIOFERTILIZERS", "Direct Soil Application",       "kg", "kg/acre", "Dosage × Total_area"),
    ("AREA_WISE", "BIOFERTILIZERS", "Root Zone Application",         "kg", "kg/acre", "Dosage × Total_area"),
    ("AREA_WISE", "BIOFERTILIZERS", "Furrow Application",            "kg", "kg/acre", "Dosage × Total_area"),
    ("AREA_WISE", "BIOFERTILIZERS", "Foliar Spray",                  "L",  "ml/L",    "(Dosage × 150 × Total_area)/1000"),
    ("AREA_WISE", "BIOFERTILIZERS", "Foliar Spray",                  "kg", "g/L",     "(Dosage × 150 × Total_area)/1000"),
    ("AREA_WISE", "BIOFERTILIZERS", "Broadcasting",                  "kg", "kg/acre", "Dosage × Total_area"),
    ("AREA_WISE", "BIOFERTILIZERS", "Apply Through Drip Irrigation", "L",  "L/acre",  "Dosage × Total_area"),
    ("AREA_WISE", "BIOFERTILIZERS", "Apply Through Drip Irrigation", "kg", "kg/acre", "Dosage × Total_area"),

    # ── Area-wise · PGRs/Hormones/Stimulants/Tonics (23) ──────────────
    ("AREA_WISE", "PGR_TONICS", "Soil Drenching",                "kg", "g/L",          "(Dosage × 200 × Total_area)/1000"),
    ("AREA_WISE", "PGR_TONICS", "Soil Drenching",                "L",  "ml/L",         "(Dosage × 200 × Total_area)/1000"),
    ("AREA_WISE", "PGR_TONICS", "Soil Drenching",                "g",  "g/L",          "Dosage × 200 × Total_area"),
    ("AREA_WISE", "PGR_TONICS", "Soil Drenching",                "ml", "ml/L",         "Dosage × 200 × Total_area"),
    ("AREA_WISE", "PGR_TONICS", "Soil Drenching",                "g",  "mg/L",         "(Dosage × 200 × Total_area)/1000"),
    ("AREA_WISE", "PGR_TONICS", "Soil Drenching",                "g",  "ppm/L",        "(Dosage × 200 × Total_area)/1000"),
    ("AREA_WISE", "PGR_TONICS", "Soil Drenching",                "ml", "ppm/L",        "(Dosage × 200 × Total_area)/1000"),
    ("AREA_WISE", "PGR_TONICS", "Foliar Spray",                  "kg", "g/L",          "(Dosage × 150 × Total_area)/1000"),
    ("AREA_WISE", "PGR_TONICS", "Foliar Spray",                  "L",  "ml/L",         "(Dosage × 150 × Total_area)/1000"),
    ("AREA_WISE", "PGR_TONICS", "Foliar Spray",                  "g",  "g/L",          "Dosage × 150 × Total_area"),
    ("AREA_WISE", "PGR_TONICS", "Foliar Spray",                  "ml", "ml/L",         "Dosage × 150 × Total_area"),
    ("AREA_WISE", "PGR_TONICS", "Foliar Spray",                  "g",  "mg/L",         "(Dosage × 150 × Total_area)/1000"),
    ("AREA_WISE", "PGR_TONICS", "Foliar Spray",                  "g",  "ppm/L",        "(Dosage × 150 × Total_area)/1000"),
    ("AREA_WISE", "PGR_TONICS", "Foliar Spray",                  "ml", "ppm/L",        "(Dosage × 150 × Total_area)/1000"),
    ("AREA_WISE", "PGR_TONICS", "Apply Through Drip Irrigation", "L",  "L/acre",       "Dosage × Total_area"),
    ("AREA_WISE", "PGR_TONICS", "Apply Through Drip Irrigation", "kg", "kg/acre",      "Dosage × Total_area"),
    ("AREA_WISE", "PGR_TONICS", "Apply Through Drip Irrigation", "kg", "g/acre",       "(Dosage × Total_area)/1000"),
    ("AREA_WISE", "PGR_TONICS", "Apply Through Drip Irrigation", "L",  "ml/acre",      "(Dosage × Total_area)/1000"),
    ("AREA_WISE", "PGR_TONICS", "Apply Through Drip Irrigation", "g",  "g/acre",       "Dosage × Total_area"),
    ("AREA_WISE", "PGR_TONICS", "Apply Through Drip Irrigation", "ml", "ml/acre",      "Dosage × Total_area"),
    ("AREA_WISE", "PGR_TONICS", "Broadcasting",                  "kg", "kg/acre",      "Dosage × Total_area"),
    ("AREA_WISE", "PGR_TONICS", "Broadcasting",                  "kg", "kg/acre",      "Dosage × Total_area"),
    ("AREA_WISE", "PGR_TONICS", "Soil Drenching",                "ml", "ml/L of water","Dosage × 200 × Total_area"),

    # ── Area-wise · Soil Amendments and Conditioners (2) ──────────────
    ("AREA_WISE", "SOIL_AMENDMENTS", "Broadcasting",                          "kg", "kg/acre", "Dosage × Total_area"),
    ("AREA_WISE", "SOIL_AMENDMENTS", "Broadcasting with soil incorporation",  "kg", "kg/acre", "Dosage × Total_area"),

    # ── Area-wise · Chemical Fertilizers — NPK (N Input) (5) ──────────
    ("AREA_WISE", "CHEMICAL_FERTILIZERS_NPK_DOSAGES__N", "Furrow Application",       "kg", "kg/acre", "(N_Dosage × 100 × Total_area)/Concentration"),
    ("AREA_WISE", "CHEMICAL_FERTILIZERS_NPK_DOSAGES__N", "Broadcasting",             "kg", "kg/acre", "(N_Dosage × 100 × Total_area)/Concentration"),
    ("AREA_WISE", "CHEMICAL_FERTILIZERS_NPK_DOSAGES__N", "Band Placement",           "kg", "kg/acre", "(N_Dosage × 100 × Total_area)/Concentration"),
    ("AREA_WISE", "CHEMICAL_FERTILIZERS_NPK_DOSAGES__N", "Direct Soil Application",  "kg", "kg/acre", "(N_Dosage × 100 × Total_area)/Concentration"),
    ("AREA_WISE", "CHEMICAL_FERTILIZERS_NPK_DOSAGES__N", "Soil Incorporation",       "kg", "kg/acre", "(N_Dosage × 100 × Total_area)/Concentration"),

    # ── Area-wise · NPK (P Input) (5) ─────────────────────────────────
    ("AREA_WISE", "CHEMICAL_FERTILIZERS_NPK_DOSAGES__P", "Furrow Application",       "kg", "kg/acre", "(P_Dosage × 100 × Total_area)/Concentration"),
    ("AREA_WISE", "CHEMICAL_FERTILIZERS_NPK_DOSAGES__P", "Broadcasting",             "kg", "kg/acre", "(P_Dosage × 100 × Total_area)/Concentration"),
    ("AREA_WISE", "CHEMICAL_FERTILIZERS_NPK_DOSAGES__P", "Band Placement",           "kg", "kg/acre", "(P_Dosage × 100 × Total_area)/Concentration"),
    ("AREA_WISE", "CHEMICAL_FERTILIZERS_NPK_DOSAGES__P", "Direct Soil Application",  "kg", "kg/acre", "(P_Dosage × 100 × Total_area)/Concentration"),
    ("AREA_WISE", "CHEMICAL_FERTILIZERS_NPK_DOSAGES__P", "Soil Incorporation",       "kg", "kg/acre", "(P_Dosage × 100 × Total_area)/Concentration"),

    # ── Area-wise · NPK (K Input) (5) ─────────────────────────────────
    ("AREA_WISE", "CHEMICAL_FERTILIZERS_NPK_DOSAGES__K", "Furrow Application",       "kg", "kg/acre", "(K_Dosage × 100 × Total_area)/Concentration"),
    ("AREA_WISE", "CHEMICAL_FERTILIZERS_NPK_DOSAGES__K", "Broadcasting",             "kg", "kg/acre", "(K_Dosage × 100 × Total_area)/Concentration"),
    ("AREA_WISE", "CHEMICAL_FERTILIZERS_NPK_DOSAGES__K", "Band Placement",           "kg", "kg/acre", "(K_Dosage × 100 × Total_area)/Concentration"),
    ("AREA_WISE", "CHEMICAL_FERTILIZERS_NPK_DOSAGES__K", "Direct Soil Application",  "kg", "kg/acre", "(K_Dosage × 100 × Total_area)/Concentration"),
    ("AREA_WISE", "CHEMICAL_FERTILIZERS_NPK_DOSAGES__K", "Soil Incorporation",       "kg", "kg/acre", "(K_Dosage × 100 × Total_area)/Concentration"),

    # ── Area-wise · NPK (Complex Input) (5) ───────────────────────────
    ("AREA_WISE", "CHEMICAL_FERTILIZERS_NPK_DOSAGES__COMPLEX", "Furrow Application",       "kg", "kg/acre", "Calculated_value × Total_area"),
    ("AREA_WISE", "CHEMICAL_FERTILIZERS_NPK_DOSAGES__COMPLEX", "Broadcasting",             "kg", "kg/acre", "Calculated_value × Total_area"),
    ("AREA_WISE", "CHEMICAL_FERTILIZERS_NPK_DOSAGES__COMPLEX", "Band Placement",           "kg", "kg/acre", "Calculated_value × Total_area"),
    ("AREA_WISE", "CHEMICAL_FERTILIZERS_NPK_DOSAGES__COMPLEX", "Direct Soil Application",  "kg", "kg/acre", "Calculated_value × Total_area"),
    ("AREA_WISE", "CHEMICAL_FERTILIZERS_NPK_DOSAGES__COMPLEX", "Soil Incorporation",       "kg", "kg/acre", "Calculated_value × Total_area"),

    # ── Area-wise · Chemical Fertilizers — Fertigation Products (4) ───
    ("AREA_WISE", "CHEMICAL_FERTILIZER_FERTIGATION_PRODUCTS", "Apply Through Drip Irrigation", "kg", "kg/acre", "Dosage × Total_area"),
    ("AREA_WISE", "CHEMICAL_FERTILIZER_FERTIGATION_PRODUCTS", "Apply Through Drip Irrigation", "L",  "L/acre",  "Dosage × Total_area"),
    ("AREA_WISE", "CHEMICAL_FERTILIZER_FERTIGATION_PRODUCTS", "Apply Through Drip Irrigation", "kg", "g/acre",  "(Dosage × Total_area)/1000"),
    ("AREA_WISE", "CHEMICAL_FERTILIZER_FERTIGATION_PRODUCTS", "Apply Through Drip Irrigation", "L",  "ml/acre", "(Dosage × Total_area)/1000"),

    # ── Area-wise · Fertigation NPK Dosages (N) (4) ───────────────────
    ("AREA_WISE", "FERTIGATION_NPK_DOSAGES__N", "Apply Through Drip Irrigation", "kg", "kg/acre", "(N_Dosage × 100 × Total_area)/Concentration"),
    ("AREA_WISE", "FERTIGATION_NPK_DOSAGES__N", "Apply Through Drip Irrigation", "L",  "L/acre",  "(N_Dosage × 100 × Total_area)/Concentration"),
    ("AREA_WISE", "FERTIGATION_NPK_DOSAGES__N", "Apply Through Drip Irrigation", "kg", "g/acre",  "(N_Dosage × 100 × Total_area)/(Concentration × 1000)"),
    ("AREA_WISE", "FERTIGATION_NPK_DOSAGES__N", "Apply Through Drip Irrigation", "L",  "ml/acre", "(N_Dosage × 100 × Total_area)/(Concentration × 1000)"),

    # ── Area-wise · Fertigation NPK Dosages (P) (4) ───────────────────
    ("AREA_WISE", "FERTIGATION_NPK_DOSAGES__P", "Apply Through Drip Irrigation", "kg", "kg/acre", "(P_Dosage × 100 × Total_area)/Concentration"),
    ("AREA_WISE", "FERTIGATION_NPK_DOSAGES__P", "Apply Through Drip Irrigation", "L",  "L/acre",  "(P_Dosage × 100 × Total_area)/Concentration"),
    ("AREA_WISE", "FERTIGATION_NPK_DOSAGES__P", "Apply Through Drip Irrigation", "kg", "g/acre",  "(P_Dosage × 100 × Total_area)/(Concentration × 1000)"),
    ("AREA_WISE", "FERTIGATION_NPK_DOSAGES__P", "Apply Through Drip Irrigation", "L",  "ml/acre", "(P_Dosage × 100 × Total_area)/(Concentration × 1000)"),

    # ── Area-wise · Fertigation NPK Dosages (K) (4) ───────────────────
    ("AREA_WISE", "FERTIGATION_NPK_DOSAGES__K", "Apply Through Drip Irrigation", "kg", "kg/acre", "(K_Dosage × 100 × Total_area)/Concentration"),
    ("AREA_WISE", "FERTIGATION_NPK_DOSAGES__K", "Apply Through Drip Irrigation", "L",  "L/acre",  "(K_Dosage × 100 × Total_area)/Concentration"),
    ("AREA_WISE", "FERTIGATION_NPK_DOSAGES__K", "Apply Through Drip Irrigation", "kg", "g/acre",  "(K_Dosage × 100 × Total_area)/(Concentration × 1000)"),
    ("AREA_WISE", "FERTIGATION_NPK_DOSAGES__K", "Apply Through Drip Irrigation", "L",  "ml/acre", "(K_Dosage × 100 × Total_area)/(Concentration × 1000)"),

    # ── Area-wise · Fertigation NPK Dosages (Complex) (4) ─────────────
    ("AREA_WISE", "FERTIGATION_NPK_DOSAGES__COMPLEX", "Apply Through Drip Irrigation", "kg", "kg/acre", "Calculated_value × Total_area"),
    ("AREA_WISE", "FERTIGATION_NPK_DOSAGES__COMPLEX", "Apply Through Drip Irrigation", "L",  "L/acre",  "Calculated_value × Total_area"),
    ("AREA_WISE", "FERTIGATION_NPK_DOSAGES__COMPLEX", "Apply Through Drip Irrigation", "kg", "g/acre",  "(Calculated_value × Total_area) / 1000"),
    ("AREA_WISE", "FERTIGATION_NPK_DOSAGES__COMPLEX", "Apply Through Drip Irrigation", "L",  "ml/acre", "(Calculated_value × Total_area) / 1000"),

    # ── Plant-wise · Chemical Pesticides (28) ─────────────────────────
    ("PLANT_WISE", "CHEMICAL_PESTICIDES", "Soil Drenching",          "g",       "g/L",        "Dosage × Vol_per_plant × Total_No_of_plants"),
    ("PLANT_WISE", "CHEMICAL_PESTICIDES", "Soil Drenching",          "ml",      "ml/L",       "Dosage × Vol_per_plant × Total_No_of_plants"),
    ("PLANT_WISE", "CHEMICAL_PESTICIDES", "Foliar Spray",            "g",       "g/L",        "Dosage × Vol_per_plant × Total_No_of_plants"),
    ("PLANT_WISE", "CHEMICAL_PESTICIDES", "Foliar Spray",            "kg",      "g/L",        "(Dosage × Vol_per_plant × Total_No_of_plants)/1000"),
    ("PLANT_WISE", "CHEMICAL_PESTICIDES", "Foliar Spray",            "ml",      "ml/L",       "Dosage × Vol_per_plant × Total_No_of_plants"),
    ("PLANT_WISE", "CHEMICAL_PESTICIDES", "Foliar Spray",            "L",       "ml/L",       "(Dosage × Vol_per_plant × Total_No_of_plants)/1000"),
    ("PLANT_WISE", "CHEMICAL_PESTICIDES", "Trunk Injection",         "g",       "g/L",        "(Dosage × Vol_per_plant × Total_No_of_plants)/1000"),
    ("PLANT_WISE", "CHEMICAL_PESTICIDES", "Trunk Injection",         "ml",      "ml/L",       "(Dosage × Vol_per_plant × Total_No_of_plants)/1000"),
    ("PLANT_WISE", "CHEMICAL_PESTICIDES", "Stem Injection",          "g",       "g/L",        "(Dosage × Vol_per_plant × Total_No_of_plants)/1000"),
    ("PLANT_WISE", "CHEMICAL_PESTICIDES", "Stem Injection",          "ml",      "ml/L",       "(Dosage × Vol_per_plant × Total_No_of_plants)/1000"),
    ("PLANT_WISE", "CHEMICAL_PESTICIDES", "Soil Drenching",          "kg",      "g/L",        "(Dosage × Vol_per_plant × Total_No_of_plants)/1000"),
    ("PLANT_WISE", "CHEMICAL_PESTICIDES", "Soil Drenching",          "L",       "ml/L",       "(Dosage × Vol_per_plant × Total_No_of_plants)/1000"),
    ("PLANT_WISE", "CHEMICAL_PESTICIDES", "Root Feeding",            "g",       "g/L",        "(Dosage × Vol_per_plant × Total_No_of_plants)/1000"),
    ("PLANT_WISE", "CHEMICAL_PESTICIDES", "Root Feeding",            "ml",      "ml/L",       "(Dosage × Vol_per_plant × Total_No_of_plants)/1000"),
    ("PLANT_WISE", "CHEMICAL_PESTICIDES", "Cone feeding",            "g",       "g/L",        "(Dosage × Vol_per_plant × Total_No_of_plants)/1000"),
    ("PLANT_WISE", "CHEMICAL_PESTICIDES", "Cone feeding",            "ml",      "ml/L",       "(Dosage × Vol_per_plant × Total_No_of_plants)/1000"),
    ("PLANT_WISE", "CHEMICAL_PESTICIDES", "Cone feeding",            "kg",      "g/plant",    "(Dosage × Total_No_of_plants)/1000"),
    ("PLANT_WISE", "CHEMICAL_PESTICIDES", "Direct Soil Application", "kg",      "g/plant",    "(Vol_per_plant × Total_No_of_plants)/1000"),
    ("PLANT_WISE", "CHEMICAL_PESTICIDES", "Direct Soil Application", "kg",      "kg/plant",   "Vol_per_plant × Total_No_of_plants"),
    ("PLANT_WISE", "CHEMICAL_PESTICIDES", "Root Zone Application",   "kg",      "g/plant",    "(Vol_per_plant × Total_No_of_plants)/1000"),
    ("PLANT_WISE", "CHEMICAL_PESTICIDES", "Root Zone Application",   "kg",      "kg/plant",   "Vol_per_plant × Total_No_of_plants"),
    ("PLANT_WISE", "CHEMICAL_PESTICIDES", "Ring Application",        "kg",      "g/plant",    "(Vol_per_plant × Total_No_of_plants)/1000"),
    ("PLANT_WISE", "CHEMICAL_PESTICIDES", "Ring Application",        "kg",      "kg/plant",   "Vol_per_plant × Total_No_of_plants"),
    ("PLANT_WISE", "CHEMICAL_PESTICIDES", "Fumigation",              "g",       "g/plant",    "Vol_per_plant × Total_No_of_plants"),
    ("PLANT_WISE", "CHEMICAL_PESTICIDES", "Fumigation",              "ml",      "ml/plant",   "Vol_per_plant × Total_No_of_plants"),
    ("PLANT_WISE", "CHEMICAL_PESTICIDES", "Fumigation",              "numbers", "tablet/plant","Vol_per_plant × Total_No_of_plants"),
    ("PLANT_WISE", "CHEMICAL_PESTICIDES", "Crown Application",       "kg",      "g/plant",    "(Vol_per_plant × Total_No_of_plants)/1000"),
    ("PLANT_WISE", "CHEMICAL_PESTICIDES", "Cone feeding",            "kg",      "g/plant",    "(Vol_per_plant × Total_No_of_plants)/1000"),

    # ── Plant-wise · Microbial Pesticides (12) ────────────────────────
    ("PLANT_WISE", "MICROBIAL_PESTICIDES", "Foliar Spray",            "g",  "g/L",          "Dosage × Vol_per_plant × Total_No_of_plants"),
    ("PLANT_WISE", "MICROBIAL_PESTICIDES", "Foliar Spray",            "ml", "ml/L",         "Dosage × Vol_per_plant × Total_No_of_plants"),
    ("PLANT_WISE", "MICROBIAL_PESTICIDES", "Soil Drenching",          "g",  "g/L",          "Dosage × Vol_per_plant × Total_No_of_plants"),
    ("PLANT_WISE", "MICROBIAL_PESTICIDES", "Soil Drenching",          "ml", "ml/L",         "Dosage × Vol_per_plant × Total_No_of_plants"),
    ("PLANT_WISE", "MICROBIAL_PESTICIDES", "Foliar Spray",            "kg", "g/L",          "(Dosage × Vol_per_plant × Total_No_of_plants)/1000"),
    ("PLANT_WISE", "MICROBIAL_PESTICIDES", "Foliar Spray",            "L",  "ml/L",         "(Dosage × Vol_per_plant × Total_No_of_plants)/1000"),
    ("PLANT_WISE", "MICROBIAL_PESTICIDES", "Soil Drenching",          "kg", "g/L",          "(Dosage × Vol_per_plant × Total_No_of_plants)/1000"),
    ("PLANT_WISE", "MICROBIAL_PESTICIDES", "Soil Drenching",          "L",  "ml/L",         "(Dosage × Vol_per_plant × Total_No_of_plants)/1000"),
    ("PLANT_WISE", "MICROBIAL_PESTICIDES", "Root Feeding",            "g",  "g/L of water", "Dosage × Vol_per_plant × Total_No_of_plants"),
    ("PLANT_WISE", "MICROBIAL_PESTICIDES", "Root Feeding",            "ml", "ml/L of water","Dosage × Vol_per_plant × Total_No_of_plants"),
    ("PLANT_WISE", "MICROBIAL_PESTICIDES", "Direct Soil Application", "kg", "g/plant",      "(Vol_per_plant × Total_No_of_plants)/1000"),
    ("PLANT_WISE", "MICROBIAL_PESTICIDES", "Direct Soil Application", "kg", "kg/plant",     "Vol_per_plant × Total_No_of_plants"),

    # ── Plant-wise · Botanical Pesticides (6) ─────────────────────────
    ("PLANT_WISE", "BOTANICAL_PESTICIDES", "Soil Drenching", "ml", "ml/L",         "Dosage × Vol_per_plant × Total_No_of_plants"),
    ("PLANT_WISE", "BOTANICAL_PESTICIDES", "Foliar Spray",   "ml", "ml/L",         "Dosage × Vol_per_plant × Total_No_of_plants"),
    ("PLANT_WISE", "BOTANICAL_PESTICIDES", "Soil Drenching", "L",  "ml/L",         "(Dosage × Vol_per_plant × Total_No_of_plants)/1000"),
    ("PLANT_WISE", "BOTANICAL_PESTICIDES", "Foliar Spray",   "L",  "ml/L",         "(Dosage × Vol_per_plant × Total_No_of_plants)/1000"),
    ("PLANT_WISE", "BOTANICAL_PESTICIDES", "Root Feeding",   "g",  "g/L of water", "Dosage × Vol_per_plant × Total_No_of_plants"),
    ("PLANT_WISE", "BOTANICAL_PESTICIDES", "Root Feeding",   "ml", "ml/L of water","Dosage × Vol_per_plant × Total_No_of_plants"),

    # ── Plant-wise · Other Pesticides (8) ─────────────────────────────
    ("PLANT_WISE", "OTHER_PESTICIDES", "Foliar Spray",   "ml", "ml/L", "Dosage × Vol_per_plant × Total_No_of_plants"),
    ("PLANT_WISE", "OTHER_PESTICIDES", "Foliar Spray",   "L",  "ml/L", "(Dosage × Vol_per_plant × Total_No_of_plants)/1000"),
    ("PLANT_WISE", "OTHER_PESTICIDES", "Soil Drenching", "ml", "ml/L", "Dosage × Vol_per_plant × Total_No_of_plants"),
    ("PLANT_WISE", "OTHER_PESTICIDES", "Soil Drenching", "L",  "ml/L", "(Dosage × Vol_per_plant × Total_No_of_plants)/1000"),
    ("PLANT_WISE", "OTHER_PESTICIDES", "Foliar Spray",   "g",  "g/L",  "Dosage × Vol_per_plant × Total_No_of_plants"),
    ("PLANT_WISE", "OTHER_PESTICIDES", "Foliar Spray",   "kg", "g/L",  "(Dosage × Vol_per_plant × Total_No_of_plants)/1000"),
    ("PLANT_WISE", "OTHER_PESTICIDES", "Soil Drenching", "g",  "g/L",  "Dosage × Vol_per_plant × Total_No_of_plants"),
    ("PLANT_WISE", "OTHER_PESTICIDES", "Soil Drenching", "kg", "g/L",  "(Dosage × Vol_per_plant × Total_No_of_plants)/1000"),

    # ── Plant-wise · Insect Biocontrol Agents (3) ─────────────────────
    ("PLANT_WISE", "INSECT_BIOCONTROL_AGENTS", "Release of Adults",                              "numbers", "number of adults/plant",                "(Dosage × Total_No_of_plants)"),
    ("PLANT_WISE", "INSECT_BIOCONTROL_AGENTS", "Release of Larvae",                              "numbers", "number of larvae/plant",                "(Dosage × Total_No_of_plants)"),
    ("PLANT_WISE", "INSECT_BIOCONTROL_AGENTS", "Release Of Infective Juveniles (IJs) nematodes", "numbers", "number of infective juveniles (IJs)/ plant", "(Dosage × Total_No_of_plants)"),

    # ── Plant-wise · Manures (6) ──────────────────────────────────────
    ("PLANT_WISE", "MANURES", "Foliar Spray",            "ml", "ml/L",     "Dosage × Vol_per_plant × Total_No_of_plants"),
    ("PLANT_WISE", "MANURES", "Foliar Spray",            "L",  "ml/L",     "(Dosage × Vol_per_plant × Total_No_of_plants)/1000"),
    ("PLANT_WISE", "MANURES", "Soil Drenching",          "ml", "ml/L",     "Dosage × Vol_per_plant × Total_No_of_plants"),
    ("PLANT_WISE", "MANURES", "Soil Drenching",          "L",  "ml/L",     "(Dosage × Vol_per_plant × Total_No_of_plants)/1000"),
    ("PLANT_WISE", "MANURES", "Direct Soil Application", "kg", "g/plant",  "(Vol_per_plant × Total_No_of_plants)/1000"),
    ("PLANT_WISE", "MANURES", "Direct Soil Application", "kg", "kg/plant", "Vol_per_plant × Total_No_of_plants"),

    # ── Plant-wise · Chemical Fertilizer Products (12) ────────────────
    ("PLANT_WISE", "CHEMICAL_FERTILIZER_PRODUCTS", "Soil Drenching",          "g",  "g/L",      "Dosage × Vol_per_plant × Total_No_of_plants"),
    ("PLANT_WISE", "CHEMICAL_FERTILIZER_PRODUCTS", "Soil Drenching",          "ml", "ml/L",     "Dosage × Vol_per_plant × Total_No_of_plants"),
    ("PLANT_WISE", "CHEMICAL_FERTILIZER_PRODUCTS", "Soil Drenching",          "kg", "g/L",      "(Dosage × Vol_per_plant × Total_No_of_plants)/1000"),
    ("PLANT_WISE", "CHEMICAL_FERTILIZER_PRODUCTS", "Soil Drenching",          "L",  "ml/L",     "(Dosage × Vol_per_plant × Total_No_of_plants)/1000"),
    ("PLANT_WISE", "CHEMICAL_FERTILIZER_PRODUCTS", "Foliar Spray",            "g",  "g/L",      "Dosage × Vol_per_plant × Total_No_of_plants"),
    ("PLANT_WISE", "CHEMICAL_FERTILIZER_PRODUCTS", "Foliar Spray",            "ml", "ml/L",     "Dosage × Vol_per_plant × Total_No_of_plants"),
    ("PLANT_WISE", "CHEMICAL_FERTILIZER_PRODUCTS", "Foliar Spray",            "kg", "g/L",      "(Dosage × Vol_per_plant × Total_No_of_plants)/1000"),
    ("PLANT_WISE", "CHEMICAL_FERTILIZER_PRODUCTS", "Foliar Spray",            "L",  "ml/L",     "(Dosage × Vol_per_plant × Total_No_of_plants)/1000"),
    ("PLANT_WISE", "CHEMICAL_FERTILIZER_PRODUCTS", "Direct Soil Application", "kg", "g/plant",  "(Vol_per_plant × Total_No_of_plants)/1000"),
    ("PLANT_WISE", "CHEMICAL_FERTILIZER_PRODUCTS", "Direct Soil Application", "kg", "kg/plant", "Vol_per_plant × Total_No_of_plants"),
    ("PLANT_WISE", "CHEMICAL_FERTILIZER_PRODUCTS", "Ring Application",        "kg", "g/plant",  "(Vol_per_plant × Total_No_of_plants)/1000"),
    ("PLANT_WISE", "CHEMICAL_FERTILIZER_PRODUCTS", "Ring Application",        "kg", "kg/plant", "Vol_per_plant × Total_No_of_plants"),

    # ── Plant-wise · Biofertilizers (14) ──────────────────────────────
    ("PLANT_WISE", "BIOFERTILIZERS", "Soil Drenching",          "g",  "g/L",          "Dosage × Vol_per_plant × Total_No_of_plants"),
    ("PLANT_WISE", "BIOFERTILIZERS", "Soil Drenching",          "ml", "ml/L",         "Dosage × Vol_per_plant × Total_No_of_plants"),
    ("PLANT_WISE", "BIOFERTILIZERS", "Soil Drenching",          "kg", "g/L",          "(Dosage × Vol_per_plant × Total_No_of_plants)/1000"),
    ("PLANT_WISE", "BIOFERTILIZERS", "Soil Drenching",          "L",  "ml/L",         "(Dosage × Vol_per_plant × Total_No_of_plants)/1000"),
    ("PLANT_WISE", "BIOFERTILIZERS", "Foliar Spray",            "g",  "g/L",          "Dosage × Vol_per_plant × Total_No_of_plants"),
    ("PLANT_WISE", "BIOFERTILIZERS", "Foliar Spray",            "ml", "ml/L",         "Dosage × Vol_per_plant × Total_No_of_plants"),
    ("PLANT_WISE", "BIOFERTILIZERS", "Foliar Spray",            "kg", "g/L",          "(Dosage × Vol_per_plant × Total_No_of_plants)/1000"),
    ("PLANT_WISE", "BIOFERTILIZERS", "Foliar Spray",            "L",  "ml/L",         "(Dosage × Vol_per_plant × Total_No_of_plants)/1000"),
    ("PLANT_WISE", "BIOFERTILIZERS", "Root Feeding",            "g",  "g/L of water", "Dosage × Vol_per_plant × Total_No_of_plants"),
    ("PLANT_WISE", "BIOFERTILIZERS", "Root Feeding",            "ml", "ml/L of water","Dosage × Vol_per_plant × Total_No_of_plants"),
    ("PLANT_WISE", "BIOFERTILIZERS", "Direct Soil Application", "kg", "g/plant",      "(Vol_per_plant × Total_No_of_plants)/1000"),
    ("PLANT_WISE", "BIOFERTILIZERS", "Direct Soil Application", "kg", "kg/plant",     "Vol_per_plant × Total_No_of_plants"),
    ("PLANT_WISE", "BIOFERTILIZERS", "Root Zone Application",   "kg", "g/plant",      "(Vol_per_plant × Total_No_of_plants)/1000"),
    ("PLANT_WISE", "BIOFERTILIZERS", "Root Zone Application",   "kg", "kg/plant",     "Vol_per_plant × Total_No_of_plants"),

    # ── Plant-wise · PGRs/Hormones/Stimulants/Tonics (10) ─────────────
    ("PLANT_WISE", "PGR_TONICS", "Soil Drenching", "g",  "g/L",   "Dosage × Vol_per_plant × Total_No_of_plants"),
    ("PLANT_WISE", "PGR_TONICS", "Soil Drenching", "ml", "ml/L",  "Dosage × Vol_per_plant × Total_No_of_plants"),
    ("PLANT_WISE", "PGR_TONICS", "Soil Drenching", "g",  "mg/L",  "(Dosage × Vol_per_plant × Total_No_of_plants)/1000"),
    ("PLANT_WISE", "PGR_TONICS", "Soil Drenching", "g",  "ppm/L", "(Dosage × Vol_per_plant × Total_No_of_plants)/1000"),
    ("PLANT_WISE", "PGR_TONICS", "Soil Drenching", "ml", "ppm/L", "(Dosage × Vol_per_plant × Total_No_of_plants)/1000"),
    ("PLANT_WISE", "PGR_TONICS", "Foliar Spray",   "g",  "g/L",   "Dosage × Vol_per_plant × Total_No_of_plants"),
    ("PLANT_WISE", "PGR_TONICS", "Foliar Spray",   "ml", "ml/L",  "Dosage × Vol_per_plant × Total_No_of_plants"),
    ("PLANT_WISE", "PGR_TONICS", "Foliar Spray",   "g",  "mg/L",  "(Dosage × Vol_per_plant × Total_No_of_plants)/1000"),
    ("PLANT_WISE", "PGR_TONICS", "Foliar Spray",   "g",  "ppm/L", "(Dosage × Vol_per_plant × Total_No_of_plants)/1000"),
    ("PLANT_WISE", "PGR_TONICS", "Foliar Spray",   "ml", "ppm/L", "(Dosage × Vol_per_plant × Total_No_of_plants)/1000"),

    # ── Plant-wise · Soil Amendments and Conditioners (4) ─────────────
    ("PLANT_WISE", "SOIL_AMENDMENTS", "Ring Application", "kg", "g/plant",  "(Vol_per_plant × Total_No_of_plants)/1000"),
    ("PLANT_WISE", "SOIL_AMENDMENTS", "Ring Application", "kg", "kg/plant", "Vol_per_plant × Total_No_of_plants"),
    ("PLANT_WISE", "SOIL_AMENDMENTS", "Spot Application", "kg", "g/plant",  "(Vol_per_plant × Total_No_of_plants)/1000"),
    ("PLANT_WISE", "SOIL_AMENDMENTS", "Spot Application", "kg", "kg/plant", "Vol_per_plant × Total_No_of_plants"),

    # ── Plant-wise · NPK (N Input) (4) ────────────────────────────────
    ("PLANT_WISE", "CHEMICAL_FERTILIZERS_NPK_DOSAGES__N", "Ring Application",         "kg", "g/plant",  "(N_Dosage × 100 × Total_No_of_plants)/(Concentration × 1000)"),
    ("PLANT_WISE", "CHEMICAL_FERTILIZERS_NPK_DOSAGES__N", "Direct Soil Application",  "kg", "g/plant",  "(N_Dosage × 100 × Total_No_of_plants)/(Concentration × 1000)"),
    ("PLANT_WISE", "CHEMICAL_FERTILIZERS_NPK_DOSAGES__N", "Ring Application",         "kg", "kg/plant", "(N_Dosage × 100 × Total_No_of_plants)/Concentration"),
    ("PLANT_WISE", "CHEMICAL_FERTILIZERS_NPK_DOSAGES__N", "Direct Soil Application",  "kg", "kg/plant", "(N_Dosage × 100 × Total_No_of_plants)/Concentration"),

    # ── Plant-wise · NPK (P Input) (4) ────────────────────────────────
    ("PLANT_WISE", "CHEMICAL_FERTILIZERS_NPK_DOSAGES__P", "Ring Application",         "kg", "g/plant",  "(P_Dosage × 100 × Total_No_of_plants)/(Concentration × 1000)"),
    ("PLANT_WISE", "CHEMICAL_FERTILIZERS_NPK_DOSAGES__P", "Direct Soil Application",  "kg", "g/plant",  "(P_Dosage × 100 × Total_No_of_plants)/(Concentration × 1000)"),
    ("PLANT_WISE", "CHEMICAL_FERTILIZERS_NPK_DOSAGES__P", "Ring Application",         "kg", "kg/plant", "(P_Dosage × 100 × Total_No_of_plants)/Concentration"),
    ("PLANT_WISE", "CHEMICAL_FERTILIZERS_NPK_DOSAGES__P", "Direct Soil Application",  "kg", "kg/plant", "(P_Dosage × 100 × Total_No_of_plants)/Concentration"),

    # ── Plant-wise · NPK (K Input) (4) ────────────────────────────────
    ("PLANT_WISE", "CHEMICAL_FERTILIZERS_NPK_DOSAGES__K", "Ring Application",         "kg", "g/plant",  "(K_Dosage × 100 × Total_No_of_plants)/(Concentration × 1000)"),
    ("PLANT_WISE", "CHEMICAL_FERTILIZERS_NPK_DOSAGES__K", "Direct Soil Application",  "kg", "g/plant",  "(K_Dosage × 100 × Total_No_of_plants)/(Concentration × 1000)"),
    ("PLANT_WISE", "CHEMICAL_FERTILIZERS_NPK_DOSAGES__K", "Ring Application",         "kg", "kg/plant", "(K_Dosage × 100 × Total_No_of_plants)/Concentration"),
    ("PLANT_WISE", "CHEMICAL_FERTILIZERS_NPK_DOSAGES__K", "Direct Soil Application",  "kg", "kg/plant", "(K_Dosage × 100 × Total_No_of_plants)/Concentration"),

    # ── Plant-wise · NPK (Complex Input) (4) ──────────────────────────
    ("PLANT_WISE", "CHEMICAL_FERTILIZERS_NPK_DOSAGES__COMPLEX", "Ring Application",        "kg", "g/plant",  "(Calculated_value × Total_No_of_plants)/1000"),
    ("PLANT_WISE", "CHEMICAL_FERTILIZERS_NPK_DOSAGES__COMPLEX", "Direct Soil Application", "kg", "g/plant",  "(Calculated_value × Total_No_of_plants)/1000"),
    ("PLANT_WISE", "CHEMICAL_FERTILIZERS_NPK_DOSAGES__COMPLEX", "Ring Application",        "kg", "kg/plant", "Calculated_value × Total_No_of_plants"),
    ("PLANT_WISE", "CHEMICAL_FERTILIZERS_NPK_DOSAGES__COMPLEX", "Direct Soil Application", "kg", "kg/plant", "Calculated_value × Total_No_of_plants"),

    # ── Plant-wise · Fertigation Products (4) ─────────────────────────
    ("PLANT_WISE", "CHEMICAL_FERTILIZER_FERTIGATION_PRODUCTS", "Apply Through Drip Irrigation", "kg", "kg/plant", "(Dosage × Total_No_of_plants)"),
    ("PLANT_WISE", "CHEMICAL_FERTILIZER_FERTIGATION_PRODUCTS", "Apply Through Drip Irrigation", "L",  "L/plant",  "(Dosage × Total_No_of_plants)"),
    ("PLANT_WISE", "CHEMICAL_FERTILIZER_FERTIGATION_PRODUCTS", "Apply Through Drip Irrigation", "kg", "g/plant",  "(Dosage × Total_No_of_plants)/1000"),
    ("PLANT_WISE", "CHEMICAL_FERTILIZER_FERTIGATION_PRODUCTS", "Apply Through Drip Irrigation", "L",  "ml/plant", "(Dosage × Total_No_of_plants)/1000"),

    # ── Plant-wise · Fertigation NPK Dosages (N) (4) ──────────────────
    ("PLANT_WISE", "FERTIGATION_NPK_DOSAGES__N", "Apply Through Drip Irrigation", "kg", "kg/plant", "(N_Dosage × 100 × Total_No_of_plants)/Concentration"),
    ("PLANT_WISE", "FERTIGATION_NPK_DOSAGES__N", "Apply Through Drip Irrigation", "L",  "L/plant",  "(N_Dosage × 100 × Total_No_of_plants)/Concentration"),
    ("PLANT_WISE", "FERTIGATION_NPK_DOSAGES__N", "Apply Through Drip Irrigation", "kg", "g/plant",  "(N_Dosage × 100 × Total_No_of_plants)/(Concentration × 1000)"),
    ("PLANT_WISE", "FERTIGATION_NPK_DOSAGES__N", "Apply Through Drip Irrigation", "L",  "ml/plant", "(N_Dosage × 100 × Total_No_of_plants)/(Concentration × 1000)"),

    # ── Plant-wise · Fertigation NPK Dosages (P) (4) ──────────────────
    ("PLANT_WISE", "FERTIGATION_NPK_DOSAGES__P", "Apply Through Drip Irrigation", "kg", "kg/plant", "(P_Dosage × 100 × Total_No_of_plants)/Concentration"),
    ("PLANT_WISE", "FERTIGATION_NPK_DOSAGES__P", "Apply Through Drip Irrigation", "L",  "L/plant",  "(P_Dosage × 100 × Total_No_of_plants)/Concentration"),
    ("PLANT_WISE", "FERTIGATION_NPK_DOSAGES__P", "Apply Through Drip Irrigation", "kg", "g/plant",  "(P_Dosage × 100 × Total_No_of_plants)/(Concentration × 1000)"),
    ("PLANT_WISE", "FERTIGATION_NPK_DOSAGES__P", "Apply Through Drip Irrigation", "L",  "ml/plant", "(P_Dosage × 100 × Total_No_of_plants)/(Concentration × 1000)"),

    # ── Plant-wise · Fertigation NPK Dosages (K) (4) ──────────────────
    ("PLANT_WISE", "FERTIGATION_NPK_DOSAGES__K", "Apply Through Drip Irrigation", "kg", "kg/plant", "(K_Dosage × 100 × Total_No_of_plants)/Concentration"),
    ("PLANT_WISE", "FERTIGATION_NPK_DOSAGES__K", "Apply Through Drip Irrigation", "L",  "L/plant",  "(K_Dosage × 100 × Total_No_of_plants)/Concentration"),
    ("PLANT_WISE", "FERTIGATION_NPK_DOSAGES__K", "Apply Through Drip Irrigation", "kg", "g/plant",  "(K_Dosage × 100 × Total_No_of_plants)/(Concentration × 1000)"),
    ("PLANT_WISE", "FERTIGATION_NPK_DOSAGES__K", "Apply Through Drip Irrigation", "L",  "ml/plant", "(K_Dosage × 100 × Total_No_of_plants)/(Concentration × 1000)"),

    # ── Plant-wise · Fertigation NPK Dosages (Complex) (4) ────────────
    ("PLANT_WISE", "FERTIGATION_NPK_DOSAGES__COMPLEX", "Apply Through Drip Irrigation", "kg", "kg/plant", "(Calculated_value × Total_No_of_plants)"),
    ("PLANT_WISE", "FERTIGATION_NPK_DOSAGES__COMPLEX", "Apply Through Drip Irrigation", "L",  "L/plant",  "(Calculated_value × Total_No_of_plants)"),
    ("PLANT_WISE", "FERTIGATION_NPK_DOSAGES__COMPLEX", "Apply Through Drip Irrigation", "kg", "g/plant",  "(Calculated_value × Total_No_of_plants) / 1000"),
    ("PLANT_WISE", "FERTIGATION_NPK_DOSAGES__COMPLEX", "Apply Through Drip Irrigation", "L",  "ml/plant", "(Calculated_value × Total_No_of_plants) / 1000"),
]


async def main() -> None:
    expected = 304
    if len(ROWS) != expected:
        print(f"!! ROW COUNT MISMATCH: {len(ROWS)} (expected {expected})", file=sys.stderr)
    async with AsyncSessionLocal() as db:
        # Truncate-and-reload — single source of truth is this script.
        await db.execute(VolumeFormula.__table__.delete())
        for measure, l2, method, brand_unit, dosage_unit, formula in ROWS:
            db.add(VolumeFormula(
                measure=measure,
                l2_practice=l2,
                application_method=method,
                brand_unit=brand_unit,
                dosage_unit=dosage_unit,
                formula=formula,
                status="ACTIVE",
            ))
        await db.commit()
    print(f"Loaded {len(ROWS)} volume formulas.")


if __name__ == "__main__":
    asyncio.run(main())
