"""Browser-only GitHub OAuth endpoints."""

import base64
import hashlib
import secrets
from urllib.parse import quote, urlencode

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse

from astrbot.dashboard.api.auth import _auth_service_response, get_auth_service
from astrbot.dashboard.services.auth_service import AuthService, AuthServiceResult
from astrbot.dashboard.services.github_oauth import password_login_enabled

router = APIRouter(tags=["Auth"])
COOKIE_PATH = "/"


@router.get("/auth/github/options")
async def github_options(service: AuthService = Depends(get_auth_service)):
    return {
        "enabled": service.github_oauth.enabled,
        "password_login_enabled": password_login_enabled(),
    }


@router.get("/auth/github/login")
async def github_login(service: AuthService = Depends(get_auth_service)):
    oauth = service.github_oauth
    if not oauth.enabled:
        return JSONResponse({"message": "GitHub OAuth is disabled"}, status_code=404)
    verifier = secrets.token_urlsafe(48)
    try:
        state = oauth.issue("verifier:" + verifier)
    except ValueError:
        return JSONResponse({"message": "Too many pending logins"}, status_code=429)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    response = RedirectResponse(
        "https://github.com/login/oauth/authorize?"
        + urlencode(
            {
                "client_id": oauth.client_id,
                "redirect_uri": oauth.redirect_uri,
                "scope": "read:org",
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        ),
        status_code=302,
    )
    response.set_cookie(
        oauth.state_cookie,
        state,
        max_age=300,
        secure=oauth.secure_cookie,
        httponly=True,
        samesite="lax",
        path=COOKIE_PATH,
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/auth/github/callback")
async def github_callback(
    request: Request, service: AuthService = Depends(get_auth_service)
):
    """Validate the browser callback and check active organization membership.

    Args:
        request: GitHub's browser redirect containing state and authorization code.
        service: Dashboard authentication service with pending browser state.

    Returns:
        A fixed login-page redirect, with a single-use cookie ticket on success.
    """
    oauth = service.github_oauth
    state = request.query_params.get("state", "")
    cookie = request.cookies.get(oauth.state_cookie, "")
    response = RedirectResponse("/auth/login?oauth=error", status_code=303)
    response.delete_cookie(
        oauth.state_cookie,
        path=COOKIE_PATH,
        secure=oauth.secure_cookie,
        httponly=True,
        samesite="lax",
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    if (
        not oauth.enabled
        or not state
        or not secrets.compare_digest(state.encode(), cookie.encode())
    ):
        return response
    stored = oauth.consume(state)
    code = request.query_params.get("code", "")
    if (
        not stored
        or not stored.startswith("verifier:")
        or not code
        or request.query_params.get("error")
    ):
        return response
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
            exchange = await client.post(
                "https://github.com/login/oauth/access_token",
                data={
                    "client_id": oauth.client_id,
                    "client_secret": oauth.client_secret,
                    "code": code,
                    "redirect_uri": oauth.redirect_uri,
                    "code_verifier": stored.removeprefix("verifier:"),
                },
                headers={"Accept": "application/json"},
            )
            exchange.raise_for_status()
            access_token = exchange.json().get("access_token")
            if not isinstance(access_token, str) or not access_token:
                return response
            headers = {
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            }
            user_response = await client.get(
                "https://api.github.com/user", headers=headers
            )
            user_response.raise_for_status()
            user_data = user_response.json()
            user_id = user_data.get("id")
            if type(user_id) is not int or user_id <= 0:
                return response
            allowed = str(user_data.get("login", "")).lower() in oauth.users
            for org in sorted(oauth.organizations):
                if allowed:
                    break
                membership = await client.get(
                    f"https://api.github.com/user/memberships/orgs/{quote(org, safe='')}",
                    headers=headers,
                )
                if membership.status_code == 404:
                    continue
                membership.raise_for_status()
                if membership.json().get("state") == "active":
                    allowed = True
                    break
            if not allowed:
                return response
        ticket = oauth.issue(f"identity:github:{user_id}")
    except (httpx.HTTPError, ValueError, TypeError, AttributeError):
        # Never expose authorization codes, tokens, secrets or GitHub responses.
        return response
    response.headers["location"] = "/auth/login?oauth=complete"
    response.set_cookie(
        oauth.ticket_cookie,
        ticket,
        max_age=300,
        secure=oauth.secure_cookie,
        httponly=True,
        samesite="strict",
        path=COOKIE_PATH,
    )
    return response


@router.post("/auth/github/session")
async def github_session(
    request: Request, service: AuthService = Depends(get_auth_service)
):
    oauth = service.github_oauth
    identity = (
        oauth.consume(request.cookies.get(oauth.ticket_cookie, ""))
        if oauth.enabled
        else None
    )
    if not identity or not identity.startswith("identity:github:"):
        return JSONResponse(
            {"message": "OAuth session expired; sign in again"}, status_code=401
        )
    username = identity.removeprefix("identity:")
    token = service.generate_jwt(username, auth_source="github")
    response = _auth_service_response(
        request,
        AuthServiceResult(
            data={"username": username, "token": token},
            jwt_token=token,
        ),
    )
    response.delete_cookie(
        oauth.ticket_cookie,
        path=COOKIE_PATH,
        secure=oauth.secure_cookie,
        httponly=True,
        samesite="strict",
    )
    response.headers["Cache-Control"] = "no-store"
    return response
