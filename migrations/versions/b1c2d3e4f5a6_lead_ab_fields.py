"""Add A/B testing and engagement fields to leads_v2.

Adds: location, designation, ab_variant, ab_subject_variant,
      opted_out, email_bounced, ooo_until, reschedule_count

Revision ID: b1c2d3e4f5a6
Revises: a1b2c3d4e5f0
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "b1c2d3e4f5a6"
down_revision = "a1b2c3d4e5f0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("leads_v2", sa.Column("location", sa.String(255), nullable=True))
    op.add_column("leads_v2", sa.Column("designation", sa.String(255), nullable=True))
    op.add_column("leads_v2", sa.Column("ab_variant", sa.String(2), nullable=True))
    op.add_column("leads_v2", sa.Column("ab_subject_variant", sa.String(2), nullable=True))
    op.add_column("leads_v2", sa.Column("opted_out", sa.Boolean(), nullable=False, server_default="false"))
    op.add_column("leads_v2", sa.Column("email_bounced", sa.Boolean(), nullable=False, server_default="false"))
    op.add_column("leads_v2", sa.Column("ooo_until", sa.DateTime(timezone=True), nullable=True))
    op.add_column("leads_v2", sa.Column("reschedule_count", sa.Integer(), nullable=False, server_default="0"))


def downgrade() -> None:
    op.drop_column("leads_v2", "reschedule_count")
    op.drop_column("leads_v2", "ooo_until")
    op.drop_column("leads_v2", "email_bounced")
    op.drop_column("leads_v2", "opted_out")
    op.drop_column("leads_v2", "ab_subject_variant")
    op.drop_column("leads_v2", "ab_variant")
    op.drop_column("leads_v2", "designation")
    op.drop_column("leads_v2", "location")
