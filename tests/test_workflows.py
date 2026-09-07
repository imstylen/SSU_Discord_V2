import base64
import json
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, urlparse

import pytest
from conftest import csrf, member, oauth_start, post
from sqlalchemy import inspect, text

from app.membership import MembershipError
from app.models import Member
from app.security import token_hash
from bot.main import MembershipBot


def test_admin_requires_password_and_csrf(env):
    assert env.client.get("/admin", follow_redirects=False).status_code == 303
    assert (
        env.client.post(
            "/admin/members", data={"email": "jane@example.com"}, follow_redirects=False
        ).status_code
        == 303
    )
    page = env.client.get("/admin/login")
    assert (
        env.client.post(
            "/admin/login", data={"password": "wrong", "csrf_token": csrf(page)}
        ).status_code
        == 401
    )
    assert (
        env.client.post("/admin/login", data={"password": "test-admin-password-long"}).status_code
        == 403
    )


def test_create_normalize_and_duplicate_email(admin):
    response = post(admin, "/admin/members", email="  JANE@Example.com ")
    assert "Member created and registration email sent" in response.text
    assert admin.mailbox.sent[0][0] == "jane@example.com"
    response = post(admin, "/admin/members", email="Jane@example.com")
    assert "A member with this email already exists" in response.text
    assert "View member" in response.text
    assert len(admin.mailbox.sent) == 1
    assert "jane@example.com" in admin.client.get("/admin/members/1").text


def test_email_failure_keeps_record_and_resend_recovers(admin):
    admin.mailbox.fail = True
    response = post(admin, "/admin/members", email="jane@example.com")
    assert "Member was created, but the email could not be sent" in response.text
    assert admin.service.get(1).invite_sent_at is None
    admin.mailbox.fail = False
    assert "New invite sent" in post(admin, "/admin/members/1/resend").text
    assert admin.service.get(1).invite_sent_at


def test_tokens_are_hashed_permanent_and_invalid_tokens_are_safe(env):
    member_id, token = member(env)
    record = env.service.get(member_id)
    assert token != record.invite_token_hash == token_hash(token)
    assert "Connect Discord" in env.client.get(f"/join/{token}").text
    assert env.client.get("/join/invalid-token").status_code == 404
    assert env.client.get("/auth/discord/callback").status_code == 400
    with env.app.state.db.session() as session:
        assert session.scalar(text("PRAGMA journal_mode")) == "wal"
    assert inspect(env.app.state.db.engine).get_table_names() == ["members"]


@pytest.mark.parametrize("state", ["", "wrong", "☀"])
def test_oauth_state_must_match(env, state):
    member_id, token = member(env)
    oauth_start(env, token)
    response = env.client.get("/auth/discord/callback", params={"state": state, "code": "code"})
    assert response.status_code == 400
    assert env.service.get(member_id).discord_user_id is None
    assert not env.fake.oauth_calls


def test_oauth_links_and_verifies_existing_guild_member(env):
    member_id, token = member(env)
    env.fake.members["1001"] = set()
    state = oauth_start(env, token)
    response = env.client.get("/auth/discord/callback", params={"state": state, "code": "code"})
    assert "You’re connected!" in response.text
    assert env.service.get(member_id).discord_user_id == "1001"
    assert env.service.get(member_id).discord_linked_at
    assert env.fake.members["1001"] == {"300"}
    assert "ephemeral-secret" not in str(env.client.cookies)
    assert "Open SSU in Discord" in response.text
    assert (
        env.client.get(
            "/auth/discord/callback", params={"state": state, "code": "code"}
        ).status_code
        == 400
    )
    assert "Connect Discord" not in env.client.get(f"/join/{token}").text
    assert env.client.get("/auth/discord/start").status_code == 404


def test_oauth_uses_identify_and_join_and_discards_tokens(env):
    _, token = member(env)
    env.client.get(f"/join/{token}")
    response = env.client.get("/auth/discord/start", follow_redirects=False)
    query = parse_qs(urlparse(response.headers["location"]).query)
    assert query["scope"] == ["identify guilds.join"]
    assert query["redirect_uri"] == ["http://localhost/auth/discord/callback"]
    env.client.get(
        "/auth/discord/callback", params={"state": query["state"][0], "code": "test-code"}
    )
    token_request, profile_request = env.fake.oauth_calls
    assert token_request.headers["content-type"] == "application/x-www-form-urlencoded"
    assert profile_request.headers["authorization"] == "Bearer ephemeral-secret"
    join_request = env.fake.join_calls[0]
    assert join_request.headers["authorization"] == "Bot bot-secret"
    assert json.loads(join_request.content) == {"access_token": "ephemeral-secret"}
    assert "refresh_token" not in {
        c["name"] for c in inspect(env.app.state.db.engine).get_columns("members")
    }


