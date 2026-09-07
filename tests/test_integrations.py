import smtplib
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import httpx
import pytest
from conftest import member, oauth_start
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.backup import backup_database
from app.config import Settings
from app.discord_api import DiscordError, DiscordService
from app.discord_oauth import DiscordOAuth
from app.email_service import EmailError, EmailService
from app.main import create_app
from app.membership import MembershipError
from app.security import token_hash


@pytest.mark.parametrize(
    "status,body",
    [
        (401, {}),
        (403, {}),
        (500, {}),
        (404, {"code": 10004}),
        (404, {"code": 10011}),
        (200, {"roles": "invalid"}),
    ],
)
def test_discord_transport_errors_are_safe(env, status, body):
    client = httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(status, json=body))
    )
    service = DiscordService(env.settings, client)
    with pytest.raises(DiscordError):
        service.get_member("1001")
    service.close()


def test_timeout_is_wrapped(env):
    def timeout(request):
        raise httpx.ReadTimeout("secret should not be displayed", request=request)

    service = DiscordService(env.settings, httpx.Client(transport=httpx.MockTransport(timeout)))
    with pytest.raises(DiscordError, match="Discord request failed"):
        service.remove_verified_role("1001")
    service.close()


def test_rate_limit_retry_is_bounded(env, monkeypatch):
    sleeps = []
    monkeypatch.setattr("app.discord_api.time.sleep", sleeps.append)
    responses = [
        httpx.Response(429, json={"retry_after": 0.1}),
        httpx.Response(200, json={"roles": []}),
    ]
    service = DiscordService(
        env.settings, httpx.Client(transport=httpx.MockTransport(lambda _: responses.pop(0)))
    )
    assert service.get_member("1001") == {"roles": []}
    assert sleeps == [0.1]
    service.close()
    service = DiscordService(
        env.settings,
        httpx.Client(
            transport=httpx.MockTransport(lambda _: httpx.Response(429, json={"retry_after": 60}))
        ),
    )
    with pytest.raises(DiscordError):
        service.get_member("1001")
    assert sleeps == [0.1]
    service.close()


def test_smtp_tls_message_and_failure(env, monkeypatch):
    smtp = MagicMock()
    factory = MagicMock()
    factory.return_value.__enter__.return_value = smtp
    monkeypatch.setattr(smtplib, "SMTP_SSL", factory)
    env.settings.smtp_host = "smtp.example.com"
    env.settings.smtp_username = "user"
    service = EmailService(env.settings)
    service.send_invite("jane@example.com", "private-token")
    message = smtp.send_message.call_args.args[0]
    assert message["To"] == "jane@example.com"
    assert "http://localhost/join/private-token" in message.get_content()
    assert "automatically" in message.get_content()
    assert factory.call_args.kwargs["context"]
    smtp.login.assert_called_once()
    smtp.send_message.side_effect = smtplib.SMTPException("bad credentials")
    with pytest.raises(EmailError):
        service.send_invite("jane@example.com", "private-token")


def test_backup_captures_wal_and_retains_only_owned_files(env, tmp_path):
    member(env)
    destination = tmp_path / "backups"
    destination.mkdir()
    old_date = datetime.now(UTC) - timedelta(days=40)
    old = destination / f"ssu-{old_date.strftime('%Y%m%dT%H%M%S%fZ')}.sqlite3"
    old.write_text("old backup")
    unrelated = destination / "ssu-unrelated.sqlite3"
    unrelated.write_text("keep")
    target = backup_database(env.settings.database_url, destination)
    with sqlite3.connect(target) as connection:
        assert connection.execute("SELECT email FROM members").fetchall() == [("jane@example.com",)]
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert unrelated.exists() and not old.exists()


@pytest.mark.parametrize(
    "changes",
    [
        {"admin_password": "short"},
        {"session_secret": "short"},
        {"app_url": "http://public.example"},
        {"app_url": "https://public.example", "session_cookie_secure": False},
        {"smtp_use_ssl": True, "smtp_use_starttls": True},
        {"discord_invite_url": "javascript:alert(1)"},
    ],
)
def test_reject_unsafe_configuration(changes):
    values = {"admin_password": "valid-admin-password", "session_secret": "s" * 40, **changes}
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **values)


