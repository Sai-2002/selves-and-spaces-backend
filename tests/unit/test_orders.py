"""Unit tests for checkout, Razorpay confirmation and orders. Supabase is faked and Razorpay runs in
dummy mode, so no network is touched. The SQL functions themselves are tested against Postgres."""

import json
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.payments.razorpay import (
    DUMMY_WEBHOOK_SECRET,
    DummyGateway,
    GatewayError,
    get_gateway,
    sign,
)

ADMIN_ID, ALICE_ID, BOB_ID = (str(uuid4()) for _ in range(3))
PRODUCT_ID = str(uuid4())
CHEAP_ID = str(uuid4())
PRICES = {PRODUCT_ID: ("Desk", "100.10"), CHEAP_ID: ("Sticker", "0.50")}
NOW = datetime.now(timezone.utc).isoformat()

BODY = {
    "customer": {"name": "Asha Rao", "phone": "+91 98765 43210"},
    "shipping_address": {"line1": "1 Main St", "city": "Chennai", "postal_code": "600001", "country": "India"},
    "items": [{"product_id": PRODUCT_ID, "quantity": 2}],
}


class Query:
    def __init__(self, db, table):
        self.db, self.table = db, table
        self.eqs, self.nulls, self.not_null, self.since, self.row = {}, [], [], None, None
        self._neg, self.start, self.end = False, None, None

    @property
    def not_(self):
        self._neg = True
        return self

    def select(self, *_):
        return self

    def insert(self, row):
        self.row = row
        return self

    def eq(self, col, val):
        self.eqs[col] = val
        return self

    def is_(self, col, _):
        (self.not_null if self._neg else self.nulls).append(col)
        self._neg = False
        return self

    def gte(self, col, val):
        self.since = (col, val)
        return self

    def order(self, *_, **__):
        return self

    def range(self, start, end):
        self.start, self.end = start, end
        return self

    def limit(self, *_):
        return self

    def execute(self):
        rows = self.db.tables[self.table]
        if self.row is not None:
            row = {"created_at": NOW, "order_id": None, **self.row}
            rows.append(row)
            return SimpleNamespace(data=[row])
        found = [
            r for r in rows
            if all(r.get(k) == v for k, v in self.eqs.items())
            and all(r.get(c) is None for c in self.nulls)
            and all(r.get(c) is not None for c in self.not_null)
            and (self.since is None or r[self.since[0]] >= self.since[1])
        ]
        if self.start is not None:
            found = found[self.start : self.end + 1]
        return SimpleNamespace(data=found)


