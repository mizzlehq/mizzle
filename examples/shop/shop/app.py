"""A multi-tenant marketplace API: users, stores, products, inventory, orders, reviews,
notifications and an audit log.

    uv run uvicorn shop.app:create_app --factory

Authentication is out of scope: the ``X-User-Id`` header (a user's uuid) stands in for a
verified JWT. Endpoints under "signed in" run as the ``shop_authenticated`` role with the
user's claims, so PostgreSQL's row-level security decides which rows they see.
"""

import asyncio
import datetime
import json
import os
import uuid
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any, Literal, final
from typing import cast as type_cast

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse, Response, StreamingResponse

from mizzle import (
    CheckViolation,
    ConnectionError,
    Database,
    DatabaseError,
    Expr,
    ForeignKeyViolation,
    QueryTimeout,
    Reconnected,
    SerializationFailure,
    Session,
    Table,
    UniqueViolation,
    alias,
    and_,
    asc,
    between,
    case_,
    cast,
    coalesce,
    concat,
    count,
    date_trunc,
    dense_rank,
    desc,
    eq,
    excluded,
    exists,
    extract,
    func,
    gt,
    gte,
    ilike,
    in_,
    is_null,
    lag,
    like,
    lower,
    lt,
    matches,
    ne,
    not_,
    now,
    ntile,
    or_,
    placeholder,
    rank,
    regex,
    round_,
    similar_to,
    sql,
    sum_,
    ts_rank,
    ts_rank_cd,
    websearch_to_tsquery,
)
from mizzle.kit import migrate
from mizzle.kit.config import load_dotenv
from mizzle.pg import cosine_distance, date, numeric
from shop.models import (
    AuditOut,
    BulkResult,
    CustomerRow,
    DayRow,
    Embedding,
    Health,
    InventoryOut,
    NotificationOut,
    OrderDetail,
    OrderIn,
    OrderItemOut,
    OrderOut,
    ProductDetail,
    ProductIn,
    ProductOut,
    ProductPatch,
    ReportRow,
    ReviewIn,
    ReviewOut,
    SalesRow,
    SearchHit,
    SimilarHit,
    StoreDetail,
    StoreIn,
    StoreOut,
    UserIn,
    UserOut,
    UserPatch,
)
from shop.schema import (
    TABLES,
    AuditAction,
    AuditLog,
    Inventory,
    Notifications,
    OrderItems,
    Orders,
    OrderStatus,
    ProductRatings,
    Products,
    Reviews,
    Stores,
    StoreSales,
    TopProducts,
    UserRole,
    Users,
)
from shop.schema_types import ProductsInsert
from shop.vectors import ProductEmbeddings

ROOT = Path(__file__).resolve().parent.parent
PRICE_CHANNEL = "shop_price"
WAREHOUSES = ("main", "backup")  # orders take stock from these, in this order


@final
class State:
    """What the lifespan opens; one per app."""

    def __init__(self, db: Database, vectors: bool) -> None:
        self.db = db
        self.vectors = vectors
        # Prepared once: the hottest lookup skips query building on every request.
        self.product_by_id = (
            db.select().from_(Products).where(eq(Products.id, placeholder("id"))).prepare()
        )


_state: dict[str, State] = {}


def get_state() -> State:
    return _state["state"]


def get_db() -> Database:
    return _state["state"].db


St = Annotated[State, Depends(get_state)]
DB = Annotated[Database, Depends(get_db)]


async def current_user(db: DB, x_user_id: Annotated[uuid.UUID, Header()]) -> Users:
    user = await db.query(Users).find_first(where=eq(Users.id, x_user_id))
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "unknown X-User-Id")
    return user


Me = Annotated[Users, Depends(current_user)]


@asynccontextmanager
async def signed_in(db: Database, user: Users) -> AsyncGenerator[Session]:
    """A transaction as the ``shop_authenticated`` role with the user's JWT claims, like
    Supabase's API does: row-level security policies see ``auth.uid()``."""
    claims = json.dumps({"sub": str(user.id), "role": "shop_authenticated"})
    async with db.transaction() as tx:
        await tx.execute(sql("SELECT set_config('request.jwt.claims', {}, true)", claims))
        await tx.execute(sql("SET LOCAL ROLE shop_authenticated"))
        yield tx


async def own_store(db: Database, me: Users, store_id: int) -> Stores:
    store = await db.query(Stores).find_first(where=eq(Stores.id, store_id))
    if store is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such store")
    if store.owner_id != me.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "not your store")
    return store


