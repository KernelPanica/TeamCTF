"""Persist the first accepted key's result."""
from alembic import op
import sqlalchemy as sa

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("matches") as batch:
        batch.add_column(sa.Column("finished_at", sa.DateTime, nullable=True))
        batch.add_column(sa.Column("winner", sa.Enum("RED", "BLUE", name="winner_team", create_constraint=True), nullable=True))


def downgrade():
    with op.batch_alter_table("matches") as batch:
        batch.drop_constraint("winner_team", type_="check")
        batch.drop_column("winner")
        batch.drop_column("finished_at")
