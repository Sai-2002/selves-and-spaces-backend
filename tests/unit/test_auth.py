"""Unit tests for token validation and role checks. No network: Supabase is faked."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from supabase_auth.errors import AuthApiError, AuthInvalidJwtError

from app.main import app


class FakeAuth:
    def __init__(self, claims=None, error=None):
        self._claims = claims
        self._error = error
        self.seen_token = None

    def get_claims(self, token):
        self.seen_token = token
        if self._error:
            raise self._error
        return None if self._claims is None else {"claims": self._claims}


class FakeAdmin:
    """Serves the live user record. Defaults to mirroring the token's own claims."""

    def __init__(self, auth, fresh=None, error=None):
        self._auth = auth
        self._fresh = fresh
        self._error = error

    def get_user_by_id(self, uid):
        if self._error:
            raise self._error
        if self._fresh is not None:
            return SimpleNamespace(user=self._fresh)
        claims = self._auth._claims
        return SimpleNamespace(
            user=SimpleNamespace(
                app_metadata=claims.get("app_metadata", {}), banned_until=None
            )
        )


class FakeSupabase:
    def __init__(self, fresh=None, admin_error=None, **kwargs):
        self.auth = FakeAuth(**kwargs)
        self.auth.admin = FakeAdmin(self.auth, fresh, admin_error)


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def fake_supabase(monkeypatch):
    """Install a fake Supabase client and return a function to configure it."""

    def install(**kwargs):
        fake = FakeSupabase(**kwargs)
        monkeypatch.setattr("app.auth.deps.get_supabase", lambda: fake)
        return fake

    return install


def bearer(token="any.jwt.token"):
    return {"Authorization": f"Bearer {token}"}


def claims(roles=None, **extra):
    data = {"sub": "user-123", "email": "a@example.com", **extra}
    if roles is not None:
        data["app_metadata"] = {"roles": roles}
    return data


# --- authentication -------------------------------------------------------


def test_missing_authorization_header_is_rejected(client):
    assert client.get("/me").status_code in (401, 403)


def test_invalid_token_returns_401(client, fake_supabase):
    fake_supabase(error=AuthInvalidJwtError("Invalid JWT signature"))
    r = client.get("/me", headers=bearer())
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == "Bearer"


def test_expired_token_returns_401(client, fake_supabase):
    fake_supabase(error=AuthInvalidJwtError("JWT has expired"))
    assert client.get("/me", headers=bearer()).status_code == 401


def test_no_claims_returned_returns_401(client, fake_supabase):
    fake_supabase(claims=None)
    assert client.get("/me", headers=bearer()).status_code == 401


def test_token_is_passed_to_supabase_unchanged(client, fake_supabase):
    fake = fake_supabase(claims=claims())
    client.get("/me", headers=bearer("abc.def.ghi"))
    assert fake.auth.seen_token == "abc.def.ghi"


# --- roles ----------------------------------------------------------------


def test_user_without_roles_defaults_to_customer(client, fake_supabase):
    fake_supabase(claims=claims(roles=None))
    r = client.get("/me", headers=bearer())
    assert r.status_code == 200
    assert r.json() == {
        "id": "user-123",
        "email": "a@example.com",
        "roles": ["customer"],
        "is_anonymous": False,
    }


def test_explicit_super_admin_role_is_read_from_app_metadata(client, fake_supabase):
    fake_supabase(claims=claims(roles=["super_admin"]))
    assert client.get("/me", headers=bearer()).json()["roles"] == ["super_admin"]


def test_unknown_roles_are_ignored(client, fake_supabase):
    fake_supabase(claims=claims(roles=["customer", "wizard"]))
    assert client.get("/me", headers=bearer()).json()["roles"] == ["customer"]


def test_user_metadata_cannot_grant_roles(client, fake_supabase):
    # user_metadata is user-editable, so a role placed there must be ignored.
    fake_supabase(claims=claims(user_metadata={"roles": ["super_admin"]}))
    assert client.get("/me", headers=bearer()).json()["roles"] == ["customer"]


# --- authorization --------------------------------------------------------


def test_customer_cannot_access_admin_route(client, fake_supabase):
    fake_supabase(claims=claims(roles=["customer"]))
    assert client.get("/admin/ping", headers=bearer()).status_code == 403


def test_super_admin_can_access_admin_route(client, fake_supabase):
    fake_supabase(claims=claims(roles=["super_admin"]))
    r = client.get("/admin/ping", headers=bearer())
    assert r.status_code == 200
    assert r.json() == {"ok": True, "user": "user-123"}


def test_admin_route_rejects_invalid_token_before_checking_role(client, fake_supabase):
    fake_supabase(error=AuthInvalidJwtError("bad"))
    assert client.get("/admin/ping", headers=bearer()).status_code == 401


# --- live re-check on privileged routes -----------------------------------


def fresh_user(roles=None, banned_until=None):
    return SimpleNamespace(
        app_metadata={"roles": roles} if roles else {}, banned_until=banned_until
    )


def test_role_revoked_after_token_issued_is_denied(client, fake_supabase):
    # Token still says super_admin, but Supabase says the role was removed.
    fake_supabase(claims=claims(roles=["super_admin"]), fresh=fresh_user(roles=["customer"]))
    assert client.get("/admin/ping", headers=bearer()).status_code == 403


def test_deactivated_user_is_locked_out_despite_valid_token(client, fake_supabase):
    until = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    fake_supabase(
        claims=claims(roles=["super_admin"]),
        fresh=fresh_user(roles=["super_admin"], banned_until=until),
    )
    assert client.get("/admin/ping", headers=bearer()).status_code == 403


def test_expired_ban_does_not_lock_user_out(client, fake_supabase):
    until = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    fake_supabase(
        claims=claims(roles=["super_admin"]),
        fresh=fresh_user(roles=["super_admin"], banned_until=until),
    )
    assert client.get("/admin/ping", headers=bearer()).status_code == 200


def test_deleted_account_with_valid_token_gets_401(client, fake_supabase):
    fake_supabase(
        claims=claims(roles=["super_admin"]),
        admin_error=AuthApiError("User not found", 404, "user_not_found"),
    )
    assert client.get("/admin/ping", headers=bearer()).status_code == 401


# --- upstream error handling ----------------------------------------------


def test_upstream_auth_failure_is_not_relayed_to_client(client, fake_supabase):
    # e.g. a rotated secret key: Supabase answers 401, which must not look like a bad user token.
    fake_supabase(
        claims=claims(roles=["super_admin"]),
        admin_error=AuthApiError("Invalid API key sb_secret_xyz", 401, "bad_jwt"),
    )
    r = client.get("/admin/ping", headers=bearer())
    assert r.status_code == 502
    assert "sb_secret_xyz" not in r.text
