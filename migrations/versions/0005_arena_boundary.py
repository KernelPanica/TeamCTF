"""Portal's durable Arena execution reference and event cursor."""
from alembic import op
import sqlalchemy as sa

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade():
    for column in [sa.Column("arena_run_id", sa.String), sa.Column("arena_instance_id", sa.String),
                   sa.Column("arena_cursor", sa.Integer, nullable=False, server_default="0"),
                   sa.Column("arena_endpoints", sa.JSON),
                   sa.Column("arena_cleanup_pending", sa.Boolean, nullable=False, server_default="0")]:
        op.add_column("matches", column)


def downgrade():
    with op.batch_alter_table("matches") as batch:
        for name in ("arena_cleanup_pending", "arena_endpoints", "arena_cursor", "arena_instance_id", "arena_run_id"):
            batch.drop_column(name)
