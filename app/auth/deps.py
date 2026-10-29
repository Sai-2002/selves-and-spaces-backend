from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from supabase_auth.errors import AuthApiError, AuthError

from app.auth.roles import Role, roles_from_app_metadata
from app.config import get_settings
from app.supabase_client import get_supabase
from app.users.service import is_banned

bearer_scheme = HTTPBearer()


class CurrentUser(BaseModel):
    id: str
    email: str | None
    roles: list[Role]
    # Supabase "anonymous sign-ins" yield a valid token with no real identity behind it.
    is_anonymous: bool = False


def _unauthorized() -> HTTPException:
    return HTTPException(
        status.HTTP_401_UNAUTHORIZED,
        "Invalid or expired token",
        headers={"WWW-Authenticate": "Bearer"},
    )


def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(bearer_scheme)],
) -> CurrentUser:
    """Validate the Supabase access token and build the current user from its claims."""
    try:
        result = get_supabase().auth.get_claims(credentials.credentials)
    except AuthError:
        result = None
    if result is None:
        raise _unauthorized()

    claims = result["claims"]
    roles = roles_from_app_metadata(claims.get("app_metadata"))
    return CurrentUser(
        id=claims["sub"],
        email=claims.get("email"),
        roles=roles,
        is_anonymous=bool(claims.get("is_anonymous", False)),
    )


def _load_active_user(user: CurrentUser):
    """Re-read the account from Supabase: token claims can be up to an hour stale."""
    try:
        fresh = get_supabase().auth.admin.get_user_by_id(user.id).user
    except AuthApiError as e:
        if e.status == 404:  # account deleted since the token was issued
            raise _unauthorized() from e
        raise
    if is_banned(fresh):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Account is deactivated")
    return fresh


def get_active_customer(user: Annotated[CurrentUser, Depends(get_current_user)]) -> CurrentUser:
    """A real, currently-active account. Use on routes that move money or create records.

    Rejects anonymous sessions, unconfirmed emails, and accounts that were deactivated or deleted
    after their token was issued (a valid JWT alone is not enough).
    """
    if user.is_anonymous:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Sign in with an account to continue")
    fresh = _load_active_user(user)
    # Supabase's "Confirm email" setting already blocks unconfirmed sign-ins; this keeps payments
    # safe even if that setting is ever switched off. Disabled via REQUIRE_CONFIRMED_EMAIL=false
    # for the no-email v1.
    if get_settings().require_confirmed_email and not fresh.email_confirmed_at:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Confirm your email to continue")
    return user


def require_roles(*allowed: Role):
    """Allow only users holding one of `allowed`.

    Token claims can be up to an hour stale, so privileged routes re-read the user from
    Supabase: a deactivated user or a revoked role is locked out immediately.
    """

    def checker(user: Annotated[CurrentUser, Depends(get_current_user)]) -> CurrentUser:
        fresh = _load_active_user(user)
        current = user.model_copy(update={"roles": roles_from_app_metadata(fresh.app_metadata)})
        if not set(current.roles) & set(allowed):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Insufficient permissions")
        return current

    return checker
