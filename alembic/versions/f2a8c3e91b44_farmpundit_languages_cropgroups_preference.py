"""farmpundit_languages_cropgroups_preference

Revision ID: f2a8c3e91b44
Revises: e7f4b2c91d88
Create Date: 2026-05-01 20:00:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = 'f2a8c3e91b44'
down_revision: Union[str, Sequence[str], None] = 'e7f4b2c91d88'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Languages the FarmPundit is conversant in
    op.create_table('farm_pundit_languages',
        sa.Column('id', sa.String(36), nullable=False),
        sa.Column('pundit_id', sa.String(36), sa.ForeignKey('farm_pundit_profiles.id'), nullable=False),
        sa.Column('language_code', sa.String(10), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('pundit_id', 'language_code'),
    )
    # Crop groups the FarmPundit is interested in
    op.create_table('farm_pundit_crop_groups',
        sa.Column('id', sa.String(36), nullable=False),
        sa.Column('pundit_id', sa.String(36), sa.ForeignKey('farm_pundit_profiles.id'), nullable=False),
        sa.Column('crop_group_cosh_id', sa.String(100), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    # Farmer's preferred FarmPundit for a specific subscription
    op.create_table('farm_pundit_preferences',
        sa.Column('id', sa.String(36), nullable=False),
        sa.Column('subscription_id', sa.String(36), sa.ForeignKey('subscriptions.id'), nullable=False),
        sa.Column('pundit_id', sa.String(36), sa.ForeignKey('farm_pundit_profiles.id'), nullable=False),
        sa.Column('set_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('subscription_id'),
    )
    # Media attached to expert responses
    op.create_table('query_response_media',
        sa.Column('id', sa.String(36), nullable=False),
        sa.Column('response_id', sa.String(36), sa.ForeignKey('query_responses.id'), nullable=False),
        sa.Column('media_type', sa.String(20), nullable=False),  # IMAGE | VIDEO | AUDIO | HYPERLINK
        sa.Column('url', sa.Text(), nullable=False),
        sa.Column('caption', sa.String(500), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    # Mark a ClientFarmPundit as a Promoter-Pundit (facilitator also registered as FarmPundit)
    op.add_column('client_farm_pundits',
        sa.Column('is_promoter_pundit', sa.Boolean(), server_default='false', nullable=False))


def downgrade() -> None:
    op.drop_column('client_farm_pundits', 'is_promoter_pundit')
    op.drop_table('query_response_media')
    op.drop_table('farm_pundit_preferences')
    op.drop_table('farm_pundit_crop_groups')
    op.drop_table('farm_pundit_languages')
