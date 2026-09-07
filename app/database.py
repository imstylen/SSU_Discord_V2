from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from app.models import Base


class Database:
    def __init__(self, url: str):
        parsed = make_url(url)
        if parsed.drivername != "sqlite" or not parsed.database or parsed.database == ":memory:":
            raise ValueError("DATABASE_URL must point to a file-backed SQLite database")
        Path(parsed.database).resolve().parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(url, connect_args={"check_same_thread": False, "timeout": 30})

        @event.listens_for(self.engine, "connect")
        def configure_sqlite(connection, _):
            cursor = connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=30000")
            cursor.close()

        self.session = sessionmaker(self.engine, expire_on_commit=False)

    def initialize(self):
        Base.metadata.create_all(self.engine)

    @contextmanager
    def write(self):
        # Serialize entitlement changes and role writes across BOTH processes.
        # Callers run in threads, so lock waits never block the async event loop.
        with self.session() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            try:
                yield session
                session.commit()
            except BaseException:
                session.rollback()
                raise
