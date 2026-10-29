"""Gateway and settings tests. The real client runs against httpx's mock transport: no network."""

import json

import httpx
import pytest
from pydantic import ValidationError

from app.config import Settings
from app.payments.razorpay import DummyGateway, GatewayError, RazorpayGateway, sign

BASE = dict(supabase_url="https://x.supabase.co", supabase_secret_key="k")
REAL = dict(razorpay_key_id="rzp_test_abc", razorpay_key_secret="secret", razorpay_webhook_secret="whsec")


def gateway_with(handler) -> RazorpayGateway:
    client = httpx.Client(base_url="https://api.razorpay.com/v1", transport=httpx.MockTransport(handler))
    return RazorpayGateway("rzp_test_abc", "secret", "whsec", client=client)


def test_create_order_sends_amount_currency_receipt_and_basic_auth():
    seen = {}

    def handler(request: httpx.Request):
        seen.update(url=str(request.url), auth=request.headers["authorization"], body=json.loads(request.content))
        return httpx.Response(200, json={"id": "order_real123"})

    assert gateway_with(handler).create_order(20020, receipt="chk-1") == "order_real123"
    assert seen["url"] == "https://api.razorpay.com/v1/orders"
    assert seen["body"] == {"amount": 20020, "currency": "INR", "receipt": "chk-1"}
    assert seen["auth"].startswith("Basic ")  # key_id:key_secret


@pytest.mark.parametrize(
    "handler",
    [
        lambda r: httpx.Response(401, json={"error": {"description": "Authentication failed"}}),
        lambda r: httpx.Response(500, text="boom"),
        lambda r: httpx.Response(200, json={"no": "id"}),
        lambda r: httpx.Response(200, text="not json"),
        lambda r: (_ for _ in ()).throw(httpx.ConnectTimeout("slow")),
    ],
)
def test_razorpay_failures_become_gateway_error(handler):
    with pytest.raises(GatewayError):
        gateway_with(handler).create_order(100, receipt="r")


def test_gateway_error_does_not_leak_the_key():
    def handler(request):
        raise httpx.ConnectError("connect failed for rzp_test_abc:secret")

    with pytest.raises(GatewayError) as exc:
        gateway_with(handler).create_order(100, receipt="r")
    assert "secret" not in str(exc.value) and exc.value.__cause__ is None


def test_payment_signature_matches_razorpays_documented_scheme():
    # Razorpay: hex HMAC-SHA256 of "<order_id>|<payment_id>" keyed by the key secret.
    gw = gateway_with(lambda r: httpx.Response(200))
    good = sign("secret", b"order_1|pay_1")
    assert gw.verify_payment_signature("order_1", "pay_1", good)
    assert not gw.verify_payment_signature("order_1", "pay_2", good)
    assert not gw.verify_payment_signature("order_2", "pay_1", good)
    assert not gw.verify_payment_signature("order_1", "pay_1", "")
    assert not gw.verify_payment_signature("order_1", "pay_1", sign("whsec", b"order_1|pay_1"))


def test_webhook_signature_uses_the_webhook_secret_not_the_key_secret():
    gw = gateway_with(lambda r: httpx.Response(200))
    assert gw.verify_webhook_signature(b'{"a":1}', sign("whsec", b'{"a":1}'))
    assert not gw.verify_webhook_signature(b'{"a":1}', sign("secret", b'{"a":1}'))
    assert not gw.verify_webhook_signature(b'{"a":2}', sign("whsec", b'{"a":1}'))


def test_dummy_gateway_round_trips_and_makes_unique_ids():
    gw = DummyGateway()
    a, b = gw.create_order(100, "r"), gw.create_order(100, "r")
    assert a != b and a.startswith("order_dummy")
    proof = gw.simulate_payment(a)
    assert gw.verify_payment_signature(proof["razorpay_order_id"], proof["razorpay_payment_id"], proof["razorpay_signature"])


# --- settings ---------------------------------------------------------------


def test_dummy_is_the_default_and_needs_no_keys():
    assert Settings(**BASE).razorpay_mode == "dummy"


def test_dummy_mode_is_refused_in_production():
    with pytest.raises(ValidationError, match="not allowed when ENVIRONMENT=production"):
        Settings(**BASE, environment="production", cors_origins="https://shop.example.com")


def test_real_modes_require_all_three_secrets():
    with pytest.raises(ValidationError, match="RAZORPAY_KEY_ID"):
        Settings(**BASE, razorpay_mode="test")
    with pytest.raises(ValidationError, match="RAZORPAY_WEBHOOK_SECRET"):
        Settings(**BASE, razorpay_mode="test", razorpay_key_id="rzp_test_a", razorpay_key_secret="s")


def test_key_prefix_must_match_mode():
    with pytest.raises(ValidationError, match="rzp_test_"):
        Settings(**BASE, razorpay_mode="test", **{**REAL, "razorpay_key_id": "rzp_live_abc"})
    with pytest.raises(ValidationError, match="rzp_live_"):
        Settings(**BASE, razorpay_mode="live", **REAL)


def test_valid_test_and_live_configs_load_and_production_may_use_live():
    assert Settings(**BASE, razorpay_mode="test", **REAL).razorpay_mode == "test"
    live = {**REAL, "razorpay_key_id": "rzp_live_abc"}
    assert Settings(
        **BASE, razorpay_mode="live", environment="production", cors_origins="https://shop.example.com", **live
    ).is_production


def test_secrets_are_not_in_repr():
    text = repr(Settings(**BASE, razorpay_mode="test", **REAL))
    assert "secret'" not in text and "whsec" not in text
