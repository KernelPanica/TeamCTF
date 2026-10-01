"""Active nickname reservations and persistent lobby queue."""
from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("players", naming_convention={"uq": "uq_%(table_name)s_%(column_0_name)s"}) as batch:
        batch.drop_constraint("uq_players_nickname", type_="unique")
        batch.add_column(sa.Column("active_nickname", sa.String, nullable=True))
        batch.add_column(sa.Column("session_expires_at", sa.DateTime, nullable=True))
        batch.add_column(sa.Column("queued_at", sa.DateTime, nullable=True))
    op.create_index("ix_players_active_nickname", "players", ["active_nickname"], unique=True)


def downgrade():
    # Historical duplicate names cannot be discarded to satisfy the old constraint.
    if op.get_bind().execute(sa.text("SELECT nickname FROM players GROUP BY nickname HAVING count(*) > 1 LIMIT 1")).first():
        raise RuntimeError("cannot downgrade while historical players share a nickname")
    op.drop_index("ix_players_active_nickname", table_name="players")
    with op.batch_alter_table("players") as batch:
        batch.drop_column("queued_at")
        batch.drop_column("session_expires_at")
        batch.drop_column("active_nickname")
        batch.create_unique_constraint("uq_players_nickname", ["nickname"])
