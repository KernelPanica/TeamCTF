import os

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


def make_engine(url=None):
    engine = create_engine(
        url or os.getenv("DATABASE_URL", "sqlite:///data/cyberrange.db"),
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(connection, _):
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return engine