def test_secure_production_cookie(env):
    settings = env.settings.model_copy(
        update={"app_url": "https://members.example.com", "session_cookie_secure": True}
    )
    app = create_app(settings)
    with TestClient(app, base_url=settings.app_url) as client:
        response = client.get("/admin/login")
        cookie = response.headers["set-cookie"]
        assert "httponly" in cookie.lower() and "secure" in cookie.lower()
        assert "samesite=lax" in cookie.lower()
        assert response.headers["strict-transport-security"] == "max-age=31536000"


def test_untrusted_host_is_rejected(env):
    response = env.client.get("/admin/login", headers={"Host": "evil.example"})
    assert response.status_code == 400


def test_autojoin_failure_keeps_identity_and_offers_fallback(env):
    member_id, token = member(env)
    env.fake.join_fail = True
    state = oauth_start(env, token)
    response = env.client.get("/auth/discord/callback", params={"state": state, "code": "code"})
    assert "couldn’t add you to the server automatically" in response.text
    assert "Join the SSU Discord" in response.text
    assert env.service.get(member_id).discord_user_id == "1001"
    assert "1001" not in env.fake.members
    assert "ephemeral-secret" not in str(env.client.cookies)
    # Member can accept the invite later; bot reconciliation grants access.
    env.fake.members["1001"] = set()
    env.service.reconcile()
    assert env.fake.members["1001"] == {"300"}
    assert "Open SSU in Discord" in env.client.get(f"/join/{token}").text


def test_insufficient_oauth_consent_rejected_before_link_or_join(env):
    def handler(request):
        return httpx.Response(200, json={"access_token": "token", "scope": "identify"})

    oauth = DiscordOAuth(env.settings, httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(DiscordError):
        oauth.identify("code")
    oauth.close()


def test_deactivation_cannot_be_undone_by_inflight_sync(env):
    member_id, _ = member(env, linked=True)
    env.fake.members["1001"] = set()
    entered = threading.Event()
    release = threading.Event()
    original = env.service.discord.add_verified_role

    def slow_add(user_id):
        entered.set()
        assert release.wait(5)
        original(user_id)

    env.service.discord.add_verified_role = slow_add
    with ThreadPoolExecutor(max_workers=2) as pool:
        sync = pool.submit(env.service.sync, member_id)
        assert entered.wait(5)
        deactivate = pool.submit(env.service.set_active, member_id, False)
        release.set()
        sync.result(timeout=5)
        deactivate.result(timeout=5)
    assert not env.service.get(member_id).active
    assert env.fake.members["1001"] == set()


def test_reset_cannot_be_undone_by_inflight_sync(env):
    member_id, token = member(env, linked=True)
    env.fake.members["1001"] = set()
    entered = threading.Event()
    release = threading.Event()
    original = env.service.discord.add_verified_role

    def slow_add(user_id):
        entered.set()
        assert release.wait(5)
        original(user_id)

    env.service.discord.add_verified_role = slow_add
    with ThreadPoolExecutor(max_workers=2) as pool:
        sync = pool.submit(env.service.sync, member_id)
        assert entered.wait(5)
        reset = pool.submit(env.service.reset, member_id, token_hash(token))
        release.set()
        sync.result(timeout=5)
        reset.result(timeout=5)
    assert env.service.get(member_id).discord_user_id is None
    assert env.fake.members["1001"] == set()


def test_stale_reset_cannot_disconnect_new_account(env):
    member_id, old_token = member(env, linked=True)
    env.service.reset(member_id, token_hash(old_token))
    env.service.link(
        env.service.get(member_id).invite_token_hash, {"id": "1002", "username": "new"}
    )
    with pytest.raises(MembershipError, match="This member changed"):
        env.service.reset(member_id, token_hash(old_token))
    assert env.service.get(member_id).discord_user_id == "1002"
