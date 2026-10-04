"""Add AutoMod warning idempotency and the normal Member XP transaction ledger."""
from alembic import op
import sqlalchemy as sa

revision = "0010_automod"
down_revision = "0009_phase14"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("moderation_actions") as batch_op:
        batch_op.add_column(sa.Column("source", sa.String(length=32), server_default="Manual", nullable=False))
        batch_op.add_column(sa.Column("message_id", sa.BigInteger(), nullable=True))
        batch_op.create_unique_constraint("uq_moderation_action_guild_message", ["guild_id", "message_id"])

    op.create_table(
        "member_xp_history",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("guild_id", sa.BigInteger(), sa.ForeignKey("guilds.guild_id", ondelete="CASCADE"), nullable=False),
        sa.Column("member_id", sa.BigInteger(), nullable=False),
        sa.Column("source", sa.String(length=40), nullable=False),
        sa.Column("amount", sa.Integer(), nullable=False),
        sa.Column("total_after", sa.Integer(), nullable=False),
        sa.Column("event_id", sa.BigInteger(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("guild_id", "event_id", name="uq_member_xp_history_guild_event"),
    )
    op.create_index("ix_member_xp_history_guild_member", "member_xp_history", ["guild_id", "member_id"])


def downgrade() -> None:
    op.drop_index("ix_member_xp_history_guild_member", table_name="member_xp_history")
    op.drop_table("member_xp_history")
    with op.batch_alter_table("moderation_actions") as batch_op:
        batch_op.drop_constraint("uq_moderation_action_guild_message", type_="unique")
        batch_op.drop_column("message_id")
        batch_op.drop_column("source")
