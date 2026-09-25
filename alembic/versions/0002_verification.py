"""Verification records and one-time staff XP awards."""
from typing import Sequence, Union
import sqlalchemy as sa
from alembic import op
revision: str = "0002"
down_revision: Union[str, Sequence[str], None] = "0001"
branch_labels = None
depends_on = None
def upgrade() -> None:
    op.create_table("verification_records",
        sa.Column("id",sa.Integer(),autoincrement=True,nullable=False),
        sa.Column("guild_id",sa.BigInteger(),nullable=False),sa.Column("member_id",sa.BigInteger(),nullable=False),
        sa.Column("staff_id",sa.BigInteger(),nullable=True),sa.Column("gender",sa.String(16),nullable=True),
        sa.Column("is_other_server_staff",sa.Boolean(),nullable=True),sa.Column("other_server_name",sa.String(100),nullable=True),
        sa.Column("status",sa.String(20),nullable=False),sa.Column("reason",sa.Text(),nullable=True),
        sa.Column("created_at",sa.DateTime(timezone=True),server_default=sa.func.now(),nullable=False),
        sa.Column("completed_at",sa.DateTime(timezone=True),nullable=True),
        sa.ForeignKeyConstraint(["guild_id"],["guilds.guild_id"],ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id",name=op.f("pk_verification_records")))
    op.create_index("ix_verification_guild_member_status","verification_records",["guild_id","member_id","status"])
    op.create_table("staff_xp_history",
        sa.Column("id",sa.Integer(),autoincrement=True,nullable=False),sa.Column("guild_id",sa.BigInteger(),nullable=False),
        sa.Column("staff_id",sa.BigInteger(),nullable=False),sa.Column("action",sa.String(40),nullable=False),
        sa.Column("xp_amount",sa.Integer(),nullable=False),sa.Column("target_id",sa.BigInteger(),nullable=False),
        sa.Column("verification_record_id",sa.Integer(),nullable=False),
        sa.Column("created_at",sa.DateTime(timezone=True),server_default=sa.func.now(),nullable=False),
        sa.ForeignKeyConstraint(["guild_id"],["guilds.guild_id"],ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["verification_record_id"],["verification_records.id"],ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id",name=op.f("pk_staff_xp_history")),
        sa.UniqueConstraint("verification_record_id",name="uq_staff_xp_verification_record"))
def downgrade() -> None:
    op.drop_table("staff_xp_history")
    op.drop_index("ix_verification_guild_member_status",table_name="verification_records")
    op.drop_table("verification_records")
