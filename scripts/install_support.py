"""Noninteractive installer validation; stdout contains only public deployment metadata."""

import argparse
import fnmatch
import ipaddress
import json
import re
import shlex
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse


class InstallError(Exception):
    pass


def validate_environment(env_file: Path, install_dir: Path) -> dict:
    # Installed dependencies are available before this helper is called. Importing
    # settings does not start the app, create a database, or contact external services.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from dotenv import dotenv_values
    from email_validator import EmailNotValidError, validate_email
    from pydantic import ValidationError
    from sqlalchemy.engine import make_url

    from app.config import Settings

    if not env_file.is_file() or env_file.is_symlink():
        raise InstallError(".env must be an existing regular file, not a symbolic link.")
    values = dotenv_values(env_file, interpolate=False)
    # An installation must not depend on the invoking shell's environment.
    if any(value and "${" in value for value in values.values()):
        raise InstallError("Use literal values in .env; environment interpolation is unsupported.")

    class DeploymentSettings(Settings):
        @classmethod
        def settings_customise_sources(cls, settings_cls, init_settings, **kwargs):
            return (init_settings,)

    try:
        settings = DeploymentSettings(
            **{key.lower(): value for key, value in values.items() if value is not None}
        )
    except ValidationError as exc:
        problems = []
        for error in exc.errors(include_input=False, include_context=False, include_url=False):
            location = ".".join(str(part).upper() for part in error["loc"])
            message = error["msg"].removeprefix("Value error, ")
            # Model-level checks contain the useful requirement (password length,
            # URL/cookie mismatch, mutually exclusive TLS settings). Preserve it,
            # but never include inputs, exception reprs, or credential values.
            for key, value in values.items():
                if value and any(word in key.upper() for word in ("PASSWORD", "SECRET", "TOKEN")):
                    message = message.replace(value, "[redacted]")
            problems.append(f"{location}: {message}" if location else message)
        raise InstallError("Invalid .env settings:\n  - " + "\n  - ".join(problems)) from None

    parsed = urlparse(settings.app_url)
    host = parsed.hostname or ""
    try:
        port = parsed.port
    except ValueError:
        raise InstallError("APP_URL has an invalid port.") from None
    try:
        ipaddress.ip_address(host)
        is_ip = True
    except ValueError:
        is_ip = False
    labels = host.split(".")
    if (
        parsed.scheme != "https"
        or port not in (None, 443)
        or is_ip
        or len(labels) < 2
        or len(host) > 253
        or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in labels)
        or labels[-1] in {"localhost", "local", "test", "invalid", "example"}
    ):
        raise InstallError("APP_URL must be an HTTPS public DNS hostname on port 443.")
    # if not settings.session_cookie_secure:
    #     raise InstallError("Set SESSION_COOKIE_SECURE=true for deployment.")

    for name in ("discord_client_id", "discord_guild_id", "discord_verified_role_id"):
        if not re.fullmatch(r"[1-9][0-9]{0,19}", getattr(settings, name)):
            raise InstallError(f"Set a valid {name.upper()} in .env.")
    if settings.discord_guild_id == settings.discord_verified_role_id:
        raise InstallError("DISCORD_VERIFIED_ROLE_ID must not be the @everyone role.")
    for name in ("discord_client_secret", "discord_bot_token"):
        secret = getattr(settings, name).get_secret_value()
        if not secret.strip() or secret.startswith("replace-"):
            raise InstallError(f"Set {name.upper()} in .env.")
    invite = urlparse(settings.discord_invite_url)
    if (
        invite.username
        or invite.query
        or invite.fragment
        or not re.fullmatch(r"/(?:invite/)?[A-Za-z0-9-]+", invite.path)
        or "replace-me" in invite.path
        or (invite.hostname == "discord.com" and not invite.path.startswith("/invite/"))
    ):
        raise InstallError("Set a permanent DISCORD_INVITE_URL in .env.")
    if (
        not settings.smtp_host.strip()
        or settings.smtp_host.endswith("example.com")
        or not 1 <= settings.smtp_port <= 65535
    ):
        raise InstallError("Set a real SMTP_HOST and valid SMTP_PORT in .env.")
    try:
        sender = validate_email(settings.smtp_from, check_deliverability=False)
    except EmailNotValidError:
        raise InstallError("Set a valid SMTP_FROM address in .env.") from None
    if sender.domain in {"example.com", "example.org", "example.net"}:
        raise InstallError("Replace the example SMTP_FROM address in .env.")
    if bool(settings.smtp_username) != bool(settings.smtp_password.get_secret_value()):
        raise InstallError("Set both SMTP_USERNAME and SMTP_PASSWORD, or neither for a relay.")
    if not settings.smtp_use_ssl and not settings.smtp_use_starttls:
        raise InstallError("Enable SMTP_USE_SSL or SMTP_USE_STARTTLS for production email.")

    target = install_dir.resolve()
    data = target / "data"
    if data.is_symlink():
        raise InstallError("The installed data directory must not be a symbolic link.")
    try:
        url = make_url(settings.database_url)
        if (
            url.drivername != "sqlite"
            or not url.database
            or url.database == ":memory:"
            or url.query
        ):
            raise ValueError
        database = Path(url.database)
        database = (
            (target / database).resolve() if not database.is_absolute() else database.resolve()
        )
        if not database.is_relative_to(data) or database == data:
            raise ValueError
    except (ValueError, TypeError):
        raise InstallError(
            "DATABASE_URL must name a SQLite file inside /opt/ssu-membership/data."
        ) from None
    if database.exists() and not database.is_file():
        raise InstallError("DATABASE_URL must name a regular file.")
    return {"hostname": host, "app_url": settings.app_url, "database": str(database)}


