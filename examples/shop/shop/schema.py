"""A multi-tenant marketplace: users own stores, stores sell products, users order them and
write reviews. Inventory, an audit log (in its own schema) and notifications come with it.

Lines marked ``# v2`` are the second schema change (see tests/test_migrations.py): the
test rebuilds version 1 from them to replay both migrations from zero.

What mizzle does not manage (extensions, the auth.uid() helper, the audit and price
triggers and their functions, grants) lives in hand-written custom migrations:
migrations/0001_extensions_and_auth and migrations/0003_triggers_and_grants.
"""

import enum
from decimal import Decimal
from typing import Any, final, override

from mizzle import (
    Constraint,
    QueryBuilder,
    Table,
    View,
    avg,
    check,
    count,
    eq,
    index,
    lower,
    many,
    one,
    policy,
    primary_key,
    rls,
    role,
    sql,
    sum_,
    unique,
    unique_index,
)
from mizzle.pg import (
    array,
    bigint,
    boolean,
    integer,
    jsonb,
    numeric,
    pg_enum,
    serial,
    smallint,
    text,
    timestamp,
    tsvector,
    uuid,
    varchar,
)
from mizzle.supabase import auth_uid, crud_policy

AUDIT = "shop_audit"
DETAILS = "details"  # v2 (v1: details => description)

# Signed-in users. The role exists before the first migration (a DBA creates it, like
# Supabase's "authenticated"), so migrations only reference it.
SHOP_AUTH = role("shop_authenticated", existing=True)
# Support staff: created and kept in sync by migrations.
SHOP_SUPPORT = role("shop_support", inherit=True)


class UserRole(enum.StrEnum):
    CUSTOMER = "customer"
    SELLER = "seller"
    ADMIN = "admin"


class OrderStatus(enum.StrEnum):
    PENDING = "pending"
    PAID = "paid"
    SHIPPED = "shipped"
    CANCELLED = "cancelled"


class AuditAction(enum.StrEnum):
    INSERT = "insert"
    UPDATE = "update"
    DELETE = "delete"


@final
class Users(Table, name="users"):
    id = uuid().primary_key().default(sql("gen_random_uuid()"))
    email = varchar(255).not_null()
    name = text().not_null()
    phone = text()  # v2
    role = pg_enum(UserRole).not_null().default(UserRole.CUSTOMER)
    tags = array(text()).not_null().default(sql("'{}'::text[]"))
    prefs = jsonb[dict[str, Any]]().not_null().default(sql("'{}'::jsonb"))
    created_at = timestamp(tz=True).not_null().default(sql("now()"))
    updated_at = timestamp(tz=True).not_null().default(sql("now()")).on_update_now()

    stores = many(lambda: Stores)
    orders = many(lambda: Orders)
    reviews = many(lambda: Reviews)

    @classmethod
    @override
    def config(cls) -> list[Constraint]:
        return [
            unique_index("users_email_lower_key").on(lower(cls.email)),
            check("users_email_format", sql("email LIKE '%_@_%'")),
        ]


@final
class Stores(Table, name="stores"):
    id = integer().primary_key().generated_always_as_identity(start=100, increment=1)
    owner_id = uuid().not_null().references(lambda: Users.id, on_delete="restrict")
    slug = varchar(64).not_null().unique()  # v2 (v1: varchar(64) => varchar(40))
    name = text().not_null()
    created_at = timestamp(tz=True).not_null().default(sql("now()"))

    owner = one(lambda: Users, fields=lambda: [Stores.owner_id], references=lambda: [Users.id])
    products = many(lambda: Products)
    orders = many(lambda: Orders)

    @classmethod
    @override
    def config(cls) -> list[Constraint]:
        return [check("stores_slug_format", sql("slug ~ '^[a-z0-9-]+$'"))]


