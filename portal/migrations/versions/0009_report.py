"""Keep the final report in the same transaction as the winner."""
from alembic import op
import sqlalchemy as sa

revision = '0009'
down_revision = '0008'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('matches', sa.Column('report', sa.JSON, nullable=True))


def downgrade():
    with op.batch_alter_table('matches') as batch:
        batch.drop_column('report')
