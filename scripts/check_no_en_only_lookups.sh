#!/usr/bin/env bash
# Guardrail: fail if anyone adds a hardcoded translations.get("en") or
# translations.get("English") outside the central i18n_cosh helper.
#
# Such sites silently return English regardless of the caller's locale
# and re-introduce the very bug we centralised i18n_cosh.pick_translation
# to fix (see commit ee0c9e2 et al, 2026-06-12).
#
# Allow-list (3 intentional exceptions):
#   app/services/i18n_cosh.py            - the helper itself
#   app/modules/sync/service.py          - ingest-time sanity check
#   app/services/cosh_crop_view.py       - SA-portal admin "Crops" browse
#   app/services/crop_snapshot.py        - CCA add-time English snapshot
#
# Run from repo root:    bash scripts/check_no_en_only_lookups.sh
# Also runs as a pytest: tests/test_no_en_only_lookups.py

set -euo pipefail

PATTERN='translations[[:space:]]*\.[[:space:]]*get\("(en|English)"|translations\["(en|English)"\]'

hits=$(grep -rEn "$PATTERN" app 2>/dev/null \
  | grep -v __pycache__ \
  | grep -vE '^app/+services/i18n_cosh\.py:' \
  | grep -vE '^app/+modules/sync/service\.py:' \
  | grep -vE '^app/+services/cosh_crop_view\.py:' \
  | grep -vE '^app/+services/crop_snapshot\.py:' \
  || true)

if [ -n "$hits" ]; then
  echo "ERROR: hardcoded translations.get(\"en\") found outside the i18n_cosh allow-list."
  echo "       Use app.services.i18n_cosh.pick_translation(translations, lang, fallback)"
  echo "       instead — see commit ee0c9e2 et al."
  echo ""
  echo "Offending sites:"
  echo "$hits" | sed 's/^/  /'
  exit 1
fi

echo "ok — no new hardcoded en-only lookups."