def test_same_discord_cannot_link_twice(env):
    first, _ = member(env, linked=True)
    second, token = member(env, "other@example.com")
    state = oauth_start(env, token)
    response = env.client.get("/auth/discord/callback", params={"state": state, "code": "code"})
    assert "already connected to another SSU membership" in response.text
    assert env.service.get(first).discord_user_id == "1001"
    assert env.service.get(second).discord_user_id is None
    assert not env.fake.join_calls


def test_oauth_automatically_joins_and_verifies(env):
    _, token = member(env)
    state = oauth_start(env, token)
    assert (
        "Open SSU in Discord"
        in env.client.get("/auth/discord/callback", params={"state": state, "code": "code"}).text
    )
    assert env.fake.members["1001"] == {"300"}
    assert len(env.fake.join_calls) == 1


@pytest.mark.parametrize(
    "known,active,expected", [(True, True, {"300"}), (True, False, set()), (False, True, set())]
)
async def test_join_event(env, mocked_member, known, active, expected):
    if known:
        member(env, linked=True, active=active, in_guild=False)
    env.fake.members["1001"] = {"300"} if known and not active else set()
    bot = MembershipBot(env.settings, env.service)
    await bot.on_member_join(mocked_member)
    assert env.fake.members["1001"] == expected
    assert bot.intents.members and not bot.intents.message_content
    await bot.close()


async def test_join_ignores_other_guild(env, mocked_member):
    mocked_member.guild.id = 999
    bot = MembershipBot(env.settings, env.service)
    await bot.on_member_join(mocked_member)
    assert env.fake.calls == []
    await bot.close()


def test_deactivate_reactivate_preserves_link_and_syncs(admin):
    member_id, _ = member(admin, linked=True)
    post(admin, f"/admin/members/{member_id}/deactivate")
    assert admin.fake.members["1001"] == set()
    record = admin.service.get(member_id)
    assert not record.active and record.deactivated_at and record.discord_user_id == "1001"
    post(admin, f"/admin/members/{member_id}/reactivate")
    record = admin.service.get(member_id)
    assert record.active and record.deactivated_at is None
    assert admin.fake.members["1001"] == {"300"}


def test_deactivation_survives_discord_failure_and_reconciliation_repairs(admin):
    member_id, _ = member(admin, linked=True)
    admin.fake.fail = True
    response = post(admin, f"/admin/members/{member_id}/deactivate")
    assert "retried automatically" in response.text
    assert not admin.service.get(member_id).active
    admin.fake.fail = False
    assert admin.service.reconcile() == (1, 0)
    assert admin.fake.members["1001"] == set()


def test_reset_confirmation_revokes_and_rotates(admin):
    member_id, token = member(admin, linked=True)
    path = f"/admin/members/{member_id}/reset-discord"
    page = admin.client.get(path)
    assert "Reset this Discord link?" in page.text
    before = admin.service.get(member_id).invite_token_hash
    post(admin, path, expected_hash=before)
    assert admin.service.get(member_id).discord_user_id == "1001"
    response = post(admin, path, expected_hash=before, confirm="reset")
    assert "Discord link reset and new invite sent" in response.text
    record = admin.service.get(member_id)
    assert (
        record.discord_user_id
        is record.discord_username
        is record.discord_global_name
        is record.discord_linked_at
        is None
    )
    assert admin.fake.members["1001"] == set()
    assert admin.client.get(f"/join/{token}").status_code == 404
    assert record.invite_token_hash != before
    assert admin.mailbox.sent[-1][1] != token


def test_reset_failure_retains_trackable_association(admin):
    member_id, token = member(admin, linked=True)
    admin.fake.fail = True
    response = post(
        admin,
        f"/admin/members/{member_id}/reset-discord",
        expected_hash=token_hash(token),
        confirm="reset",
    )
    assert "The link was kept" in response.text
    assert admin.service.get(member_id).discord_user_id == "1001"
    assert admin.service.get(member_id).invite_token_hash == token_hash(token)
    assert len(admin.mailbox.sent) == 1


def test_resend_rotates_and_invalidates_inflight_oauth(admin):
    member_id, token = member(admin)
    state = oauth_start(admin, token)
    post(admin, f"/admin/members/{member_id}/resend")
    assert admin.client.get(f"/join/{token}").status_code == 404
    assert admin.client.get(f"/join/{admin.mailbox.sent[-1][1]}").status_code == 200
    assert (
        admin.client.get(
            "/auth/discord/callback", params={"state": state, "code": "code"}
        ).status_code
        == 400
    )
    assert admin.service.get(member_id).discord_user_id is None


def test_reconciliation_repairs_both_directions_and_keeps_other_roles(env):
    member_id, _ = member(env, linked=True)
    env.fake.members["1001"] = {"other-role"}
    assert env.service.reconcile() == (1, 0)
    assert env.fake.members["1001"] == {"300", "other-role"}
    env.service.set_active(member_id, False)
    env.fake.members["1001"].add("300")
    env.service.reconcile()
    assert env.fake.members["1001"] == {"other-role"}