router = APIRouter()


# -- users ----------------------------------------------------------------------------


@router.post("/users", status_code=status.HTTP_201_CREATED)
async def create_user(db: DB, body: UserIn) -> UserOut:
    [user] = await db.insert(Users).values(body).returning()
    return UserOut.model_validate(user)


@router.get("/me")
async def me(me: Me) -> UserOut:
    return UserOut.model_validate(me)


@router.patch("/me")
async def update_me(db: DB, me: Me, body: UserPatch) -> UserOut:
    [user] = await db.update(Users).set(body).where(eq(Users.id, me.id)).returning()
    return UserOut.model_validate(user)  # updated_at moved: on_update_now()


# -- stores ---------------------------------------------------------------------------


@router.post("/stores", status_code=status.HTTP_201_CREATED)
async def create_store(db: DB, me: Me, body: StoreIn) -> StoreOut:
    if me.role is UserRole.CUSTOMER:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "customers cannot open stores")
    values = {**body.model_dump(exclude_unset=True), "owner_id": me.id}
    [store] = await db.insert(Stores).values(values).returning()
    return StoreOut.model_validate(store)


@router.get("/stores/{slug}")
async def store_detail(db: DB, slug: str) -> StoreDetail:
    store = await db.query(Stores).find_first(
        where=eq(Stores.slug, slug),
        with_={
            "owner": True,
            "products": {
                "where": eq(Products.active, True),
                "order_by": Products.id,
                "with_": {"inventory": {"columns": [Inventory.quantity]}},
            },
        },
    )
    if store is None or store.owner is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such store")
    return StoreDetail(
        store=StoreOut.model_validate(store),
        owner=UserOut.model_validate(store.owner),
        products=[ProductOut.model_validate(p) for p in store.products],
        stock={p.id: sum(i.quantity for i in p.inventory) for p in store.products},
    )


@router.get("/stores/{store_id}/sales")
async def store_sales(db: DB, store_id: int) -> SalesRow:
    await db.refresh(StoreSales, concurrently=True)
    rows = await db.primary.select().from_(StoreSales).where(eq(StoreSales.store_id, store_id))
    if not rows:
        return SalesRow(store_id=store_id, orders=0, revenue=None)
    return SalesRow(store_id=store_id, orders=rows[0].orders, revenue=rows[0].revenue)


@router.get("/stores/{store_id}/report")
async def store_report(db: DB, store_id: int) -> list[ReportRow]:
    """Products by revenue (paid orders), with window functions over a subquery."""
    paid = (
        db.select(
            {
                "product_id": OrderItems.product_id,
                "units": sum_(OrderItems.quantity),
                "revenue": sum_(OrderItems.line_total),
            }
        )
        .from_(OrderItems)
        .join(Orders, eq(Orders.id, OrderItems.order_id))
        .where(and_(eq(Orders.store_id, store_id), eq(Orders.status, OrderStatus.PAID)))
        .group_by(OrderItems.product_id)
        .subquery("paid")
    )
    pid, units, revenue = (
        paid.c("product_id", int),
        paid.c("units", int),
        paid.c("revenue", Decimal),
    )
    rows = await (
        db.select(
            pid,
            Products.name,
            units,
            revenue,
            rank().over(order_by=desc(revenue)),
            sum_(revenue).over("by_revenue"),
            lag(revenue).over("by_revenue"),
            ntile(4).over("by_revenue"),
        )
        .from_(paid)
        .join(Products, eq(Products.id, pid))
        .window("by_revenue", order_by=[desc(revenue), asc(pid)])
        .order_by(desc(revenue), asc(pid))
    )
    return [
        ReportRow(
            product_id=p,
            name=n,
            units=u,
            revenue=r,
            rank_in_store=rk,
            running_revenue=Decimal(run or 0),
            previous_revenue=prev,
            quartile=q,
        )
        for p, n, u, r, rk, run, prev, q in rows
    ]


# -- products -------------------------------------------------------------------------


@router.post("/stores/{store_id}/products", status_code=status.HTTP_201_CREATED)
async def create_product(db: DB, me: Me, store_id: int, body: ProductIn) -> ProductOut:
    await own_store(db, me, store_id)
    values = {**body.model_dump(exclude_unset=True, exclude={"stock"}), "store_id": store_id}
    async with db.transaction() as tx:
        [product] = await tx.insert(Products).values(values).returning()
        if body.stock:
            stock = [
                {"product_id": product.id, "warehouse": w, "quantity": q}
                for w, q in body.stock.items()
            ]
            await tx.insert(Inventory).values(stock)
    return ProductOut.model_validate(product)


