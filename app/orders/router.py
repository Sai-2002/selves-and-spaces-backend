import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.auth.deps import CurrentUser, get_active_customer, get_current_user, require_roles
from app.auth.roles import Role
from app.orders import service
from app.orders.schemas import (
    CheckoutOut,
    DeliveryStatus,
    DeliveryUpdate,
    OrderCreate,
    OrderOut,
    VerifyPayment,
)
from app.payments.razorpay import Gateway, GatewayError, get_gateway
from app.supabase_client import get_supabase

# Audit lines carry ids only, never customer names, addresses or phone numbers.
logger = logging.getLogger("audit")

# Reading your own orders needs a valid token. Paying needs more: see get_active_customer.
Viewer = Annotated[CurrentUser, Depends(get_current_user)]
Payer = Annotated[CurrentUser, Depends(get_active_customer)]
SuperAdmin = Annotated[CurrentUser, Depends(require_roles(Role.SUPER_ADMIN))]
PaymentGateway = Annotated[Gateway, Depends(get_gateway)]

Limit = Annotated[int, Query(ge=1, le=100)]
Offset = Annotated[int, Query(ge=0)]


def _not_found() -> HTTPException:
    # Same answer for "doesn't exist" and "isn't yours", so ids can't be probed.
    return HTTPException(status.HTTP_404_NOT_FOUND, "Not found")


# --- checkout: authenticate, price the cart, hand off to Razorpay; confirm after payment ---------

checkout_router = APIRouter(prefix="/checkout", tags=["checkout"])


@checkout_router.post("", status_code=status.HTTP_201_CREATED)
def start_checkout(data: OrderCreate, user: Payer, gateway: PaymentGateway) -> CheckoutOut:
    """Step 1. Returns what the frontend passes to Razorpay Checkout. Creates no order."""
    try:
        result = service.start_checkout(get_supabase(), gateway, user.id, user.email, data)
    except service.UnknownProductError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "One or more products do not exist") from e
    except service.InvalidAmountError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Order total must be at least 1.00") from e
    except service.TooManyCheckoutsError as e:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many unpaid checkouts; try again later") from e
    except GatewayError as e:
        logger.error("checkout.gateway_error actor=%s", user.id)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Payment provider unavailable") from e
    logger.info("checkout.start actor=%s checkout=%s", user.id, result["checkout_id"])
    return CheckoutOut(**result)


@checkout_router.post("/verify")
def verify_payment(data: VerifyPayment, user: Payer, gateway: PaymentGateway) -> OrderOut:
    """Step 2, after Razorpay Checkout succeeds. Verifies the signature, then creates the order.

    Idempotent: repeating it (or the webhook getting there first) returns the same order.
    """
    db = get_supabase()
    try:
        # The checkout must be this user's own: nobody can claim someone else's payment.
        service.get_checkout(db, data.razorpay_order_id, user_id=user.id)
    except service.CheckoutNotFoundError as e:
        raise _not_found() from e
    if not gateway.verify_payment_signature(
        data.razorpay_order_id, data.razorpay_payment_id, data.razorpay_signature
    ):
        logger.warning("checkout.bad_signature actor=%s razorpay_order=%s", user.id, data.razorpay_order_id)
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Payment verification failed")
    order_id = service.complete_checkout(db, data.razorpay_order_id, data.razorpay_payment_id)
    logger.info("order.paid via=verify actor=%s order=%s", user.id, order_id)
    return service.to_order_out(service.get_order(db, order_id))


# --- customers: view their OWN orders -----------------------------------------------------------

router = APIRouter(prefix="/orders", tags=["orders"])


@router.get("")
def my_orders(user: Viewer, limit: Limit = 20, offset: Offset = 0) -> list[OrderOut]:
    rows = service.list_orders(get_supabase(), limit, offset, user_id=user.id)
    return [service.to_order_out(r) for r in rows]


@router.get("/{order_id}")
def my_order(order_id: UUID, user: Viewer) -> OrderOut:
    try:
        return service.to_order_out(service.get_order(get_supabase(), str(order_id), user_id=user.id))
    except service.OrderNotFoundError as e:
        raise _not_found() from e


# --- super admin: see everything and move delivery forward --------------------------------------

admin_router = APIRouter(prefix="/admin/orders", tags=["admin orders"])


@admin_router.get("")
def all_orders(
    _: SuperAdmin,
    limit: Limit = 20,
    offset: Offset = 0,
    delivery_status: DeliveryStatus | None = None,
) -> list[OrderOut]:
    rows = service.list_orders(get_supabase(), limit, offset, delivery_status=delivery_status)
    return [service.to_order_out(r) for r in rows]


@admin_router.get("/{order_id}")
def any_order(order_id: UUID, _: SuperAdmin) -> OrderOut:
    try:
        return service.to_order_out(service.get_order(get_supabase(), str(order_id)))
    except service.OrderNotFoundError as e:
        raise _not_found() from e


@admin_router.post("/{order_id}/delivery-status")
def update_delivery_status(order_id: UUID, data: DeliveryUpdate, current: SuperAdmin) -> OrderOut:
    db = get_supabase()
    try:
        service.set_delivery_status(db, str(order_id), data.status)
        row = service.get_order(db, str(order_id))
    except service.OrderNotFoundError as e:
        raise _not_found() from e
    except service.InvalidTransitionError as e:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Delivery status can only move forward one step: order_placed, in_transit, delivered",
        ) from e
    logger.info("order.delivery actor=%s order=%s status=%s", current.id, order_id, data.status.value)
    return service.to_order_out(row)