def test_oauth_survives_discord_role_failure(env):
    member_id, token = member(env)
    env.fake.fail = True
    state = oauth_start(env, token)
    response = env.client.get("/auth/discord/callback", params={"state": state, "code": "code"})
    assert response.status_code == 200 and "Use the invite below" in response.text
    assert env.service.get(member_id).discord_user_id == "1001"


def test_inactive_cannot_claim_even_after_start(env):
    member_id, token = member(env)
    state = oauth_start(env, token)
    env.service.set_active(member_id, False)
    response = env.client.get("/auth/discord/callback", params={"state": state, "code": "code"})
    assert response.status_code == 400
    assert env.service.get(member_id).discord_user_id is None
    assert env.client.get(f"/join/{token}").status_code == 403
    assert not env.fake.join_calls


def test_email_correction_invalidates_old_link(admin):
    member_id, token = member(admin)
    post(admin, f"/admin/members/{member_id}/email", email=" NEW@Example.com ")
    assert admin.service.get(member_id).email == "new@example.com"
    assert admin.client.get(f"/join/{token}").status_code == 404
    assert admin.mailbox.sent[-1][0] == "new@example.com"


def test_search_escape_and_pagination(admin):
    member(admin, linked=True)
    assert "jane@example.com" in admin.client.get("/admin?q=janedoe").text
    assert "No matching members" in admin.client.get("/admin?q=%25").text
    response = admin.client.get("/admin?q=<script>alert(1)</script>")
    assert "<script>alert(1)</script>" not in response.text
    assert "&lt;script&gt;" in response.text
    with admin.app.state.db.write() as session:
        for i in range(55):
            session.add(
                Member(email=f"member{i:02}@example.com", invite_token_hash=token_hash(str(i)))
            )
    assert "Page 1 of 2" in admin.client.get("/admin").text
    assert "Page 2 of 2" in admin.client.get("/admin?page=2").text


def test_csrf_origin_logout_and_headers(admin):
    response = admin.client.get("/admin")
    assert response.headers["referrer-policy"] == "same-origin"
    assert response.headers["cache-control"] == "no-store"
    assert "script-src 'none'" in response.headers["content-security-policy"]
    for token in ("wrong", "☀"):
        assert admin.client.post("/admin/logout", data={"csrf_token": token}).status_code == 403
    assert (
        admin.client.post(
            "/admin/logout",
            data={"csrf_token": csrf(response)},
            headers={"Origin": "https://evil.example"},
        ).status_code
        == 403
    )
    post(admin, "/admin/logout")
    assert admin.client.get("/admin", follow_redirects=False).status_code == 303


def test_expired_oauth_state_is_rejected(env, monkeypatch):
    _, token = member(env)
    state = oauth_start(env, token)
    now = time.time()
    monkeypatch.setattr("app.routes.registration.time.time", lambda: now + 601)
    assert (
        env.client.get(
            "/auth/discord/callback", params={"state": state, "code": "code"}
        ).status_code
        == 400
    )


def test_login_throttled(env):
    token = csrf(env.client.get("/admin/login"))
    for _ in range(10):
        assert (
            env.client.post(
                "/admin/login", data={"csrf_token": token, "password": "wrong"}
            ).status_code
            == 401
        )
    assert (
        env.client.post("/admin/login", data={"csrf_token": token, "password": "wrong"}).status_code
        == 429
    )


def test_concurrent_claims_cannot_overwrite_membership(env):
    member_id, token = member(env)
    invite_hash = token_hash(token)

    def claim(user_id):
        try:
            env.service.link(invite_hash, {"id": user_id, "username": "name"})
            return True
        except MembershipError:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, ["1001", "1002"]))
    assert sorted(results) == [False, True]
    assert env.service.get(member_id).discord_user_id in {"1001", "1002"}


def test_signed_session_contains_no_raw_registration_token(env):
    _, token = member(env)
    oauth_start(env, token)
    cookie = env.client.cookies.get("ssu_session")
    payload = json.loads(base64.b64decode(cookie.split(".")[0]))
    assert token not in json.dumps(payload)
    assert "invite_hash" in payload["oauth"]


def test_password_change_invalidates_existing_session(admin):
    from pydantic import SecretStr

    admin.settings.admin_password = SecretStr("a-new-admin-password-long")
    assert admin.client.get("/admin", follow_redirects=False).status_code == 303


def test_oauth_cancel_can_retry(env):
    _, token = member(env)
    state = oauth_start(env, token)
    response = env.client.get(
        "/auth/discord/callback", params={"state": state, "error": "access_denied"}
    )
    assert "cancelled" in response.text
    assert env.client.get("/auth/discord/start", follow_redirects=False).status_code == 303
