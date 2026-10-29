import logging
from typing import Annotated

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from postgrest.exceptions import APIError as PostgrestError
from supabase_auth.errors import AuthApiError

from app.auth.deps import CurrentUser, get_current_user, require_roles
from app.auth.roles import Role
from app.config import get_settings
from app.orders.router import admin_router as admin_orders_router
from app.orders.router import checkout_router
from app.orders.router import router as orders_router
from app.payments.router import router as payments_router
from app.products.router import router as products_router
from app.users.router import router as users_router

logger = logging.getLogger(__name__)

# Don't advertise the API surface in production.
_docs = None if get_settings().is_production else "/docs"
app = FastAPI(
    title="Selves and Spaces",
    docs_url=_docs,
    redoc_url=None,
    openapi_url=None if get_settings().is_production else "/openapi.json",
)
# Browsers block a SPA on another origin from calling this API unless it is explicitly allowed.
# Auth is a bearer token in a header (never a cookie), so credentials stay off.
app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_origin_list,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["Authorization", "Content-Type"],
)
app.include_router(users_router)
app.include_router(products_router)
app.include_router(checkout_router)
app.include_router(orders_router)
app.include_router(admin_orders_router)
app.include_router(payments_router)


@app.exception_handler(AuthApiError)
def supabase_auth_error(_: Request, exc: AuthApiError) -> JSONResponse:
    # Only "not found" is the caller's problem. Anything else (bad/rotated secret key, rate
    # limits, outages) is an upstream failure: never relay its status or message to the client.
    if exc.status == 404:
        return JSONResponse({"detail": "Not found"}, status_code=404)
    logger.error("Supabase auth error: status=%s code=%s", exc.status, exc.code)
    return JSONResponse({"detail": "Upstream authentication service error"}, status_code=502)


@app.exception_handler(PostgrestError)
def database_error(_: Request, exc: PostgrestError) -> JSONResponse:
    logger.error("Database error: code=%s message=%s", exc.code, exc.message)
    return JSONResponse({"detail": "Database error"}, status_code=502)


@app.get("/me")
def me(user: Annotated[CurrentUser, Depends(get_current_user)]) -> CurrentUser:
    return user


@app.get("/admin/ping")
def admin_ping(
    user: Annotated[CurrentUser, Depends(require_roles(Role.SUPER_ADMIN))],
) -> dict:
    return {"ok": True, "user": user.id}
