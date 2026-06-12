#!/usr/bin/env bash
# Guardrail: fail if anyone adds a hardcoded .get("en") / .get("English")
# read against a translations-shaped dict outside the central i18n_cosh
# helper.
#
# Such sites silently return English regardless of the caller's locale
# and re-introduce the very bug we centralised i18n_cosh.pick_translation
# to fix (see commit ee0c9e2 et al, 2026-06-12).
#
# 2026-06-13 — the v1 pattern required the literal variable name
# `translations`. That missed aliased forms like `tr = cc.translations
# or {}; tr.get("en")` which leaked through in orders/router.py:4402
# (Funnel trap / Acephate stayed English in Kannada PWA). v2 broadens
# the match to ANY `.get("en")` / `.get("English")` and uses a path-level
# allow-list to exempt the small set of intentional EN-key sites:
#   - audit-trail columns (canonical EN persisted alongside JSONB)
#   - BL-06 formula lookup keys (the 304 formulas key on EN method names)
#   - NPK / diagnosis pipelines keyed on EN concentration / problem codes
#   - SA-portal admin browses that show only English
#   - cache builders that mirror the EN column for fallback display
#
# Run from repo root:    bash scripts/check_no_en_only_lookups.sh
# Also runs as a pytest: tests/test_no_en_only_lookups.py

set -euo pipefail

PATTERN='\.get\("(en|English)"\)|\["(en|English)"\]'

hits=$(grep -rEn "$PATTERN" app 2>/dev/null \
  | grep -v __pycache__ \
  | grep -vE '^app/+services/i18n_cosh\.py:' \
  | grep -vE '^app/+modules/sync/service\.py:' \
  | grep -vE '^app/+modules/sync/router\.py:' \
  | grep -vE '^app/+services/cosh_crop_view\.py:' \
  | grep -vE '^app/+services/crop_snapshot\.py:' \
  | grep -vE '^app/+services/cosh_options_view\.py:' \
  | grep -vE '^app/+services/cosh_pv_view\.py:' \
  | grep -vE '^app/+services/cosh_cascade\.py:' \
  | grep -vE '^app/+services/brand_cache\.py:' \
  | grep -vE '^app/+services/npk_candidates\.py:' \
  | grep -vE '^app/+services/npk_trade_names\.py:' \
  | grep -vE '^app/+services/pest_diagnosis_images_view\.py:' \
  | grep -vE '^app/+services/cha_problem_groups\.py:' \
  | grep -vE '^app/+services/cha_hierarchy\.py:' \
  | grep -vE '^app/+services/bl07_brand_options\.py:262:' \
  | grep -vE '^app/+modules/diagnosis/' \
  | grep -vE '^app/+modules/orders/router\.py:(2660|5935|6085|6125|6136|6450):' \
  | grep -vE '^app/+modules/advisory/router\.py:(1609|5818):' \
  | grep -vE '^app/+modules/clients/router\.py:766:' \
  | grep -vE '^app/+modules/seed_mgmt/router\.py:235:' \
  | grep -vE '^app/+modules/qr/router\.py:(105|141|750):' \
  | grep -vE '^app/+modules/farmpundit/router\.py:712:' \
  || true)

if [ -n "$hits" ]; then
  echo "ERROR: hardcoded .get(\"en\") found outside the i18n_cosh allow-list."
  echo "       Use app.services.i18n_cosh.pick_translation(translations, lang, fallback)"
  echo "       instead — see commit ee0c9e2 et al."
  echo ""
  echo "Offending sites:"
  echo "$hits" | sed 's/^/  /'
  exit 1
fi

echo "ok — no new hardcoded en-only lookups."
