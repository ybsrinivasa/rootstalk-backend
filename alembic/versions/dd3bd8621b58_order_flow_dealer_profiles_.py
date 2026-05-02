"""order_flow_dealer_profiles_relationships_farm_area

Revision ID: dd3bd8621b58
Revises: a3f7e2b19c55
Create Date: 2026-05-02 09:48:15.681918

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSON

revision: str = 'dd3bd8621b58'
down_revision: Union[str, Sequence[str], None] = 'a3f7e2b19c55'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'dealer_profiles',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('user_id', sa.String(36), sa.ForeignKey('users.id'), unique=True, nullable=False),
        sa.Column('shop_name', sa.String(500), nullable=True),
        sa.Column('shop_address', sa.Text, nullable=True),
        sa.Column('sell_categories', JSON, nullable=True),
        sa.Column('pesticide_licence_url', sa.Text, nullable=True),
        sa.Column('fertiliser_licence_url', sa.Text, nullable=True),
        sa.Column('shop_registration_url', sa.Text, nullable=True),
        sa.Column('shop_photo_url', sa.Text, nullable=True),
        sa.Column('shop_gps_lat', sa.DECIMAL(10, 7), nullable=True),
        sa.Column('shop_gps_lng', sa.DECIMAL(10, 7), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now(), onupdate=sa.func.now()),
    )

    op.create_table(
        'dealer_relationships',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('dealer_user_id', sa.String(36), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('manufacturer_name', sa.String(500), nullable=False),
        sa.Column('manufacturer_client_id', sa.String(36), sa.ForeignKey('clients.id'), nullable=True),
        sa.Column('status', sa.String(20), nullable=False, server_default='ACTIVE'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )
    op.create_index('ix_dealer_relationships_dealer', 'dealer_relationships', ['dealer_user_id'])

    op.add_column('subscriptions', sa.Column('farm_area_acres', sa.DECIMAL(10, 2), nullable=True))
    op.add_column('subscriptions', sa.Column('area_unit', sa.String(20), nullable=True, server_default='acres'))


def downgrade() -> None:
    op.drop_column('subscriptions', 'area_unit')
    op.drop_column('subscriptions', 'farm_area_acres')
    op.drop_index('ix_dealer_relationships_dealer', table_name='dealer_relationships')
    op.drop_table('dealer_relationships')
    op.drop_table('dealer_profiles')