@router.post("/stores/{store_id}/products/bulk")
async def bulk_products(db: DB, me: Me, store_id: int, body: list[ProductIn]) -> BulkResult:
    """Create or update many products by SKU (insert_many with an upsert)."""
    await own_store(db, me, store_id)
    rows: list[ProductsInsert] = [
        {
            "store_id": store_id,
            "sku": p.model_dump()["sku"],
            "name": p.model_dump()["name"],
            "price": p.model_dump()["price"],
            "tags": p.model_dump().get("tags") or [],
        }
        for p in body
    ]
    upsert = db.insert(Products).on_conflict_do_update(
        target=[Products.store_id, Products.sku],
        set_={"name": excluded(Products.name), "price": excluded(Products.price)},
    )
    saved = await db.insert_many(Products, rows, returning=True, on_conflict=upsert, batch_size=50)
    return BulkResult(rows=len(saved), ids=[p.id for p in saved])


@router.post("/stores/{store_id}/inventory/load")
async def load_inventory(
    db: DB, me: Me, store_id: int, body: dict[int, dict[str, int]]
) -> BulkResult:
    """Load stock for new products with COPY: {product_id: {warehouse: quantity}}."""
    await own_store(db, me, store_id)
    rows = [
        {"product_id": pid, "warehouse": w, "quantity": q}
        for pid, stock in body.items()
        for w, q in stock.items()
    ]
    loaded = await db.copy_into(
        Inventory, rows, columns=[Inventory.product_id, Inventory.warehouse, Inventory.quantity]
    )
    return BulkResult(rows=loaded)


