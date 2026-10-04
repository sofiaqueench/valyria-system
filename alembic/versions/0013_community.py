"""Add guild-scoped community systems and channel-specific auto reactions."""
from alembic import op
import sqlalchemy as sa

revision = "0013_community"
down_revision = "0012_appeals"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("role_reaction_rules") as batch:
        batch.add_column(sa.Column("channel_id", sa.BigInteger(), nullable=True))
        batch.drop_constraint("uq_role_reaction_rule", type_="unique")
        batch.create_unique_constraint("uq_role_reaction_rule", ["guild_id", "channel_id", "role_id"])
    op.create_table("server_stats",
        sa.Column("id", sa.Integer(), primary_key=True), sa.Column("guild_id", sa.BigInteger(), sa.ForeignKey("guilds.guild_id", ondelete="CASCADE"), nullable=False),
        sa.Column("stat_type", sa.String(24), nullable=False), sa.Column("channel_id", sa.BigInteger(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()), sa.Column("last_name", sa.String(100)),
        sa.UniqueConstraint("guild_id", "stat_type", name="uq_server_stat_guild_type"))
    op.create_table("community_sequences",
        sa.Column("id", sa.Integer(), primary_key=True), sa.Column("guild_id", sa.BigInteger(), sa.ForeignKey("guilds.guild_id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(24), nullable=False), sa.Column("last_number", sa.Integer(), nullable=False, server_default="0"),
        sa.UniqueConstraint("guild_id", "kind", name="uq_community_sequence_guild_kind"))
    op.create_table("suggestions",
        sa.Column("id", sa.Integer(), primary_key=True), sa.Column("guild_id", sa.BigInteger(), sa.ForeignKey("guilds.guild_id", ondelete="CASCADE"), nullable=False),
        sa.Column("suggestion_number", sa.Integer(), nullable=False), sa.Column("author_id", sa.BigInteger(), nullable=False),
        sa.Column("title", sa.String(200), nullable=False), sa.Column("description", sa.Text(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False, server_default="PENDING"), sa.Column("review_message_id", sa.BigInteger()),
        sa.Column("review_channel_id", sa.BigInteger()), sa.Column("approved_message_id", sa.BigInteger()), sa.Column("approved_channel_id", sa.BigInteger()),
        sa.Column("reviewed_by", sa.BigInteger()), sa.Column("review_reason", sa.Text()), sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(timezone=True)), sa.UniqueConstraint("guild_id", "suggestion_number", name="uq_suggestion_guild_number"))
    op.create_index("ix_suggestion_guild_status", "suggestions", ["guild_id", "status"])
    op.create_table("community_polls",
        sa.Column("id", sa.Integer(), primary_key=True), sa.Column("guild_id", sa.BigInteger(), sa.ForeignKey("guilds.guild_id", ondelete="CASCADE"), nullable=False),
        sa.Column("poll_number", sa.Integer(), nullable=False), sa.Column("channel_id", sa.BigInteger(), nullable=False), sa.Column("message_id", sa.BigInteger()),
        sa.Column("creator_id", sa.BigInteger(), nullable=False), sa.Column("question", sa.String(300), nullable=False), sa.Column("options_json", sa.Text(), nullable=False),
        sa.Column("multiple", sa.Boolean(), nullable=False, server_default=sa.false()), sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="OPEN"), sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("guild_id", "poll_number", name="uq_poll_guild_number"))
    op.create_index("ix_poll_guild_status", "community_polls", ["guild_id", "status"])
    op.create_table("poll_votes", sa.Column("id", sa.Integer(), primary_key=True), sa.Column("poll_id", sa.Integer(), sa.ForeignKey("community_polls.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False), sa.Column("option_index", sa.Integer(), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("poll_id", "user_id", "option_index", name="uq_poll_vote_option"))
    op.create_index("ix_poll_vote_poll", "poll_votes", ["poll_id"])
    op.create_table("confessions", sa.Column("id", sa.Integer(), primary_key=True), sa.Column("guild_id", sa.BigInteger(), sa.ForeignKey("guilds.guild_id", ondelete="CASCADE"), nullable=False),
        sa.Column("confession_number", sa.Integer(), nullable=False), sa.Column("author_id", sa.BigInteger(), nullable=False), sa.Column("content", sa.Text(), nullable=False),
        sa.Column("channel_id", sa.BigInteger(), nullable=False), sa.Column("message_id", sa.BigInteger()), sa.Column("status", sa.String(16), nullable=False, server_default="ACTIVE"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("guild_id", "confession_number", name="uq_confession_guild_number"))
    op.create_index("ix_confession_guild_author_time", "confessions", ["guild_id", "author_id", "created_at"])


def downgrade() -> None:
    for table, index in (("confessions", "ix_confession_guild_author_time"), ("poll_votes", "ix_poll_vote_poll"), ("community_polls", "ix_poll_guild_status"), ("suggestions", "ix_suggestion_guild_status")):
        op.drop_index(index, table_name=table)
    for table in ("confessions", "poll_votes", "community_polls", "suggestions", "community_sequences", "server_stats"):
        op.drop_table(table)
    with op.batch_alter_table("role_reaction_rules") as batch:
        batch.drop_constraint("uq_role_reaction_rule", type_="unique")
        batch.drop_column("channel_id")
        batch.create_unique_constraint("uq_role_reaction_rule", ["guild_id", "role_id"])
