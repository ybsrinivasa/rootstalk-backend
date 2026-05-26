"""pundit profile cosh reshape

Drop free-text and pre-Cosh columns from farm_pundit_profiles; add
Cosh-id columns + employment flag + non-employed kind. Create two
junction tables (farming_methods, cultivation_types). Wipe all
existing FP profile data — testing-only, no real Pundits onboarded
yet (user direction 2026-05-26).

Revision ID: a1b9c2d4e5f6
Revises: d8f1a4c92e51
Create Date: 2026-05-26
"""
from alembic import op
import sqlalchemy as sa


revision = "a1b9c2d4e5f6"
down_revision = "d8f1a4c92e51"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Wipe all existing FP data — junctions first, then parent rows,
    # then anything that FKs the user_role (FARM_PUNDIT) so the
    # demoted users can re-register cleanly. The associated User rows
    # are untouched; only the FP-specific data is removed.
    op.execute("DELETE FROM farm_pundit_preferences")
    op.execute("DELETE FROM farm_pundit_expertise")
    op.execute("DELETE FROM farm_pundit_support_areas")
    op.execute("DELETE FROM farm_pundit_languages")
    op.execute("DELETE FROM farm_pundit_crop_groups")
    op.execute("DELETE FROM pundit_invitations")
    op.execute("DELETE FROM client_farm_pundits")
    op.execute("DELETE FROM farm_pundit_profiles")
    op.execute("DELETE FROM user_roles WHERE role_type = 'FARM_PUNDIT'")

    # Drop old single-value columns; add new ones.
    with op.batch_alter_table("farm_pundit_profiles") as batch:
        batch.drop_column("education")
        batch.drop_column("experience_band")
        batch.drop_column("support_method")
        batch.drop_column("cultivation_type")
        batch.drop_column("organisation_name")
        batch.add_column(sa.Column("education_cosh_id", sa.String(100), nullable=True))
        batch.add_column(sa.Column("experience_cosh_id", sa.String(100), nullable=True))
        batch.add_column(sa.Column(
            "is_employed_by_organization", sa.Boolean(),
            nullable=False, server_default=sa.text("false"),
        ))
        batch.add_column(sa.Column("non_employed_kind", sa.String(30), nullable=True))

    # New junction tables.
    op.create_table(
        "farm_pundit_farming_methods",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("pundit_id", sa.String(36),
                  sa.ForeignKey("farm_pundit_profiles.id"), nullable=False),
        sa.Column("farming_method_cosh_id", sa.String(100), nullable=False),
        sa.UniqueConstraint("pundit_id", "farming_method_cosh_id",
                             name="uq_fp_farming_method"),
    )
    op.create_table(
        "farm_pundit_cultivation_types",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("pundit_id", sa.String(36),
                  sa.ForeignKey("farm_pundit_profiles.id"), nullable=False),
        sa.Column("cultivation_type_cosh_id", sa.String(100), nullable=False),
        sa.UniqueConstraint("pundit_id", "cultivation_type_cosh_id",
                             name="uq_fp_cultivation_type"),
    )


def downgrade() -> None:
    op.drop_table("farm_pundit_cultivation_types")
    op.drop_table("farm_pundit_farming_methods")
    with op.batch_alter_table("farm_pundit_profiles") as batch:
        batch.drop_column("non_employed_kind")
        batch.drop_column("is_employed_by_organization")
        batch.drop_column("experience_cosh_id")
        batch.drop_column("education_cosh_id")
        batch.add_column(sa.Column("organisation_name", sa.String(500), nullable=True))
        batch.add_column(sa.Column("cultivation_type", sa.String(100), nullable=True))
        batch.add_column(sa.Column("support_method", sa.String(20), nullable=True))
        batch.add_column(sa.Column("experience_band", sa.String(30), nullable=True))
        batch.add_column(sa.Column("education", sa.String(50), nullable=True))
