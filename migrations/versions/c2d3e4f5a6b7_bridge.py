"""Bridge revision — marks the state applied in the previous session.

Revision ID: c2d3e4f5a6b7
Revises: b1c2d3e4f5a6
"""
from __future__ import annotations

from alembic import op

revision = "c2d3e4f5a6b7"
down_revision = "b1c2d3e4f5a6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
