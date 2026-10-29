from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID, uuid4

from app.money import money
from app.orders.schemas import DeliveryStatus, OrderCreate, OrderOut
from app.payments.razorpay import CURRENCY, Gateway

SELECT = "*, items:order_items(*)"

# Each started checkout creates a Razorpay order and a row; cap how many unpaid ones one user can
# pile up so the endpoint can't be used to spam either.
MAX_OPEN_CHECKOUTS_PER_HOUR = 10
MIN_AMOUNT_PAISE = 100  # Razorpay's minimum is Rs 1.00


class OrderNotFoundError(Exception):
    pass


class CheckoutNotFoundError(Exception):
    pass


class UnknownProductError(Exception):
    pass


class InvalidAmountError(Exception):
    pass


class TooManyCheckoutsError(Exception):
    pass


class InvalidTransitionError(Exception):
    pass


def to_order_out(row: dict) -> OrderOut:
    items = sorted(row.get("items") or [], key=lambda i: i["product_title"])
    return OrderOut(
        id=row["id"],
        customer={"name": row["customer_name"], "email": row["customer_email"], "phone": row["customer_phone"]},
        shipping_address={
            "line1": row["ship_line1"],
            "line2": row["ship_line2"],
            "city": row["ship_city"],
            "state": row["ship_state"],
            "postal_code": row["ship_postal_code"],
            "country": row["ship_country"],
        },
        items=[
            {
                "id": i["id"],
                "product_id": i["product_id"],
                "product_title": i["product_title"],
                "unit_price": money(i["unit_price"]),
                "quantity": i["quantity"],
                "line_total": money(i["unit_price"]) * i["quantity"],
            }
            for i in items
        ],
        total_amount=money(row["total_amount"]),
        paid_at=row["paid_at"],
        razorpay_order_id=row["razorpay_order_id"],
        razorpay_payment_id=row["razorpay_payment_id"],
        delivery_status=row["delivery_status"],
        in_transit_at=row["in_transit_at"],
        delivered_at=row["delivered_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


# --- checkout (before payment) -----------------------------------------------------------------


def start_checkout(db, gateway: Gateway, user_id: str, account_email: str | None, data: OrderCreate) -> dict:
    """Price the cart, create the Razorpay order, and remember the quote. Creates NO order."""
    since = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    open_recent = (
        db.table("checkouts")
        .select("id")
        .eq("user_id", user_id)
        .is_("order_id", "null")
        .gte("created_at", since)
        .execute()
        .data
    )
    if len(open_recent) >= MAX_OPEN_CHECKOUTS_PER_HOUR:
        raise TooManyCheckoutsError()

    items = [{"product_id": str(i.product_id), "quantity": i.quantity} for i in data.items]
    quote = db.rpc("quote_order", {"p": {"items": items}}).execute().data
    if not quote:
        raise UnknownProductError()

    total = Decimal(quote["total"])
    amount_paise = int(total * 100)  # exact: total has 2 decimal places
    if amount_paise < MIN_AMOUNT_PAISE:
        raise InvalidAmountError()

    checkout_id = str(uuid4())
    razorpay_order_id = gateway.create_order(amount_paise, receipt=checkout_id)

    c, a = data.customer, data.shipping_address
    snapshot = {
        "customer_name": c.name,
        "customer_email": c.email or account_email,
        "customer_phone": c.phone,
        "ship_line1": a.line1,
        "ship_line2": a.line2,
        "ship_city": a.city,
        "ship_state": a.state,
        "ship_postal_code": a.postal_code,
        "ship_country": a.country,
        "total": quote["total"],
        "items": quote["items"],
    }
    db.table("checkouts").insert(
        {
            "id": checkout_id,
            "user_id": user_id,
            "razorpay_order_id": razorpay_order_id,
            "amount_paise": amount_paise,
            "currency": CURRENCY,
            "snapshot": snapshot,
        }
    ).execute()
    return {
        "checkout_id": checkout_id,
        "razorpay_key_id": gateway.key_id,
        "razorpay_order_id": razorpay_order_id,
        "amount": amount_paise,
        "currency": CURRENCY,
        "total": total,
        "items": [
            {**i, "unit_price": Decimal(i["unit_price"])} for i in quote["items"]
        ],
        "prefill": {"name": snapshot["customer_name"], "email": snapshot["customer_email"], "contact": c.phone},
    }


def get_checkout(db, razorpay_order_id: str, user_id: str | None = None) -> dict:
    query = db.table("checkouts").select("*").eq("razorpay_order_id", razorpay_order_id)
    if user_id is not None:
        query = query.eq("user_id", user_id)
    rows = query.limit(1).execute().data
    if not rows:
        raise CheckoutNotFoundError(razorpay_order_id)
    return rows[0]


def complete_checkout(db, razorpay_order_id: str, razorpay_payment_id: str) -> str:
    """Turn a paid checkout into an order. Idempotent: repeat calls return the same order id."""
    order_id = (
        db.rpc("complete_checkout", {"rzp_order_id": razorpay_order_id, "rzp_payment_id": razorpay_payment_id})
        .execute()
        .data
    )
    if not order_id:
        raise CheckoutNotFoundError(razorpay_order_id)
    return order_id


# --- orders (after payment) --------------------------------------------------------------------


def get_order(db, order_id: str, user_id: str | None = None) -> dict:
    """Fetch one order. With `user_id`, only that customer's own order is visible."""
    query = db.table("orders").select(SELECT).eq("id", order_id)
    if user_id is not None:
        query = query.eq("user_id", user_id)
    rows = query.limit(1).execute().data
    if not rows:
        raise OrderNotFoundError(order_id)
    return rows[0]


def list_orders(
    db,
    limit: int,
    offset: int,
    user_id: str | None = None,
    delivery_status: DeliveryStatus | None = None,
) -> list[dict]:
    query = db.table("orders").select(SELECT)
    if user_id is not None:
        query = query.eq("user_id", user_id)
    if delivery_status is not None:
        query = query.eq("delivery_status", delivery_status.value)
    return query.order("created_at", desc=True).order("id").range(offset, offset + limit - 1).execute().data


def set_delivery_status(db, order_id: str, status: DeliveryStatus) -> None:
    result = db.rpc("update_delivery_status", {"oid": order_id, "new_status": status.value}).execute().data
    if result == "not_found":
        raise OrderNotFoundError(order_id)
    if result != "ok":
        raise InvalidTransitionError(result)
