"""End-to-end tests of every endpoint, with exact checks on the returned data."""

import asyncio
import datetime
import json
import math
import struct
import uuid
from decimal import Decimal
from typing import Any

import httpx
import pytest
from shop.schema import Notifications, Products
from shop.vectors import ProductEmbeddings

from mizzle import Database, eq, sql
from mizzle.dialects.postgres import PostgresDialect
from mizzle.studio.server import create_app as studio_app
from tests.conftest import APP_NAME, Sync


def tag() -> str:
    return uuid.uuid4().hex[:8]


def H(user_id: str) -> dict[str, str]:
    return {"X-User-Id": user_id}


async def ok(response: httpx.Response, code: int = 200) -> Any:
    assert response.status_code == code, response.text
    return response.json() if response.content else None


async def make_user(client: httpx.AsyncClient, name: str, role: str = "customer") -> str:
    body = {"email": f"{name}-{tag()}@example.com", "name": name, "role": role}
    user = await ok(await client.post("/users", json=body), 201)
    return str(user["id"])


async def make_store(client: httpx.AsyncClient, owner: str) -> dict[str, Any]:
    slug = f"store-{tag()}"
    return await ok(
        await client.post("/stores", json={"slug": slug, "name": slug}, headers=H(owner)), 201
    )


async def make_product(
    client: httpx.AsyncClient, owner: str, store_id: int, **fields: Any
) -> dict[str, Any]:
    body = {"sku": f"sku-{tag()}", "name": "thing", "price": "1.00", **fields}
    url = f"/stores/{store_id}/products"
    return await ok(await client.post(url, json=body, headers=H(owner)), 201)


# -- users ----------------------------------------------------------------------------


async def test_users(client: httpx.AsyncClient) -> None:
    t = tag()
    body = {
        "email": f"Ann-{t}@Example.com",
        "name": "Ann",
        "tags": ["vip"],
        "prefs": {"lang": "en"},
    }
    ann = await ok(await client.post("/users", json=body), 201)
    assert (ann["name"], ann["role"], ann["tags"], ann["prefs"], ann["phone"]) == (
        "Ann",
        "customer",
        ["vip"],
        {"lang": "en"},
        None,
    )
    assert uuid.UUID(ann["id"]).version == 4  # gen_random_uuid()

    # The unique index is on lower(email); the check needs an @.
    dup = await client.post("/users", json={"email": f"ann-{t}@example.COM", "name": "x"})
    assert (dup.status_code, dup.json()) == (
        409,
        {"detail": "already exists (users_email_lower_key)"},
    )
    bad = await client.post("/users", json={"email": "nope", "name": "x"})
    assert (bad.status_code, bad.json()) == (422, {"detail": "invalid value (users_email_format)"})
    # Pydantic: enum values are checked, and fields outside the allowlist are refused.
    assert (
        await client.post("/users", json={"email": "a@b.c", "name": "x", "role": "root"})
    ).status_code == 422
    forced = {"email": f"o-{t}@x.io", "name": "O", "id": str(uuid.uuid4())}
    assert (await client.post("/users", json=forced)).json()["detail"][0][
        "type"
    ] == "extra_forbidden"

    me = await ok(await client.get("/me", headers=H(ann["id"])))
    assert me == ann
    await asyncio.sleep(0.01)
    patched = await ok(
        await client.patch("/me", json={"name": "Ann B", "phone": "+1 555"}, headers=H(ann["id"]))
    )
    assert (patched["name"], patched["phone"], patched["email"]) == (
        "Ann B",
        "+1 555",
        ann["email"],
    )
    assert patched["created_at"] == ann["created_at"]
    assert patched["updated_at"] > ann["updated_at"]  # on_update_now()
    assert (await client.get("/me", headers=H(str(uuid.uuid4())))).status_code == 401


# -- stores and products --------------------------------------------------------------


