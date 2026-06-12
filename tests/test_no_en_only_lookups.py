"""Guardrail test: fail if anyone introduces a new hardcoded
`translations.get("en")` outside the central i18n_cosh helper.

Wraps `scripts/check_no_en_only_lookups.sh` so this fires automatically
on `pytest` runs without requiring a separate CI hook. See the script
for the allow-list and rationale (commit ee0c9e2 et al, 2026-06-12).
"""
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "check_no_en_only_lookups.sh"


def test_no_new_en_only_lookups():
    result = subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )
    # Show the script's output on failure so the diff is obvious.
    assert result.returncode == 0, (
        f"Guardrail tripped. Output:\n"
        f"--- stdout ---\n{result.stdout}\n"
        f"--- stderr ---\n{result.stderr}"
    )
