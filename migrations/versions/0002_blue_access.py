"""Player bearer token hashes and temporary BLUE credentials."""
from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("players", sa.Column("token_hash", sa.String(64), nullable=True))
    op.create_index("ix_players_token_hash", "players", ["token_hash"], unique=True)
    op.add_column("matches", sa.Column("target_host", sa.String, nullable=True))
    op.add_column("matches", sa.Column("blue_password", sa.String, nullable=True))


def downgrade():
    with op.batch_alter_table("matches") as batch:
        batch.drop_column("blue_password")
        batch.drop_column("target_host")
    op.drop_index("ix_players_token_hash", table_name="players")
    with op.batch_alter_table("players") as batch:
        batch.drop_column("token_hash")
