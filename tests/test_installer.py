import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path, PurePosixPath

import pytest

from scripts.install_support import (
    InstallError,
    backup_database,
    caddy_root_config,
    validate_environment,
)

ROOT = Path(__file__).resolve().parents[1]
BASH = shutil.which("bash") or (
    "C:/Program Files/Git/bin/bash.exe"
    if Path("C:/Program Files/Git/bin/bash.exe").is_file()
    else None
)


@pytest.fixture
def deployment(tmp_path):
    source = tmp_path / "upload"
    target = tmp_path / "installed"
    source.mkdir()
    target.mkdir()
    values = {
        "APP_URL": "https://members.ssu-apps.link",
        "ADMIN_PASSWORD": "installer-password-private",
        "SESSION_SECRET": "installer-session-private-" * 3,
        "SESSION_COOKIE_SECURE": "true",
        "DATABASE_URL": "sqlite:///./data/app.db",
        "SMTP_HOST": "smtp.mailprovider.net",
        "SMTP_FROM": "community@ssu-apps.link",
        "SMTP_USE_SSL": "true",
        "DISCORD_CLIENT_ID": "123",
        "DISCORD_CLIENT_SECRET": "oauth-private-secret",
        "DISCORD_BOT_TOKEN": "bot-private-secret",
        "DISCORD_GUILD_ID": "456",
        "DISCORD_VERIFIED_ROLE_ID": "789",
        "DISCORD_INVITE_URL": "https://discord.gg/ssu-community",
    }

    def write(**overrides):
        env = source / ".env"
        env.write_text(
            "\n".join(f'{key}="{value}"' for key, value in (values | overrides).items()),
            encoding="utf-8",
        )
        return env

    return source, target, write


def bash(script, tmp_path, **environment):
    if BASH is None:
        pytest.skip("Bash is not installed")
    env = os.environ.copy()
    env.update(
        INSTALLER_SCRIPT=(ROOT / "install.sh").as_posix(),
        TEST_PYTHON=Path(sys.executable).as_posix(),
        CALL_LOG=(tmp_path / "calls").as_posix(),
        SSH_CONNECTION="",
        **environment,
    )
    # Only explicitly called, mocked functions run; sourcing never invokes main.
    return subprocess.run(
        [BASH, "-c", 'source "$INSTALLER_SCRIPT"\n' + script],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_production_validation_does_not_change_or_disclose_env(deployment, monkeypatch):
    source, target, write = deployment
    path = write()
    before = path.read_bytes()
    monkeypatch.setenv("APP_URL", "http://localhost:8010")
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "")
    metadata = validate_environment(path, target)
    assert metadata == {
        "hostname": "members.ssu-apps.link",
        "app_url": "https://members.ssu-apps.link",
        "database": str(target / "data" / "app.db"),
    }
    assert "private" not in json.dumps(metadata)
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "changes",
    [
        {"APP_URL": "http://localhost:8010"},
        {"APP_URL": "https://127.0.0.1"},
        {"APP_URL": "https://members.ssu-apps.link:444"},
        {"APP_URL": "https://members.ssu-apps.link:secret"},
        {"APP_URL": "https://bad{host.ssu-apps.link"},
        {"DATABASE_URL": "sqlite:///../outside.db"},
        {"DATABASE_URL": "sqlite:///:memory:"},
        {"DATABASE_URL": "postgresql://name:private@host/db"},
        {"SESSION_COOKIE_SECURE": "false"},
        {"ADMIN_PASSWORD": "bad"},
        {"DISCORD_BOT_TOKEN": ""},
        {"DISCORD_CLIENT_ID": "not-a-snowflake"},
        {"DISCORD_VERIFIED_ROLE_ID": "456"},
        {"DISCORD_INVITE_URL": "https://discord.gg/replace-me"},
        {"SMTP_HOST": "mail.example.com"},
        {"SMTP_FROM": "community@example.com"},
        {"SMTP_USERNAME": "user", "SMTP_PASSWORD": ""},
        {"SMTP_USE_SSL": "false", "SMTP_USE_STARTTLS": "false"},
        {"DISCORD_CLIENT_SECRET": "${DISCORD_BOT_TOKEN}"},
    ],
)
def test_invalid_deployment_fails_without_secret_values(deployment, changes):
    _, target, write = deployment
    with pytest.raises(InstallError) as error:
        validate_environment(write(**changes), target)
    assert "private" not in str(error.value)


def test_absolute_database_under_data_is_supported(deployment):
    _, target, write = deployment
    path = target / "data" / "nested" / "members.db"
    metadata = validate_environment(write(DATABASE_URL=f"sqlite:///{path.as_posix()}"), target)
    assert metadata["database"] == str(path)


def test_env_is_not_executed_as_shell(deployment):
    _, target, write = deployment
    metadata = validate_environment(
        write(DISCORD_CLIENT_SECRET="$(touch should-not-exist)"), target
    )
    assert metadata["hostname"] == "members.ssu-apps.link"
    assert not (target / "should-not-exist").exists()