@router.get("/products/search")
async def search(
    db: DB,
    q: str,
    min_price: Decimal | None = None,
    max_price: Decimal | None = None,
    tag: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> list[SearchHit]:
    """Full-text search (name weighs more than details), then names that merely contain
    the words, with optional price range and tag."""
    query = websearch_to_tsquery(q, config="english")
    score = ts_rank(Products.search, query, weights=[0.1, 0.2, 0.4, 1.0])
    cover = ts_rank_cd(Products.search, query, normalization=1)
    band = (
        case_()
        .when(lt(Products.price, Decimal(10)), "budget")
        .when(between(Products.price, Decimal(10), Decimal(100)), "standard")
        .else_("premium")
    )
    filters = and_(
        eq(Products.active, True),
        or_(matches(Products.search, query), ilike(Products.name, f"%{q}%")),
        gte(Products.price, min_price) if min_price is not None else None,
        lt(Products.price, max_price) if max_price is not None else None,
        sql("{} = ANY({})", tag, Products.tags) if tag is not None else None,
    )
    rows = await (
        db.select(Products.id, Products.name, Products.price, score, cover, band)
        .from_(Products)
        .where(filters)
        .order_by(desc(score), asc(Products.id))
        .limit(limit)
    )
    return [
        SearchHit(id=i, name=n, price=p, rank=r or 0.0, cover=c or 0.0, price_band=b)
        for i, n, p, r, c, b in rows
    ]


@router.get("/products/export.csv")
async def export_csv(db: DB, store_id: int) -> Response:
    chunks: list[bytes] = []

    async def write(chunk: bytes) -> None:
        chunks.append(chunk)

    query = (
        db.select(Products.sku, Products.name, Products.price)
        .from_(Products)
        .where(eq(Products.store_id, store_id))
        .order_by(Products.sku)
    )
    await db.copy_from(query, write, header=True)
    return Response(b"".join(chunks), media_type="text/csv")


@router.get("/products/stream")
async def stream_products(db: DB, store_id: int) -> StreamingResponse:
    """Every product of a store as NDJSON, read in batches (constant memory)."""

    async def lines() -> AsyncIterator[bytes]:
        query = db.query(Products).find_many(
            where=eq(Products.store_id, store_id), order_by=Products.id
        )
        async for product in query.stream(batch_size=7):
            yield ProductOut.model_validate(product).model_dump_json().encode() + b"\n"

    return StreamingResponse(lines(), media_type="application/x-ndjson")


@router.get("/products/{product_id}")
async def product_detail(db: DB, st: St, product_id: int) -> ProductDetail:
    if not await st.product_by_id.execute(id=product_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such product")
    product = await db.query(Products).find_first(
        where=eq(Products.id, product_id),
        with_={"store": True, "reviews": {"order_by": desc(Reviews.id)}},
    )
    if product is None or product.store is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such product")
    ratings = await (
        db.select(ProductRatings.reviews, ProductRatings.rating)
        .from_(ProductRatings)
        .where(eq(ProductRatings.product_id, product_id))
    )
    review_count, rating = ratings[0] if ratings else (0, None)
    return ProductDetail(
        product=ProductOut.model_validate(product),
        store=StoreOut.model_validate(product.store),
        reviews=[ReviewOut.model_validate(r) for r in product.reviews],
        review_count=review_count,
        rating=round(rating, 2) if rating is not None else None,
    )


@router.patch("/products/{product_id}")
async def update_product(db: DB, me: Me, product_id: int, body: ProductPatch) -> ProductOut:
    """A price change fires the products_price_changed trigger (NOTIFY + notification)."""
    owned = db.select(Stores.id).from_(Stores).where(eq(Stores.owner_id, me.id))
    updated = await (
        db.update(Products)
        .set(body)
        .where(and_(eq(Products.id, product_id), in_(Products.store_id, owned)))
        .returning()
    )
    if not updated:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such product of yours")
    return ProductOut.model_validate(updated[0])


@router.get("/products/{product_id}/alternatives")
async def alternatives(db: DB, product_id: int) -> list[ProductOut]:
    """Cheaper active products sharing a tag (a self-join through an alias)."""
    other = alias(Products, "other")
    rows = await (
        db.select(other)
        .from_(Products)
        .join(other, sql("{} && {}", other.tags, Products.tags))
        .where(
            and_(
                eq(Products.id, product_id),
                ne(other.id, Products.id),
                lt(other.price, Products.price),
                eq(other.active, True),
            )
        )
        .order_by(asc(other.price), asc(other.id))
    )
    return [ProductOut.model_validate(r) for (r,) in rows]


@router.put("/products/{product_id}/embedding", status_code=status.HTTP_204_NO_CONTENT)
async def put_embedding(db: DB, st: St, product_id: int, body: Embedding) -> None:
    if not st.vectors:
        raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "this server has no pgvector")
    values = {"product_id": product_id, **body.model_dump()}
    await (
        db.insert(ProductEmbeddings)
        .values(values)
        .on_conflict_do_update(
            target=ProductEmbeddings.product_id,
            set_={
                "embedding": excluded(ProductEmbeddings.embedding),
                "half": excluded(ProductEmbeddings.half),
                "keywords": excluded(ProductEmbeddings.keywords),
            },
        )
    )


@router.get("/products/{product_id}/similar")
async def similar(db: DB, st: St, product_id: int, limit: int = 3) -> list[SimilarHit]:
    if not st.vectors:
        raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "this server has no pgvector")
    me = alias(ProductEmbeddings, "me")
    distance = cosine_distance(ProductEmbeddings.embedding, me.embedding)
    rows = await (
        db.select(Products.id, Products.name, distance)
        .from_(ProductEmbeddings)
        .join(me, eq(me.product_id, product_id))
        .join(Products, eq(Products.id, ProductEmbeddings.product_id))
        .where(ne(ProductEmbeddings.product_id, product_id))
        .order_by(distance, Products.id)
        .limit(limit)
    )
    return [SimilarHit(id=i, name=n, distance=d) for i, n, d in rows]


# -- orders ---------------------------------------------------------------------------


class OutOfStock(Exception):
    pass


async def take_stock(tx: Session, product_id: int, quantity: int) -> None:
    """Take ``quantity`` units from the warehouses in order; the CHECK (quantity >= 0)
    rejects a warehouse without enough, and its savepoint rolls back."""
    rows = await (
        tx.select(Inventory.warehouse, Inventory.quantity)
        .from_(Inventory)
        .where(and_(eq(Inventory.product_id, product_id), in_(Inventory.warehouse, WAREHOUSES)))
        .order_by(Inventory.warehouse)
        .for_update()
    )
    for warehouse in WAREHOUSES:
        if warehouse not in {w for w, _ in rows}:
            continue
        try:
            async with tx.transaction() as savepoint:  # nested: a SAVEPOINT
                await (
                    savepoint.update(Inventory)
                    .set(quantity=Inventory.quantity - quantity)
                    .where(
                        and_(
                            eq(Inventory.product_id, product_id), eq(Inventory.warehouse, warehouse)
                        )
                    )
                )
            return
        except CheckViolation:
            continue
    raise OutOfStock(product_id)