class FakeDB:
    def __init__(self):
        self.tables = {"orders": [], "checkouts": []}
        self.rpc_calls = []
        self.tokens = {}  # bearer token -> JWT claims, so each test client keeps its own identity
        self.users = {
            ADMIN_ID: SimpleNamespace(app_metadata={"roles": ["super_admin"]}, banned_until=None),
            ALICE_ID: SimpleNamespace(app_metadata={}, banned_until=None, email_confirmed_at=NOW),
            BOB_ID: SimpleNamespace(app_metadata={}, banned_until=None, email_confirmed_at=NOW),
        }
        self.auth = self
        self.admin = self

    @property
    def orders(self):
        return self.tables["orders"]

    @property
    def checkouts(self):
        return self.tables["checkouts"]

    # auth
    def get_claims(self, token):
        return {"claims": self.tokens[token]}

    def get_user_by_id(self, uid):
        from supabase_auth.errors import AuthApiError

        if uid not in self.users:
            raise AuthApiError("User not found", 404, "user_not_found")
        return SimpleNamespace(user=self.users[uid])

    # data
    def table(self, name):
        return Query(self, name)

    def add_order(self, user_id, **over):
        row = {
            "id": str(uuid4()), "user_id": user_id,
            "customer_name": "Asha", "customer_email": None, "customer_phone": "+91 98765 43210",
            "ship_line1": "1 Main", "ship_line2": None, "ship_city": "Chennai", "ship_state": None,
            "ship_postal_code": "600001", "ship_country": "India",
            "total_amount": 220.25, "paid_at": NOW,
            "razorpay_order_id": f"order_{uuid4().hex[:8]}", "razorpay_payment_id": f"pay_{uuid4().hex[:8]}",
            "delivery_status": "order_placed", "in_transit_at": None, "delivered_at": None,
            "created_at": NOW, "updated_at": NOW,
            "items": [{"id": str(uuid4()), "product_id": PRODUCT_ID, "product_title": "Desk", "unit_price": 100.10, "quantity": 2}],
            **over,
        }
        self.orders.append(row)
        return row

    def rpc(self, name, params):
        self.rpc_calls.append((name, params))
        result = getattr(self, f"_rpc_{name}")(params)
        return SimpleNamespace(execute=lambda: SimpleNamespace(data=result))

    def _rpc_quote_order(self, params):
        items = params["p"]["items"]
        if not items or any(i["product_id"] not in PRICES for i in items):
            return None
        total = sum(Decimal(PRICES[i["product_id"]][1]) * i["quantity"] for i in items)
        return {
            "total": str(total),
            "items": [
                {"product_id": i["product_id"], "product_title": PRICES[i["product_id"]][0],
                 "unit_price": PRICES[i["product_id"]][1], "quantity": i["quantity"]}
                for i in items
            ],
        }

    def _rpc_complete_checkout(self, params):
        c = next((c for c in self.checkouts if c["razorpay_order_id"] == params["rzp_order_id"]), None)
        if c is None:
            return None
        if c["order_id"]:
            return c["order_id"]
        s = c["snapshot"]
        order = self.add_order(
            c["user_id"], customer_name=s["customer_name"], customer_email=s["customer_email"],
            total_amount=s["total"], razorpay_order_id=c["razorpay_order_id"],
            razorpay_payment_id=params["rzp_payment_id"],
            items=[{"id": str(uuid4()), **i} for i in s["items"]],
        )
        c["order_id"] = order["id"]
        return order["id"]

    def _rpc_update_delivery_status(self, params):
        found = next((o for o in self.orders if o["id"] == params["oid"]), None)
        nxt = {"order_placed": "in_transit", "in_transit": "delivered"}
        if not found:
            return "not_found"
        if nxt.get(found["delivery_status"]) != params["new_status"]:
            return "invalid_transition"
        found["delivery_status"] = params["new_status"]
        return "ok"


@pytest.fixture(autouse=True)
def email_confirmation_required(monkeypatch):
    """Tests must not depend on the developer's local .env: assume the production default (on)."""
    monkeypatch.setattr("app.auth.deps.get_settings", lambda: SimpleNamespace(require_confirmed_email=True))


@pytest.fixture
def db(monkeypatch):
    fake = FakeDB()
    for target in (
        "app.auth.deps.get_supabase",
        "app.orders.router.get_supabase",
        "app.payments.router.get_supabase",
    ):
        monkeypatch.setattr(target, lambda: fake)
    return fake


@pytest.fixture
def gateway():
    gw = DummyGateway()
    app.dependency_overrides[get_gateway] = lambda: gw
    yield gw
    app.dependency_overrides.pop(get_gateway, None)


def client_as(db, user_id, **claims):
    token = f"tok.{user_id}.{len(db.tokens)}"
    db.tokens[token] = {"sub": user_id, **claims}
    return TestClient(app, headers={"Authorization": f"Bearer {token}"})


@pytest.fixture
def alice(db, gateway):
    return client_as(db, ALICE_ID, email="alice@example.com")


@pytest.fixture
def bob(db, gateway):
    return client_as(db, BOB_ID)


@pytest.fixture
def admin(db, gateway):
    return client_as(db, ADMIN_ID)


