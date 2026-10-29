"""Unit tests for the super-admin user management routes. Supabase is faked."""

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from supabase_auth.errors import AuthApiError
from supabase_auth.types import User

from app.main import app

ADMIN_ID = str(uuid4())
CUSTOMER_ID = str(uuid4())
OTHER_ADMIN_ID = str(uuid4())


def make_user(id, email, roles=None, banned_until=None):
    return User(
        id=id,
        email=email,
        app_metadata={"roles": roles} if roles else {},
        user_metadata={},
        aud="authenticated",
        created_at=datetime.now(timezone.utc),
        banned_until=banned_until,
    )


class FakeAdmin:
    def __init__(self, users):
        self.users = {u.id: u for u in users}
        self.updates = []

    def list_users(self, page=None, per_page=None):
        everyone = list(self.users.values())
        start = (page - 1) * per_page
        return everyone[start : start + per_page]

    def get_user_by_id(self, uid):
        if uid not in self.users:
            raise AuthApiError("User not found", 404, "user_not_found")
        return SimpleNamespace(user=self.users[uid])

    def update_user_by_id(self, uid, attributes):
        self.updates.append((uid, attributes))
        user = self.users[uid]
        ban = attributes.get("ban_duration")
        user.banned_until = None if ban == "none" else "2126-01-01T00:00:00Z"
        return SimpleNamespace(user=user)


class FakeAuth:
    def __init__(self, admin, caller):
        self.admin = admin
        self._caller = caller

    def get_claims(self, token):
        return {"claims": self._caller}


@pytest.fixture
def setup(monkeypatch):
    def build(caller_id=ADMIN_ID, caller_roles=("super_admin",)):
        admin = FakeAdmin(
            [
                make_user(ADMIN_ID, "boss@example.com", ["super_admin"]),
                make_user(CUSTOMER_ID, "cust@example.com"),
                make_user(str(uuid4()), "cust2@example.com", ["customer"]),
            ]
        )
        caller = {"sub": caller_id, "app_metadata": {"roles": list(caller_roles)}}
        fake = SimpleNamespace(auth=FakeAuth(admin, caller))
        monkeypatch.setattr("app.auth.deps.get_supabase", lambda: fake)
        monkeypatch.setattr("app.users.router.get_supabase", lambda: fake)
        return TestClient(app, headers={"Authorization": "Bearer t.t.t"}), admin

    return build


# --- authorization ----------------------------------------------------------


@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/users"),
        ("get", f"/users/{CUSTOMER_ID}"),
        ("post", f"/users/{CUSTOMER_ID}/deactivate"),
        ("post", f"/users/{CUSTOMER_ID}/activate"),
    ],
)
def test_customers_are_forbidden_from_every_user_route(setup, method, path):
    client, admin = setup(caller_id=CUSTOMER_ID, caller_roles=("customer",))
    assert getattr(client, method)(path).status_code == 403
    assert admin.updates == []


# --- list / get ----------------------------------------------------------


def test_list_users_returns_everyone_with_effective_roles(setup):
    client, _ = setup()
    r = client.get("/users")
    assert r.status_code == 200
    by_email = {u["email"]: u for u in r.json()}
    assert by_email["boss@example.com"]["roles"] == ["super_admin"]
    assert by_email["cust@example.com"]["roles"] == ["customer"]  # no explicit role -> customer
    assert by_email["cust@example.com"]["deactivated"] is False


def test_list_users_filters_by_role_including_default_customers(setup):
    client, _ = setup()
    emails = {u["email"] for u in client.get("/users", params={"role": "customer"}).json()}
    assert emails == {"cust@example.com", "cust2@example.com"}


def test_list_users_paginates(setup):
    client, _ = setup()
    assert len(client.get("/users", params={"per_page": 2, "page": 1}).json()) == 2
    assert len(client.get("/users", params={"per_page": 2, "page": 2}).json()) == 1


def test_list_users_rejects_bad_paging_and_unknown_role(setup):
    client, _ = setup()
    assert client.get("/users", params={"page": 0}).status_code == 422
    assert client.get("/users", params={"per_page": 1000}).status_code == 422
    assert client.get("/users", params={"role": "wizard"}).status_code == 422


def test_get_user(setup):
    client, _ = setup()
    r = client.get(f"/users/{CUSTOMER_ID}")
    assert r.status_code == 200 and r.json()["email"] == "cust@example.com"


def test_get_unknown_user_is_404(setup):
    client, _ = setup()
    assert client.get(f"/users/{uuid4()}").status_code == 404


def test_malformed_user_id_is_422(setup):
    client, _ = setup()
    assert client.get("/users/not-a-uuid").status_code == 422


# --- deactivate / activate --------------------------------------------------


def test_deactivate_bans_the_customer(setup):
    client, admin = setup()
    r = client.post(f"/users/{CUSTOMER_ID}/deactivate")
    assert r.status_code == 200 and r.json()["deactivated"] is True
    assert admin.updates == [(CUSTOMER_ID, {"ban_duration": "876000h"})]


def test_activate_lifts_the_ban(setup):
    client, admin = setup()
    client.post(f"/users/{CUSTOMER_ID}/deactivate")
    r = client.post(f"/users/{CUSTOMER_ID}/activate")
    assert r.status_code == 200 and r.json()["deactivated"] is False
    assert admin.updates[-1] == (CUSTOMER_ID, {"ban_duration": "none"})


def test_super_admin_cannot_deactivate_themselves(setup):
    client, admin = setup()
    assert client.post(f"/users/{ADMIN_ID}/deactivate").status_code == 400
    assert admin.updates == []


def test_deactivating_unknown_user_is_404(setup):
    client, admin = setup()
    assert client.post(f"/users/{uuid4()}/deactivate").status_code == 404
    assert admin.updates == []
