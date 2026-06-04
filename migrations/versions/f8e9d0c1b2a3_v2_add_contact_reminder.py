"""Add contact_name, follow_up_count, reminder_sent_at to leads_v2

Revision ID: f8e9d0c1b2a3
Revises: e5f6a7b8c9d0
Create Date: 2026-06-04 12:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = 'f8e9d0c1b2a3'
down_revision = 'e5f6a7b8c9d0'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(sa.text("""
        DO $$ BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name='leads_v2' AND column_name='contact_name'
            ) THEN ALTER TABLE leads_v2 ADD COLUMN contact_name VARCHAR(255); END IF;
        END $$;
    """))
    op.execute(sa.text("""
        DO $$ BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name='leads_v2' AND column_name='follow_up_count'
            ) THEN ALTER TABLE leads_v2 ADD COLUMN follow_up_count INTEGER NOT NULL DEFAULT 0; END IF;
        END $$;
    """))
    op.execute(sa.text("""
        DO $$ BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name='leads_v2' AND column_name='reminder_sent_at'
            ) THEN ALTER TABLE leads_v2 ADD COLUMN reminder_sent_at TIMESTAMPTZ; END IF;
        END $$;
    """))
    op.execute(sa.text("""
        DO $$ BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name='leads_v2' AND column_name='summary'
            ) THEN ALTER TABLE leads_v2 ADD COLUMN summary TEXT; END IF;
        END $$;
    """))


def downgrade() -> None:
    op.drop_column('leads_v2', 'summary')
    op.drop_column('leads_v2', 'reminder_sent_at')
    op.drop_column('leads_v2', 'follow_up_count')
    op.drop_column('leads_v2', 'contact_name')