def pay(client, gateway, checkout):
    """What happens in the browser: pay in Razorpay Checkout, then call verify."""
    return client.post("/checkout/verify", json=gateway.simulate_payment(checkout["razorpay_order_id"]))


def webhook_post(db_client, payload: dict, secret=DUMMY_WEBHOOK_SECRET, signature=None):
    raw = json.dumps(payload).encode()
    headers = {"x-razorpay-signature": signature if signature is not None else sign(secret, raw)}
    return TestClient(app).post("/webhooks/razorpay", content=raw, headers=headers)


def captured(checkout, payment_id="pay_hook1", **over):
    entity = {
        "id": payment_id, "order_id": checkout["razorpay_order_id"], "status": "captured",
        "amount": checkout["amount"], "currency": "INR", **over,
    }
    return {"event": "payment.captured", "payload": {"payment": {"entity": entity}}}


# --- authentication gate on checkout ---------------------------------------


def test_checkout_requires_a_token(db, gateway):
    assert TestClient(app).post("/checkout", json=BODY).status_code in (401, 403)
    assert not db.checkouts


def test_invalid_token_rejected(db, gateway):
    from supabase_auth.errors import AuthInvalidJwtError

    def boom(token):
        raise AuthInvalidJwtError("bad")

    db.get_claims = boom
    r = TestClient(app, headers={"Authorization": "Bearer x.y.z"}).post("/checkout", json=BODY)
    assert r.status_code == 401 and not db.checkouts


def test_anonymous_supabase_session_cannot_check_out(db, gateway):
    c = client_as(db, ALICE_ID, is_anonymous=True)
    assert c.post("/checkout", json=BODY).status_code == 403
    assert not db.checkouts


def test_deactivated_account_cannot_check_out_despite_valid_token(alice, db):
    db.users[ALICE_ID].banned_until = "2999-01-01T00:00:00Z"
    assert alice.post("/checkout", json=BODY).status_code == 403
    assert not db.checkouts


def test_unconfirmed_email_cannot_check_out_or_pay(alice, db, gateway):
    checkout = alice.post("/checkout", json=BODY).json()  # confirmed at this point
    db.users[ALICE_ID].email_confirmed_at = None
    assert alice.post("/checkout", json=BODY).status_code == 403
    assert pay(alice, gateway, checkout).status_code == 403
    assert not db.orders


def test_unconfirmed_email_is_allowed_when_the_requirement_is_off(alice, db, gateway, monkeypatch):
    # v1 runs without an email service: Supabase "Confirm email" off, REQUIRE_CONFIRMED_EMAIL=false.
    monkeypatch.setattr("app.auth.deps.get_settings", lambda: SimpleNamespace(require_confirmed_email=False))
    db.users[ALICE_ID].email_confirmed_at = None
    checkout = alice.post("/checkout", json=BODY)
    assert checkout.status_code == 201
    assert pay(alice, gateway, checkout.json()).status_code == 200


def test_the_requirement_is_on_by_default():
    from app.config import Settings

    assert Settings(_env_file=None, supabase_url="u", supabase_secret_key="k").require_confirmed_email is True


def test_deleted_account_cannot_check_out(alice, db):
    del db.users[ALICE_ID]
    assert alice.post("/checkout", json=BODY).status_code == 401


def test_deactivated_account_cannot_verify_a_payment(alice, db, gateway):
    checkout = alice.post("/checkout", json=BODY).json()
    db.users[ALICE_ID].banned_until = "2999-01-01T00:00:00Z"
    assert pay(alice, gateway, checkout).status_code == 403
    assert not db.orders


# --- starting a checkout ----------------------------------------------------


