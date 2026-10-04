"""Persist raid state, jail actions, and reversible Jail channel permission snapshots."""
from alembic import op
import sqlalchemy as sa

revision = "0014_security"
down_revision = "0013_community"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table("raid_states",
        sa.Column("guild_id", sa.BigInteger(), sa.ForeignKey("guilds.guild_id", ondelete="CASCADE"), primary_key=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("activated_by", sa.BigInteger()), sa.Column("reason", sa.String(300)),
        sa.Column("join_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("activated_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))
    op.create_table("jail_records",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("guild_id", sa.BigInteger(), sa.ForeignKey("guilds.guild_id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False), sa.Column("moderator_id", sa.BigInteger(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False), sa.Column("original_role_ids", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("jail_role_id", sa.BigInteger(), nullable=False), sa.Column("status", sa.String(16), nullable=False, server_default="ACTIVE"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("unjailed_by", sa.BigInteger()), sa.Column("unjailed_at", sa.DateTime(timezone=True)))
    op.create_index("uq_jail_active_member", "jail_records", ["guild_id", "user_id"], unique=True,
        sqlite_where=sa.text("status = 'ACTIVE'"), postgresql_where=sa.text("status = 'ACTIVE'"))
    op.create_index("ix_jail_guild_user_status", "jail_records", ["guild_id", "user_id", "status"])
    op.create_table("jail_channel_permissions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("guild_id", sa.BigInteger(), sa.ForeignKey("guilds.guild_id", ondelete="CASCADE"), nullable=False),
        sa.Column("role_id", sa.BigInteger(), nullable=False), sa.Column("channel_id", sa.BigInteger(), nullable=False),
        sa.Column("had_overwrite", sa.Boolean(), nullable=False), sa.Column("allow_bits", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("deny_bits", sa.BigInteger(), nullable=False, server_default="0"),
        sa.UniqueConstraint("guild_id", "role_id", "channel_id", name="uq_jail_channel_permission"))


def downgrade() -> None:
    op.drop_table("jail_channel_permissions")
    op.drop_index("ix_jail_guild_user_status", table_name="jail_records")
    op.drop_index("uq_jail_active_member", table_name="jail_records")
    op.drop_table("jail_records")
    op.drop_table("raid_states")
