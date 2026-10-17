"""approvals: escalated calls waiting for, or decided by, a human.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-16
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

STATUSES = "('pending', 'approved', 'rejected', 'expired', 'executing', 'executed', 'blocked')"


def upgrade() -> None:
    op.create_table(
        "approvals",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("session_id", sa.Text, nullable=False),
        sa.Column("tool", sa.Text, nullable=False),
        sa.Column("args", JSONB, nullable=False),
        sa.Column("rule_id", sa.Text, nullable=False),
        sa.Column("reason", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default="pending"),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_by", sa.Text, nullable=True),
        sa.Column("note", sa.Text, nullable=True),
        sa.Column("result", JSONB, nullable=True),
        sa.Column("audit_event_id", sa.BigInteger, sa.ForeignKey("audit_events.id"),
                  nullable=True),
        sa.CheckConstraint(f"status in {STATUSES}", name="approval_status_values"),
    )  # fmt: skip
    op.create_index("approvals_status_created", "approvals", ["status", "created_at"])
    op.create_index("approvals_session", "approvals", ["session_id", "created_at"])


def downgrade() -> None:
    op.drop_table("approvals")