def backup_database(database: Path, destination: Path) -> Path:
    """Use only stdlib so an older/broken application environment cannot block recovery."""
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    result = destination / f"pre-install-{stamp}.sqlite3"
    result.touch(mode=0o600, exist_ok=False)
    try:
        with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as source:
            with sqlite3.connect(result) as backup:
                source.backup(backup)
                if backup.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise InstallError("Pre-install database backup failed its integrity check.")
    except BaseException:
        result.unlink(missing_ok=True)
        raise
    return result


def caddy_root_config(original: str, root_file: Path, site_file: Path) -> str:
    # Preserve user configuration byte-for-byte apart from adding one top-level
    # import. Also recognize the usual wildcard imports to avoid double inclusion.
    for line in original.splitlines():
        try:
            words = shlex.split(line, comments=True)
        except ValueError:
            continue  # caddy validate is the authoritative syntax check.
        if len(words) >= 2 and words[0] == "import":
            pattern = PurePosixPath(words[1])
            if not pattern.is_absolute() and not re.match(r"^[A-Za-z]:/", str(pattern)):
                pattern = PurePosixPath(root_file.parent.as_posix()) / pattern
            if fnmatch.fnmatchcase(site_file.as_posix(), str(pattern)):
                return original
    return original.rstrip() + f"\n\n# Managed by SSU install.sh\nimport {site_file.as_posix()}\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    validate = sub.add_parser("validate")
    validate.add_argument("--env", type=Path, required=True)
    validate.add_argument("--install-dir", type=Path, required=True)
    backup = sub.add_parser("backup")
    backup.add_argument("--database", type=Path, required=True)
    backup.add_argument("--destination", type=Path, required=True)
    caddy = sub.add_parser("caddy-root")
    caddy.add_argument("--root", type=Path, required=True)
    caddy.add_argument("--site", type=Path, required=True)
    caddy.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "validate":
            print(json.dumps(validate_environment(args.env, args.install_dir)))
        elif args.command == "backup":
            print(
                f"Verified pre-install backup: {backup_database(args.database, args.destination)}"
            )
        else:
            original = args.root.read_text() if args.root.exists() else ""
            args.output.write_text(caddy_root_config(original, args.root, args.site))
    except InstallError as exc:
        print(f"Installer: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        # Exceptions from validators/parsers can include credential values.
        print(
            f"Installer helper failed ({type(exc).__name__}); check configuration and paths.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
