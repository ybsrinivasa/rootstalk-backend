"""Soil Nutrient Management module.

Design doc:
`/Users/ybsrinivasa/.claude/projects/-Users-ybsrinivasa/memory/project_soil_nutrient_module_design.md`

Phase 1 (2026-10-15) — platform-scoped foundation tables (Decisions L, N, P):
  - platform_soil_parameters: 21-parameter canon
  - preset_soil_profiles: Mock input presets

Phase 6d (2026-10-17) removed product_categories + product_category_fields —
Decision U made products Cosh-sourced references, so the category/field
schema that drove the Phase 1 authoring UIs has no consumer.

Client-scoped tables (client_products, client_normal_ranges,
package_normal_range_overrides, fallback_products, etc.) land in
later phases.
"""
