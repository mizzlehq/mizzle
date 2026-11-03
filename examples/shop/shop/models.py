"""API request and response models, built from the tables.

Request models list the fields a client may send with ``include=`` (an allowlist).
"""

import datetime
import uuid
from decimal import Decimal

from pydantic import BaseModel, Field

from mizzle.pydantic import insert_model, select_model, update_model
from shop.schema import (
    AuditLog,
    Inventory,
    Notifications,
    OrderItems,
    Orders,
    Products,
    Reviews,
    Stores,
    Users,
)


class UserIn(insert_model(Users, include=["email", "name", "phone", "role", "tags", "prefs"])):
    pass


class UserPatch(update_model(Users, include=["name", "phone", "tags", "prefs"])):
    pass


class UserOut(select_model(Users)):
    pass


class StoreIn(insert_model(Stores, include=["slug", "name"])):
    pass


class StoreOut(select_model(Stores)):
    pass


class ProductIn(
    insert_model(Products, include=["sku", "name", "details", "price", "tags", "attrs", "active"])
):
    stock: dict[str, int] = Field(default_factory=dict[str, int])


class ProductPatch(update_model(Products, include=["name", "details", "price", "tags", "active"])):
    pass


class ProductOut(select_model(Products, exclude=["search"])):
    pass


class InventoryOut(select_model(Inventory)):
    pass


class ItemIn(BaseModel):
    product_id: int
    quantity: int = Field(gt=0)


class OrderIn(BaseModel):
    store_id: int
    items: list[ItemIn] = Field(min_length=1)
    note: str | None = None


class OrderOut(select_model(Orders)):
    pass


class OrderItemOut(select_model(OrderItems)):
    pass


class OrderDetail(BaseModel):
    order: OrderOut
    items: list[OrderItemOut]
    store: StoreOut
    customer: UserOut


class ReviewIn(insert_model(Reviews, include=["rating", "body"])):
    pass


class ReviewOut(select_model(Reviews)):
    pass


class NotificationOut(select_model(Notifications)):
    pass


class AuditOut(select_model(AuditLog)):
    pass


class StoreDetail(BaseModel):
    store: StoreOut
    owner: UserOut
    products: list[ProductOut]
    stock: dict[int, int]  # product id -> units over all warehouses


class ProductDetail(BaseModel):
    product: ProductOut
    store: StoreOut
    reviews: list[ReviewOut]
    review_count: int
    rating: Decimal | None


class SearchHit(BaseModel):
    id: int
    name: str
    price: Decimal
    rank: float
    cover: float
    price_band: str


class SimilarHit(BaseModel):
    id: int
    name: str
    distance: float


class SalesRow(BaseModel):
    store_id: int
    orders: int
    revenue: Decimal | None


class ReportRow(BaseModel):
    product_id: int
    name: str
    units: int
    revenue: Decimal
    rank_in_store: int
    running_revenue: Decimal
    previous_revenue: Decimal | None
    quartile: int


class DayRow(BaseModel):
    day: datetime.datetime
    orders: int
    revenue: Decimal
    weekday: Decimal
    size: str


class CustomerRow(BaseModel):
    user_id: uuid.UUID
    name: str
    spent: Decimal
    orders: int
    label: str


class Embedding(BaseModel):
    embedding: list[float]
    half: list[float] | None = None
    keywords: dict[int, float] | None = None


class BulkResult(BaseModel):
    rows: int
    ids: list[int] = Field(default_factory=list[int])


class Health(BaseModel):
    pool_size: int
    pool_idle: int
    replicas: int
    vectors: bool
