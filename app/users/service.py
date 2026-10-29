from collections.abc import Iterator
from datetime import datetime, timezone

from supabase_auth.types import User

from app.auth.roles import Role, roles_from_app_metadata

PER_PAGE = 50


class SuperAdminExistsError(Exception):
    def __init__(self, existing: User):
        super().__init__(f"A super admin already exists: {existing.email}")
        self.existing = existing


def user_roles(user: User) -> list[str]:
    return user.app_metadata.get("roles") or []


def effective_roles(user: User) -> list[str]:
    """Roles as the API sees them: users with no explicit role are customers."""
    return [r.value for r in roles_from_app_metadata(user.app_metadata)]


def is_banned(user: User) -> bool:
    """True while the user's ban is in effect (an expired ban no longer counts)."""
    until = user.banned_until
    if not until:
        return False
    if isinstance(until, str):
        until = datetime.fromisoformat(until.replace("Z", "+00:00"))
    if until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)
    return until > datetime.now(timezone.utc)


def list_users(admin, page: int, per_page: int, role: Role | None = None) -> list[User]:
    if role is None:
        return admin.list_users(page=page, per_page=per_page)
    # Supabase can't filter by role, so filter everything and slice the page ourselves.
    matching = [u for u in iter_users(admin) if role in effective_roles(u)]
    start = (page - 1) * per_page
    return matching[start : start + per_page]


def iter_users(admin) -> Iterator[User]:
    """Yield every user, paging through the Supabase admin API."""
    page = 1
    while True:
        users = admin.list_users(page=page, per_page=PER_PAGE)
        yield from users
        if len(users) < PER_PAGE:
            return
        page += 1


def find_super_admin(admin) -> User | None:
    return next((u for u in iter_users(admin) if Role.SUPER_ADMIN in user_roles(u)), None)


def find_user_by_email(admin, email: str) -> User | None:
    email = email.lower()
    return next((u for u in iter_users(admin) if (u.email or "").lower() == email), None)


def ensure_single_super_admin(admin, email: str, password: str | None = None) -> User:
    """Make `email` the super admin, creating the account if needed.

    Raises SuperAdminExistsError if any super admin already exists, so there can only ever be one.
    `password` is only needed when the account doesn't exist yet.
    """
    existing = find_super_admin(admin)
    if existing:
        raise SuperAdminExistsError(existing)

    app_metadata = {"roles": [Role.SUPER_ADMIN.value]}
    user = find_user_by_email(admin, email)
    if user:
        return admin.update_user_by_id(user.id, {"app_metadata": app_metadata}).user

    if not password:
        raise ValueError("A password is required to create a new super admin account")
    return admin.create_user(
        {
            "email": email,
            "password": password,
            "email_confirm": True,
            "app_metadata": app_metadata,
        }
    ).user
