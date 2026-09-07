import hmac
import secrets
import time
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, or_, select

from app.admin_auth import admin_post, admin_signature, check_csrf, is_admin, require_admin
from app.membership import MembershipError
from app.models import Member
from app.views import flash, render

router = APIRouter()
Post = Depends(admin_post)


def back(member_id=None):
    return RedirectResponse(
        f"/admin#member-{member_id}" if member_id else "/admin", status_code=303
    )


@router.get("/admin/login")
def login_page(request: Request):
    if is_admin(request):
        return back()
    return render(request, "admin_login.html")


@router.post("/admin/login", dependencies=[Depends(check_csrf)])
def login(request: Request, password: Annotated[str, Form()]):
    address = request.client.host if request.client else "unknown"
    limiter = request.app.state.login_limiter
    if not limiter.allow(address):
        return render(
            request, "admin_login.html", 429, error="Too many attempts. Try again in 15 minutes."
        )
    if not hmac.compare_digest(
        password.encode(), request.app.state.settings.admin_password.get_secret_value().encode()
    ):
        return render(request, "admin_login.html", 401, error="Incorrect password.")
    limiter.clear(address)
    request.session.clear()
    request.session.update(
        admin=admin_signature(request.app.state.settings),
        admin_at=time.time(),
        csrf=secrets.token_urlsafe(32),
    )
    return back()


@router.post("/admin/logout", dependencies=[Post])
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/admin/login", status_code=303)


@router.get("/admin", dependencies=[Depends(require_admin)])
def dashboard(request: Request, q: str = "", page: int = 1):
    q = q.strip()[:200]
    page = max(1, page)
    with request.app.state.db.session() as session:
        statement = select(Member)
        if q:
            statement = statement.where(
                or_(
                    Member.email.contains(q, autoescape=True),
                    Member.discord_username.contains(q, autoescape=True),
                    Member.discord_global_name.contains(q, autoescape=True),
                    Member.discord_user_id.contains(q, autoescape=True),
                )
            )
        count = session.scalar(select(func.count()).select_from(statement.subquery()))
        pages = max(1, (count + 49) // 50)
        page = min(page, pages)
        members = list(
            session.scalars(
                statement.order_by(Member.active.desc(), Member.email)
                .offset((page - 1) * 50)
                .limit(50)
            )
        )
        active_count = session.scalar(
            select(func.count()).select_from(Member).where(Member.active.is_(True))
        )
        total = session.scalar(select(func.count()).select_from(Member))
    return render(
        request,
        "admin_dashboard.html",
        members=members,
        q=q,
        page=page,
        pages=pages,
        count=count,
        active_count=active_count,
        inactive_count=total - active_count,
    )


@router.get("/admin/members/{member_id}", dependencies=[Depends(require_admin)])
def view_member(request: Request, member_id: int):
    try:
        member = request.app.state.membership.get(member_id)
    except MembershipError as exc:
        return render(request, "error.html", 404, message=str(exc))
    return RedirectResponse(f"/admin?q={quote(member.email)}#member-{member.id}", status_code=303)


@router.post("/admin/members", dependencies=[Post])
def create(request: Request, email: Annotated[str, Form()]):
    try:
        member_id, sent = request.app.state.membership.create(email)
    except MembershipError as exc:
        flash(request, str(exc), "error", exc.member_id)
        return back(exc.member_id)
    flash(
        request,
        "Member created and registration email sent."
        if sent
        else "Member was created, but the email could not be sent. Use Resend invite to try again.",
        "success" if sent else "error",
    )
    return back(member_id)


@router.post("/admin/members/{member_id}/email", dependencies=[Post])
def edit_email(request: Request, member_id: int, email: Annotated[str, Form()]):
    try:
        sent = request.app.state.membership.edit_email(member_id, email)
        message = "Email updated."
        if sent is not None:
            message += (
                " A new registration link was sent; the old link is invalid."
                if sent
                else " The old link is invalid, but email delivery failed. Use Resend invite."
            )
        flash(request, message, "error" if sent is False else "success")
    except MembershipError as exc:
        flash(request, str(exc), "error", exc.member_id)
    return back(member_id)


@router.post("/admin/members/{member_id}/resend", dependencies=[Post])
def resend(request: Request, member_id: int):
    try:
        sent = request.app.state.membership.resend(member_id)
        flash(
            request,
            "New invite sent. The old link is now invalid."
            if sent
            else "The old link is invalid, but email delivery failed. Use Resend invite to retry.",
            "success" if sent else "error",
        )
    except MembershipError as exc:
        flash(request, str(exc), "error")
    return back(member_id)


def change_active(request, member_id, active):
    try:
        result = request.app.state.membership.set_active(member_id, active)
        message = "Member reactivated." if active else "Member deactivated."
        if result == "failed":
            message += (
                " Discord role synchronization failed and will be retried automatically by the bot."
            )
        flash(request, message, "error" if result == "failed" else "success")
    except MembershipError as exc:
        flash(request, str(exc), "error")
    return back(member_id)


@router.post("/admin/members/{member_id}/deactivate", dependencies=[Post])
def deactivate(request: Request, member_id: int):
    return change_active(request, member_id, False)


@router.post("/admin/members/{member_id}/reactivate", dependencies=[Post])
def reactivate(request: Request, member_id: int):
    return change_active(request, member_id, True)


@router.get("/admin/members/{member_id}/reset-discord", dependencies=[Depends(require_admin)])
def reset_page(request: Request, member_id: int):
    try:
        member = request.app.state.membership.get(member_id)
    except MembershipError as exc:
        return render(request, "error.html", 404, message=str(exc))
    if not member.discord_user_id:
        flash(request, "This member has no Discord account to reset.", "error")
        return back(member_id)
    return render(request, "reset_discord.html", member=member)


@router.post("/admin/members/{member_id}/reset-discord", dependencies=[Post])
def reset(
    request: Request,
    member_id: int,
    expected_hash: Annotated[str, Form()],
    confirm: Annotated[str, Form()] = "",
):
    if confirm != "reset":
        flash(request, "Confirm the reset before continuing.", "error")
        return RedirectResponse(f"/admin/members/{member_id}/reset-discord", status_code=303)
    try:
        sent = request.app.state.membership.reset(member_id, expected_hash)
        flash(
            request,
            "Discord link reset and new invite sent."
            if sent
            else (
                "Discord link reset, but email delivery failed. "
                "Use Resend invite after reactivation if needed."
            ),
            "success" if sent else "error",
        )
    except MembershipError as exc:
        flash(request, str(exc), "error")
    return back(member_id)