async def test_catalog(client: httpx.AsyncClient) -> None:
    sam = await make_user(client, "sam", "seller")
    cus = await make_user(client, "cus")
    t = tag()
    store = await ok(
        await client.post("/stores", json={"slug": f"sam-{t}", "name": "Sam's"}, headers=H(sam)),
        201,
    )
    assert store["id"] >= 100 and store["owner_id"] == sam  # identity START WITH 100
    assert (
        await client.post("/stores", json={"slug": f"c-{t}", "name": "x"}, headers=H(cus))
    ).status_code == 403
    bad = await client.post("/stores", json={"slug": "Bad Slug", "name": "x"}, headers=H(sam))
    assert bad.json() == {"detail": "invalid value (stores_slug_format)"}
    assert (
        await client.post("/stores", json={"slug": "x" * 65, "name": "x"}, headers=H(sam))
    ).status_code == 422
    dup = await client.post("/stores", json={"slug": f"sam-{t}", "name": "x"}, headers=H(sam))
    assert dup.json() == {"detail": "already exists (stores_slug_key)"}

    sid = store["id"]
    scarf = await make_product(
        client,
        sam,
        sid,
        sku="b-scarf",
        name="Red Wool Scarf",
        details="warm winter scarf",
        price="19.90",
        tags=["winter", "wool"],
        attrs={"color": "red"},
        stock={"main": 5, "backup": 3},
    )
    socks = await make_product(
        client,
        sam,
        sid,
        sku="a-socks",
        name="Wool Socks",
        price="7.50",
        tags=["wool"],
        stock={"main": 1},
    )
    jacket = await make_product(
        client,
        sam,
        sid,
        sku="c-jacket",
        name="Winter Jacket",
        price="149.00",
        tags=["winter"],
        stock={"main": 0, "backup": 2},
    )
    hat = await make_product(
        client, sam, sid, sku="d-hat", name="Old Hat", price="5.00", tags=["winter"], active=False
    )
    assert scarf["id"] >= 1000 and socks["id"] == scarf["id"] + 1  # identity START WITH 1000
    assert (scarf["price"], scarf["tags"], scarf["attrs"], scarf["details"]) == (
        "19.90",
        ["winter", "wool"],
        {"color": "red"},
        "warm winter scarf",
    )
    assert "search" not in scarf

    # numeric(10, 2): pydantic rejects a third decimal; another seller cannot add products.
    three = {"sku": "x", "name": "x", "price": "1.999"}
    assert (
        await client.post(f"/stores/{sid}/products", json=three, headers=H(sam))
    ).status_code == 422
    eve = await make_user(client, "eve", "seller")
    assert (
        await client.post(f"/stores/{sid}/products", json={**three, "price": "1"}, headers=H(eve))
    ).status_code == 403
    dup = await client.post(
        f"/stores/{sid}/products",
        json={"sku": "a-socks", "name": "x", "price": "1"},
        headers=H(sam),
    )
    assert dup.json() == {"detail": "already exists (products_store_id_sku_key)"}
    neg = await client.post(
        f"/stores/{sid}/products", json={"sku": "neg", "name": "x", "price": "-1"}, headers=H(sam)
    )
    assert neg.json() == {"detail": "invalid value (products_price_positive)"}

    detail = await ok(await client.get(f"/stores/sam-{t}"))
    assert detail["owner"]["id"] == sam
    assert [p["id"] for p in detail["products"]] == [
        scarf["id"],
        socks["id"],
        jacket["id"],
    ]  # active
    assert detail["stock"] == {str(scarf["id"]): 8, str(socks["id"]): 1, str(jacket["id"]): 2}
    assert (await client.get("/stores/nope")).status_code == 404

    # Self-join through an alias: cheaper, active, sharing a tag.
    alt = await ok(await client.get(f"/products/{jacket['id']}/alternatives"))
    assert [p["id"] for p in alt] == [scarf["id"]]
    alt = await ok(await client.get(f"/products/{scarf['id']}/alternatives"))
    assert [p["id"] for p in alt] == [socks["id"]]
    del hat

    csv = await client.get("/products/export.csv", params={"store_id": sid})
    assert csv.text == (
        "sku,name,price\n"
        "a-socks,Wool Socks,7.50\n"
        "b-scarf,Red Wool Scarf,19.90\n"
        "c-jacket,Winter Jacket,149.00\n"
        "d-hat,Old Hat,5.00\n"
    )
    lines = (await client.get("/products/stream", params={"store_id": sid})).text.splitlines()
    assert [json.loads(line)["sku"] for line in lines] == [
        "b-scarf",
        "a-socks",
        "c-jacket",
        "d-hat",
    ]

    # Bulk upsert by SKU (insert_many in batches of 50, ON CONFLICT DO UPDATE).
    rows = [{"sku": f"bulk-{i:03}", "name": f"Bulk {i}", "price": "1.00"} for i in range(120)]
    first = await ok(await client.post(f"/stores/{sid}/products/bulk", json=rows, headers=H(sam)))
    assert first["rows"] == 120 and len(set(first["ids"])) == 120
    again = [{**r, "price": "2.00"} for r in rows] + [
        {"sku": f"bulk-{i:03}", "name": f"Bulk {i}", "price": "3.00"} for i in range(120, 125)
    ]
    second = await ok(await client.post(f"/stores/{sid}/products/bulk", json=again, headers=H(sam)))
    assert second["rows"] == 125 and second["ids"][:120] == first["ids"]
    assert (await ok(await client.get(f"/products/{first['ids'][7]}")))["product"][
        "price"
    ] == "2.00"

    # Load stock for the bulk products; a duplicate key stores nothing at all.
    stock = {str(pid): {"main": 10, "backup": i % 3} for i, pid in enumerate(second["ids"])}
    loaded = await ok(
        await client.post(f"/stores/{sid}/inventory/load", json=stock, headers=H(sam))
    )
    assert loaded["rows"] == 250
    dup_stock = {str(second["ids"][0]): {"main": 1}, str(scarf["id"]): {"spare": 1, "main": 1}}
    assert (
        await client.post(f"/stores/{sid}/inventory/load", json=dup_stock, headers=H(sam))
    ).status_code == 409
    inv = await ok(await client.get("/admin/inventory", params={"store_id": sid}))
    assert len(inv) == 5 + 250  # the failed load left nothing behind
    assert {
        (i["warehouse"], i["quantity"]) for i in inv if i["product_id"] == second["ids"][4]
    } == {("main", 10), ("backup", 1)}


