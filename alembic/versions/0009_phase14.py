"""Phase 14 moderation, lock state, reaction mappings, and nickname snapshots."""
from alembic import op
import sqlalchemy as sa

revision = "0009_phase14"
down_revision = "0008_staff_games"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "moderation_actions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("guild_id", sa.BigInteger(), sa.ForeignKey("guilds.guild_id", ondelete="CASCADE"), nullable=False),
        sa.Column("target_id", sa.BigInteger(), nullable=False),
        sa.Column("moderator_id", sa.BigInteger(), nullable=False),
        sa.Column("action_type", sa.String(32), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("duration_seconds", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_moderation_actions_guild_id", "moderation_actions", ["guild_id"])
    op.create_index("ix_moderation_actions_target_id", "moderation_actions", ["target_id"])
    op.create_table(
        "channel_lock_states",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("guild_id", sa.BigInteger(), sa.ForeignKey("guilds.guild_id", ondelete="CASCADE"), nullable=False),
        sa.Column("channel_id", sa.BigInteger(), nullable=False),
        sa.Column("previous_send_messages", sa.Boolean(), nullable=True),
        sa.Column("previous_send_messages_in_threads", sa.Boolean(), nullable=True),
        sa.Column("locked_by", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("guild_id", "channel_id", name="uq_channel_lock_guild_channel"),
    )
    op.create_table(
        "reaction_role_mappings",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("guild_id", sa.BigInteger(), sa.ForeignKey("guilds.guild_id", ondelete="CASCADE"), nullable=False),
        sa.Column("channel_id", sa.BigInteger(), nullable=False),
        sa.Column("message_id", sa.BigInteger(), nullable=False),
        sa.Column("emoji_key", sa.String(128), nullable=False),
        sa.Column("emoji_display", sa.String(128), nullable=False),
        sa.Column("role_id", sa.BigInteger(), nullable=False),
        sa.Column("persistent", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("guild_id", "message_id", "emoji_key", name="uq_reaction_role_mapping"),
    )
    op.create_index("ix_reaction_role_mappings_guild_id", "reaction_role_mappings", ["guild_id"])
    op.create_table(
        "role_reaction_rules",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("guild_id", sa.BigInteger(), sa.ForeignKey("guilds.guild_id", ondelete="CASCADE"), nullable=False),
        sa.Column("role_id", sa.BigInteger(), nullable=False),
        sa.Column("emoji_display", sa.String(128), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.UniqueConstraint("guild_id", "role_id", name="uq_role_reaction_rule"),
    )
    op.create_table(
        "game_nickname_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("guild_id", sa.BigInteger(), sa.ForeignKey("guilds.guild_id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("previous_nickname", sa.String(32), nullable=True),
        sa.Column("game_id", sa.String(80), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("guild_id", "user_id", name="uq_game_nickname_guild_user"),
    )


def downgrade() -> None:
    op.drop_table("game_nickname_snapshots")
    op.drop_table("role_reaction_rules")
    op.drop_index("ix_reaction_role_mappings_guild_id", table_name="reaction_role_mappings")
    op.drop_table("reaction_role_mappings")
    op.drop_table("channel_lock_states")
    op.drop_index("ix_moderation_actions_target_id", table_name="moderation_actions")
    op.drop_index("ix_moderation_actions_guild_id", table_name="moderation_actions")
    op.drop_table("moderation_actions")
