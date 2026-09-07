import hmac
import secrets
import time

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from app.discord_api import DiscordError
from app.membership import MembershipError
from app.security import token_hash
from app.views import render

router = APIRouter()


def unavailable(request):
    return render(
        request,
        "error.html",
        404,
        message="This registration link is unavailable. Please contact SSU for assistance.",
    )


def complete(request, member):
    if not member.active:
        return render(
            request,
            "error.html",
            403,
            message="Your membership is inactive. Please contact SSU for assistance.",
        )
    lookup_failed = False
    try:
        in_guild = request.app.state.discord.get_member(member.discord_user_id) is not None
    except DiscordError:
        in_guild = False
        lookup_failed = True
    return render(
        request,
        "join_complete.html",
        member=member,
        invite_url=request.app.state.settings.discord_invite_url,
        server_url=f"https://discord.com/channels/{request.app.state.settings.discord_guild_id}",
        in_guild=in_guild,
        lookup_failed=lookup_failed,
        join_failed=request.session.pop("join_failed", False),
        sync_failed=request.session.pop("sync_failed", False),
    )


@router.get("/join/complete")
def join_complete(request: Request):
    member = request.app.state.membership.by_hash(request.session.get("completed_hash", ""))
    if not member or not member.discord_user_id:
        return unavailable(request)
    return complete(request, member)


@router.get("/join/{token}")
def join(request: Request, token: str):
    if len(token) > 200:
        return unavailable(request)
    member = request.app.state.membership.by_hash(token_hash(token))
    if not member:
        return unavailable(request)
    if member.discord_user_id:
        return complete(request, member)
    if not member.active:
        return render(
            request,
            "error.html",
            403,
            message="Your membership is inactive. Please contact SSU for assistance.",
        )
    request.session["join_hash"] = member.invite_token_hash
    return render(request, "join.html")


@router.get("/auth/discord/start")
def oauth_start(request: Request):
    invite_hash = request.session.get("join_hash", "")
    member = request.app.state.membership.by_hash(invite_hash)
    if not member or not member.active or member.discord_user_id:
        return unavailable(request)
    state = secrets.token_urlsafe(32)
    try:
        url = request.app.state.oauth.authorization_url(state)
    except DiscordError:
        return render(
            request,
            "error.html",
            503,
            message="Discord connection is unavailable. Please contact SSU.",
        )
    request.session["oauth"] = {"state": state, "created": time.time(), "invite_hash": invite_hash}
    return RedirectResponse(url, status_code=303)


@router.get("/auth/discord/callback")
def oauth_callback(request: Request, state: str = "", code: str = "", error: str = ""):
    pending = request.session.pop("oauth", None)
    if (
        not pending
        or not state
        or not hmac.compare_digest(state.encode(), pending["state"].encode())
        or not 0 <= time.time() - pending["created"] <= 600
    ):
        return render(
            request,
            "error.html",
            400,
            message=(
                "Your Discord connection session expired or was invalid. "
                "Open your registration link again."
            ),
        )
    if error or not code:
        return render(
            request,
            "error.html",
            400,
            message="Discord connection was cancelled. You can try again.",
            retry=True,
        )
    try:
        user, access_token = request.app.state.oauth.identify(code)
        _, result = request.app.state.membership.link(pending["invite_hash"], user, access_token)
    except (DiscordError, MembershipError) as exc:
        return render(request, "error.html", 400, message=str(exc), retry=True)
    request.session["completed_hash"] = pending["invite_hash"]
    request.session["sync_failed"] = result == "failed"
    request.session["join_failed"] = result == "join_failed"
    request.session.pop("join_hash", None)
    return RedirectResponse("/join/complete", status_code=303)
