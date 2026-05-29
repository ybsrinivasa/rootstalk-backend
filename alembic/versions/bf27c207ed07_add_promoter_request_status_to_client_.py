"""add promoter_request_status to client_promoters

Revision ID: bf27c207ed07
Revises: d4e6a1c89b22
Create Date: 2026-05-29 09:11:14.811096

R9 (2026-05-29): the Promoter sub-role is now a two-sided handshake.

Pre-change, the Client's Field Manager could unilaterally flip
`client_promoters.is_promoter` via `toggle_promoter_flag`. The
Facilitator had no say.

After this migration, the same row carries the lifecycle of the
invitation:

  NONE     — never invited, or fully revoked.
  PENDING  — Client has invited the Facilitator; waiting for them.
  ACCEPTED — Facilitator accepted; `is_promoter` is True.
  DECLINED — Facilitator declined; `is_promoter` is False.

The endpoints that drive these transitions land in the same batch:

  Client side (FM):
    PUT  .../request-promoter   — NONE → PENDING (sends invitation)
    PUT  .../revoke-promoter    — any → NONE     (FM teardown)

  Facilitator side:
    GET  /facilitator/promoter-invitations
    PUT  /facilitator/promoter-invitations/{id}/accept   — PENDING → ACCEPTED
    PUT  /facilitator/promoter-invitations/{id}/decline  — PENDING → DECLINED
    PUT  /facilitator/promoter-status/{id}/step-down     — ACCEPTED → NONE

Backfill: existing rows with `is_promoter=True` are treated as
implicitly accepted (status='ACCEPTED', responded_at=registered_at);
all other rows get status='NONE'.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'bf27c207ed07'
down_revision: Union[str, Sequence[str], None] = 'd4e6a1c89b22'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "client_promoters",
        sa.Column(
            "promoter_request_status", sa.String(length=20),
            nullable=False, server_default="NONE",
        ),
    )
    op.add_column(
        "client_promoters",
        sa.Column(
            "promoter_request_sent_at",
            sa.DateTime(timezone=True), nullable=True,
        ),
    )
    op.add_column(
        "client_promoters",
        sa.Column(
            "promoter_request_responded_at",
            sa.DateTime(timezone=True), nullable=True,
        ),
    )

    # Backfill: existing is_promoter=True rows are treated as
    # implicitly accepted. We don't have an accurate "responded_at"
    # timestamp pre-migration, so we use `registered_at` as a
    # best-effort stand-in — better than NULL for downstream UI that
    # might want to render "accepted on …".
    op.execute("""
        UPDATE client_promoters
           SET promoter_request_status = 'ACCEPTED',
               promoter_request_responded_at = registered_at
         WHERE is_promoter = TRUE
    """)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("client_promoters", "promoter_request_responded_at")
    op.drop_column("client_promoters", "promoter_request_sent_at")
    op.drop_column("client_promoters", "promoter_request_status")
