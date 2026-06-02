"""add meeting_link to organizers

Revision ID: 0002_add_meeting_link
Revises: 0001_initial
Create Date: 2026-06-01
"""
import sqlalchemy as sa
from alembic import op

revision = "0002_add_meeting_link"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("organizers", sa.Column("meeting_link", sa.String(512), nullable=True))


def downgrade() -> None:
    op.drop_column("organizers", "meeting_link")
