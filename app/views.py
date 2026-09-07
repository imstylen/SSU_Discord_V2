from app.admin_auth import csrf_token, is_admin


def render(request, template, status_code=200, **context):
    return request.app.state.templates.TemplateResponse(
        request=request,
        name=template,
        status_code=status_code,
        context={
            "app_name": request.app.state.settings.app_name,
            "admin": is_admin(request),
            "csrf_token": csrf_token(request),
            "flash": request.session.pop("flash", None),
            **context,
        },
    )


def flash(request, message, kind="success", member_id=None):
    request.session["flash"] = {"message": message, "kind": kind, "member_id": member_id}