# -- search ---------------------------------------------------------------------------


async def test_search(client: httpx.AsyncClient) -> None:
    sam = await make_user(client, "sam", "seller")
    sid = (await make_store(client, sam))["id"]
    w = f"glow{tag()}"
    lamp = await make_product(
        client, sam, sid, name=f"{w} lamp", details="bright", price="9.99", tags=["home"]
    )
    desk = await make_product(client, sam, sid, name="Desk", details=f"a {w} desk", price="50.00")
    throne = await make_product(client, sam, sid, name=f"{w} throne", price="500.00", tags=["home"])
    await make_product(client, sam, sid, name=f"{w} off", price="1.00", active=False)

    hits = await ok(await client.get("/products/search", params={"q": w}))
    assert [h["id"] for h in hits[:1]] == [lamp["id"]] or [h["id"] for h in hits[:1]] == [
        throne["id"]
    ]
    by_id = {h["id"]: h for h in hits}
    assert set(by_id) == {lamp["id"], desk["id"], throne["id"]}
    assert by_id[lamp["id"]]["rank"] > by_id[desk["id"]]["rank"] > 0  # name (A) beats details (B)
    assert hits[-1]["id"] == desk["id"]
    assert [by_id[p["id"]]["price_band"] for p in (lamp, desk, throne)] == [
        "budget",
        "standard",
        "premium",
    ]
    assert all(h["cover"] > 0 for h in hits)

    cheap = await ok(await client.get("/products/search", params={"q": w, "max_price": "100"}))
    assert {h["id"] for h in cheap} == {lamp["id"], desk["id"]}
    pricey = await ok(await client.get("/products/search", params={"q": w, "min_price": "10"}))
    assert {h["id"] for h in pricey} == {desk["id"], throne["id"]}
    tagged = await ok(await client.get("/products/search", params={"q": w, "tag": "home"}))
    assert {h["id"] for h in tagged} == {lamp["id"], throne["id"]}
    # Not a word: no full-text match, the ILIKE fallback finds it in names only (rank 0).
    part = await ok(await client.get("/products/search", params={"q": w[1:]}))
    assert [(h["id"], h["rank"]) for h in part] == [(lamp["id"], 0.0), (throne["id"], 0.0)]


# -- orders, payments, cancellations, reports -----------------------------------------