@router.post("/orders", status_code=status.HTTP_201_CREATED)
async def place_order(db: DB, me: Me, body: OrderIn) -> OrderDetail:
    ids = sorted({i.product_id for i in body.items})
    try:
        async with db.transaction() as tx:
            products = await (
                tx.select(Products.id, Products.price)
                .from_(Products)
                .where(
                    and_(
                        in_(Products.id, ids),
                        eq(Products.store_id, body.store_id),
                        eq(Products.active, True),
                    )
                )
            )
            prices = dict(products)
            if len(prices) != len(ids):
                raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown product in this store")
            for pid in ids:  # same lock order everywhere: no deadlocks
                wanted = sum(i.quantity for i in body.items if i.product_id == pid)
                await take_stock(tx, pid, wanted)
            [order] = await (
                tx.insert(Orders)
                .values(user_id=me.id, store_id=body.store_id, note=body.note)
                .returning()
            )
            items = await (
                tx.insert(OrderItems)
                .values(
                    [
                        {
                            "order_id": order.id,
                            "line": n,
                            "product_id": item.product_id,
                            "quantity": item.quantity,
                            "unit_price": prices[item.product_id],
                        }
                        for n, item in enumerate(body.items, start=1)
                    ]
                )
                .returning()
            )
            total = sum((i.line_total or Decimal(0) for i in items), Decimal(0))
            [order] = (
                await tx.update(Orders).set(total=total).where(eq(Orders.id, order.id)).returning()
            )
    except OutOfStock as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, f"out of stock: {exc.args[0]}") from exc
    return await order_detail(db, me, order.id)


@router.get("/orders")
async def my_orders(db: DB, me: Me) -> list[OrderOut]:
    """No WHERE on the user: row-level security returns only the caller's orders."""
    async with signed_in(db, me) as tx:
        orders = await tx.select().from_(Orders).order_by(Orders.id)
    return [OrderOut.model_validate(o) for o in orders]


@router.get("/orders/{order_id}")
async def order_detail(db: DB, me: Me, order_id: int) -> OrderDetail:
    order = await db.primary.query(Orders).find_first(
        where=and_(eq(Orders.id, order_id), eq(Orders.user_id, me.id)),
        with_={"items": {"order_by": OrderItems.line}, "store": True, "user": True},
    )
    if order is None or order.store is None or order.user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such order")
    return OrderDetail(
        order=OrderOut.model_validate(order),
        items=[OrderItemOut.model_validate(i) for i in order.items],
        store=StoreOut.model_validate(order.store),
        customer=UserOut.model_validate(order.user),
    )


@router.post("/orders/{order_id}/pay")
async def pay(db: DB, me: Me, order_id: int) -> OrderOut:
    async def attempt() -> list[Orders]:
        async with db.transaction("serializable") as tx:
            return await (
                tx.update(Orders)
                .set(status=OrderStatus.PAID)
                .where(
                    and_(
                        eq(Orders.id, order_id),
                        eq(Orders.user_id, me.id),
                        eq(Orders.status, OrderStatus.PENDING),
                    )
                )
                .returning()
            )

    paid = await db.retry(attempt, attempts=5)
    if not paid:
        raise HTTPException(status.HTTP_409_CONFLICT, "order is not pending")
    return OrderOut.model_validate(paid[0])


@router.post("/orders/{order_id}/cancel")
async def cancel(db: DB, me: Me, order_id: int) -> OrderOut:
    async with db.transaction() as tx:
        locked = await (
            tx.select()
            .from_(Orders)
            .where(and_(eq(Orders.id, order_id), eq(Orders.user_id, me.id)))
            .for_update(nowait=False)
        )
        if not locked or locked[0].status not in (OrderStatus.PENDING, OrderStatus.PAID):
            raise HTTPException(status.HTTP_409_CONFLICT, "cannot cancel this order")
        items = await tx.select().from_(OrderItems).where(eq(OrderItems.order_id, order_id))
        for item in sorted(items, key=lambda i: i.product_id):
            await (
                tx.update(Inventory)
                .set(quantity=Inventory.quantity + item.quantity)
                .where(
                    and_(
                        eq(Inventory.product_id, item.product_id),
                        eq(Inventory.warehouse, WAREHOUSES[0]),
                    )
                )
            )
        [order] = await (
            tx.update(Orders)
            .set(status=OrderStatus.CANCELLED)
            .where(eq(Orders.id, order_id))
            .returning()
        )
    return OrderOut.model_validate(order)


# -- reviews (signed in: row-level security) -----------------------------------------


