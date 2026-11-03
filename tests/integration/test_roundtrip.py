"""Schema -> SQL -> database -> introspection must give back the same schema."""

import datetime
import enum
import uuid
from decimal import Decimal
from typing import Any

from mizzle import Table, check, index, primary_key, sql, unique, unique_index
from mizzle.core.snapshot import empty_snapshot
from mizzle.core.steps import Step
from mizzle.dialects.postgres import PostgresDialect
from mizzle.dialects.postgres.driver import PostgresDriver
from mizzle.kit.diff import diff
from mizzle.kit.snapshot import build_snapshot
from mizzle.pg import (
    array,
    bigint,
    bigserial,
    boolean,
    bytea,
    date,
    double_precision,
    inet,
    integer,
    interval,
    jsonb,
    numeric,
    pg_enum,
    real,
    serial,
    smallint,
    text,
    time,
    timestamp,
    varchar,
)
from mizzle.pg import (
    uuid as uuid_col,
)

dialect = PostgresDialect()


class Mood(enum.StrEnum):
    HAPPY = "happy"
    SAD = "sad"


class Org(Table, name="org", schema="tenant"):
    id = bigserial().primary_key()
    slug = varchar(64).not_null().unique()


class Everything(Table, name="everything"):
    id = serial().primary_key()
    org_id = bigint().references(lambda: Org.id, on_delete="set null", on_update="cascade")
    small = smallint().not_null().default(-1)
    num = numeric(10, 2).default(Decimal("1.50"))
    r = real()
    d = double_precision()
    flag = boolean().not_null().default(False)
    label = text().default("it's")
    born = date()
    at = timestamp(tz=True).not_null().default(sql("now()"))
    naive = timestamp()
    t = time()
    uid = uuid_col().default(sql("gen_random_uuid()"))
    blob = bytea()
    ip = inet()
    span = interval()
    data = jsonb[dict[str, Any]]().default({"a": 1})
    tags = array(text().not_null())
    mood = pg_enum(Mood).not_null().default(Mood.HAPPY)
    moods = array(pg_enum(Mood).not_null())
    score = integer().check(sql("score >= 0"))

    @classmethod
    def config(cls) -> list[Any]:
        return [
            index("everything_label_idx").on(cls.label, cls.born),
            index("everything_label_lower_idx").on(sql("lower(label)")),
            unique_index("everything_flag_label_uq").on(cls.flag, sql("upper(label)")),
            # A key whose text contains quotes and brackets must come back exactly.
            index("everything_tricky_idx").on(sql("(label || ')''(\"')")),
            unique_index("everything_uid_live").on(cls.uid).where(sql("flag")),
            unique(cls.small, cls.r, name="everything_small_r_uq"),
            check("everything_num_positive", sql("num > 0")),
        ]


class Pair(Table, name="pair"):
    a = integer().not_null()
    b = integer().not_null()

    @classmethod
    def config(cls) -> list[Any]:
        return [primary_key(cls.a, cls.b)]


TABLES = [Org, Everything, Pair]


async def apply(driver: PostgresDriver, steps: list[Step]) -> None:
    async with driver.acquire() as conn:
        for statement in dialect.render(steps):
            await conn.execute(statement.sql, [])


async def reset(driver: PostgresDriver) -> None:
    async with driver.acquire() as conn:
        await conn.execute(
            "DROP SCHEMA IF EXISTS tenant CASCADE; DROP TABLE IF EXISTS everything, pair, "
            "renamed CASCADE; DROP TYPE IF EXISTS mood CASCADE",
            [],
        )


async def test_create_then_introspect_matches(driver: PostgresDriver) -> None:
    await reset(driver)
    desired = build_snapshot(TABLES, dialect)
    await apply(driver, diff(empty_snapshot("postgresql"), desired))

    async with driver.acquire() as conn:
        live = await dialect.introspect(conn, ["public", "tenant"])
    live["tables"].pop("public.users", None)
    live["tables"].pop("public.posts", None)
    live["enums"].pop("public.role", None)

    remaining = diff(live, desired, compare_expressions=False)
    assert remaining == [], [s.describe() for s in remaining]
    assert live["tables"]["public.everything"]["columns"]["small"]["default"] == "-1"
    assert live["tables"]["public.everything"]["columns"]["id"]["type"] == "serial"
    assert live["enums"]["public.mood"]["values"] == ["happy", "sad"]
    tricky = live["tables"]["public.everything"]["indexes"]["everything_tricky_idx"]
    assert tricky.get("expression") == "(label || ')''(\"'::text)"
    assert tricky["columns"] == []