async def test_orders(client: httpx.AsyncClient) -> None:
    sam = await make_user(client, "sam", "seller")
    c1 = await make_user(client, "Cleo")
    c2 = await make_user(client, "Dan")
    store = await make_store(client, sam)
    sid = store["id"]
    x = await make_product(
        client, sam, sid, sku="x", name="X", price="10.00", stock={"main": 2, "backup": 5}
    )
    y = await make_product(client, sam, sid, sku="y", name="Y", price="3.50", stock={"main": 10})
    z = await make_product(client, sam, sid, sku="z", name="Z", price="1.25", stock={"main": 1})

    # 3 X: "main" has 2, its CHECK fails inside a savepoint, "backup" serves it.
    body = {
        "store_id": sid,
        "items": [{"product_id": x["id"], "quantity": 3}, {"product_id": y["id"], "quantity": 4}],
    }
    placed = await ok(await client.post("/orders", json=body, headers=H(c1)), 201)
    order = placed["order"]
    assert (order["status"], order["total"], order["user_id"], placed["customer"]["name"]) == (
        "pending",
        "44.00",
        c1,
        "Cleo",
    )
    assert [
        (i["line"], i["quantity"], i["unit_price"], i["line_total"]) for i in placed["items"]
    ] == [
        (1, 3, "10.00", "30.00"),
        (2, 4, "3.50", "14.00"),
    ]
    assert placed["store"]["id"] == sid

    async def stock() -> dict[tuple[int, str], int]:
        rows = await ok(await client.get("/admin/inventory", params={"store_id": sid}))
        return {(r["product_id"], r["warehouse"]): r["quantity"] for r in rows}

    after_first = {
        (x["id"], "main"): 2,
        (x["id"], "backup"): 2,
        (y["id"], "main"): 6,
        (z["id"], "main"): 1,
    }
    assert await stock() == after_first

    # Not enough anywhere: 409 and nothing changes (the whole transaction rolls back).
    short = {
        "store_id": sid,
        "items": [{"product_id": y["id"], "quantity": 1}, {"product_id": x["id"], "quantity": 3}],
    }
    response = await client.post("/orders", json=short, headers=H(c1))
    assert (response.status_code, response.json()) == (409, {"detail": f"out of stock: {x['id']}"})
    assert await stock() == after_first
    other = await make_store(client, sam)
    foreign = {"store_id": other["id"], "items": [{"product_id": y["id"], "quantity": 1}]}
    assert (await client.post("/orders", json=foreign, headers=H(c1))).status_code == 404
    assert (
        await client.post("/orders", json={"store_id": sid, "items": []}, headers=H(c1))
    ).status_code == 422

    paid = await ok(await client.post(f"/orders/{order['id']}/pay", headers=H(c1)))
    assert paid["status"] == "paid"
    assert (await client.post(f"/orders/{order['id']}/pay", headers=H(c1))).status_code == 409
    assert (await client.post(f"/orders/{order['id']}/pay", headers=H(c2))).status_code == 409

    # Dan's order, then Z sells out (paid).
    dan = await ok(
        await client.post(
            "/orders",
            json={"store_id": sid, "items": [{"product_id": y["id"], "quantity": 1}]},
            headers=H(c2),
        ),
        201,
    )
    zed = await ok(
        await client.post(
            "/orders",
            json={"store_id": sid, "items": [{"product_id": z["id"], "quantity": 1}]},
            headers=H(c1),
        ),
        201,
    )
    await ok(await client.post(f"/orders/{zed['order']['id']}/pay", headers=H(c1)))

    # Row-level security: GET /orders has no WHERE on the user.
    mine = await ok(await client.get("/orders", headers=H(c1)))
    assert [o["id"] for o in mine] == [order["id"], zed["order"]["id"]]
    assert [o["id"] for o in await ok(await client.get("/orders", headers=H(c2)))] == [
        dan["order"]["id"]
    ]
    assert await ok(await client.get("/orders", headers=H(sam))) == []
    assert (await client.get(f"/orders/{dan['order']['id']}", headers=H(c1))).status_code == 404

    cancelled = await ok(await client.post(f"/orders/{dan['order']['id']}/cancel", headers=H(c2)))
    assert cancelled["status"] == "cancelled"
    assert (
        await client.post(f"/orders/{dan['order']['id']}/cancel", headers=H(c2))
    ).status_code == 409
    assert (await stock())[(y["id"], "main")] == 6  # restocked
    assert (await stock())[(z["id"], "main")] == 0

    # The audit trigger (in the shop_audit schema) saw every change of order 1.
    log = await ok(
        await client.get("/admin/audit", params={"table": "orders", "row_id": str(order["id"])})
    )
    assert [e["action"] for e in log] == ["insert", "update", "update"]
    assert [e["data"]["status"] for e in log] == ["pending", "pending", "paid"]
    assert [e["data"]["total"] for e in log] == [0, 44.0, 44.0] or [
        e["data"]["total"] for e in log
    ] == [0, 44, 44]
    assert {e["actor"] for e in log} == {"mizzle"}
    updates = await ok(
        await client.get(
            "/admin/audit",
            params={"table": "orders", "action": "update", "row_id": str(order["id"])},
        )
    )
    assert len(updates) == 2
    store_log = await ok(
        await client.get("/admin/audit", params={"table": "stores", "row_id": str(sid)})
    )
    assert [e["action"] for e in store_log] == ["insert"]

    # Reports over paid orders only.
    report = await ok(await client.get(f"/stores/{sid}/report"))
    assert [
        (
            r["product_id"],
            r["units"],
            r["revenue"],
            r["rank_in_store"],
            r["running_revenue"],
            r["previous_revenue"],
            r["quartile"],
        )
        for r in report
    ] == [
        (x["id"], 3, "30.00", 1, "30.00", None, 1),
        (y["id"], 4, "14.00", 2, "44.00", "30.00", 2),
        (z["id"], 1, "1.25", 3, "45.25", "14.00", 3),
    ]
    sales = await ok(await client.get(f"/stores/{sid}/sales"))
    assert sales == {"store_id": sid, "orders": 2, "revenue": "45.25"}
    customers = await ok(await client.get("/stats/customers", params={"store_id": sid}))
    assert [(c["user_id"], c["spent"], c["orders"], c["label"]) for c in customers] == [
        (c1, "45.25", 2, "#1 CLEO")
    ]
    today = datetime.datetime.now(datetime.UTC).date() - datetime.timedelta(days=1)
    days = await ok(await client.get("/stats/days", params={"store_id": sid, "since": str(today)}))
    assert [(d["orders"], d["revenue"], d["size"]) for d in days] == [(2, "45.25", "quiet")]
    day = datetime.datetime.fromisoformat(days[0]["day"])
    placed_at = datetime.datetime.fromisoformat(order["placed_at"])
    assert day <= placed_at < day + datetime.timedelta(days=1)
    assert 1 <= Decimal(days[0]["weekday"]) <= 7
    assert (
        await ok(
            await client.get(
                "/stats/days", params={"store_id": sid, "since": str(today), "min_revenue": "100"}
            )
        )
        == []
    )

    # Best sellers UNION best rated, EXCEPT sold-out ones (Z).
    await ok(await client.put(f"/products/{x['id']}/review", json={"rating": 5}, headers=H(c1)))
    featured = await ok(await client.get("/stats/featured", params={"store_id": sid}))
    assert featured == sorted([x["id"], y["id"]])


