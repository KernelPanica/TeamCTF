from alembic import context

from backend.app.database import Base, make_engine
from backend.app import models  # noqa: F401

engine = make_engine()
with engine.connect() as connection:
    context.configure(connection=connection, target_metadata=Base.metadata, render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()
engine.dispose()
