from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow():
    # SQLite stores naive timestamps; all application timestamps are UTC.
    return datetime.now(UTC).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class Member(Base):
    __tablename__ = "members"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    discord_user_id: Mapped[str | None] = mapped_column(String(32), unique=True)
    discord_username: Mapped[str | None] = mapped_column(String(100))
    discord_global_name: Mapped[str | None] = mapped_column(String(100))
    invite_token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    invite_sent_at: Mapped[datetime | None] = mapped_column(DateTime)
    discord_linked_at: Mapped[datetime | None] = mapped_column(DateTime)
    deactivated_at: Mapped[datetime | None] = mapped_column(DateTime)