async def test_alter_migration_applies(driver: PostgresDriver) -> None:
    await reset(driver)
    v1 = build_snapshot(TABLES, dialect)
    await apply(driver, diff(empty_snapshot("postgresql"), v1))

    class Mood2(enum.StrEnum):
        GLAD = "glad"
        HAPPY = "happy"
        SAD = "sad"
        MAD = "mad"

    class Renamed(Table, name="renamed"):  # was "pair", columns renamed
        x = integer().not_null()
        b = bigint().not_null()
        extra = text().not_null().default("x")
        mood = pg_enum(Mood2, type_name="mood")

        @classmethod
        def config(cls) -> list[Any]:
            return [primary_key(cls.x, cls.b, name="pair_pkey")]

    v2 = build_snapshot([Org, Renamed], dialect)

    def resolve(kind: str, scope: str, removed: list[str], added: list[str]) -> dict[str, str]:
        if kind == "table" and "pair" in removed:
            return {"pair": "renamed"}
        if kind == "column" and scope == "public.renamed":
            return {"a": "x"}
        return {}

    steps = diff(v1, v2, resolve)
    described = [s.describe() for s in steps]
    assert "rename table pair to renamed" in described
    assert "rename column renamed.a to x" in described
    assert "drop table everything" in described
    assert any(s.startswith("change type of renamed.b") for s in described)
    await apply_non_transactional_safe(driver, steps)

    async with driver.acquire() as conn:
        live = await dialect.introspect(conn, ["public", "tenant"])
    for k in ("public.users", "public.posts"):
        live["tables"].pop(k, None)
    live["enums"].pop("public.role", None)
    assert diff(live, v2, compare_expressions=False) == []


async def apply_non_transactional_safe(driver: PostgresDriver, steps: list[Step]) -> None:
    # ALTER TYPE ... ADD VALUE runs outside a transaction; plain autocommit is fine here.
    await apply(driver, steps)


def test_default_literals_are_normalized() -> None:
    snap = build_snapshot([Everything, Org], dialect)
    cols = snap["tables"]["public.everything"]["columns"]
    assert cols["label"]["default"] == "'it''s'"
    assert cols["num"]["default"] == "1.50"
    assert cols["flag"]["default"] == "false"
    assert cols["mood"]["default"] == "'happy'"
    assert cols["data"]["default"] == "'{\"a\": 1}'"
    assert cols["at"]["type"] == "timestamp with time zone"
    assert cols["num"]["type"] == "numeric(10,2)"
    assert cols["moods"]["type"] == "mood[]"
    _ = (datetime, uuid)


async def test_temporal_literal_defaults_do_not_drift(driver: PostgresDriver) -> None:
    from mizzle.pg import time

    chatham = datetime.timezone(datetime.timedelta(hours=12, minutes=45))

    class Stamps(Table, name="stamps"):
        id = serial().primary_key()
        aware = timestamp(tz=True).default(datetime.datetime(2024, 1, 2, 3, 4, 5, 123456, chatham))
        utc = timestamp(tz=True).default(
            datetime.datetime(2024, 1, 2, 3, 4, 5, tzinfo=datetime.UTC)
        )
        naive = timestamp().default(datetime.datetime(2024, 1, 2, 3, 4, 5, 500000))
        day = date().default(datetime.date(2024, 1, 2))
        clock = time().default(datetime.time(3, 4, 5, 500000))
        clock_tz = time(tz=True).default(datetime.time(3, 4, 5, tzinfo=chatham))

    async with driver.acquire() as conn:
        await conn.execute("DROP TABLE IF EXISTS stamps", [])
    desired = build_snapshot([Stamps], dialect)
    await apply(driver, diff(empty_snapshot("postgresql"), desired))
    try:
        # PostgreSQL prints timestamptz defaults in the session time zone.
        for zone in ("UTC", "Asia/Kathmandu", "America/St_Johns"):
            async with driver.acquire() as conn, conn.transaction():
                await conn.execute(f"SET LOCAL TimeZone TO '{zone}'", [])
                live = await dialect.introspect(conn, ["public"])
            live["tables"] = {"public.stamps": live["tables"]["public.stamps"]}
            live["enums"] = {}
            remaining = diff(live, desired, compare_expressions=False)
            assert remaining == [], (zone, [s.describe() for s in remaining])
    finally:
        async with driver.acquire() as conn:
            await conn.execute("DROP TABLE stamps", [])
