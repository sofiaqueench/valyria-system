"""Add invite attribution and allow staff XP history rows without verification records.

Revision ID: 0003
Revises: 0002
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: Union[str, Sequence[str], None] = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("staff_xp_history") as batch_op:
        batch_op.alter_column(
            "verification_record_id",
            existing_type=sa.Integer(),
            nullable=True,
        )

    op.create_table(
        "invite_attributions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("member_id", sa.BigInteger(), nullable=False),
        sa.Column("inviter_id", sa.BigInteger(), nullable=True),
        sa.Column("invite_code", sa.String(length=32), nullable=True),
        sa.Column("uses_before", sa.Integer(), nullable=True),
        sa.Column("uses_after", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("xp_awarded", sa.Boolean(), server_default=sa.text("0"), nullable=False),
        sa.Column("xp_amount", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["guild_id"], ["guilds.guild_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_invite_attributions")),
        sa.UniqueConstraint("guild_id", "member_id", name="uq_invite_attributions_guild_member"),
    )
    op.create_index(
        "ix_invite_attributions_guild_inviter",
        "invite_attributions",
        ["guild_id", "inviter_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_invite_attributions_guild_inviter", table_name="invite_attributions")
    op.drop_table("invite_attributions")
    # Invite XP rows have no verification record; remove them before restoring NOT NULL.
    op.execute("DELETE FROM staff_xp_history WHERE verification_record_id IS NULL")
    with op.batch_alter_table("staff_xp_history") as batch_op:
        batch_op.alter_column(
            "verification_record_id",
            existing_type=sa.Integer(),
            nullable=False,
        )
