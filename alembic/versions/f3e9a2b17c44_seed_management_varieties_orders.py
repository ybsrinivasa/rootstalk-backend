"""seed_management_varieties_orders

Revision ID: f3e9a2b17c44
Revises: dd3bd8621b58
Create Date: 2026-05-02 10:30:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSON

revision: str = 'f3e9a2b17c44'
down_revision: Union[str, Sequence[str], None] = 'dd3bd8621b58'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'seed_varieties',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('client_id', sa.String(36), sa.ForeignKey('clients.id'), nullable=False),
        sa.Column('crop_cosh_id', sa.String(100), nullable=False),
        sa.Column('name', sa.String(500), nullable=False),
        sa.Column('variety_type', sa.String(20), nullable=False, server_default='SEED'),
        sa.Column('description_points', JSON, nullable=True),
        sa.Column('dus_characters', JSON, nullable=True),
        sa.Column('photos', JSON, nullable=True),
        sa.Column('status', sa.String(20), nullable=False, server_default='ACTIVE'),
        sa.Column('created_by_user_id', sa.String(36), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint('client_id', 'crop_cosh_id', 'name', name='uq_variety_client_crop_name'),
    )
    op.create_index('ix_seed_varieties_client', 'seed_varieties', ['client_id'])
    op.create_index('ix_seed_varieties_crop', 'seed_varieties', ['crop_cosh_id'])

    op.create_table(
        'variety_pop_assignments',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('variety_id', sa.String(36), sa.ForeignKey('seed_varieties.id'), nullable=False),
        sa.Column('package_id', sa.String(36), sa.ForeignKey('packages.id'), nullable=False),
        sa.Column('status', sa.String(20), nullable=False, server_default='ACTIVE'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint('variety_id', 'package_id', name='uq_variety_pop'),
    )

    op.create_table(
        'seed_orders_full',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('subscription_id', sa.String(36), sa.ForeignKey('subscriptions.id'), nullable=False),
        sa.Column('farmer_user_id', sa.String(36), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('variety_id', sa.String(36), sa.ForeignKey('seed_varieties.id'), nullable=False),
        sa.Column('client_id', sa.String(36), sa.ForeignKey('clients.id'), nullable=False),
        sa.Column('dealer_user_id', sa.String(36), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('facilitator_user_id', sa.String(36), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('unit', sa.String(20), nullable=True),
        sa.Column('quantity', sa.DECIMAL(10, 3), nullable=True),
        sa.Column('total_price', sa.DECIMAL(10, 2), nullable=True),
        sa.Column('status', sa.String(30), nullable=False, server_default='SENT'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index('ix_seed_orders_full_farmer', 'seed_orders_full', ['farmer_user_id'])
    op.create_index('ix_seed_orders_full_dealer', 'seed_orders_full', ['dealer_user_id'])


def downgrade() -> None:
    op.drop_index('ix_seed_orders_full_dealer', 'seed_orders_full')
    op.drop_index('ix_seed_orders_full_farmer', 'seed_orders_full')
    op.drop_table('seed_orders_full')
    op.drop_table('variety_pop_assignments')
    op.drop_index('ix_seed_varieties_crop', 'seed_varieties')
    op.drop_index('ix_seed_varieties_client', 'seed_varieties')
    op.drop_table('seed_varieties')
