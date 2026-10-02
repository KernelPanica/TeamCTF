"""Match keys and BLUE stabilization state."""
from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("matches", sa.Column("red_key", sa.String, nullable=True))
    op.add_column("matches", sa.Column("blue_key", sa.String, nullable=True))
    op.add_column("matches", sa.Column("securing_started_at", sa.DateTime, nullable=True))
    op.add_column("matches", sa.Column("blue_key_issued_at", sa.DateTime, nullable=True))


def downgrade():
    with op.batch_alter_table("matches") as batch:
        batch.drop_column("blue_key_issued_at")
        batch.drop_column("securing_started_at")
        batch.drop_column("blue_key")
        batch.drop_column("red_key")
