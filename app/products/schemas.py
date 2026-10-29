from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, Field, StringConstraints
from typing import Annotated

# Strip whitespace so "   " can't pass as a title.
Title = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
SubTitle = Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)]
Paragraph = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=5000)]
Amount = Annotated[Decimal, Field(ge=0, max_digits=12, decimal_places=2)]

MAX_DETAILS = 50


class DetailIn(BaseModel):
    """One section of the product's dynamic sub form."""

    title: Title
    subtitle: SubTitle | None = None
    paragraph: Paragraph


class ProductCreate(BaseModel):
    title: Title
    amount: Amount
    description: Annotated[str, StringConstraints(max_length=10000)] = ""
    details: list[DetailIn] = Field(default_factory=list, max_length=MAX_DETAILS)


class ProductUpdate(BaseModel):
    """Partial update. Omitted fields are untouched; `details`, when sent, replaces the whole list."""

    title: Title | None = None
    amount: Amount | None = None
    description: Annotated[str, StringConstraints(max_length=10000)] | None = None
    details: list[DetailIn] | None = Field(default=None, max_length=MAX_DETAILS)


class DetailOut(BaseModel):
    id: UUID
    title: str
    subtitle: str | None
    paragraph: str
    position: int


class ImageOut(BaseModel):
    id: UUID
    url: str
    position: int


class ProductOut(BaseModel):
    id: UUID
    title: str
    amount: Decimal
    description: str
    images: list[ImageOut]
    details: list[DetailOut]
    created_at: datetime
    updated_at: datetime
