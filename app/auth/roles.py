from collections.abc import Mapping
from enum import StrEnum


class Role(StrEnum):
    CUSTOMER = "customer"
    SUPER_ADMIN = "super_admin"


def roles_from_app_metadata(app_metadata: Mapping | None) -> list[Role]:
    """Known roles from `app_metadata`; users with no (known) role are customers.

    Only `app_metadata` is trusted: it can be written solely with the secret key.
    """
    raw = (app_metadata or {}).get("roles") or []
    roles = [Role(r) for r in raw if r in Role._value2member_map_]
    return roles or [Role.CUSTOMER]
