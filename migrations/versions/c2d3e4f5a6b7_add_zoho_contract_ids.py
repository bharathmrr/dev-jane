"""Add nda_zoho_contract_id and agreement_zoho_contract_id to onboarding_records.

Revision ID: c2d3e4f5a6b7
Revises: b1c2d3e4f5a6
Create Date: 2026-06-08 00:00:00.000000
"""
from __future__ import annotations
import sqlalchemy as sa
from alembic import op

revision = 'c2d3e4f5a6b7'
down_revision = 'b1c2d3e4f5a6'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('onboarding_records', sa.Column('nda_zoho_contract_id', sa.String(255), nullable=True))
    op.add_column('onboarding_records', sa.Column('agreement_zoho_contract_id', sa.String(255), nullable=True))


def downgrade() -> None:
    op.drop_column('onboarding_records', 'agreement_zoho_contract_id')
    op.drop_column('onboarding_records', 'nda_zoho_contract_id')
