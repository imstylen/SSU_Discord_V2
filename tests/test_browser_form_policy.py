import pytest
from conftest import csrf, member


def test_login_policy_and_same_origin_form_submission(env):
    page = env.client.get("/admin/login")
    # Unlike no-referrer, same-origin preserves Origin on browser form POSTs.
    assert page.headers["referrer-policy"] == "same-origin"
    response = env.client.post(
        "/admin/login",
        data={"password": "test-admin-password-long", "csrf_token": csrf(page)},
        headers={"Origin": env.settings.app_url},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/admin"
    assert env.client.get("/admin").status_code == 200


@pytest.mark.parametrize("origin", ["null", "https://evil.example", "http://localhost:9999"])
def test_hidden_and_foreign_origins_remain_rejected(env, origin):
    page = env.client.get("/admin/login")
    response = env.client.post(
        "/admin/login",
        data={"password": "test-admin-password-long", "csrf_token": csrf(page)},
        headers={"Origin": origin},
    )
    assert response.status_code == 403
    assert env.client.get("/admin", follow_redirects=False).status_code == 303


def test_sensitive_public_urls_still_suppress_referrers(env):
    _, token = member(env)
    for path in (f"/join/{token}", "/auth/discord/start", "/auth/discord/callback"):
        response = env.client.get(path, follow_redirects=False)
        assert response.headers["referrer-policy"] == "no-referrer"
