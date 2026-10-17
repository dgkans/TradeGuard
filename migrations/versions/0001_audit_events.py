"""audit_events: one row per gateway tool call.

Revision ID: 0001
Revises:
Create Date: 2026-10-16
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "audit_events",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.Column("session_id", sa.Text, nullable=False),
        sa.Column("upstream", sa.Text, nullable=True),
        sa.Column("tool", sa.Text, nullable=False),
        sa.Column("args", JSONB, nullable=False),
        sa.Column("decision", sa.Text, nullable=False),
        sa.Column("rule_id", sa.Text, nullable=False),
        sa.Column("reason", sa.Text, nullable=False),
        # Filled in after the upstream call. NULL means the call never completed.
        sa.Column("latency_ms", sa.Float, nullable=True),
        sa.Column("upstream_ms", sa.Float, nullable=True),
        sa.Column("overhead_ms", sa.Float, nullable=True),
        sa.Column("is_error", sa.Boolean, nullable=True),
        sa.Column("result_summary", sa.Text, nullable=True),
        sa.Column("trace_id", sa.Text, nullable=True),
        sa.Column("span_id", sa.Text, nullable=True),
        sa.CheckConstraint("decision in ('ALLOW', 'DENY', 'ESCALATE')", name="decision_values"),
    )  # fmt: skip
    op.create_index("audit_events_session", "audit_events", ["session_id", "created_at"])
    op.create_index("audit_events_tool_decision", "audit_events", ["tool", "decision"])


def downgrade() -> None:
    op.drop_table("audit_events")
