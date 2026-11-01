from __future__ import annotations

import hmac

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from reqpilotx_server.saas import store


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(min_length=3, max_length=254, pattern=r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
    password: str = Field(min_length=12, max_length=256)


class RegistrationRequest(LoginRequest):
    invitation: str = Field(min_length=32, max_length=512)


def check_origin(request: Request, *, required: bool = False) -> None:
    settings = request.app.state.saas_settings
    origin = request.headers.get("origin")
    if (required and not origin) or (origin and origin != settings.origin):
        raise HTTPException(403, "saas_origin_rejected")


def require_session(request: Request) -> dict:
    settings = request.app.state.saas_settings
    check_origin(request)
    result = store.read_session(request.cookies.get(settings.cookie_name, ""))
    if result is None:
        raise HTTPException(401, "saas_session_required")
    return result


def require_csrf(request: Request, session: dict) -> None:
    check_origin(request, required=True)
    if not hmac.compare_digest(request.headers.get("x-reqpilot-csrf", ""), session["csrf_token"]):
        raise HTTPException(403, "saas_csrf_rejected")


def _view(user=None, csrf=None, *, enabled=True):
    return {"enabled": enabled, "user": user, "csrf_token": csrf}


def _login_response(request: Request, response: Response, user: dict):
    settings = request.app.state.saas_settings
    token, csrf = store.issue_session(user["id"], settings.session_seconds,
                                     request.cookies.get(settings.cookie_name, ""))
    response.set_cookie(settings.cookie_name, token, max_age=settings.session_seconds,
                        secure=settings.secure_cookie, httponly=True, samesite="strict", path="/")
    response.headers["Cache-Control"] = "no-store"
    return _view(user, csrf)


def build_auth_router() -> APIRouter:
    router = APIRouter(tags=["saas-auth"])

    @router.get("/api/auth/session")
    def session(request: Request, response: Response):
        response.headers["Cache-Control"] = "no-store"
        settings = request.app.state.saas_settings
        if not settings.enabled:
            return _view(enabled=False)
        check_origin(request)
        result = store.read_session(request.cookies.get(settings.cookie_name, ""))
        if result is None:
            return _view()
        return _view({"id": result["id"], "email": result["email"]}, result["csrf_token"])

    @router.post("/api/auth/register", status_code=201)
    def register(payload: RegistrationRequest, request: Request, response: Response):
        settings = request.app.state.saas_settings
        if not settings.enabled:
            raise HTTPException(404, "saas_not_enabled")
        check_origin(request, required=True)
        email = payload.email.strip().casefold()
        peer = request.client.host if request.client else "unknown"
        if not store.throttle("register", peer, email):
            raise HTTPException(429, "saas_auth_rate_limited")
        if not store.invitation_valid(payload.invitation):
            raise HTTPException(403, "saas_invitation_rejected")
        with store.db.get_conn() as conn:
            if conn.execute("SELECT 1 FROM saas_accounts WHERE email=?", (email,)).fetchone():
                raise HTTPException(409, "saas_account_exists")
        try:
            user = store.create_account(email, payload.password, payload.invitation)
        except store.InvitationRejected:
            raise HTTPException(403, "saas_invitation_rejected") from None
        except Exception as exc:
            # The unique email key arbitrates concurrent registration. Do not
            # leak SQL details, passwords or infrastructure errors to callers.
            with store.db.get_conn() as conn:
                duplicate = conn.execute("SELECT 1 FROM saas_accounts WHERE email=?", (email,)).fetchone()
            if duplicate:
                raise HTTPException(409, "saas_account_exists") from exc
            raise
        return _login_response(request, response, user)

    @router.post("/api/auth/login")
    def login(payload: LoginRequest, request: Request, response: Response):
        settings = request.app.state.saas_settings
        if not settings.enabled:
            raise HTTPException(404, "saas_not_enabled")
        check_origin(request, required=True)
        email = payload.email.strip().casefold()
        peer = request.client.host if request.client else "unknown"
        if not store.throttle("login", peer, email):
            raise HTTPException(429, "saas_auth_rate_limited")
        user = store.authenticate(email, payload.password)
        if user is None:
            raise HTTPException(401, "saas_credentials_rejected")
        return _login_response(request, response, user)

    @router.post("/api/auth/logout")
    def logout(request: Request, response: Response):
        settings = request.app.state.saas_settings
        if not settings.enabled:
            raise HTTPException(404, "saas_not_enabled")
        require_csrf(request, require_session(request))
        store.revoke_session(request.cookies.get(settings.cookie_name, ""))
        response.delete_cookie(settings.cookie_name, secure=settings.secure_cookie,
                               httponly=True, samesite="strict", path="/")
        response.headers["Cache-Control"] = "no-store"
        return _view()

    from reqpilotx_server.saas.config import SaaSSettings
    if SaaSSettings.from_environment().enabled:
        @router.get("/api/healthz", include_in_schema=False)
        def saas_health():
            try:
                with store.db.get_conn() as conn:
                    ready = conn.execute("SELECT 1 AS ready").fetchone()
                if ready is None or int(ready["ready"]) != 1:
                    raise RuntimeError("database_not_ready")
            except Exception:
                return JSONResponse({"status": "unavailable", "authentication": "required"}, status_code=503)
            return {"status": "ok", "authentication": "required"}
    return router
