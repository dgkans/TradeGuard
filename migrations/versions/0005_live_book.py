"""Live prices (F10f): save each trader's book as a snapshot; drop the replay clock.

An order history can't rebuild an account priced live, so `traders.events` is replaced by
`traders.book` (cash, positions, orders). Old order histories belonged to the replayed
market and are dropped; a downgrade restores the columns empty.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-18
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("traders", sa.Column("book", JSONB, nullable=True))
    op.drop_column("traders", "events")
    op.drop_table("desk_state")


def downgrade() -> None:
    op.create_table(
        "desk_state",
        sa.Column("key", sa.Text, primary_key=True),
        sa.Column("value", sa.Text, nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.add_column(
        "traders",
        sa.Column("events", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
    )
    op.drop_column("traders", "book")
