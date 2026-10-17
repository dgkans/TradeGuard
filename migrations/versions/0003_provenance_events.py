"""provenance_events: what entered each session's agent context, with its trust level.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-16
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "provenance_events",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("session_id", sa.Text, nullable=False),
        sa.Column("seq", sa.Integer, nullable=False),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("source", sa.Text, nullable=False),
        sa.Column("trust", sa.Text, nullable=False),
        sa.Column("risk", sa.Float, nullable=False),
        sa.Column("signals", JSONB, nullable=False),
        sa.Column("text", sa.Text, nullable=False),
        sa.Column("sha256", sa.Text, nullable=False),
        sa.UniqueConstraint("session_id", "seq", name="provenance_session_seq"),
        sa.CheckConstraint("kind in ('user_message', 'tool_result')", name="provenance_kind"),
        sa.CheckConstraint("trust in ('trusted', 'untrusted')", name="provenance_trust"),
    )


def downgrade() -> None:
    op.drop_table("provenance_events")
