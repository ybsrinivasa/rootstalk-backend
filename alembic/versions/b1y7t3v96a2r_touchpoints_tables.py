"""Subscription Touchpoints — 7 tables.

Revision ID: b1y7t3v96a2r
Revises: a9x6s2u85z1q
Create Date: 2026-11-24

Creates:
  touchpoints, touchpoint_assignments, touchpoint_questions,
  touchpoint_attachments, touchpoint_recipients, touchpoint_responses,
  touchpoint_response_answers

Companion to the Messaging Module. Parallel tables (not shared with
`campaigns`) because audience semantics + lifecycle differ enough
that overloading the campaign schema would muddy both. See
`app/modules/touchpoints/models.py` module docstring for rationale.

Idempotent via information_schema check on the parent `touchpoints`
table — if present we assume the whole batch is in place.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b1y7t3v96a2r"
down_revision: Union[str, Sequence[str], None] = "a9x6s2u85z1q"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    exists = bind.execute(sa.text("""
        SELECT 1 FROM information_schema.tables
        WHERE table_name = 'touchpoints'
    """)).first()
    if exists:
        return

    # ── touchpoints ─────────────────────────────────────────────────
    op.create_table(
        "touchpoints",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "client_id", sa.String(36),
            sa.ForeignKey("clients.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("content_type", sa.String(30), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("title", sa.String(200), nullable=False, server_default=""),
        sa.Column("body", sa.Text(), nullable=False, server_default=""),
        sa.Column("language_code", sa.String(10), nullable=True),
        sa.Column("rating_scale_type", sa.String(20), nullable=True),
        sa.Column("per_firing_expiry_days", sa.Integer(), nullable=False, server_default="7"),
        sa.Column("status", sa.String(20), nullable=False, server_default="DRAFT"),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("paused_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("content_locked_at", sa.DateTime(timezone=True), nullable=True),
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
    op.create_index(
        "ix_touchpoints_client_status",
        "touchpoints", ["client_id", "status"],
    )
    op.create_index(
        "ix_touchpoints_updated_at",
        "touchpoints", ["updated_at"],
    )

    # ── touchpoint_assignments ──────────────────────────────────────
    op.create_table(
        "touchpoint_assignments",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "touchpoint_id", sa.String(36),
            sa.ForeignKey("touchpoints.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "package_id", sa.String(36),
            sa.ForeignKey("packages.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "timeline_id", sa.String(36),
            sa.ForeignKey("timelines.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("fire_at", sa.String(20), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="ACTIVE"),
        sa.Column(
            "activated_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.Column("paused_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_by_user_id", sa.String(36),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.UniqueConstraint(
            "touchpoint_id", "package_id", "timeline_id", "fire_at",
            name="uq_touchpoint_assignment",
        ),
    )
    op.create_index(
        "ix_touchpoint_assignments_lookup",
        "touchpoint_assignments",
        ["package_id", "timeline_id", "fire_at", "status"],
    )
    op.create_index(
        "ix_touchpoint_assignments_touchpoint",
        "touchpoint_assignments", ["touchpoint_id"],
    )

    # ── touchpoint_questions ────────────────────────────────────────
    op.create_table(
        "touchpoint_questions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "touchpoint_id", sa.String(36),
            sa.ForeignKey("touchpoints.id", ondelete="CASCADE"),
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
        "ix_touchpoint_questions_touchpoint_sort",
        "touchpoint_questions", ["touchpoint_id", "sort_order"],
    )

    # ── touchpoint_attachments ──────────────────────────────────────
    op.create_table(
        "touchpoint_attachments",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "touchpoint_id", sa.String(36),
            sa.ForeignKey("touchpoints.id", ondelete="CASCADE"),
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
        "ix_touchpoint_attachments_touchpoint",
        "touchpoint_attachments", ["touchpoint_id", "sort_order"],
    )

    # ── touchpoint_recipients ───────────────────────────────────────
    op.create_table(
        "touchpoint_recipients",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "touchpoint_id", sa.String(36),
            sa.ForeignKey("touchpoints.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "assignment_id", sa.String(36),
            sa.ForeignKey("touchpoint_assignments.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "subscription_id", sa.String(36),
            sa.ForeignKey("subscriptions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "user_id", sa.String(36),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("crop_cosh_id", sa.String(100), nullable=True),
        sa.Column("timeline_id", sa.String(36), nullable=True),
        sa.Column("timeline_name", sa.String(200), nullable=True),
        sa.Column("fire_at", sa.String(20), nullable=False),
        sa.Column("recipient_name", sa.String(200), nullable=False),
        sa.Column("recipient_phone", sa.String(20), nullable=False),
        sa.Column("recipient_language_code", sa.String(10), nullable=True),
        sa.Column("recipient_state_cosh_id", sa.String(100), nullable=True),
        sa.Column("recipient_district_cosh_id", sa.String(100), nullable=True),
        sa.Column(
            "fired_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("pushed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("responded_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "assignment_id", "subscription_id",
            name="uq_touchpoint_recipient",
        ),
    )
    op.create_index(
        "ix_touchpoint_recipients_touchpoint",
        "touchpoint_recipients", ["touchpoint_id"],
    )
    op.create_index(
        "ix_touchpoint_recipients_user",
        "touchpoint_recipients", ["user_id", "touchpoint_id"],
    )
    op.create_index(
        "ix_touchpoint_recipients_fired_at",
        "touchpoint_recipients", ["fired_at"],
    )
    op.create_index(
        "ix_touchpoint_recipients_expires_at",
        "touchpoint_recipients", ["expires_at"],
    )

    # ── touchpoint_responses ────────────────────────────────────────
    op.create_table(
        "touchpoint_responses",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "touchpoint_id", sa.String(36),
            sa.ForeignKey("touchpoints.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "recipient_id", sa.String(36),
            sa.ForeignKey("touchpoint_recipients.id", ondelete="CASCADE"),
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
        sa.UniqueConstraint("recipient_id", name="uq_touchpoint_response"),
    )
    op.create_index(
        "ix_touchpoint_responses_touchpoint",
        "touchpoint_responses", ["touchpoint_id"],
    )

    # ── touchpoint_response_answers ─────────────────────────────────
    op.create_table(
        "touchpoint_response_answers",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "response_id", sa.String(36),
            sa.ForeignKey("touchpoint_responses.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "question_id", sa.String(36),
            sa.ForeignKey("touchpoint_questions.id", ondelete="CASCADE"),
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
        sa.UniqueConstraint(
            "response_id", "question_id",
            name="uq_touchpoint_response_question",
        ),
    )
    op.create_index(
        "ix_touchpoint_response_answers_response",
        "touchpoint_response_answers", ["response_id"],
    )
    op.create_index(
        "ix_touchpoint_response_answers_question",
        "touchpoint_response_answers", ["question_id"],
    )


def downgrade() -> None:
    op.drop_table("touchpoint_response_answers")
    op.drop_table("touchpoint_responses")
    op.drop_table("touchpoint_recipients")
    op.drop_table("touchpoint_attachments")
    op.drop_table("touchpoint_questions")
    op.drop_table("touchpoint_assignments")
    op.drop_table("touchpoints")