def test_checkout_prices_cart_and_creates_no_order(alice, db):
    r = alice.post("/checkout", json=BODY)
    assert r.status_code == 201
    body = r.json()
    assert body["amount"] == 20020 and body["currency"] == "INR" and body["total"] == "200.20"
    assert body["razorpay_key_id"] == "rzp_test_dummy" and body["razorpay_order_id"].startswith("order_dummy")
    assert body["prefill"] == {"name": "Asha Rao", "email": "alice@example.com", "contact": "+91 98765 43210"}
    assert not db.orders  # nothing is an order until it is paid
    stored = db.checkouts[0]
    assert stored["user_id"] == ALICE_ID and stored["amount_paise"] == 20020
    assert stored["razorpay_order_id"] == body["razorpay_order_id"]


def test_client_cannot_influence_price_owner_or_status(alice, db):
    evil = {
        **BODY, "user_id": BOB_ID, "total": "0.01", "amount": 1, "paid": True,
        "items": [{"product_id": PRODUCT_ID, "quantity": 1, "unit_price": "0.01"}],
    }
    r = alice.post("/checkout", json=evil)
    assert r.status_code == 201 and r.json()["amount"] == 10010  # priced from the database
    assert db.checkouts[0]["user_id"] == ALICE_ID
    assert db.rpc_calls[0][1] == {"p": {"items": [{"product_id": PRODUCT_ID, "quantity": 1}]}}


def test_explicit_email_beats_account_email(alice, db):
    body = {**BODY, "customer": {**BODY["customer"], "email": "other@example.com"}}
    assert alice.post("/checkout", json=body).json()["prefill"]["email"] == "other@example.com"


def test_unknown_product_is_422_and_stores_nothing(alice, db):
    body = {**BODY, "items": [{"product_id": str(uuid4()), "quantity": 1}]}
    assert alice.post("/checkout", json=body).status_code == 422
    assert not db.checkouts


def test_total_below_minimum_is_422(alice, db):
    body = {**BODY, "items": [{"product_id": CHEAP_ID, "quantity": 1}]}  # Rs 0.50
    assert alice.post("/checkout", json=body).status_code == 422
    assert not db.checkouts


def test_razorpay_failure_is_502_and_stores_nothing(alice, db, gateway):
    def fail(*a, **k):
        raise GatewayError("down")

    gateway.create_order = fail
    assert alice.post("/checkout", json=BODY).status_code == 502
    assert not db.checkouts


def test_too_many_unpaid_checkouts_is_429(alice, db):
    codes = [alice.post("/checkout", json=BODY).status_code for _ in range(11)]
    assert codes == [201] * 10 + [429]


def test_paid_checkouts_do_not_count_toward_the_limit(alice, db, gateway):
    for _ in range(10):
        pay(alice, gateway, alice.post("/checkout", json=BODY).json())
    assert alice.post("/checkout", json=BODY).status_code == 201


@pytest.mark.parametrize(
    "patch",
    [
        {"items": []},
        {"items": [{"product_id": PRODUCT_ID, "quantity": 0}]},
        {"items": [{"product_id": PRODUCT_ID, "quantity": 1001}]},
        {"items": [{"product_id": "nope", "quantity": 1}]},
        {"items": [{"product_id": PRODUCT_ID, "quantity": 1}] * 2},
        {"customer": {"name": " ", "phone": "+91 98765 43210"}},
        {"customer": {"name": "A", "phone": "abc"}},
        {"customer": {"name": "A", "phone": "+91 98765 43210", "email": "not-an-email"}},
        {"shipping_address": {"line1": "x", "city": "c", "postal_code": "1"}},
    ],
)
def test_invalid_checkout_is_rejected(alice, db, patch):
    assert alice.post("/checkout", json={**BODY, **patch}).status_code == 422
    assert not db.checkouts


# --- confirming payment from the browser -----------------------------------


def test_paying_creates_the_order(alice, db, gateway):
    checkout = alice.post("/checkout", json=BODY).json()
    r = pay(alice, gateway, checkout)
    assert r.status_code == 200
    order = r.json()
    assert order["razorpay_order_id"] == checkout["razorpay_order_id"]
    assert order["razorpay_payment_id"].startswith("pay_dummy") and order["paid_at"]
    assert order["delivery_status"] == "order_placed" and order["total_amount"] == "200.20"
    assert order["items"][0]["line_total"] == "200.20"
    assert len(db.orders) == 1 and db.orders[0]["user_id"] == ALICE_ID


