from alembic import context

from backend.app.database import Base, make_engine
from backend.app import models  # noqa: F401

engine = make_engine()
with engine.connect() as connection:
    # SQLite batch migrations rebuild referenced tables. Check integrity before commit.
    connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
    connection.commit()
    context.configure(connection=connection, target_metadata=Base.metadata, render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()
        if connection.exec_driver_sql("PRAGMA foreign_key_check").first() is not None:
            raise RuntimeError("migration violated foreign keys")
    connection.commit()
    connection.exec_driver_sql("PRAGMA foreign_keys=ON")
engine.dispose()