async def test_patterns_and_casts(client: httpx.AsyncClient) -> None:
    sam = await make_user(client, "sam", "seller")
    sid = (await make_store(client, sam))["id"]
    for sku, price in (("ab-1", "1.25"), ("ab-2", "10.00"), ("AB-3", "0.99"), ("cd-1", "7.00")):
        await make_product(client, sam, sid, sku=sku, name=sku.upper(), price=price)
    got = await ok(await client.get("/stats/skus", params={"store_id": sid, "pattern": "ab*"}))
    assert sorted(got["like"]) == ["ab-1", "ab-2"]
    assert sorted(got["similar"]) == ["ab-1", "ab-2"]
    assert sorted(got["regex"]) == ["AB-3", "ab-1", "ab-2"]
    assert sorted(got["price_cents"]) == sorted(["125", "1000", "99", "700"])
    assert sorted(got["labels"]) == sorted(["ab-1: AB-1", "ab-2: AB-2", "AB-3: AB-3", "cd-1: CD-1"])


# -- reviews: row-level security ------------------------------------------------------


async def test_reviews_rls(client: httpx.AsyncClient) -> None:
    sam = await make_user(client, "sam", "seller")
    u1, u2 = await make_user(client, "u1"), await make_user(client, "u2")
    sid = (await make_store(client, sam))["id"]
    pid = (await make_product(client, sam, sid))["id"]

    first = await ok(
        await client.put(
            f"/products/{pid}/review", json={"rating": 4, "body": "good"}, headers=H(u1)
        )
    )
    again = await ok(
        await client.put(
            f"/products/{pid}/review", json={"rating": 5, "body": "great"}, headers=H(u1)
        )
    )
    assert again["id"] == first["id"] and (again["rating"], again["body"]) == (5, "great")
    assert (
        await client.patch(f"/reviews/{first['id']}", json={"rating": 1}, headers=H(u2))
    ).status_code == 404
    assert (await client.delete(f"/reviews/{first['id']}", headers=H(u2))).status_code == 404
    forged = await client.post(
        "/reviews/forge", params={"product_id": pid, "as_user": u1}, headers=H(u2)
    )
    assert (forged.status_code, forged.json()) == (403, {"detail": "not allowed"})
    out_of_range = await client.put(f"/products/{pid}/review", json={"rating": 6}, headers=H(u2))
    assert out_of_range.json() == {"detail": "invalid value (reviews_rating_range)"}
    second = await ok(
        await client.put(f"/products/{pid}/review", json={"rating": 3}, headers=H(u2))
    )
    assert second["body"] == ""  # column default

    detail = await ok(await client.get(f"/products/{pid}"))
    assert (detail["review_count"], detail["rating"]) == (2, "4.00")
    assert [r["id"] for r in detail["reviews"]] == [second["id"], first["id"]]
    edited = await ok(
        await client.patch(f"/reviews/{first['id']}", json={"rating": 2}, headers=H(u1))
    )
    assert (edited["rating"], edited["body"]) == (2, "great")
    await ok(await client.delete(f"/reviews/{first['id']}", headers=H(u1)), 204)
    detail = await ok(await client.get(f"/products/{pid}"))
    assert (detail["review_count"], detail["rating"]) == (1, "3.00")
    assert (await client.get("/products/999999999")).status_code == 404