def test_installer_backup_captures_wal_without_modifying_source(tmp_path):
    database = tmp_path / "app.db"
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE members(email TEXT)")
        connection.execute("INSERT INTO members VALUES ('sample@ssu-apps.link')")
        connection.commit()
        result = backup_database(database, tmp_path / "backups")
        with sqlite3.connect(result) as backup:
            assert (
                backup.execute("SELECT email FROM members").fetchone()[0] == "sample@ssu-apps.link"
            )
        assert connection.execute("SELECT count(*) FROM members").fetchone()[0] == 1
    assert result.name.startswith("pre-install-")


@pytest.mark.parametrize(
    "existing",
    [
        "import /etc/caddy/ssu-membership.caddy\n",
        'import "/etc/caddy/ssu-membership.caddy" # already included\n',
        "import /etc/caddy/*.caddy\n",
        "import *.caddy\n",
    ],
)
def test_caddy_import_is_not_duplicated(existing):
    root = PurePosixPath("/etc/caddy/Caddyfile")
    assert caddy_root_config(existing, root, root.parent / "ssu-membership.caddy") == existing


def test_caddy_other_sites_preserved_on_fresh_install_and_rerun():
    original = "{\n email ops@ssu-apps.link\n}\nother.ssu-apps.link {\n respond ok\n}\n"
    root = PurePosixPath("/etc/caddy/Caddyfile")
    site = root.parent / "ssu-membership.caddy"
    first = caddy_root_config(original, root, site)
    assert first.startswith(original.rstrip())
    assert first.count(f"import {site}") == 1
    assert caddy_root_config(first, root, site) == first


def test_shell_syntax_and_help(tmp_path):
    result = bash("parse_args --help", tmp_path)
    assert result.returncode == 0 and "Ubuntu 26.04" in result.stdout
    assert not (tmp_path / "calls").exists()


@pytest.mark.parametrize(
    "arguments",
    ["--bad-option", "--ssh-port", "--ssh-port 0", "--ssh-port 65536", "--ssh-port bad"],
)
def test_invalid_arguments_abort_without_commands(tmp_path, arguments):
    result = bash(f"parse_args {arguments}", tmp_path)
    assert result.returncode != 0
    assert not (tmp_path / "calls").exists()


def test_ssh_sources_and_rules_precede_enable(tmp_path):
    result = bash(
        r"""
parse_args --ssh-port 02222 --ssh-port 2200
SSH_CONNECTION='10.0.0.1 54321 10.0.0.2 2200'
sshd() { printf 'port 22\n'; }
ss() { printf 'LISTEN 0 128 0.0.0.0:2201 0.0.0.0:* users:(("sshd",pid=23,fd=3))\n'; }
systemctl() { printf '[::]:2223 (Stream) 0.0.0.0:2224 (Stream)\n'; }
ufw() { printf '%s\n' "$*" >> "$CALL_LOG"; }
configure_firewall
""",
        tmp_path,
    )
    assert result.returncode == 0, result.stderr
    lines = (tmp_path / "calls").read_text().splitlines()
    for port in (22, 2200, 2201, 2222, 2223, 2224):
        assert sum(f"allow {port}/tcp" in line for line in lines) == 1
    assert any("allow 80/tcp" in line for line in lines)
    assert any("allow 443/tcp" in line for line in lines)
    assert lines[-1] == "--force enable"
    assert not any("reset" in line or "delete" in line or "default" in line for line in lines)


def test_unknown_ssh_port_aborts_before_firewall_changes(tmp_path):
    result = bash(
        r"""
sshd() { return 1; }
ss() { return 0; }
systemctl() { return 0; }
ufw() { printf '%s\n' "$*" >> "$CALL_LOG"; }
configure_firewall
""",
        tmp_path,
    )
    assert result.returncode != 0
    assert "--ssh-port" in result.stderr
    assert not (tmp_path / "calls").exists()


@pytest.mark.parametrize("own", [True, False])
def test_unrelated_port_is_not_taken_over(tmp_path, own):
    result = bash(
        r"""
ss() { printf 'LISTEN 0 128 127.0.0.1:8010 0.0.0.0:* users:(("python",pid=123,fd=3))\n'; }
unit_owns_pid() { return "$OWN_STATUS"; }
check_listener 8010 ssu-membership-web.service
""",
        tmp_path,
        OWN_STATUS="0" if own else "1",
    )
    assert (result.returncode == 0) == own
    if not own:
        assert "occupied outside" in result.stderr


@pytest.mark.parametrize("same", [True, False])
def test_rerun_env_is_preserved_and_conflicts_stop(deployment, tmp_path, same):
    source, target, write = deployment
    path = write()
    installed = path.read_bytes() if same else b"different-private-credentials"
    (target / ".env").write_bytes(installed)
    result = bash(
        'SOURCE_DIR="$SOURCE"; INSTALL_DIR="$TARGET"; check_env_conflict',
        tmp_path,
        SOURCE=source.as_posix(),
        TARGET=target.as_posix(),
    )
    assert (result.returncode == 0) == same
    assert (target / ".env").read_bytes() == installed
    assert "private" not in result.stdout + result.stderr


