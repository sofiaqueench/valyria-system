"""Persist Staff games and idempotent game-host XP references."""
from alembic import op
import sqlalchemy as sa


revision = "0008_staff_games"
down_revision = "0007_staff_warnings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "staff_game_sessions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("game_type", sa.String(length=32), nullable=False),
        sa.Column("host_id", sa.BigInteger(), nullable=False),
        sa.Column("created_by", sa.BigInteger(), nullable=False),
        sa.Column("panel_channel_id", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("winner", sa.String(length=200), nullable=True),
        sa.Column("xp_awarded", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_by", sa.BigInteger(), nullable=True),
        sa.Column("result_message_id", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["guild_id"], ["guilds.guild_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_staff_game_sessions_guild_id", "staff_game_sessions", ["guild_id"])
    op.create_index(
        "uq_staff_game_one_active_per_guild_type", "staff_game_sessions",
        ["guild_id", "game_type"], unique=True,
        sqlite_where=sa.text("status = 'active'"),
        postgresql_where=sa.text("status = 'active'"),
    )
    with op.batch_alter_table("staff_xp_history") as batch_op:
        batch_op.add_column(sa.Column("game_session_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            "fk_staff_xp_history_game_session_id_staff_game_sessions",
            "staff_game_sessions", ["game_session_id"], ["id"], ondelete="SET NULL",
        )
        batch_op.create_unique_constraint("uq_staff_xp_history_game_session_id", ["game_session_id"])


def downgrade() -> None:
    with op.batch_alter_table("staff_xp_history") as batch_op:
        batch_op.drop_constraint("uq_staff_xp_history_game_session_id", type_="unique")
        batch_op.drop_constraint("fk_staff_xp_history_game_session_id_staff_game_sessions", type_="foreignkey")
        batch_op.drop_column("game_session_id")
    op.drop_index("uq_staff_game_one_active_per_guild_type", table_name="staff_game_sessions")
    op.drop_index("ix_staff_game_sessions_guild_id", table_name="staff_game_sessions")
    op.drop_table("staff_game_sessions")