# -- notifications: triggers and row-level security ---------------------------


async def test_price_changes_notify(client: httpx.AsyncClient, db: Database) -> None:
    sam = await make_user(client, "sam", "seller")
    eve = await make_user(client, "eve", "seller")
    sid = (await make_store(client, sam))["id"]
    p = await make_product(client, sam, sid, price="10.00")
    q = await make_product(client, sam, sid, price="20.00")

    await asyncio.sleep(0.01)
    changed = await ok(
        await client.patch(f"/products/{p['id']}", json={"price": "11.00"}, headers=H(sam))
    )
    assert changed["updated_at"] > p["updated_at"] and changed["price"] == "11.00"
    await ok(
        await client.patch(f"/products/{p['id']}", json={"name": "renamed"}, headers=H(sam))
    )  # not price
    await ok(
        await client.patch(f"/products/{p['id']}", json={"price": "11.00"}, headers=H(sam))
    )  # same price
    await ok(await client.patch(f"/products/{q['id']}", json={"price": "19.50"}, headers=H(sam)))
    assert (
        await client.patch(f"/products/{q['id']}", json={"price": "1"}, headers=H(eve))
    ).status_code == 404

    mine = await ok(await client.get("/me/notifications", headers=H(sam)))
    assert [(n["kind"], n["payload"]["product_id"]) for n in mine] == [
        ("price_changed", p["id"]),
        ("price_changed", q["id"]),
    ]
    assert await ok(await client.get("/me/notifications", headers=H(eve))) == []
    assert await ok(await client.post("/me/notifications/read", headers=H(eve))) == 0
    assert await ok(await client.post("/me/notifications/read", headers=H(sam))) == 2
    assert (
        await ok(await client.get("/me/notifications", params={"unread": True}, headers=H(sam)))
        == []
    )
    # The app's own role bypasses RLS (superuser): it sees both rows.
    rows = (
        await db.primary.select()
        .from_(Notifications)
        .where(eq(Notifications.user_id, uuid.UUID(sam)))
    )
    assert len(rows) == 2 and all(r.read_at is not None for r in rows)


# -- vectors ---------------------------------------------------------------------------


def f32(x: float) -> float:
    return float(struct.unpack("f", struct.pack("f", x))[0])


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    return 1 - dot / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))