def test_backup_precedes_application_replacement(tmp_path):
    # Exercise the actual pre-upgrade function with mocked service/backup commands.
    database = tmp_path / "app.db"
    database.write_text("existing data")
    result = bash(
        r"""
DATABASE_PATH="$DB_FILE"
INSTALL_DIR="$TARGET"
systemctl() { printf 'systemctl %s\n' "$*" >> "$CALL_LOG"; }
runuser() { printf 'backup called\n' >> "$CALL_LOG"; }
stop_and_backup
printf 'replace application\n' >> "$CALL_LOG"
""",
        tmp_path,
        DB_FILE=database.as_posix(),
        TARGET=tmp_path.as_posix(),
    )
    assert result.returncode == 0, result.stderr
    lines = (tmp_path / "calls").read_text().splitlines()
    assert lines[-2:] == ["backup called", "replace application"]
    assert any("stop ssu-membership-web.service" in line for line in lines[:-2])
    assert any("stop ssu-membership-backup.timer" in line for line in lines[:-2])
    assert database.read_text() == "existing data"


def test_service_units_are_validated_before_start(tmp_path):
    result = bash(
        r"""
install() { printf 'install %s\n' "$*" >> "$CALL_LOG"; }
systemd-analyze() { printf 'validate units\n' >> "$CALL_LOG"; }
systemctl() { printf '%s\n' "$*" >> "$CALL_LOG"; }
install_services
""",
        tmp_path,
    )
    assert result.returncode == 0, result.stderr
    lines = (tmp_path / "calls").read_text().splitlines()
    assert len([line for line in lines if line.startswith("install ")]) == 4
    assert lines[-3] == "validate units"
    assert lines[-2] == "daemon-reload"
    assert lines[-1].startswith("enable --now ")


@pytest.mark.parametrize("failure", ["validation", "reload", "none"])
def test_caddy_transaction_restores_on_failure_and_is_repeatable(tmp_path, failure):
    work = tmp_path / "work"
    work.mkdir()
    root = tmp_path / "Caddyfile"
    site = tmp_path / "ssu-membership.caddy"
    original = "other.ssu-apps.link { respond ok }\n"
    previous = "# Managed by SSU install.sh.\nold.ssu-apps.link { respond ok }\n"
    root.write_text(original)
    site.write_text(previous)
    target = tmp_path / "installed"
    (target / ".venv" / "bin").mkdir(parents=True)
    (target / "scripts").mkdir()
    shutil.copyfile(
        ROOT / "scripts" / "install_support.py", target / "scripts" / "install_support.py"
    )
    shim = target / ".venv" / "bin" / "python"
    shim.write_text('#!/usr/bin/env bash\nexec "$TEST_PYTHON" "$@"\n')
    shim.chmod(0o755)
    result = bash(
        r"""
WORK_DIR="$FIXTURE_WORK"; CADDY_ROOT="$FIXTURE_ROOT"; CADDY_SITE="$FIXTURE_SITE"
INSTALL_DIR="$TARGET"
APP_HOST=members.ssu-apps.link
install() { cp -- "${@: -2:1}" "${@: -1}"; }
caddy() {
    printf 'validate\n' >> "$CALL_LOG"
    [[ "$FAILURE" != validation ]]
}
systemctl() {
    printf '%s\n' "$*" >> "$CALL_LOG"
    [[ "$FAILURE" != reload ]]
}
configure_caddy
configure_caddy
""",
        tmp_path,
        FIXTURE_WORK=work.as_posix(),
        FIXTURE_ROOT=root.as_posix(),
        FIXTURE_SITE=site.as_posix(),
        TARGET=target.as_posix(),
        FAILURE=failure,
    )
    calls = (tmp_path / "calls").read_text().splitlines()
    assert calls[0] == "validate"
    if failure != "none":
        assert result.returncode != 0
        assert "restored" in result.stderr
        assert root.read_text() == original and site.read_text() == previous
    else:
        assert result.returncode == 0, result.stderr
        assert root.read_text().count("import ") == 1
        assert root.read_text().startswith(original.rstrip())
        assert "members.ssu-apps.link" in site.read_text()
        assert calls.count("reload-or-restart caddy") == 2


def test_model_validation_explains_the_failed_rule(deployment):
    _, target, write = deployment
    with pytest.raises(InstallError, match="SESSION_SECRET must be") as error:
        validate_environment(write(SESSION_SECRET="short-private-secret"), target)
    assert "short-private-secret" not in str(error.value)
    with pytest.raises(InstallError, match="Insecure session cookies"):
        validate_environment(write(SESSION_COOKIE_SECURE="false"), target)


def test_early_failure_does_not_suggest_missing_services(tmp_path):
    result = bash(
        "CURRENT_STEP='validating configuration and Python dependencies'; failed 1 199", tmp_path
    )
    assert result.returncode == 1
    assert "have not been installed" in result.stderr
    assert "systemctl status" not in result.stderr