def test_verifying_twice_returns_the_same_single_order(alice, db, gateway):
    checkout = alice.post("/checkout", json=BODY).json()
    proof = gateway.simulate_payment(checkout["razorpay_order_id"])
    first = alice.post("/checkout/verify", json=proof).json()
    second = alice.post("/checkout/verify", json=proof).json()
    assert first["id"] == second["id"] and len(db.orders) == 1


def test_bad_signature_creates_no_order(alice, db, gateway):
    checkout = alice.post("/checkout", json=BODY).json()
    proof = {**gateway.simulate_payment(checkout["razorpay_order_id"]), "razorpay_signature": "0" * 64}
    assert alice.post("/checkout/verify", json=proof).status_code == 400
    assert not db.orders


def test_signature_for_a_different_payment_id_is_rejected(alice, db, gateway):
    checkout = alice.post("/checkout", json=BODY).json()
    proof = gateway.simulate_payment(checkout["razorpay_order_id"])
    forged = {**proof, "razorpay_payment_id": "pay_someone_elses"}
    assert alice.post("/checkout/verify", json=forged).status_code == 400
    assert not db.orders


def test_signature_made_with_the_wrong_secret_is_rejected(alice, db, gateway):
    checkout = alice.post("/checkout", json=BODY).json()
    oid = checkout["razorpay_order_id"]
    forged = {"razorpay_order_id": oid, "razorpay_payment_id": "pay_x",
              "razorpay_signature": sign("not-the-secret", f"{oid}|pay_x".encode())}
    assert alice.post("/checkout/verify", json=forged).status_code == 400


def test_cannot_claim_another_users_checkout(alice, bob, db, gateway):
    checkout = alice.post("/checkout", json=BODY).json()
    proof = gateway.simulate_payment(checkout["razorpay_order_id"])  # even a valid signature
    assert bob.post("/checkout/verify", json=proof).status_code == 404
    assert not db.orders


def test_unknown_checkout_is_404(alice, gateway):
    assert alice.post("/checkout/verify", json=gateway.simulate_payment("order_nope")).status_code == 404


def test_verify_requires_authentication(db, gateway):
    proof = gateway.simulate_payment("order_x")
    assert TestClient(app).post("/checkout/verify", json=proof).status_code in (401, 403)


# --- confirming payment via webhook ----------------------------------------


def test_webhook_creates_the_order_when_the_browser_never_calls_back(alice, db):
    checkout = alice.post("/checkout", json=BODY).json()
    r = webhook_post(db, captured(checkout))  # no bearer token: the signature is the auth
    assert r.status_code == 200 and r.json() == {"status": "completed"}
    assert len(db.orders) == 1 and db.orders[0]["razorpay_payment_id"] == "pay_hook1"
    assert db.orders[0]["user_id"] == ALICE_ID


def test_webhook_then_browser_verify_gives_one_order(alice, db, gateway):
    checkout = alice.post("/checkout", json=BODY).json()
    webhook_post(db, captured(checkout))
    r = pay(alice, gateway, checkout)
    assert r.status_code == 200 and len(db.orders) == 1 and r.json()["id"] == db.orders[0]["id"]


def test_browser_verify_then_webhook_gives_one_order(alice, db, gateway):
    checkout = alice.post("/checkout", json=BODY).json()
    pay(alice, gateway, checkout)
    assert webhook_post(db, captured(checkout)).status_code == 200
    assert len(db.orders) == 1


def test_webhook_redelivery_is_harmless(alice, db):
    checkout = alice.post("/checkout", json=BODY).json()
    for _ in range(3):
        assert webhook_post(db, captured(checkout)).status_code == 200
    assert len(db.orders) == 1


