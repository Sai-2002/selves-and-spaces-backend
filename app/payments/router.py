import json
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from app.auth.deps import CurrentUser, get_active_customer
from app.orders import service
from app.payments.razorpay import DummyGateway, Gateway, get_gateway
from app.supabase_client import get_supabase

logger = logging.getLogger("audit")

router = APIRouter(tags=["payments"])

PAID_EVENTS = {"payment.captured", "order.paid"}


def process_webhook(db, gateway: Gateway, body: bytes, signature: str) -> str:
    """Handle one Razorpay webhook delivery. Returns a short outcome label.

    Raises HTTPException(400) for a bad signature. Every other outcome is a 2xx so Razorpay stops
    retrying; transient failures (e.g. database down) propagate as 5xx so it DOES retry.
    """
    # Verify against the exact raw bytes BEFORE parsing anything.
    if not gateway.verify_webhook_signature(body, signature):
        logger.warning("webhook.bad_signature")
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid signature")

    try:
        event = json.loads(body)
        if event.get("event") not in PAID_EVENTS:
            return "ignored_event"
        payment = event["payload"]["payment"]["entity"]
        order_id, payment_id = payment["order_id"], payment["id"]
    except (ValueError, KeyError, TypeError, AttributeError):
        return "ignored_malformed"
    if payment.get("status") != "captured":
        return "ignored_not_captured"

    try:
        checkout = service.get_checkout(db, order_id)
    except service.CheckoutNotFoundError:
        logger.warning("webhook.unknown_checkout razorpay_order=%s", order_id)
        return "ignored_unknown_checkout"

    # Never create an order for a payment that doesn't cover what we quoted.
    if payment.get("amount") != checkout["amount_paise"] or payment.get("currency") != checkout["currency"]:
        logger.error("webhook.amount_mismatch razorpay_order=%s payment=%s", order_id, payment_id)
        return "ignored_amount_mismatch"

    new_order = service.complete_checkout(db, order_id, payment_id)
    logger.info("order.paid via=webhook order=%s", new_order)
    return "completed"


@router.post("/webhooks/razorpay")
async def razorpay_webhook(request: Request, gateway: Annotated[Gateway, Depends(get_gateway)]) -> dict:
    """Razorpay calls this server-to-server. It's the safety net for customers who pay and then
    close the tab before the browser's verify call. No bearer token: the signature is the auth."""
    body = await request.body()
    signature = request.headers.get("x-razorpay-signature", "")
    outcome = await run_in_threadpool(process_webhook, get_supabase(), gateway, body, signature)
    return {"status": outcome}


# --- dummy mode only ---------------------------------------------------------------------------


class SimulatePayment(BaseModel):
    razorpay_order_id: str


@router.post("/dev/razorpay/simulate-payment", include_in_schema=False)
def simulate_payment(
    data: SimulatePayment,
    user: Annotated[CurrentUser, Depends(get_active_customer)],
    gateway: Annotated[Gateway, Depends(get_gateway)],
) -> dict:
    """Stands in for paying in Razorpay Checkout when there is no real Razorpay: returns the three
    values the browser would receive. Only exists in dummy mode (which production refuses)."""
    if not isinstance(gateway, DummyGateway):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    try:
        service.get_checkout(get_supabase(), data.razorpay_order_id, user_id=user.id)
    except service.CheckoutNotFoundError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found") from e
    return gateway.simulate_payment(data.razorpay_order_id)
