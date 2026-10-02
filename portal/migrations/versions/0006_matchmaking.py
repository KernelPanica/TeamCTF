"""Persist the seed of automatically assigned matches."""
from alembic import op
import sqlalchemy as sa

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("matches", sa.Column("seed", sa.BigInteger, nullable=True))


def downgrade():
    with op.batch_alter_table("matches") as batch:
        batch.drop_column("seed")
