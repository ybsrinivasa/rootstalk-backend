"""Add Client.payment_model — Company Pays / Farmer Pays (spec §11.1)

Revision ID: d8b1e6c4a572
Revises: a3f51e2dc874
Create Date: 2026-05-08 12:30:00.000000

Per Agriculture Team Document v5 §11.1, every client has a
client-level subscription configuration with two values:

  COMPANY_PAYS — farmers cannot self-subscribe; only Promoters
                 assign packages on behalf of the company.
  FARMER_PAYS  — farmers can self-subscribe directly, AND the
                 company may additionally assign via Promoters.

Both configurations require a Pool. The label "Farmer Pays" refers to
availability of farmer self-subscription, not an exclusive model.

Migration adds the column NOT NULL with a server-side default of
FARMER_PAYS for the backfill — chosen because it's the more permissive
of the two (any existing dev/test row keeps working without surprise
restrictions). New rows from this point forward must specify the
value explicitly via the API; the schema-level default is a backfill-
only safety net.
"""
from alembic import op
import sqlalchemy as sa


revision = "d8b1e6c4a572"
down_revision = "a3f51e2dc874"
branch_labels = None
depends_on = None


_PAYMENT_MODEL_VALUES = ("COMPANY_PAYS", "FARMER_PAYS")


def upgrade():
    payment_model_enum = sa.Enum(
        *_PAYMENT_MODEL_VALUES, name="paymentmodel",
    )
    payment_model_enum.create(op.get_bind(), checkfirst=True)

    op.add_column(
        "clients",
        sa.Column(
            "payment_model",
            payment_model_enum,
            nullable=False,
            server_default="FARMER_PAYS",
        ),
    )

    # Drop the server_default — only here for the backfill of existing rows.
    # New rows must specify the value via the API; let the column reject
    # writes that don't.
    op.alter_column("clients", "payment_model", server_default=None)


def downgrade():
    op.drop_column("clients", "payment_model")
    sa.Enum(name="paymentmodel").drop(op.get_bind(), checkfirst=True)