@router.put("/products/{product_id}/review")
async def review(db: DB, me: Me, product_id: int, body: ReviewIn) -> ReviewOut:
    values = {**body.model_dump(exclude_unset=True), "product_id": product_id, "user_id": me.id}
    async with signed_in(db, me) as tx:
        [saved] = await (
            tx.insert(Reviews)
            .values(values)
            .on_conflict_do_update(
                target=[Reviews.product_id, Reviews.user_id],
                set_={"rating": excluded(Reviews.rating), "body": excluded(Reviews.body)},
            )
            .returning()
        )
    return ReviewOut.model_validate(saved)


@router.patch("/reviews/{review_id}")
async def edit_review(db: DB, me: Me, review_id: int, body: ReviewIn) -> ReviewOut:
    """No ownership check here: the update policy hides other users' reviews."""
    async with signed_in(db, me) as tx:
        updated = await tx.update(Reviews).set(body).where(eq(Reviews.id, review_id)).returning()
    if not updated:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such review of yours")
    return ReviewOut.model_validate(updated[0])


@router.delete("/reviews/{review_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_review(db: DB, me: Me, review_id: int) -> None:
    async with signed_in(db, me) as tx:
        deleted = await tx.delete(Reviews).where(eq(Reviews.id, review_id))
    if not deleted:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such review of yours")


@router.post("/reviews/forge", status_code=status.HTTP_201_CREATED)
async def forge_review(db: DB, me: Me, product_id: int, as_user: uuid.UUID) -> ReviewOut:
    """Tries to write a review in someone else's name: the insert policy refuses."""
    async with signed_in(db, me) as tx:
        [saved] = await (
            tx.insert(Reviews)
            .values(product_id=product_id, user_id=as_user, rating=1, body="forged")
            .returning()
        )
    return ReviewOut.model_validate(saved)


# -- notifications (signed in) --------------------------------------------------------


@router.get("/me/notifications")
async def notifications(db: DB, me: Me, unread: bool = False) -> list[NotificationOut]:
    async with signed_in(db, me) as tx:
        query = tx.select().from_(Notifications).order_by(Notifications.id)
        rows = await (query.where(is_null(Notifications.read_at)) if unread else query)
    return [NotificationOut.model_validate(n) for n in rows]


@router.post("/me/notifications/read")
async def read_all(db: DB, me: Me) -> int:
    """Updates "all rows": the policy limits it to the caller's."""
    async with signed_in(db, me) as tx:
        return await tx.update(Notifications).set(read_at=sql("now()")).all_rows()


@router.get("/events/prices")
async def price_events(
    db: DB,
    n: Annotated[int, Query(ge=1, le=100)],
    timeout: float = 5.0,
    overflow: Literal["raise", "drop_oldest", "block"] = "block",
) -> list[dict[str, Any]]:
    """The next ``n`` price changes (LISTEN; reconnects if the connection drops)."""
    got: list[dict[str, Any]] = []
    async with (
        asyncio.timeout(timeout),
        db.listen(PRICE_CHANNEL, reconnect=True, max_queue=n, overflow=overflow) as events,
    ):
        async for event in events:
            if isinstance(event, Reconnected):
                continue
            got.append(type_cast(dict[str, Any], json.loads(event.payload)))
            if len(got) == n:
                break
    return got


# -- stats ----------------------------------------------------------------------------


@router.get("/stats/days")
async def sales_by_day(
    db: DB, store_id: int, since: datetime.date, min_revenue: Decimal | None = None
) -> list[DayRow]:
    day = date_trunc("day", Orders.placed_at)
    revenue = sum_(Orders.total)
    size = case_().when(gt(count(), 2), "busy").else_("quiet")
    rows = await (
        db.select(day, count(), coalesce(revenue, Decimal(0)), extract("isodow", day), size)
        .from_(Orders)
        .where(
            and_(
                eq(Orders.store_id, store_id),
                ne(Orders.status, OrderStatus.CANCELLED),
                gte(cast(Orders.placed_at, date().not_null()), since),
                lt(Orders.placed_at, now() + datetime.timedelta(days=1)),
            )
        )
        .group_by(day)
        .having(gte(coalesce(revenue, Decimal(0)), min_revenue or Decimal(0)))
        .order_by(day)
    )
    return [DayRow(day=d, orders=c, revenue=r, weekday=w, size=s) for d, c, r, w, s in rows]


@router.get("/stats/customers")
async def top_customers(db: DB, store_id: int) -> list[CustomerRow]:
    """A CTE of spend per customer, joined back to users, with a dense rank label."""
    spend = (
        db.select({"user_id": Orders.user_id, "spent": sum_(Orders.total), "orders": count()})
        .from_(Orders)
        .where(and_(eq(Orders.store_id, store_id), eq(Orders.status, OrderStatus.PAID)))
        .group_by(Orders.user_id)
        .cte("spend")
    )
    user_id, spent, orders = (
        spend.c("user_id", uuid.UUID),
        spend.c("spent", Decimal),
        spend.c("orders", int),
    )
    position = dense_rank().over(order_by=desc(spent))
    label = concat("#", position, " ", func[str]("upper", Users.name))
    rows = await (
        db.with_(spend)
        .select(user_id, Users.name, spent, orders, label)
        .from_(spend)
        .join(Users, eq(Users.id, user_id))
        .order_by(desc(spent), asc(Users.name))
    )
    return [CustomerRow(user_id=u, name=n, spent=s, orders=o, label=lb) for u, n, s, o, lb in rows]


@router.get("/stats/featured")
async def featured(db: DB, store_id: int) -> list[int]:
    """Best sellers plus best rated (UNION), minus out-of-stock ones (EXCEPT)."""
    ids = db.select(Products.id).from_(Products).where(eq(Products.store_id, store_id))
    sellers = (
        db.select(TopProducts.product_id)
        .from_(TopProducts)
        .where(and_(gt(TopProducts.sold, 0), in_(TopProducts.product_id, ids)))
    )
    rated = (
        db.select(ProductRatings.product_id)
        .from_(ProductRatings)
        .where(and_(gte(ProductRatings.rating, Decimal(4)), in_(ProductRatings.product_id, ids)))
    )
    empty = (
        db.select(Products.id)
        .from_(Products)
        .where(
            and_(
                eq(Products.store_id, store_id),
                not_(
                    exists(
                        db.select(Inventory.product_id)
                        .from_(Inventory)
                        .where(
                            and_(eq(Inventory.product_id, Products.id), gt(Inventory.quantity, 0))
                        )
                    )
                ),
            )
        )
    )
    rows = await sellers.union(rated).except_(empty).order_by(TopProducts.product_id)
    return [pid for (pid,) in rows]


@router.get("/stats/skus")
async def sku_check(db: DB, store_id: int, pattern: str) -> dict[str, list[str]]:
    """The pattern operators side by side (LIKE, SIMILAR TO, regex, lower())."""
    base = db.select(Products.sku).from_(Products).where(eq(Products.store_id, store_id))

    async def skus(condition: Expr[bool]) -> list[str]:
        return [s for (s,) in await base.where(condition).order_by(Products.sku)]

    return {
        "like": await skus(like(Products.sku, pattern.replace("*", "%"))),
        "similar": await skus(similar_to(Products.sku, pattern.replace("*", "%"))),
        "regex": await skus(
            regex(lower(Products.sku), "^" + pattern.replace("*", ".*").lower() + "$")
        ),
        "price_cents": [
            str(c)
            for (c,) in await db.select(cast(round_(Products.price * 100, 0), numeric(12, 0)))
            .from_(Products)
            .where(eq(Products.store_id, store_id))
            .order_by(Products.sku)
        ],
        "labels": [
            s
            for (s,) in await db.select(Products.sku + ": " + Products.name)
            .from_(Products)
            .where(eq(Products.store_id, store_id))
            .order_by(Products.sku)
        ],
    }


# -- admin ----------------------------------------------------------------------------


@router.get("/admin/audit")
async def audit(
    db: DB, table: str, action: AuditAction | None = None, row_id: str | None = None
) -> list[AuditOut]:
    rows = await (
        db.primary.select()
        .from_(AuditLog)
        .where(
            and_(
                eq(AuditLog.table_name, table),
                eq(AuditLog.action, action) if action is not None else None,
                eq(AuditLog.row_id, row_id) if row_id is not None else None,
            )
        )
        .order_by(AuditLog.id)
    )
    return [AuditOut.model_validate(r) for r in rows]


@router.get("/admin/slow")
async def slow(db: DB, seconds: float, timeout: float) -> dict[str, bool]:
    await db.with_timeout(timeout).execute(sql("SELECT pg_sleep({})", seconds))
    return {"finished": True}


@router.get("/admin/health")
async def health(db: DB, st: St) -> Health:
    rows = await db.execute(
        sql(
            "SELECT count(*) AS total, count(*) FILTER (WHERE state = 'idle') AS idle "
            "FROM pg_stat_activity WHERE application_name = current_setting('application_name')"
        )
    )
    total, idle = rows[0]["total"], rows[0]["idle"]
    assert isinstance(total, int) and isinstance(idle, int)
    return Health(pool_size=total, pool_idle=idle, replicas=len(replica_urls()), vectors=st.vectors)


@router.get("/admin/inventory")
async def inventory(db: DB, store_id: int) -> list[InventoryOut]:
    rows = await (
        db.primary.select(Inventory)
        .from_(Inventory)
        .join(Products, eq(Products.id, Inventory.product_id))
        .where(eq(Products.store_id, store_id))
        .order_by(Inventory.product_id, Inventory.warehouse)
    )
    return [InventoryOut.model_validate(r) for (r,) in rows]


# -- app ------------------------------------------------------------------------------


async def _error(status_code: int, detail: str) -> JSONResponse:
    return JSONResponse({"detail": detail}, status_code=status_code)


async def _unique_violation(_request: Request, exc: Exception) -> JSONResponse:
    constraint = exc.constraint if isinstance(exc, UniqueViolation) else None
    return await _error(409, f"already exists ({constraint})")


async def _check_violation(_request: Request, exc: Exception) -> JSONResponse:
    constraint = exc.constraint if isinstance(exc, CheckViolation) else None
    return await _error(422, f"invalid value ({constraint})")


async def _foreign_key_violation(_request: Request, _exc: Exception) -> JSONResponse:
    return await _error(404, "a referenced row does not exist")


async def _timeout(_request: Request, _exc: Exception) -> JSONResponse:
    return await _error(504, "the database took too long")


async def _unavailable(_request: Request, _exc: Exception) -> JSONResponse:
    return await _error(503, "database connection lost; retry")


async def _conflict(_request: Request, _exc: Exception) -> JSONResponse:
    return await _error(409, "concurrent update; retry")


async def _database_error(_request: Request, exc: Exception) -> JSONResponse:
    if (
        isinstance(exc, DatabaseError) and exc.sqlstate == "42501"
    ):  # row-level security or a missing privilege
        return await _error(403, "not allowed")
    return await _error(500, "database error")


def replica_urls() -> list[str]:
    return [u for u in os.environ.get("REPLICA_URLS", "").split(",") if u]


async def has_pgvector(db: Database) -> bool:
    rows = await db.primary.execute(
        sql("SELECT 1 AS ok FROM pg_available_extensions WHERE name = 'vector'")
    )
    return bool(rows)


def create_app(
    database_url: str | None = None,
    *,
    replicas: list[str] | None = None,
    migrate_on_start: bool | None = None,
    pool_size: int | None = None,
    statement_cache_size: int | None = None,
) -> FastAPI:
    """Build the API. Defaults come from the environment / .env."""
    load_dotenv(ROOT / ".env")
    url = database_url or os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("set DATABASE_URL")
    run_migrations = (
        migrate_on_start
        if migrate_on_start is not None
        else os.environ.get("MIGRATE_ON_START") == "1"
    )
    replica_list = replicas if replicas is not None else replica_urls()
    size = pool_size or int(os.environ.get("POOL_SIZE", "10"))
    cache = (
        statement_cache_size
        if statement_cache_size is not None
        else int(os.environ.get("STATEMENT_CACHE_SIZE", "100"))
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
        tables: list[type[Table]] = [*TABLES, ProductEmbeddings]
        async with await Database.connect(
            url,
            schema=tables,
            replicas=replica_list,
            pool_size=size,
            pool_min_size=1,
            max_idle=60,
            query_timeout=10,
            statement_cache_size=cache,
            reset_connections="auto",
        ) as db:
            vectors = await has_pgvector(db)
            if run_migrations:  # advisory-locked: safe when many workers start at once
                await migrate(db, ROOT / ("migrations_vector" if vectors else "migrations"))
            _state["state"] = State(db, vectors)
            try:
                yield
            finally:
                del _state["state"]

    app = FastAPI(title="mizzle shop example", lifespan=lifespan)
    app.add_exception_handler(UniqueViolation, _unique_violation)
    app.add_exception_handler(ForeignKeyViolation, _foreign_key_violation)
    app.add_exception_handler(CheckViolation, _check_violation)
    app.add_exception_handler(QueryTimeout, _timeout)
    app.add_exception_handler(ConnectionError, _unavailable)
    app.add_exception_handler(SerializationFailure, _conflict)
    app.add_exception_handler(DatabaseError, _database_error)
    app.include_router(router)
    return app
