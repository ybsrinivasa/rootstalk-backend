"""Locked Cosh-side identifiers — captured from the first production
sync, 2026-05-09 (sync_id 93e3e668-2266-4839-986f-b09898db3fc0).

Cosh consolidates all biological identifiers (crops, pests, biocontrol
agents) into a single `biological_names` Core. Each item's semantic
role is expressed by a separate Connect (`biological_names_and_roles`)
linking it to one of the items in the `roles_of_biological_names` Core.

Filter on UUIDs, never on translated strings. The string "Crop" can be
edited by Cosh curators; the UUID is the stable identifier.
"""
from __future__ import annotations

# Core entity_type slugs (the `core_type` column on cosh_core_items)
COSH_BIOLOGICAL_NAMES_CORE = "biological_names"
COSH_ROLES_CORE = "roles_of_biological_names"

# Connect entity_type slug (the `connect_type` column on cosh_connect_rows)
COSH_NAME_ROLE_CONNECT = "biological_names_and_roles"

# Stable UUIDs of the three role items. New roles can be added in Cosh
# without touching this file — they're just unused unless we add a
# constant referring to them.
COSH_ROLE_CROP_UUID = "9735f960-2d5a-498d-8fdd-429be0cfb950"
COSH_ROLE_PEST_UUID = "4138f142-d2bd-43c2-8252-d658799bcc99"
COSH_ROLE_BIO_CONTROL_AGENT_UUID = "7f2f5283-e427-484a-a5b8-40181d633fa2"

# Role names used inside Connect endpoints (the `role` key of each
# endpoint dict). Cosh emits `entity_type` of the underlying Core as
# the role string, so these match the Core slugs above.
ENDPOINT_ROLE_BIOLOGICAL_NAME = COSH_BIOLOGICAL_NAMES_CORE
ENDPOINT_ROLE_OF_NAME = COSH_ROLES_CORE
