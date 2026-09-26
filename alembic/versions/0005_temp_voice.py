"""Temporary voice rooms and per-room access controls."""
from alembic import op
import sqlalchemy as sa


revision = "0005_temp_voice"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "temp_voice_rooms",
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("channel_id", sa.BigInteger(), nullable=False),
        sa.Column("owner_id", sa.BigInteger(), nullable=False),
        sa.Column("category_id", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("user_limit", sa.Integer(), nullable=False),
        sa.Column("locked", sa.Boolean(), nullable=False),
        sa.Column("panel_message_id", sa.BigInteger(), nullable=True),
        sa.ForeignKeyConstraint(["guild_id"], ["guilds.guild_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("guild_id", "channel_id"),
    )
    op.create_index("ix_temp_voice_owner", "temp_voice_rooms", ["guild_id", "owner_id"])

    for table in ("temp_voice_room_bans", "temp_voice_room_access"):
        op.create_table(
            table,
            sa.Column("guild_id", sa.BigInteger(), nullable=False),
            sa.Column("channel_id", sa.BigInteger(), nullable=False),
            sa.Column("user_id", sa.BigInteger(), nullable=False),
            sa.ForeignKeyConstraint(
                ["guild_id", "channel_id"],
                ["temp_voice_rooms.guild_id", "temp_voice_rooms.channel_id"],
                ondelete="CASCADE",
            ),
            sa.PrimaryKeyConstraint("guild_id", "channel_id", "user_id"),
        )


def downgrade() -> None:
    op.drop_table("temp_voice_room_access")
    op.drop_table("temp_voice_room_bans")
    op.drop_index("ix_temp_voice_owner", table_name="temp_voice_rooms")
    op.drop_table("temp_voice_rooms")