@final
class Products(Table, name="products"):
    id = bigint().primary_key().generated_by_default_as_identity(start=1000, increment=1)
    store_id = integer().not_null().references(lambda: Stores.id, on_delete="cascade")
    sku = text().not_null()
    name = text().not_null()
    details = text()  # v2 (v1: details => description)
    price = numeric(10, 2).not_null()
    tags = array(text()).not_null().default(sql("'{}'::text[]"))
    attrs = jsonb[dict[str, Any]]().not_null().default(sql("'{}'::jsonb"))
    active = boolean().not_null().default(True)
    search = tsvector().generated_always_as(
        sql(
            "setweight(to_tsvector('english', name), 'A') || "
            f"setweight(to_tsvector('english', coalesce({DETAILS}, '')), 'B')"
        )
    )
    created_at = timestamp(tz=True).not_null().default(sql("now()"))
    updated_at = timestamp(tz=True).not_null().default(sql("now()")).on_update_now()

    store = one(lambda: Stores, fields=lambda: [Products.store_id], references=lambda: [Stores.id])
    inventory = many(lambda: Inventory)
    reviews = many(lambda: Reviews)

    @classmethod
    @override
    def config(cls) -> list[Constraint]:
        return [
            unique(cls.store_id, cls.sku),
            check("products_price_positive", sql("price >= 0")),
            index("products_search_idx").using("gin").on(cls.search),
            index("products_tags_idx").using("gin").on(cls.tags),
            index("products_name_trgm_idx").using("gin", "gin_trgm_ops").on(cls.name),  # pg_trgm
            index("products_active_store_idx").on(cls.store_id).where(sql("active")),
        ]


@final
class Inventory(Table, name="inventory"):
    product_id = bigint().not_null().references(lambda: Products.id, on_delete="cascade")
    warehouse = text().not_null()
    quantity = integer().not_null().default(0)
    updated_at = timestamp(tz=True).not_null().default(sql("now()")).on_update_now()

    product = one(
        lambda: Products, fields=lambda: [Inventory.product_id], references=lambda: [Products.id]
    )

    @classmethod
    @override
    def config(cls) -> list[Constraint]:
        return [
            primary_key(cls.product_id, cls.warehouse),
            check("inventory_quantity_not_negative", sql("quantity >= 0")),
        ]


@final
class Orders(Table, name="orders"):
    id = bigint().primary_key().generated_always_as_identity()
    user_id = uuid().not_null().references(lambda: Users.id, on_delete="cascade")
    store_id = integer().not_null().references(lambda: Stores.id, on_delete="cascade")
    status = pg_enum(OrderStatus).not_null().default(OrderStatus.PENDING)
    total = numeric(12, 2).not_null().default(Decimal(0))
    note = text()
    placed_at = timestamp(tz=True).not_null().default(sql("now()"))

    user = one(lambda: Users, fields=lambda: [Orders.user_id], references=lambda: [Users.id])
    store = one(lambda: Stores, fields=lambda: [Orders.store_id], references=lambda: [Stores.id])
    items = many(lambda: OrderItems)

    @classmethod
    @override
    def config(cls) -> list[Constraint]:
        owner = policy(
            "orders_owner", for_="select", to=[SHOP_AUTH], using=eq(cls.user_id, auth_uid())
        )
        return [
            index("orders_user_placed_idx").on(cls.user_id, cls.placed_at),
            index("orders_open_idx").on(cls.store_id).where(sql("status IN ('pending', 'paid')")),
            rls(),  # v2
            owner,  # v2
        ]


@final
class OrderItems(Table, name="order_items"):
    order_id = bigint().not_null().references(lambda: Orders.id, on_delete="cascade")
    line = smallint().not_null()
    product_id = bigint().not_null().references(lambda: Products.id, on_delete="restrict")
    quantity = integer().not_null()
    unit_price = numeric(10, 2).not_null()
    line_total = numeric(12, 2).generated_always_as(sql("quantity * unit_price"))

    order = one(
        lambda: Orders, fields=lambda: [OrderItems.order_id], references=lambda: [Orders.id]
    )
    product = one(
        lambda: Products, fields=lambda: [OrderItems.product_id], references=lambda: [Products.id]
    )

    @classmethod
    @override
    def config(cls) -> list[Constraint]:
        return [
            primary_key(cls.order_id, cls.line),
            check("order_items_quantity_positive", sql("quantity > 0")),
            index("order_items_product_idx").on(cls.product_id),
        ]


