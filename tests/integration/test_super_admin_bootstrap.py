"""Integration test for the one-super-admin rule, against the real Supabase project.

Skips itself if the project already has a super admin, so it never touches a real one.
"""

import uuid

import pytest

from app.supabase_client import get_supabase
from app.users.service import SuperAdminExistsError, ensure_single_super_admin, find_super_admin

pytestmark = pytest.mark.integration


@pytest.fixture
def admin():
    return get_supabase().auth.admin


@pytest.fixture
def temp_users(admin):
    ids = []
    yield ids
    for uid in ids:
        admin.delete_user(uid)


def new_user(admin, ids):
    email = f"it-{uuid.uuid4().hex[:12]}@example.com"
    user = admin.create_user({"email": email, "password": "Integration-pass-1!", "email_confirm": True}).user
    ids.append(user.id)
    return user


def test_only_one_super_admin_can_exist(admin, temp_users):
    if find_super_admin(admin):
        pytest.skip("This project already has a super admin; not touching it.")

    first, second = new_user(admin, temp_users), new_user(admin, temp_users)

    promoted = ensure_single_super_admin(admin, first.email)
    assert promoted.app_metadata["roles"] == ["super_admin"]
    assert find_super_admin(admin).id == first.id

    with pytest.raises(SuperAdminExistsError):
        ensure_single_super_admin(admin, second.email)
    assert find_super_admin(admin).id == first.id
