"""traders and desk_state: trader accounts and the shared replayed market clock (F10e).

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-18
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "traders",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("username", sa.Text, nullable=False),
        sa.Column("display_name", sa.Text, nullable=False),
        sa.Column("password_hash", sa.Text, nullable=False),
        sa.Column("profile", sa.Text, nullable=False),
        sa.Column("starting_cash", sa.Float, nullable=False),
        sa.Column("limits", JSONB, nullable=True),
        # The trader's order history (place and cancel, with market times): the market is
        # rebuilt from it after a restart, deterministically.
        sa.Column("events", JSONB, nullable=False, server_default="[]"),
        sa.Column("transcript", JSONB, nullable=False, server_default="[]"),
        sa.CheckConstraint("username = lower(username)", name="traders_username_lower"),
        sa.CheckConstraint(
            "starting_cash >= 1000 and starting_cash <= 1000000", name="traders_starting_cash"
        ),
    )
    op.create_index("traders_username", "traders", ["username"], unique=True)
    op.create_table(
        "desk_state",
        sa.Column("key", sa.Text, primary_key=True),
        sa.Column("value", sa.Text, nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )


def downgrade() -> None:
    op.drop_table("desk_state")
    op.drop_index("traders_username", table_name="traders")
    op.drop_table("traders")
