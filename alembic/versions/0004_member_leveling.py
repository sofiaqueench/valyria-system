"""Add guild-scoped member XP storage.

Revision ID: 0004
Revises: 0003
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: Union[str, Sequence[str], None] = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "member_xp",
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("member_id", sa.BigInteger(), nullable=False),
        sa.Column("total_xp", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_message_xp_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["guild_id"], ["guilds.guild_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("guild_id", "member_id", name=op.f("pk_member_xp")),
    )
    op.create_index("ix_member_xp_guild_total", "member_xp", ["guild_id", "total_xp"])


def downgrade() -> None:
    op.drop_index("ix_member_xp_guild_total", table_name="member_xp")
    op.drop_table("member_xp")
