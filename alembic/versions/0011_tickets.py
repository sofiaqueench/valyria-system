"""Create guild-scoped professional ticket tables."""
from alembic import op
import sqlalchemy as sa

revision = "0011_tickets"
down_revision = "0010_automod"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ticket_sequences",
        sa.Column("guild_id", sa.BigInteger(), sa.ForeignKey("guilds.guild_id", ondelete="CASCADE"), primary_key=True),
        sa.Column("last_number", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_table(
        "tickets",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("guild_id", sa.BigInteger(), sa.ForeignKey("guilds.guild_id", ondelete="CASCADE"), nullable=False),
        sa.Column("ticket_number", sa.Integer(), nullable=False),
        sa.Column("ticket_type", sa.String(32), nullable=False),
        sa.Column("creator_id", sa.BigInteger(), nullable=False),
        sa.Column("channel_id", sa.BigInteger(), nullable=False),
        sa.Column("category_id", sa.BigInteger(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="open"),
        sa.Column("claimed_by", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_by", sa.BigInteger(), nullable=True),
        sa.Column("opening_message_id", sa.BigInteger(), nullable=True),
        sa.UniqueConstraint("guild_id", "ticket_number", name="uq_ticket_guild_number"),
        sa.UniqueConstraint("guild_id", "channel_id", name="uq_ticket_guild_channel"),
    )
    op.create_index("ix_ticket_guild_creator_status", "tickets", ["guild_id", "creator_id", "status"])
    op.create_index("uq_ticket_one_open_per_member", "tickets", ["guild_id", "creator_id"], unique=True,
                    sqlite_where=sa.text("status = 'open'"), postgresql_where=sa.text("status = 'open'"))


def downgrade() -> None:
    op.drop_index("ix_ticket_guild_creator_status", table_name="tickets")
    op.drop_index("uq_ticket_one_open_per_member", table_name="tickets")
    op.drop_table("tickets")
    op.drop_table("ticket_sequences")
