import hashlib
import hmac
import secrets
import threading
import time
from collections import OrderedDict

from fastapi import HTTPException, Request


def admin_signature(settings):
    # Changing the password invalidates previously issued admin sessions.
    return hmac.new(
        settings.session_secret.get_secret_value().encode(),
        settings.admin_password.get_secret_value().encode(),
        hashlib.sha256,
    ).hexdigest()


def is_admin(request):
    signature = request.session.get("admin", "")
    return (
        isinstance(signature, str)
        and hmac.compare_digest(signature, admin_signature(request.app.state.settings))
        and time.time() - request.session.get("admin_at", 0) < 7 * 86400
    )


def require_admin(request: Request):
    if not is_admin(request):
        raise HTTPException(303, headers={"Location": "/admin/login"})


def csrf_token(request):
    if "csrf" not in request.session:
        request.session["csrf"] = secrets.token_urlsafe(32)
    return request.session["csrf"]


async def check_csrf(request: Request):
    form = await request.form()
    submitted = form.get("csrf_token", "")
    expected = request.session.get("csrf", "")
    if (
        not expected
        or not isinstance(submitted, str)
        or not hmac.compare_digest(expected.encode(), submitted.encode())
    ):
        raise HTTPException(403, "This form expired. Reload the page and try again.")
    origin = request.headers.get("origin")
    if origin and origin != request.app.state.settings.app_url:
        raise HTTPException(403, "Invalid form origin.")


async def admin_post(request: Request):
    require_admin(request)
    await check_csrf(request)


class LoginLimiter:
    def __init__(self):
        self.attempts = OrderedDict()
        self.lock = threading.Lock()

    def allow(self, address):
        now = time.monotonic()
        with self.lock:
            recent = [t for t in self.attempts.pop(address, []) if now - t < 900]
            allowed = len(recent) < 10
            if allowed:
                recent.append(now)
            self.attempts[address] = recent
            while len(self.attempts) > 10000:
                self.attempts.popitem(last=False)
            return allowed

    def clear(self, address):
        with self.lock:
            self.attempts.pop(address, None)
