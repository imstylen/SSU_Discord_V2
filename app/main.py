import logging
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.exc import OperationalError
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.admin_auth import LoginLimiter
from app.config import Settings
from app.database import Database
from app.discord_api import DiscordService
from app.discord_oauth import DiscordOAuth
from app.email_service import EmailService
from app.membership import MembershipService
from app.routes import admin, registration
from app.views import render


def create_app(settings=None, *, database=None, discord=None, email=None, oauth=None):
    settings = settings or Settings()
    db = database or Database(settings.database_url)
    discord = discord or DiscordService(settings)
    oauth = oauth or DiscordOAuth(settings)

    @asynccontextmanager
    async def lifespan(application):
        db.initialize()
        # HTTP client logs can contain OAuth codes or sensitive registration URLs.
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
        yield
        discord.close()
        oauth.close()
        db.engine.dispose()

    app = FastAPI(
        title=settings.app_name, lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None
    )
    app.state.settings = settings
    app.state.db = db
    app.state.discord = discord
    app.state.oauth = oauth
    app.state.membership = MembershipService(db, discord, email or EmailService(settings))
    app.state.login_limiter = LoginLimiter()
    directory = Path(__file__).parent
    app.state.templates = Jinja2Templates(directory=directory / "templates")
    app.mount("/static", StaticFiles(directory=directory / "static"), name="static")
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret.get_secret_value(),
        session_cookie="ssu_session",
        max_age=7 * 86400,
        same_site="lax",
        https_only=settings.session_cookie_secure,
    )
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=[urlparse(settings.app_url).hostname],
        www_redirect=False,
    )

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        # no-referrer makes browsers send Origin: null on ordinary form POSTs.
        # Admin forms need their same-origin Origin header for CSRF validation;
        # public registration/OAuth URLs must never be sent as referrers.
        admin_page = request.url.path == "/admin" or request.url.path.startswith("/admin/")
        response.headers["Referrer-Policy"] = "same-origin" if admin_page else "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self'; img-src 'self'; "
            "script-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        if settings.session_cookie_secure:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response

    @app.exception_handler(OperationalError)
    async def database_busy(request, exc):
        logging.getLogger(__name__).error("Database operation failed (%s)", type(exc).__name__)
        return render(
            request,
            "error.html",
            503,
            message="The membership database is temporarily unavailable. Please retry shortly.",
        )

    @app.get("/")
    def root():
        return RedirectResponse("/admin", status_code=303)

    @app.get("/healthz")
    def health():
        from sqlalchemy import text

        with db.session() as session:
            session.execute(text("SELECT 1 FROM members LIMIT 1"))
        return {"status": "ok"}

    app.include_router(admin.router)
    app.include_router(registration.router)
    return app


def __getattr__(name):
    # Keep the app factory importable by tests/tools without production secrets.
    if name == "app":
        return create_app()
    raise AttributeError(name)
