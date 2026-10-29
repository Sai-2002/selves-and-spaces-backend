import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.auth.deps import CurrentUser, require_roles
from app.auth.roles import Role
from app.supabase_client import get_supabase
from app.users import service
from app.users.schemas import UserOut

logger = logging.getLogger("audit")

router = APIRouter(prefix="/users", tags=["users"])

SuperAdmin = Annotated[CurrentUser, Depends(require_roles(Role.SUPER_ADMIN))]

# Supabase has no "disable user" flag; a very long ban is the documented way.
DEACTIVATE_FOR = "876000h"  # ~100 years


@router.get("")
def list_users(
    _: SuperAdmin,
    page: Annotated[int, Query(ge=1)] = 1,
    per_page: Annotated[int, Query(ge=1, le=100)] = 50,
    role: Role | None = None,
) -> list[UserOut]:
    users = service.list_users(get_supabase().auth.admin, page, per_page, role)
    return [UserOut.from_supabase(u) for u in users]


@router.get("/{user_id}")
def get_user(user_id: UUID, _: SuperAdmin) -> UserOut:
    user = get_supabase().auth.admin.get_user_by_id(str(user_id)).user
    return UserOut.from_supabase(user)


@router.post("/{user_id}/deactivate")
def deactivate_user(user_id: UUID, current: SuperAdmin) -> UserOut:
    admin = get_supabase().auth.admin
    target = admin.get_user_by_id(str(user_id)).user
    if target.id == current.id or Role.SUPER_ADMIN in service.user_roles(target):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "The super admin cannot be deactivated")
    updated = admin.update_user_by_id(target.id, {"ban_duration": DEACTIVATE_FOR}).user
    logger.info("user.deactivate actor=%s target=%s", current.id, target.id)
    return UserOut.from_supabase(updated)


@router.post("/{user_id}/activate")
def activate_user(user_id: UUID, current: SuperAdmin) -> UserOut:
    admin = get_supabase().auth.admin
    updated = admin.update_user_by_id(str(user_id), {"ban_duration": "none"}).user
    logger.info("user.activate actor=%s target=%s", current.id, user_id)
    return UserOut.from_supabase(updated)
