from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, Field, StringConstraints, field_validator


class DeliveryStatus(StrEnum):
    ORDER_PLACED = "order_placed"
    IN_TRANSIT = "in_transit"
    DELIVERED = "delivered"


def _text(min_length: int, max_length: int):
    return Annotated[str, StringConstraints(strip_whitespace=True, min_length=min_length, max_length=max_length)]


MAX_ITEMS = 50


class Customer(BaseModel):
    name: _text(1, 200)
    # Optional on input: defaults to the signed-in account's email.
    email: Annotated[str, StringConstraints(strip_whitespace=True, max_length=320, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")] | None = None
    phone: Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^\+?[0-9][0-9 \-]{5,18}[0-9]$")]


class ShippingAddress(BaseModel):
    line1: _text(1, 200)
    line2: _text(0, 200) | None = None
    city: _text(1, 100)
    state: _text(0, 100) | None = None
    postal_code: _text(1, 20)
    country: _text(2, 100)


class OrderItemIn(BaseModel):
    product_id: UUID
    quantity: Annotated[int, Field(ge=1, le=1000)]


class OrderCreate(BaseModel):
    """What the customer sends. Prices and totals are deliberately absent: the database sets them."""

    customer: Customer
    shipping_address: ShippingAddress
    items: Annotated[list[OrderItemIn], Field(min_length=1, max_length=MAX_ITEMS)]

    @field_validator("items")
    @classmethod
    def no_duplicate_products(cls, items: list[OrderItemIn]) -> list[OrderItemIn]:
        ids = [i.product_id for i in items]
        if len(ids) != len(set(ids)):
            raise ValueError("Each product may appear only once; increase its quantity instead")
        return items


class VerifyPayment(BaseModel):
    """The three values Razorpay Checkout hands the browser after a successful payment."""

    razorpay_order_id: _text(1, 100)
    razorpay_payment_id: _text(1, 100)
    razorpay_signature: _text(1, 200)


class CheckoutItemOut(BaseModel):
    product_id: UUID
    product_title: str
    unit_price: Decimal
    quantity: int


class Prefill(BaseModel):
    name: str
    email: str | None
    contact: str


class CheckoutOut(BaseModel):
    """Everything the frontend needs to open Razorpay Checkout. No order exists yet."""

    checkout_id: UUID
    razorpay_key_id: str
    razorpay_order_id: str
    amount: int  # in paise, exactly what Razorpay will charge
    currency: str
    total: Decimal
    items: list[CheckoutItemOut]
    prefill: Prefill


class DeliveryUpdate(BaseModel):
    status: DeliveryStatus


class OrderItemOut(BaseModel):
    id: UUID
    # Null if the product was later deleted; the title and price below are the snapshot.
    product_id: UUID | None
    product_title: str
    unit_price: Decimal
    quantity: int
    line_total: Decimal


class OrderOut(BaseModel):
    id: UUID
    customer: Customer
    shipping_address: ShippingAddress
    items: list[OrderItemOut]
    total_amount: Decimal
    paid_at: datetime
    razorpay_order_id: str
    razorpay_payment_id: str
    delivery_status: DeliveryStatus
    in_transit_at: datetime | None
    delivered_at: datetime | None
    created_at: datetime
    updated_at: datetime
