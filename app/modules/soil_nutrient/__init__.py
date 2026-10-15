"""Soil Nutrient Management module.

Design doc:
`/Users/ybsrinivasa/.claude/projects/-Users-ybsrinivasa/memory/project_soil_nutrient_module_design.md`

Phase 1 (2026-10-15) — platform-scoped foundation tables (Decisions L, N, P):
  - platform_soil_parameters: 21-parameter canon
  - product_categories: platform-seeded category list
  - product_category_fields: per-category field schema
  - preset_soil_profiles: Mock input presets

Client-scoped tables (client_products, client_normal_ranges,
package_normal_range_overrides, fallback_products, etc.) land in
later phases.
"""
