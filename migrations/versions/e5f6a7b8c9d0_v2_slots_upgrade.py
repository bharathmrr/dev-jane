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
    op.drop_index('ix_available_dates_v2_date_value', table_name='available_dates_v2')
    op.drop_column('available_dates_v2', 'date_value')
    op.add_column('available_dates_v2',
        sa.Column('slot_datetime', sa.DateTime(timezone=True),
                  server_default=sa.text('now()'), nullable=False))
    op.add_column('available_dates_v2',
        sa.Column('is_available', sa.Boolean(), server_default='true', nullable=False))
    op.alter_column('available_dates_v2', 'slot_datetime', server_default=None)
    op.create_index('ix_available_dates_v2_slot_datetime', 'available_dates_v2',
                    ['slot_datetime'], unique=True)

    # --- leads_v2: add offered_slots_json + zoho_meeting_link ---
    op.add_column('leads_v2', sa.Column('offered_slots_json', sa.Text(), nullable=True))
    op.add_column('leads_v2', sa.Column('zoho_meeting_link', sa.String(length=512), nullable=True))


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
