"""ai_usage: AI turns used per day, for the free plan's daily caps (F12).

The API sleeps when idle on Render's free plan and restarts on wake, so the counts live in
Postgres rather than in memory. `key` is "all" for the whole site or "trader-<id>".

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-18
"""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ai_usage",
        sa.Column("day", sa.Date, nullable=False),
        sa.Column("key", sa.Text, nullable=False),
        sa.Column("turns", sa.Integer, nullable=False, server_default="0"),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("day", "key"),
        sa.CheckConstraint("turns >= 0", name="ai_usage_turns_not_negative"),
    )


def downgrade() -> None:
    op.drop_table("ai_usage")
