"""Persist current Staff promotion state."""
from alembic import op
import sqlalchemy as sa


revision = "0006_staff_promotions"
down_revision = "0005_temp_voice"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "staff_promotion_states",
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("role_id", sa.BigInteger(), nullable=False),
        sa.Column("rank_type", sa.String(length=16), nullable=False),
        sa.Column("xp_at_promotion", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(length=40), nullable=False),
        sa.Column("promoted_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["guild_id"], ["guilds.guild_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("guild_id", "user_id"),
    )


def downgrade() -> None:
    op.drop_table("staff_promotion_states")
