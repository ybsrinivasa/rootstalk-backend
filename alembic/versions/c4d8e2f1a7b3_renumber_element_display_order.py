"""Renumber Element.display_order to match rule-book order (Batch 39C-bugfix2).

Pre-fix every Element row was created with display_order=0 (the schema
default), so the read-only practice card on the SA portal sorted
elements arbitrarily by insertion order. Batch 39C-bugfix2 fixed the
write side to stamp display_order = request-position on every new
create / update. This data migration backfills existing rows the same
way — walk every Practice that has a known l2_type, look up the L2Spec
in `app.services.l2_element_rules`, and renumber the practice's
Elements so element_type ↔ position in the rule book's `fields` tuple.

Elements whose element_type doesn't appear in the rule book (legacy
removed L2s, custom field names) are left at their existing
display_order — they sit after the known fields naturally because
known fields are stamped 0..N-1.

Revision ID: c4d8e2f1a7b3
Revises: b36a92e1f08c
Create Date: 2026-05-15
"""
from alembic import op
from sqlalchemy import text


revision = "c4d8e2f1a7b3"
down_revision = "b36a92e1f08c"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from app.services.l2_element_rules import L2_ELEMENT_RULES

    bind = op.get_bind()

    practices = bind.execute(text(
        "SELECT id, l2_type FROM practices WHERE l2_type IS NOT NULL"
    )).fetchall()

    updated = 0
    for prac_id, l2 in practices:
        spec = L2_ELEMENT_RULES.get(l2)
        if spec is None:
            continue
        order_map = {fr.name: idx for idx, fr in enumerate(spec.fields)}
        if not order_map:
            continue
        elements = bind.execute(text(
            "SELECT id, element_type FROM elements WHERE practice_id = :pid"
        ), {"pid": prac_id}).fetchall()
        for el_id, el_type in elements:
            if el_type in order_map:
                bind.execute(text(
                    "UPDATE elements SET display_order = :do WHERE id = :id"
                ), {"do": order_map[el_type], "id": el_id})
                updated += 1

    # Cheap log so the deploy output shows the volume.
    print(f"[c4d8e2f1a7b3] Renumbered display_order on {updated} Element rows.")


def downgrade() -> None:
    # Irreversible data fill — reset to the old default of 0 so a
    # rollback puts the read side back into "arbitrary insertion order"
    # mode. No harm; new creates after rollback would still stamp the
    # right value if Batch 39C-bugfix2's code hadn't also been reverted.
    op.execute("UPDATE elements SET display_order = 0")
