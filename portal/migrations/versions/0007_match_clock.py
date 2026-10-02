"""Persist the authoritative start time; do not invent times for old matches."""
from alembic import op
import sqlalchemy as sa

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("matches", sa.Column("started_at", sa.DateTime, nullable=True))


def downgrade():
    with op.batch_alter_table("matches") as batch:
        batch.drop_column("started_at")
