"""Consistent online SQLite backups, including transactions still in the WAL."""

import argparse
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy.engine import make_url

from app.config import Settings


def backup_database(database_url: str, destination: Path, retention_days: int = 30) -> Path:
    parsed = make_url(database_url)
    if parsed.drivername != "sqlite" or not parsed.database:
        raise ValueError("Only file-backed SQLite databases can be backed up")
    source = Path(parsed.database).resolve(strict=True)
    destination = destination.resolve()
    destination.mkdir(parents=True, exist_ok=True)
    now = datetime.now(UTC)
    target = destination / f"ssu-{now.strftime('%Y%m%dT%H%M%S%fZ')}.sqlite3"
    target.touch(exist_ok=False, mode=0o600)
    try:
        with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as origin:
            with sqlite3.connect(target) as backup:
                origin.backup(backup)
                if backup.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise RuntimeError("Backup integrity check failed")
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    cutoff = now - timedelta(days=retention_days)
    if retention_days > 0:
        for candidate in destination.glob("ssu-*.sqlite3"):
            try:
                created = datetime.strptime(candidate.stem, "ssu-%Y%m%dT%H%M%S%fZ").replace(
                    tzinfo=UTC
                )
            except ValueError:
                continue
            if (
                created < cutoff
                and candidate != target
                and candidate.is_file()
                and not candidate.is_symlink()
            ):
                candidate.unlink()
    return target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, default=Path("backups"))
    parser.add_argument("--retention-days", type=int, default=30, help="0 keeps all backups")
    args = parser.parse_args()
    if args.retention_days < 0:
        parser.error("retention-days must be non-negative")
    result = backup_database(Settings().database_url, args.destination, args.retention_days)
    print(f"Verified backup created: {result}")


if __name__ == "__main__":
    main()
