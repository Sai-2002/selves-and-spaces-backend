"""Razorpay integration.

Two interchangeable gateways behind one interface:
  * RazorpayGateway: the real API (test or live keys).
  * DummyGateway:    in-process fake for local development and tests. No network, no keys.

Payment confirmation never trusts the browser: the checkout signature (and webhook signature) are
HMAC-SHA256s that only someone holding the Razorpay secret could produce, and we recompute them.
"""

import hashlib
import hmac
import uuid
from functools import lru_cache

import httpx

from app.config import get_settings

API_URL = "https://api.razorpay.com/v1"
CURRENCY = "INR"

# Dummy-mode credentials. Deliberately obvious, and refused outright in production.
DUMMY_KEY_ID = "rzp_test_dummy"
DUMMY_KEY_SECRET = "dummy_key_secret"
DUMMY_WEBHOOK_SECRET = "dummy_webhook_secret"


class GatewayError(Exception):
    """Razorpay was unreachable or rejected the request."""


def sign(secret: str, message: bytes) -> str:
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def _matches(expected: str, supplied: str) -> bool:
    return hmac.compare_digest(expected.encode(), supplied.encode())


class Gateway:
    key_id: str
    _key_secret: str
    _webhook_secret: str

    def create_order(self, amount_paise: int, receipt: str) -> str:
        """Create a Razorpay order for `amount_paise` and return its id."""
        raise NotImplementedError

    def verify_payment_signature(self, order_id: str, payment_id: str, signature: str) -> bool:
        """The signature Razorpay Checkout returns to the browser after a successful payment."""
        return _matches(sign(self._key_secret, f"{order_id}|{payment_id}".encode()), signature)

    def verify_webhook_signature(self, body: bytes, signature: str) -> bool:
        """The X-Razorpay-Signature header, computed over the exact raw request body."""
        return _matches(sign(self._webhook_secret, body), signature)


class RazorpayGateway(Gateway):
    def __init__(self, key_id: str, key_secret: str, webhook_secret: str, client: httpx.Client | None = None):
        self.key_id = key_id
        self._key_secret = key_secret
        self._webhook_secret = webhook_secret
        self._client = client or httpx.Client(base_url=API_URL, timeout=10.0)

    def create_order(self, amount_paise: int, receipt: str) -> str:
        try:
            response = self._client.post(
                "/orders",
                auth=(self.key_id, self._key_secret),
                json={"amount": amount_paise, "currency": CURRENCY, "receipt": receipt},
            )
            response.raise_for_status()
            return response.json()["id"]
        except (httpx.HTTPError, KeyError, ValueError):
            # Don't chain the original: httpx errors can echo request details.
            raise GatewayError("Could not create the Razorpay order") from None


class DummyGateway(Gateway):
    """Behaves like Razorpay for the happy path, entirely in memory."""

    key_id = DUMMY_KEY_ID
    _key_secret = DUMMY_KEY_SECRET
    _webhook_secret = DUMMY_WEBHOOK_SECRET

    def create_order(self, amount_paise: int, receipt: str) -> str:
        return f"order_dummy{uuid.uuid4().hex[:14]}"

    def simulate_payment(self, order_id: str) -> dict:
        """What the browser would receive from Razorpay Checkout after paying."""
        payment_id = f"pay_dummy{uuid.uuid4().hex[:14]}"
        return {
            "razorpay_order_id": order_id,
            "razorpay_payment_id": payment_id,
            "razorpay_signature": sign(self._key_secret, f"{order_id}|{payment_id}".encode()),
        }


@lru_cache
def get_gateway() -> Gateway:
    settings = get_settings()
    if settings.razorpay_mode == "dummy":
        return DummyGateway()
    return RazorpayGateway(
        settings.razorpay_key_id, settings.razorpay_key_secret, settings.razorpay_webhook_secret
    )
