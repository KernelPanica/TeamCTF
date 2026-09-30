"""Initial Stage 1 schema."""
from alembic import op
import sqlalchemy as sa

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("players", sa.Column("id", sa.Integer, primary_key=True),
                    sa.Column("nickname", sa.String, nullable=False, unique=True))
    op.create_table("cases", sa.Column("id", sa.String, primary_key=True),
                    sa.Column("name", sa.String, nullable=False))
    op.create_table(
        "matches", sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("state", sa.Enum("WAITING", "MATCHMAKING", "PROVISIONING", "RUNNING",
                  "FINISHING", "FINISHED", "FAILED", name="matchstate", create_constraint=True),
                  nullable=False, server_default="WAITING"),
        sa.Column("case_id", sa.String, sa.ForeignKey("cases.id")),
    )
    op.create_table(
        "match_players",
        sa.Column("match_id", sa.Integer, sa.ForeignKey("matches.id"), primary_key=True),
        sa.Column("player_id", sa.Integer, sa.ForeignKey("players.id"), primary_key=True),
        sa.Column("team", sa.Enum("RED", "BLUE", name="team", create_constraint=True)),
    )
    op.create_table(
        "match_events", sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("match_id", sa.Integer, sa.ForeignKey("matches.id"), nullable=False),
        sa.Column("type", sa.String, nullable=False),
        sa.Column("timestamp", sa.DateTime, nullable=False),
        sa.Column("metadata", sa.JSON, nullable=False),
    )
    op.create_index("ix_match_events_match_id", "match_events", ["match_id"])


def downgrade():
    for table in ("match_events", "match_players", "matches", "cases", "players"):
        op.drop_table(table)
