import re
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.discord_api import DiscordService
from app.discord_oauth import DiscordOAuth
from app.email_service import EmailError
from app.main import create_app


class Mailbox:
    def __init__(self):
        self.sent = []
        self.fail = False

    def send_invite(self, email, token):
        if self.fail:
            raise EmailError("SMTP down")
        self.sent.append((email, token))


class FakeDiscord:
    def __init__(self):
        self.members = {}
        self.calls = []
        self.fail = False
        self.user = {"id": "1001", "username": "janedoe", "global_name": "Jane"}
        self.oauth_calls = []
        self.join_calls = []
        self.join_fail = False

    def handle(self, request):
        path = request.url.path
        if "oauth2/token" in path:
            self.oauth_calls.append(request)
            return httpx.Response(
                200,
                json={
                    "access_token": "ephemeral-secret",
                    "refresh_token": "discard",
                    "scope": "identify guilds.join",
                },
            )
        if path.endswith("/users/@me"):
            self.oauth_calls.append(request)
            return httpx.Response(200, json=self.user)
        self.calls.append((request.method, path))
        if self.fail:
            return httpx.Response(503)
        parts = path.split("/")
        user_id = parts[parts.index("members") + 1]
        if request.method == "PUT" and "/roles/" not in path:
            self.join_calls.append(request)
            if self.join_fail:
                return httpx.Response(403, json={"code": 50013})
            if user_id in self.members:
                return httpx.Response(204)
            self.members[user_id] = set()
            return httpx.Response(201, json={"roles": []})
        if user_id not in self.members:
            return httpx.Response(404, json={"code": 10007, "message": "Unknown Member"})
        roles = self.members[user_id]
        if request.method == "GET":
            return httpx.Response(200, json={"roles": sorted(roles)})
        if request.method == "PUT":
            roles.add(parts[-1])
        elif request.method == "DELETE":
            roles.discard(parts[-1])
        return httpx.Response(204)


def csrf(response):
    return re.search(r'name="csrf_token" value="([^"]+)"', response.text).group(1)


@pytest.fixture
def env(tmp_path):
    settings = Settings(
        _env_file=None,
        app_url="http://localhost",
        session_cookie_secure=False,
        admin_password="test-admin-password-long",
        session_secret="s" * 48,
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        discord_client_id="123",
        discord_client_secret="oauth-secret",
        discord_bot_token="bot-secret",
        discord_guild_id="200",
        discord_verified_role_id="300",
    )
    fake = FakeDiscord()
    mailbox = Mailbox()
    transport = httpx.MockTransport(fake.handle)
    app = create_app(
        settings,
        discord=DiscordService(settings, httpx.Client(transport=transport)),
        oauth=DiscordOAuth(settings, httpx.Client(transport=transport)),
        email=mailbox,
    )
    with TestClient(app, base_url="http://localhost") as client:
        yield SimpleNamespace(
            app=app,
            client=client,
            fake=fake,
            mailbox=mailbox,
            service=app.state.membership,
            settings=settings,
        )


@pytest.fixture
def admin(env):
    response = env.client.get("/admin/login")
    env.client.post(
        "/admin/login", data={"csrf_token": csrf(response), "password": "test-admin-password-long"}
    )
    return env


def post(env, path, **data):
    data["csrf_token"] = csrf(env.client.get("/admin"))
    return env.client.post(path, data=data)


def member(env, email="jane@example.com", *, linked=False, active=True, in_guild=True):
    member_id, _ = env.service.create(email)
    if linked:
        if in_guild:
            env.fake.members["1001"] = set()
        env.service.link(env.service.get(member_id).invite_token_hash, env.fake.user)
    if not active:
        env.service.set_active(member_id, False)
    return member_id, env.mailbox.sent[-1][1]


def oauth_start(env, token):
    from urllib.parse import parse_qs, urlparse

    env.client.get(f"/join/{token}")
    response = env.client.get("/auth/discord/start", follow_redirects=False)
    assert response.status_code == 303
    return parse_qs(urlparse(response.headers["location"]).query)["state"][0]


@pytest.fixture
def mocked_member():
    return Mock(id=1001, guild=Mock(id=200))