@pytest.mark.parametrize("signature", ["", "0" * 64, "garbage"])
def test_webhook_with_bad_signature_is_rejected(alice, db, signature):
    checkout = alice.post("/checkout", json=BODY).json()
    assert webhook_post(db, captured(checkout), signature=signature).status_code == 400
    assert not db.orders


def test_webhook_signed_with_wrong_secret_is_rejected(alice, db):
    checkout = alice.post("/checkout", json=BODY).json()
    assert webhook_post(db, captured(checkout), secret="attacker").status_code == 400
    assert not db.orders


def test_webhook_signature_covers_the_body(alice, db):
    checkout = alice.post("/checkout", json=BODY).json()
    raw = json.dumps(captured(checkout)).encode()
    sig = sign(DUMMY_WEBHOOK_SECRET, raw)
    tampered = raw.replace(b"pay_hook1", b"pay_evil99")
    r = TestClient(app).post("/webhooks/razorpay", content=tampered, headers={"x-razorpay-signature": sig})
    assert r.status_code == 400 and not db.orders


def test_webhook_for_a_smaller_payment_than_quoted_makes_no_order(alice, db):
    checkout = alice.post("/checkout", json=BODY).json()
    r = webhook_post(db, captured(checkout, amount=100))
    assert r.status_code == 200 and r.json() == {"status": "ignored_amount_mismatch"}
    assert not db.orders


def test_webhook_for_wrong_currency_makes_no_order(alice, db):
    checkout = alice.post("/checkout", json=BODY).json()
    assert webhook_post(db, captured(checkout, currency="USD")).json() == {"status": "ignored_amount_mismatch"}
    assert not db.orders


@pytest.mark.parametrize("status_value", ["failed", "authorized", "created"])
def test_webhook_for_unpaid_payment_makes_no_order(alice, db, status_value):
    checkout = alice.post("/checkout", json=BODY).json()
    assert webhook_post(db, captured(checkout, status=status_value)).json() == {"status": "ignored_not_captured"}
    assert not db.orders


def test_webhook_ignores_other_events_and_unknown_checkouts_with_200(alice, db):
    checkout = alice.post("/checkout", json=BODY).json()
    assert webhook_post(db, {"event": "refund.created", "payload": {}}).json() == {"status": "ignored_event"}
    unknown = {**checkout, "razorpay_order_id": "order_nope"}
    assert webhook_post(db, captured(unknown)).json() == {"status": "ignored_unknown_checkout"}
    assert webhook_post(db, {"event": "payment.captured", "payload": {}}).json() == {"status": "ignored_malformed"}
    assert not db.orders


# --- dummy-mode helper ------------------------------------------------------


def test_simulate_payment_only_for_your_own_checkout(alice, bob, db):
    checkout = alice.post("/checkout", json=BODY).json()
    ok = alice.post("/dev/razorpay/simulate-payment", json={"razorpay_order_id": checkout["razorpay_order_id"]})
    assert ok.status_code == 200 and ok.json()["razorpay_signature"]
    assert bob.post("/dev/razorpay/simulate-payment", json={"razorpay_order_id": checkout["razorpay_order_id"]}).status_code == 404


def test_simulate_payment_does_not_exist_with_a_real_gateway(alice, db):
    from app.payments.razorpay import RazorpayGateway

    checkout = alice.post("/checkout", json=BODY).json()
    app.dependency_overrides[get_gateway] = lambda: RazorpayGateway("rzp_test_x", "s", "w")
    try:
        r = alice.post("/dev/razorpay/simulate-payment", json={"razorpay_order_id": checkout["razorpay_order_id"]})
    finally:
        app.dependency_overrides.pop(get_gateway, None)
    assert r.status_code == 404


# --- customers see only their own orders ------------------------------------


def test_anonymous_cannot_view_orders(db, gateway):
    assert TestClient(app).get("/orders").status_code in (401, 403)


