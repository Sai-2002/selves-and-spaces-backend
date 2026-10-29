"""Integration tests against the real Supabase project in .env.

Run with: uv run pytest -m integration
Each test creates a throwaway pre-confirmed user (no email is sent) and deletes it afterwards.
"""

import uuid

import pytest
from fastapi.testclient import TestClient
from supabase import ClientOptions, create_client

from app.config import get_settings
from app.main import app
from app.supabase_client import get_supabase

pytestmark = pytest.mark.integration

PASSWORD = "Integration-test-pass-1!"


def sign_in(email: str) -> dict:
    """Sign in like the frontend would and return Authorization headers."""
    settings = get_settings()
    # Separate client so the session isn't stored on the shared admin client.
    client = create_client(
        settings.supabase_url,
        settings.supabase_secret_key,
        options=ClientOptions(auto_refresh_token=False, persist_session=False),
    )
    session = client.auth.sign_in_with_password({"email": email, "password": PASSWORD}).session
    return {"Authorization": f"Bearer {session.access_token}"}


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def temp_user():
    admin = get_supabase().auth.admin
    email = f"it-{uuid.uuid4().hex[:12]}@example.com"
    user = admin.create_user({"email": email, "password": PASSWORD, "email_confirm": True}).user
    yield {"id": user.id, "email": email}
    admin.delete_user(user.id)


def test_new_user_is_a_customer(client, temp_user):
    r = client.get("/me", headers=sign_in(temp_user["email"]))
    assert r.status_code == 200
    assert r.json() == {"id": temp_user["id"], "email": temp_user["email"], "roles": ["customer"]}


def test_customer_is_forbidden_from_admin_route(client, temp_user):
    r = client.get("/admin/ping", headers=sign_in(temp_user["email"]))
    assert r.status_code == 403


def test_super_admin_role_grants_admin_access(client, temp_user):
    get_supabase().auth.admin.update_user_by_id(
        temp_user["id"], {"app_metadata": {"roles": ["super_admin"]}}
    )
    # Roles are baked into the token at sign-in, so sign in again after the change.
    headers = sign_in(temp_user["email"])
    assert client.get("/me", headers=headers).json()["roles"] == ["super_admin"]
    assert client.get("/admin/ping", headers=headers).status_code == 200


def test_user_cannot_promote_themselves_via_user_metadata(client, temp_user):
    get_supabase().auth.admin.update_user_by_id(
        temp_user["id"], {"user_metadata": {"roles": ["super_admin"]}}
    )
    headers = sign_in(temp_user["email"])
    assert client.get("/admin/ping", headers=headers).status_code == 403


def test_tampered_token_is_rejected(client, temp_user):
    token = sign_in(temp_user["email"])["Authorization"].removeprefix("Bearer ")
    header, payload, signature = token.split(".")
    tampered = f"{header}.{payload}.{signature[:-4]}AAAA"
    r = client.get("/me", headers={"Authorization": f"Bearer {tampered}"})
    assert r.status_code == 401


def test_garbage_token_is_rejected(client):
    r = client.get("/me", headers={"Authorization": "Bearer not-a-jwt"})
    assert r.status_code == 401
