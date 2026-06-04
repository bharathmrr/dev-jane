"""V2 slots upgrade: datetime slots + lead meeting fields

Revision ID: e5f6a7b8c9d0
Revises: a2ae93993c6b
Create Date: 2026-06-03 13:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = 'e5f6a7b8c9d0'
down_revision = 'a2ae93993c6b'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- available_dates_v2: replace date_value with slot_datetime + is_available ---
    op.execute(sa.text('DROP INDEX IF EXISTS ix_available_dates_v2_date_value'))
    op.execute(sa.text("""
        DO $$ BEGIN
            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name='available_dates_v2' AND column_name='date_value'
            ) THEN ALTER TABLE available_dates_v2 DROP COLUMN date_value; END IF;
        END $$;
    """))
    op.execute(sa.text("""
        DO $$ BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name='available_dates_v2' AND column_name='slot_datetime'
            ) THEN ALTER TABLE available_dates_v2 ADD COLUMN slot_datetime TIMESTAMPTZ NOT NULL DEFAULT NOW(); END IF;
        END $$;
    """))
    op.execute(sa.text('ALTER TABLE available_dates_v2 ALTER COLUMN slot_datetime DROP DEFAULT'))
    op.execute(sa.text("""
        DO $$ BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name='available_dates_v2' AND column_name='is_available'
            ) THEN ALTER TABLE available_dates_v2 ADD COLUMN is_available BOOLEAN NOT NULL DEFAULT TRUE; END IF;
        END $$;
    """))
    op.execute(sa.text(
        'CREATE UNIQUE INDEX IF NOT EXISTS ix_available_dates_v2_slot_datetime '
        'ON available_dates_v2(slot_datetime)'
    ))

    # --- leads_v2: add offered_slots_json + zoho_meeting_link ---
    op.execute(sa.text("""
        DO $$ BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name='leads_v2' AND column_name='offered_slots_json'
            ) THEN ALTER TABLE leads_v2 ADD COLUMN offered_slots_json TEXT; END IF;
        END $$;
    """))
    op.execute(sa.text("""
        DO $$ BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name='leads_v2' AND column_name='zoho_meeting_link'
            ) THEN ALTER TABLE leads_v2 ADD COLUMN zoho_meeting_link VARCHAR(512); END IF;
        END $$;
    """))


def downgrade() -> None:
    op.drop_column('leads_v2', 'zoho_meeting_link')
    op.drop_column('leads_v2', 'offered_slots_json')

    op.drop_index('ix_available_dates_v2_slot_datetime', table_name='available_dates_v2')
    op.drop_column('available_dates_v2', 'is_available')
    op.drop_column('available_dates_v2', 'slot_datetime')
    op.add_column('available_dates_v2',
        sa.Column('date_value', sa.Date(), nullable=False, server_default='2000-01-01'))
    op.alter_column('available_dates_v2', 'date_value', server_default=None)
    op.create_index('ix_available_dates_v2_date_value', 'available_dates_v2',
                    ['date_value'], unique=True)
