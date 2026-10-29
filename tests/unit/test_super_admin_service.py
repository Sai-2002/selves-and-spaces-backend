"""Unit tests for the super-admin bootstrap logic. Supabase's admin API is faked."""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from supabase_auth.types import User

from app.users import service
from app.users.service import SuperAdminExistsError, ensure_single_super_admin, find_super_admin


def make_user(email, roles=None, id=None):
    return User(
        id=id or f"id-{email}",
        email=email,
        app_metadata={"roles": roles} if roles else {},
        user_metadata={},
        aud="authenticated",
        created_at=datetime.now(timezone.utc),
    )


class FakeAdmin:
    def __init__(self, users):
        self.users = list(users)
        self.created = []
        self.updated = []

    def list_users(self, page=None, per_page=None):
        start = (page - 1) * per_page
        return self.users[start : start + per_page]

    def update_user_by_id(self, uid, attributes):
        self.updated.append((uid, attributes))
        return SimpleNamespace(user=make_user("promoted@example.com", ["super_admin"], id=uid))

    def create_user(self, attributes):
        self.created.append(attributes)
        return SimpleNamespace(
            user=make_user(attributes["email"], attributes["app_metadata"]["roles"])
        )


def test_find_super_admin_returns_none_when_nobody_has_the_role():
    admin = FakeAdmin([make_user("a@example.com"), make_user("b@example.com", ["customer"])])
    assert find_super_admin(admin) is None


def test_find_super_admin_searches_every_page(monkeypatch):
    monkeypatch.setattr(service, "PER_PAGE", 2)
    users = [make_user(f"u{i}@example.com") for i in range(5)]
    users.append(make_user("boss@example.com", ["super_admin"]))  # lands on page 3
    assert find_super_admin(FakeAdmin(users)).email == "boss@example.com"


def test_refuses_when_a_super_admin_already_exists():
    admin = FakeAdmin([make_user("boss@example.com", ["super_admin"])])
    with pytest.raises(SuperAdminExistsError):
        ensure_single_super_admin(admin, "other@example.com", "pw")
    assert admin.created == [] and admin.updated == []


def test_promotes_an_existing_account_without_needing_a_password():
    admin = FakeAdmin([make_user("me@example.com", id="abc")])
    ensure_single_super_admin(admin, "ME@example.com")  # email match is case-insensitive
    assert admin.updated == [("abc", {"app_metadata": {"roles": ["super_admin"]}})]
    assert admin.created == []


def test_creates_a_confirmed_account_when_none_exists():
    admin = FakeAdmin([make_user("someone@example.com")])
    user = ensure_single_super_admin(admin, "new@example.com", "s3cret-pass")
    assert admin.created == [
        {
            "email": "new@example.com",
            "password": "s3cret-pass",
            "email_confirm": True,
            "app_metadata": {"roles": ["super_admin"]},
        }
    ]
    assert user.email == "new@example.com"


def test_creating_a_new_account_requires_a_password():
    with pytest.raises(ValueError):
        ensure_single_super_admin(FakeAdmin([]), "new@example.com")