@final
class Reviews(Table, name="reviews"):
    id = serial().primary_key()
    product_id = bigint().not_null().references(lambda: Products.id, on_delete="cascade")
    user_id = uuid().not_null().references(lambda: Users.id, on_delete="cascade")
    rating = smallint().not_null()
    body = text().not_null().default("")
    created_at = timestamp(tz=True).not_null().default(sql("now()"))

    product = one(
        lambda: Products, fields=lambda: [Reviews.product_id], references=lambda: [Products.id]
    )
    user = one(lambda: Users, fields=lambda: [Reviews.user_id], references=lambda: [Users.id])

    @classmethod
    @override
    def config(cls) -> list[Constraint]:
        return [
            unique(cls.product_id, cls.user_id),
            check("reviews_rating_range", sql("rating BETWEEN 1 AND 5")),
            rls(),
            *crud_policy(
                "reviews_owner", to=[SHOP_AUTH], read=True, modify=eq(cls.user_id, auth_uid())
            ),
        ]


@final
class Notifications(Table, name="notifications"):
    id = bigint().primary_key().generated_always_as_identity()
    user_id = uuid().not_null().references(lambda: Users.id, on_delete="cascade")
    kind = text().not_null()
    payload = jsonb[dict[str, Any]]().not_null().default(sql("'{}'::jsonb"))
    read_at = timestamp(tz=True)
    created_at = timestamp(tz=True).not_null().default(sql("now()"))

    @classmethod
    @override
    def config(cls) -> list[Constraint]:
        return [
            index("notifications_unread_idx").on(cls.user_id).where(sql("read_at IS NULL")),
            rls(force=True),
            policy("notifications_own", to=[SHOP_AUTH], using=eq(cls.user_id, auth_uid())),
            policy("notifications_support", for_="select", to=[SHOP_SUPPORT], using=sql("true")),
        ]


@final
class AuditLog(Table, name="audit_log", schema=AUDIT):
    id = bigint().primary_key().generated_always_as_identity()
    table_name = text().not_null()
    action = pg_enum(AuditAction, schema=AUDIT).not_null()
    row_id = text().not_null()
    data = jsonb[dict[str, Any]]()
    actor = text().not_null().default(sql("current_user"))
    at = timestamp(tz=True).not_null().default(sql("now()"))

    @classmethod
    @override
    def config(cls) -> list[Constraint]:
        return [index("audit_log_row_idx").on(cls.table_name, cls.row_id)]


# -- views ----------------------------------------------------------------------------


@final
class ProductRatings(View, name="product_ratings"):
    product_id = bigint().not_null()
    reviews = bigint().not_null()
    rating = numeric()

    @classmethod
    @override
    def query(cls, db: QueryBuilder):
        return (
            db.select(
                {
                    "product_id": Reviews.product_id,
                    "reviews": count(),
                    "rating": avg(Reviews.rating),
                }
            )
            .from_(Reviews)
            .group_by(Reviews.product_id)
        )


@final
class StoreSales(View, name="store_sales", materialized=True):
    store_id = integer().not_null()
    orders = bigint().not_null()
    revenue = numeric()

    @classmethod
    @override
    def query(cls, db: QueryBuilder):
        return (
            db.select(
                {"store_id": Orders.store_id, "orders": count(), "revenue": sum_(Orders.total)}
            )
            .from_(Orders)
            .where(eq(Orders.status, OrderStatus.PAID))
            .group_by(Orders.store_id)
        )

    @classmethod
    @override
    def config(cls) -> list[Constraint]:
        return [unique_index("store_sales_store_key").on(cls.store_id)]


# v2-only:start
@final
class TopProducts(View, name="top_products"):
    product_id = bigint().not_null()
    sold = bigint()

    @classmethod
    @override
    def query(cls, db: QueryBuilder):
        return (
            db.select({"product_id": OrderItems.product_id, "sold": sum_(OrderItems.quantity)})
            .from_(OrderItems)
            .group_by(OrderItems.product_id)
        )


# v2-only:end


TABLES: list[type[Table]] = [
    Users,
    Stores,
    Products,
    Inventory,
    Orders,
    OrderItems,
    Reviews,
    Notifications,
    AuditLog,
    ProductRatings,
    StoreSales,
    TopProducts,  # v2
]
