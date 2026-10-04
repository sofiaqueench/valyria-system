"""Add persisted appeals linked to the existing tickets table."""
from alembic import op
import sqlalchemy as sa

revision = "0012_appeals"
down_revision = "0011_tickets"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Normal tickets remain one-open-per-member; appeal tickets have their own
    # Pending uniqueness constraint in the appeals table.
    op.drop_index("uq_ticket_one_open_per_member", table_name="tickets")
    op.create_index(
        "uq_ticket_one_open_per_member", "tickets", ["guild_id", "creator_id"], unique=True,
        sqlite_where=sa.text("status = 'open' AND ticket_type != 'appeal'"),
        postgresql_where=sa.text("status = 'open' AND ticket_type != 'appeal'"),
    )
    op.create_table(
        "appeal_sequences",
        sa.Column("guild_id", sa.BigInteger(), sa.ForeignKey("guilds.guild_id", ondelete="CASCADE"), primary_key=True),
        sa.Column("last_number", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_table(
        "appeals",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("guild_id", sa.BigInteger(), sa.ForeignKey("guilds.guild_id", ondelete="CASCADE"), nullable=False),
        sa.Column("appeal_number", sa.Integer(), nullable=False),
        sa.Column("ticket_id", sa.Integer(), sa.ForeignKey("tickets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("username", sa.String(128), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("explanation", sa.Text(), nullable=False),
        sa.Column("additional_information", sa.Text(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="Pending"),
        sa.Column("staff_id", sa.BigInteger(), nullable=True),
        sa.Column("closed_by", sa.BigInteger(), nullable=True),
        sa.Column("opening_message_id", sa.BigInteger(), nullable=True),
        sa.Column("staff_decision", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("guild_id", "appeal_number", name="uq_appeal_guild_number"),
        sa.UniqueConstraint("guild_id", "ticket_id", name="uq_appeal_guild_ticket"),
    )
    op.create_index("ix_appeal_guild_status", "appeals", ["guild_id", "status"])
    op.create_index(
        "uq_appeal_one_pending_per_user", "appeals", ["guild_id", "user_id"], unique=True,
        sqlite_where=sa.text("status = 'Pending'"), postgresql_where=sa.text("status = 'Pending'"),
    )


def downgrade() -> None:
    op.drop_index("uq_appeal_one_pending_per_user", table_name="appeals")
    op.drop_index("ix_appeal_guild_status", table_name="appeals")
    op.drop_table("appeals")
    op.drop_table("appeal_sequences")
    op.execute("UPDATE tickets SET status = 'closed' WHERE ticket_type = 'appeal' AND status = 'open'")
    op.drop_index("uq_ticket_one_open_per_member", table_name="tickets")
    op.create_index(
        "uq_ticket_one_open_per_member", "tickets", ["guild_id", "creator_id"], unique=True,
        sqlite_where=sa.text("status = 'open'"), postgresql_where=sa.text("status = 'open'"),
    )
