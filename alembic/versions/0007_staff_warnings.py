"""Persist Staff warnings for XP penalties."""
from alembic import op
import sqlalchemy as sa


revision = "0007_staff_warnings"
down_revision = "0006_staff_promotions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "staff_warnings",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("staff_user_id", sa.BigInteger(), nullable=False),
        sa.Column("moderator_id", sa.BigInteger(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("xp_penalty", sa.Integer(), nullable=False),
        sa.Column("interaction_id", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["guild_id"], ["guilds.guild_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("interaction_id", name="uq_staff_warning_interaction"),
    )


def downgrade() -> None:
    op.drop_table("staff_warnings")