async def test_similar_products(client: httpx.AsyncClient, db: Database) -> None:
    health = await ok(await client.get("/admin/health"))
    available = await db.primary.execute(
        sql("SELECT 1 AS ok FROM pg_available_extensions WHERE name = 'vector'")
    )
    assert health["vectors"] is bool(available)
    sam = await make_user(client, "sam", "seller")
    sid = (await make_store(client, sam))["id"]
    ids = [(await make_product(client, sam, sid))["id"] for _ in range(4)]
    vectors = [[1.0, 0.0, 0.0], [0.9, 0.1, 0.0], [0.0, 1.0, 0.0], [-1.0, 0.0, 0.5]]
    if not health["vectors"]:
        response = await client.put(f"/products/{ids[0]}/embedding", json={"embedding": vectors[0]})
        assert response.status_code == 501
        assert (await client.get(f"/products/{ids[0]}/similar")).status_code == 501
        pytest.skip("no pgvector on this server: only the 501 path was checked")
    for pid, v in zip(ids, vectors, strict=True):
        body = {"embedding": [0.5, 0.5, 0.5], "half": [1.0, 2.0, 3.0], "keywords": {"1": 0.5}}
        await ok(await client.put(f"/products/{pid}/embedding", json=body), 204)
        body = {"embedding": v, "half": [v[0], 0.5, 0.25], "keywords": {"1": 0.5, "7": 2.0}}
        await ok(await client.put(f"/products/{pid}/embedding", json=body), 204)  # upsert
    hits = await ok(await client.get(f"/products/{ids[0]}/similar", params={"limit": 3}))
    assert [h["id"] for h in hits] == ids[1:]
    for hit, v in zip(hits, vectors[1:], strict=True):
        assert hit["distance"] == pytest.approx(cosine(vectors[0], v), abs=1e-6)
    [row] = (
        await db.primary.select()
        .from_(ProductEmbeddings)
        .where(eq(ProductEmbeddings.product_id, ids[1]))
    )
    # float4 / float2 values come back exactly as stored (0.9 is 0.89999997... in float4).
    assert row.embedding == [f32(0.9), f32(0.1), 0.0]
    assert row.half == pytest.approx([0.9, 0.5, 0.25], abs=1e-3)
    assert row.keywords == {1: 0.5, 7: 2.0}
    extra = await make_product(client, sam, sid)
    loaded = await db.insert_many(
        ProductEmbeddings, [{"product_id": extra["id"], "embedding": [1.0, 1.0, 0.0]}]
    )
    assert loaded == 1


# -- admin, timeouts, studio ----------------------------------------------------------


async def test_timeouts_and_health(client: httpx.AsyncClient) -> None:
    slow = await client.get("/admin/slow", params={"seconds": 3, "timeout": 0.2})
    assert (slow.status_code, slow.json()) == (504, {"detail": "the database took too long"})
    assert await ok(await client.get("/admin/slow", params={"seconds": 0.01, "timeout": 2})) == {
        "finished": True
    }
    health = await ok(await client.get("/admin/health"))
    assert health["pool_size"] >= 1


async def test_studio(app_url: str, sync: Sync) -> None:
    del sync
    url = app_url.replace(f"application_name={APP_NAME}", "application_name=shop_studio")
    studio = studio_app(
        dialect=PostgresDialect(),
        url=url,
        schemas=["public", "shop_audit"],
        token="t",
        allowed_hosts={"test"},
    )
    async with studio.router.lifespan_context(studio):
        transport = httpx.ASGITransport(app=studio)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            assert (await c.get("/api/tables")).status_code == 401
            tables = (await c.get("/api/tables", headers={"Authorization": "Bearer t"})).json()[
                "tables"
            ]
    by_name = {t["name"]: t for t in tables}
    assert {"users", "stores", "products", "orders", "order_items", "audit_log"} <= set(by_name)
    assert by_name["order_items"]["primary_key"]["columns"] == ["order_id", "line"]
    assert "details" in by_name["products"]["columns"]


async def test_tsvector_is_generated(client: httpx.AsyncClient, db: Database) -> None:
    sam = await make_user(client, "sam", "seller")
    sid = (await make_store(client, sam))["id"]
    pid = (await make_product(client, sam, sid, name="Running Shoes", details="for runners"))["id"]
    [(search,)] = (
        await db.primary.select(Products.search).from_(Products).where(eq(Products.id, pid))
    )
    want = (
        "setweight(to_tsvector('english', 'Running Shoes'), 'A') || "
        "setweight(to_tsvector('english', 'for runners'), 'B')"
    )
    [expected] = await db.execute(sql(f"SELECT ({want})::text AS v"))
    assert search is not None and search == expected["v"] and "'shoe':2A" in search
