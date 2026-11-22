"""Messaging Module — campaigns + attachments + questions + recipients
+ responses + response_answers + send_audits.

Revision ID: a9x6s2u85z1q
Revises: z8w5r1t74y0p
Create Date: 2026-11-22

Seven tables in one migration — they ship together and share indexes.
Idempotent via information_schema check on the parent `campaigns`
table; if campaigns exists we assume the whole batch is in place.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a9x6s2u85z1q"
down_revision: Union[str, Sequence[str], None] = "z8w5r1t74y0p"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    exists = bind.execute(sa.text("""
        SELECT 1 FROM information_schema.tables
        WHERE table_name = 'campaigns'
    """)).first()
    if exists:
        return

    # ── campaigns ───────────────────────────────────────────────────
    op.create_table(
        "campaigns",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "client_id", sa.String(36),
            sa.ForeignKey("clients.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("campaign_type", sa.String(30), nullable=False),
        sa.Column("recipient_role", sa.String(20), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("language_code", sa.String(10), nullable=True),
        sa.Column("audience_filters", sa.JSON(), nullable=False),
        sa.Column("rating_scale_type", sa.String(20), nullable=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("recipient_count", sa.Integer(), nullable=True),
        sa.Column("pushed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("opened_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("responded_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_by_user_id", sa.String(36),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
    )
    op.create_index("ix_campaigns_client_status", "campaigns", ["client_id", "status"])
    op.create_index("ix_campaigns_scheduled_at", "campaigns", ["scheduled_at"])
    op.create_index("ix_campaigns_expires_at", "campaigns", ["expires_at"])

    # ── campaign_attachments ────────────────────────────────────────
    op.create_table(
        "campaign_attachments",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "campaign_id", sa.String(36),
            sa.ForeignKey("campaigns.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("storage_path", sa.String(500), nullable=False),
        sa.Column("original_filename", sa.String(300), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("mime_type", sa.String(100), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
    )
    op.create_index(
        "ix_campaign_attachments_campaign",
        "campaign_attachments", ["campaign_id", "sort_order"],
    )

    # ── campaign_questions ──────────────────────────────────────────
    op.create_table(
        "campaign_questions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "campaign_id", sa.String(36),
            sa.ForeignKey("campaigns.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("question_type", sa.String(30), nullable=False),
        sa.Column("label", sa.String(500), nullable=False),
        sa.Column("help_text", sa.String(500), nullable=True),
        sa.Column("is_mandatory", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("choice_options", sa.JSON(), nullable=True),
        sa.Column("number_unit", sa.String(50), nullable=True),
        sa.Column("rating_scale_type", sa.String(20), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
    )
    op.create_index(
        "ix_campaign_questions_campaign_sort",
        "campaign_questions", ["campaign_id", "sort_order"],
    )

    # ── campaign_recipients ─────────────────────────────────────────
    op.create_table(
        "campaign_recipients",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "campaign_id", sa.String(36),
            sa.ForeignKey("campaigns.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id", sa.String(36),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("recipient_name", sa.String(200), nullable=False),
        sa.Column("recipient_phone", sa.String(20), nullable=False),
        sa.Column("recipient_language_code", sa.String(10), nullable=True),
        sa.Column("recipient_state_cosh_id", sa.String(100), nullable=True),
        sa.Column("recipient_district_cosh_id", sa.String(100), nullable=True),
        sa.Column("pushed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("responded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_test", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.UniqueConstraint("campaign_id", "user_id", name="uq_campaign_recipient"),
    )
    op.create_index(
        "ix_campaign_recipients_campaign",
        "campaign_recipients", ["campaign_id"],
    )
    op.create_index(
        "ix_campaign_recipients_user_pending",
        "campaign_recipients", ["user_id", "campaign_id"],
    )

    # ── campaign_responses ──────────────────────────────────────────
    op.create_table(
        "campaign_responses",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "campaign_id", sa.String(36),
            sa.ForeignKey("campaigns.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "recipient_id", sa.String(36),
            sa.ForeignKey("campaign_recipients.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id", sa.String(36),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("interest_answer", sa.String(10), nullable=True),
        sa.Column("rating", sa.Integer(), nullable=True),
        sa.Column(
            "submitted_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.UniqueConstraint("campaign_id", "user_id", name="uq_campaign_response"),
    )
    op.create_index(
        "ix_campaign_responses_campaign",
        "campaign_responses", ["campaign_id"],
    )

    # ── campaign_response_answers ───────────────────────────────────
    op.create_table(
        "campaign_response_answers",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "response_id", sa.String(36),
            sa.ForeignKey("campaign_responses.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "question_id", sa.String(36),
            sa.ForeignKey("campaign_questions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("value_text", sa.Text(), nullable=True),
        sa.Column("value_number", sa.Float(), nullable=True),
        sa.Column("value_choice", sa.Text(), nullable=True),
        sa.Column("value_boolean", sa.Boolean(), nullable=True),
        sa.Column("value_date", sa.Date(), nullable=True),
        sa.Column("value_media_path", sa.String(500), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.UniqueConstraint("response_id", "question_id", name="uq_response_question"),
    )
    op.create_index(
        "ix_campaign_response_answers_response",
        "campaign_response_answers", ["response_id"],
    )
    op.create_index(
        "ix_campaign_response_answers_question",
        "campaign_response_answers", ["question_id"],
    )

    # ── campaign_send_audits ────────────────────────────────────────
    op.create_table(
        "campaign_send_audits",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "campaign_id", sa.String(36),
            sa.ForeignKey("campaigns.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "client_id", sa.String(36),
            sa.ForeignKey("clients.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "sender_user_id", sa.String(36),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("campaign_name", sa.String(200), nullable=False),
        sa.Column("campaign_type", sa.String(30), nullable=False),
        sa.Column("recipient_role", sa.String(20), nullable=False),
        sa.Column("recipient_count", sa.Integer(), nullable=False),
        sa.Column("is_test", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column(
            "sent_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.Column("ip_address", sa.String(64), nullable=True),
        sa.Column("user_agent", sa.String(255), nullable=True),
    )
    op.create_index(
        "ix_campaign_send_audits_sent_at",
        "campaign_send_audits", ["sent_at"],
    )
    op.create_index(
        "ix_campaign_send_audits_client",
        "campaign_send_audits", ["client_id"],
    )
    op.create_index(
        "ix_campaign_send_audits_sender",
        "campaign_send_audits", ["sender_user_id"],
    )


def downgrade() -> None:
    op.drop_table("campaign_send_audits")
    op.drop_table("campaign_response_answers")
    op.drop_table("campaign_responses")
    op.drop_table("campaign_recipients")
    op.drop_table("campaign_questions")
    op.drop_table("campaign_attachments")
    op.drop_table("campaigns")
