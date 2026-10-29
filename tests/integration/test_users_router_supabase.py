"""Integration tests for the super-admin user routes, against the real Supabase project.

Uses throwaway users only (a temporary super admin + a temporary customer), all deleted afterwards.
Skips itself if the project already has a super admin, so it never promotes a second one.
"""

import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from supabase import ClientOptions, create_client
from supabase_auth.errors import AuthApiError

from app.config import get_settings
from app.main import app
from app.supabase_client import get_supabase
from app.users.service import find_super_admin

pytestmark = pytest.mark.integration

PASSWORD = "Integration-test-pass-1!"


def sign_in(email: str):
    settings = get_settings()
    client = create_client(
        settings.supabase_url,
        settings.supabase_secret_key,
        options=ClientOptions(auto_refresh_token=False, persist_session=False),
    )
    return client.auth.sign_in_with_password({"email": email, "password": PASSWORD})


def headers_for(email: str) -> dict:
    return {"Authorization": f"Bearer {sign_in(email).session.access_token}"}


@pytest.fixture
def admin():
    return get_supabase().auth.admin


@pytest.fixture
def users(admin):
    """A temporary super admin and customer."""
    if find_super_admin(admin):
        pytest.skip("This project already has a super admin; not creating a second one.")

    created = []

    def make(roles=None):
        email = f"it-{uuid.uuid4().hex[:12]}@example.com"
        attrs = {"email": email, "password": PASSWORD, "email_confirm": True}
        if roles:
            attrs["app_metadata"] = {"roles": roles}
        user = admin.create_user(attrs).user
        created.append(user.id)
        return user

    yield SimpleNamespace(boss=make(["super_admin"]), customer=make())
    for uid in created:
        admin.delete_user(uid)


@pytest.fixture
def client():
    return TestClient(app)


def test_super_admin_can_list_and_filter_users(client, users):
    h = headers_for(users.boss.email)
    everyone = {u["id"]: u for u in client.get("/users", headers=h, params={"per_page": 100}).json()}
    assert everyone[users.boss.id]["roles"] == ["super_admin"]
    assert everyone[users.customer.id]["roles"] == ["customer"]

    customers = client.get("/users", headers=h, params={"role": "customer", "per_page": 100}).json()
    assert users.customer.id in {u["id"] for u in customers}
    assert users.boss.id not in {u["id"] for u in customers}


def test_customer_cannot_list_users(client, users):
    r = client.get("/users", headers=headers_for(users.customer.email))
    assert r.status_code == 403


def test_deactivated_customer_cannot_sign_in_until_reactivated(client, users):
    h = headers_for(users.boss.email)

    r = client.post(f"/users/{users.customer.id}/deactivate", headers=h)
    assert r.status_code == 200 and r.json()["deactivated"] is True
    with pytest.raises(AuthApiError):
        sign_in(users.customer.email)

    r = client.post(f"/users/{users.customer.id}/activate", headers=h)
    assert r.status_code == 200 and r.json()["deactivated"] is False
    assert sign_in(users.customer.email).session is not None


def test_super_admin_cannot_deactivate_themselves(client, users):
    h = headers_for(users.boss.email)
    assert client.post(f"/users/{users.boss.id}/deactivate", headers=h).status_code == 400
    assert sign_in(users.boss.email).session is not None


def test_unknown_user_is_404(client, users):
    h = headers_for(users.boss.email)
    assert client.get(f"/users/{uuid.uuid4()}", headers=h).status_code == 404