def test_customer_sees_only_own_orders(alice, db):
    mine = db.add_order(ALICE_ID)
    db.add_order(BOB_ID)
    assert [o["id"] for o in alice.get("/orders").json()] == [mine["id"]]


def test_customer_cannot_read_someone_elses_order(alice, db):
    bobs = db.add_order(BOB_ID)
    assert alice.get(f"/orders/{bobs['id']}").status_code == 404  # 404, not 403: no id probing
    assert alice.get(f"/orders/{uuid4()}").status_code == 404


def test_customer_can_read_own_order(alice, db):
    mine = db.add_order(ALICE_ID)
    assert alice.get(f"/orders/{mine['id']}").json()["id"] == mine["id"]


def test_amounts_always_have_two_decimal_places(alice, db):
    db.add_order(ALICE_ID, total_amount=20, items=[
        {"id": str(uuid4()), "product_id": PRODUCT_ID, "product_title": "Pen", "unit_price": 10, "quantity": 2},
    ])
    body = alice.get("/orders").json()[0]
    assert body["total_amount"] == "20.00"
    assert body["items"][0]["unit_price"] == "10.00" and body["items"][0]["line_total"] == "20.00"


# --- admin only -------------------------------------------------------------


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("get", "/admin/orders", None),
        ("get", f"/admin/orders/{uuid4()}", None),
        ("post", f"/admin/orders/{uuid4()}/delivery-status", {"status": "in_transit"}),
    ],
)
def test_customer_cannot_use_admin_order_routes(alice, method, path, body):
    assert getattr(alice, method)(path, **({"json": body} if body is not None else {})).status_code == 403


def test_there_is_no_way_to_mark_an_order_paid_by_hand(admin, db):
    o = db.add_order(ALICE_ID)
    assert admin.post(f"/admin/orders/{o['id']}/mark-paid").status_code == 404


def test_admin_sees_all_orders_and_can_filter_by_delivery(admin, db):
    db.add_order(ALICE_ID)
    db.add_order(BOB_ID)
    transit = db.add_order(ALICE_ID, delivery_status="in_transit")
    assert len(admin.get("/admin/orders").json()) == 3
    assert [o["id"] for o in admin.get("/admin/orders?delivery_status=in_transit").json()] == [transit["id"]]
    assert admin.get("/admin/orders?delivery_status=shipped").status_code == 422


def test_admin_can_read_any_order(admin, db):
    bobs = db.add_order(BOB_ID)
    assert admin.get(f"/admin/orders/{bobs['id']}").json()["id"] == bobs["id"]


def test_delivery_moves_forward_one_step_at_a_time(admin, db):
    o = db.add_order(ALICE_ID)
    url = f"/admin/orders/{o['id']}/delivery-status"
    assert admin.post(url, json={"status": "delivered"}).status_code == 409  # can't skip
    assert admin.post(url, json={"status": "in_transit"}).json()["delivery_status"] == "in_transit"
    assert admin.post(url, json={"status": "in_transit"}).status_code == 409  # can't repeat
    assert admin.post(url, json={"status": "delivered"}).json()["delivery_status"] == "delivered"
    assert admin.post(url, json={"status": "order_placed"}).status_code == 409  # can't go back


def test_delivery_rejects_unknown_status_and_unknown_order(admin, db):
    o = db.add_order(ALICE_ID)
    assert admin.post(f"/admin/orders/{o['id']}/delivery-status", json={"status": "lost"}).status_code == 422
    assert admin.post(f"/admin/orders/{uuid4()}/delivery-status", json={"status": "in_transit"}).status_code == 404


def test_deactivated_admin_cannot_change_orders(admin, db):
    o = db.add_order(ALICE_ID)
    db.users[ADMIN_ID].banned_until = "2999-01-01T00:00:00Z"
    assert admin.post(f"/admin/orders/{o['id']}/delivery-status", json={"status": "in_transit"}).status_code == 403
