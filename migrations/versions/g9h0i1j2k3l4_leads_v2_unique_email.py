"""Add unique constraint on leads_v2.email to prevent duplicate lead insertion

Revision ID: g9h0i1j2k3l4
Revises: f8e9d0c1b2a3
Create Date: 2026-06-05 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = 'g9h0i1j2k3l4'
down_revision = 'f8e9d0c1b2a3'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Remove duplicate rows first — keep only the most recently created one per email
    op.execute("""
        DELETE FROM leads_v2 a
        USING leads_v2 b
        WHERE a.email = b.email
          AND a.created_at < b.created_at
    """)
    # Now add the unique constraint
    op.create_unique_constraint('uq_leads_v2_email', 'leads_v2', ['email'])


def downgrade() -> None:
    op.drop_constraint('uq_leads_v2_email', 'leads_v2', type_='unique')
