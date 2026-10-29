from datetime import datetime

from pydantic import BaseModel
from supabase_auth.types import User

from app.users.service import effective_roles, is_banned


class UserOut(BaseModel):
    id: str
    email: str | None
    roles: list[str]
    created_at: datetime
    last_sign_in_at: datetime | None
    deactivated: bool

    @classmethod
    def from_supabase(cls, user: User) -> "UserOut":
        return cls(
            id=user.id,
            email=user.email,
            roles=effective_roles(user),
            created_at=user.created_at,
            last_sign_in_at=user.last_sign_in_at,
            deactivated=is_banned(user),
        )
