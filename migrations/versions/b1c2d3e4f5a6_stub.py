"""Stub migration to reconcile missing revision.

Revision ID: b1c2d3e4f5a6
Revises: a1b2c3d4e5f0
Create Date: 2026-06-01 00:00:00.000000
"""
from __future__ import annotations
from alembic import op

revision = 'b1c2d3e4f5a6'
down_revision = 'a1b2c3d4e5f0'
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
