"""Differential testing: random WHERE trees, orderings, limits and aggregates run through
mizzle on PostgreSQL and through a small Python model with SQL's three-valued logic (NULL is
"unknown"). Any difference is a bug in query building or decoding.
"""

import asyncio
import os
import re
from collections.abc import Callable, Generator
from dataclasses import dataclass
from typing import Literal, TypedDict, final

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from mizzle import (
    Database,
    Table,
    and_,
    asc,
    between,
    count,
    desc,
    eq,
    gt,
    gte,
    ilike,
    in_,
    is_not_null,
    is_null,
    like,
    lt,
    lte,
    max_,
    min_,
    ne,
    not_,
    not_in,
    not_similar_to,
    or_,
    similar_to,
    sum_,
)
from mizzle.core.sql.nodes import Expr, OrderBy
from mizzle.pg import boolean, integer, serial, text


@final
class Items(Table, name="fuzz_items"):
    id = serial().primary_key()
    n = integer()
    s = text()
    f = boolean()


DDL = """
DROP TABLE IF EXISTS fuzz_items;
CREATE TABLE fuzz_items (id serial PRIMARY KEY, n integer, s text COLLATE "C", f boolean);
"""


class Row(TypedDict):
    id: int
    n: int | None
    s: str | None
    f: bool | None


type NewRow = dict[str, int | str | bool | None]
type Nulls = Literal["first", "last"] | None
type Truth = bool | None  # SQL three-valued logic: None is UNKNOWN


@dataclass(frozen=True)
class Cond:
    expr: Expr[bool]
    eval: Callable[[Row], Truth]
    text: str

    def __repr__(self) -> str:
        return self.text


# -- the Python model of SQL ---------------------------------------------------------


def cmp[T: (int, str, bool)](op: str, a: T | None, b: T | None) -> Truth:
    if a is None or b is None:
        return None
    # ints with ints, strs with strs (the column uses C collation: code point order)
    match op:
        case "=":
            return a == b
        case "<>":
            return a != b
        case "<":
            return a < b
        case "<=":
            return a <= b
        case ">":
            return a > b
        case _:
            return a >= b


def sql_and(values: list[Truth]) -> Truth:
    if any(v is False for v in values):
        return False
    return None if any(v is None for v in values) else True


def sql_or(values: list[Truth]) -> Truth:
    if any(v is True for v in values):
        return True
    return None if any(v is None for v in values) else False


def sql_not(value: Truth) -> Truth:
    return None if value is None else not value


def sql_is(value: Truth) -> Truth:
    return value


def sql_in(value: int | None, options: list[int | None], negate: bool) -> Truth:
    if not options:
        return negate
    result = sql_or([cmp("=", value, o) for o in options])
    return sql_not(result) if negate else result


def like_regex(pattern: str, fold: bool) -> re.Pattern[str]:
    body = "".join(".*" if ch == "%" else "." if ch == "_" else re.escape(ch) for ch in pattern)
    return re.compile(body, re.DOTALL | (re.IGNORECASE if fold else 0))


def sql_like(value: str | None, pattern: str, fold: bool) -> Truth:
    if value is None:
        return None
    return like_regex(pattern, fold).fullmatch(value) is not None


def sql_similar(value: str | None, pattern: str) -> Truth:
    """SIMILAR TO with patterns of letters, ``%``, ``_`` and ``|``: a whole-string match."""
    if value is None:
        return None
    body = "".join(
        ".*" if ch == "%" else "." if ch == "_" else ch if ch == "|" else re.escape(ch)
        for ch in pattern
    )
    return re.fullmatch(f"(?:{body})", value, re.DOTALL) is not None


# -- random conditions ---------------------------------------------------------------

ints = st.integers(-3, 3)
strs = st.text("abAB", max_size=3)
patterns = st.text("ab%_", max_size=4)
similar_patterns = st.text("ab%_|", max_size=5)
OPS = {"=": eq, "<>": ne, "<": lt, "<=": lte, ">": gt, ">=": gte}


@st.composite
def leaves(draw: st.DrawFn) -> Cond:
    kind = draw(
        st.sampled_from(["cmp_n", "cmp_s", "null", "in", "between", "like", "similar", "bool"])
    )
    match kind:
        case "cmp_n":
            op, number = draw(st.sampled_from(sorted(OPS))), draw(ints)
            return Cond(
                OPS[op](Items.n, number),
                lambda r: cmp(op, r["n"], number),
                f"n {op} {number}",
            )
        case "cmp_s":
            op, word = draw(st.sampled_from(sorted(OPS))), draw(strs)
            return Cond(
                OPS[op](Items.s, word),
                lambda r: cmp(op, r["s"], word),
                f"s {op} {word!r}",
            )
        case "null":
            name = draw(st.sampled_from(["n", "s", "f"]))
            col = {"n": Items.n, "s": Items.s, "f": Items.f}[name]
            value_of: Callable[[Row], object] = {
                "n": lambda r: r["n"],
                "s": lambda r: r["s"],
                "f": lambda r: r["f"],
            }[name]
            if draw(st.booleans()):
                return Cond(is_null(col), lambda r: value_of(r) is None, f"{name} IS NULL")
            return Cond(is_not_null(col), lambda r: value_of(r) is not None, f"{name} NOT NULL")
        case "in":
            options = draw(st.lists(st.none() | ints, max_size=4))
            negate = draw(st.booleans())
            fn = not_in if negate else in_
            return Cond(
                fn(Items.n, options),
                lambda r: sql_in(r["n"], list(options), negate),
                f"n {'NOT ' if negate else ''}IN {options}",
            )
        case "between":
            low, high = draw(ints), draw(ints)
            return Cond(
                between(Items.n, low, high),
                lambda r: sql_and([cmp(">=", r["n"], low), cmp("<=", r["n"], high)]),
                f"n BETWEEN {low} AND {high}",
            )
        case "like":
            pattern, fold = draw(patterns), draw(st.booleans())
            return Cond(
                (ilike if fold else like)(Items.s, pattern),
                lambda r: sql_like(r["s"], pattern, fold),
                f"s {'ILIKE' if fold else 'LIKE'} {pattern!r}",
            )
        case "similar":
            pattern, negate = draw(similar_patterns), draw(st.booleans())
            return Cond(
                (not_similar_to if negate else similar_to)(Items.s, pattern),
                lambda r: (sql_not if negate else sql_is)(sql_similar(r["s"], pattern)),
                f"s {'NOT ' if negate else ''}SIMILAR TO {pattern!r}",
            )
        case _:
            value = draw(st.booleans())
            return Cond(eq(Items.f, value), lambda r: cmp("=", r["f"], value), f"f = {value}")


def combine(children: st.SearchStrategy[Cond]) -> st.SearchStrategy[Cond]:
    def build(kind: str, items: list[Cond]) -> Cond:
        if kind == "not":
            item = items[0]
            return Cond(not_(item.expr), lambda r: sql_not(item.eval(r)), f"NOT ({item})")
        joined = f" {kind.upper()} ".join(f"({c})" for c in items)
        if kind == "and":
            return Cond(
                and_(*[c.expr for c in items]),
                lambda r: sql_and([c.eval(r) for c in items]),
                joined,
            )
        return Cond(
            or_(*[c.expr for c in items]), lambda r: sql_or([c.eval(r) for c in items]), joined
        )

    return st.builds(
        build, st.sampled_from(["and", "or", "not"]), st.lists(children, min_size=1, max_size=3)
    )


conditions = st.recursive(leaves(), combine, max_leaves=8)
rows_strategy = st.lists(
    st.fixed_dictionaries(
        {"n": st.none() | ints, "s": st.none() | strs, "f": st.none() | st.booleans()}
    ),
    max_size=12,
)


# -- expected ordering ---------------------------------------------------------------


def value_of(column: str) -> Callable[[Row], int | str | bool | None]:
    return {"n": lambda r: r["n"], "s": lambda r: r["s"], "f": lambda r: r["f"]}[column]


def expected_order(rows: list[Row], column: str, descending: bool, nulls: Nulls) -> list[Row]:
    """PostgreSQL defaults: ASC NULLS LAST, DESC NULLS FIRST. Ties break by id ascending
    (Python's sort is stable, also with reverse=True)."""
    get = value_of(column)
    by_id = sorted(rows, key=lambda r: r["id"])
    values = [r for r in by_id if get(r) is not None]
    # One column holds one type, so comparing its non-NULL values is safe.
    values.sort(
        key=lambda r: str(get(r)) if column == "s" else int(get(r) or 0), reverse=descending
    )
    null_rows = [r for r in by_id if get(r) is None]
    nulls_first = nulls == "first" if nulls else descending
    return null_rows + values if nulls_first else values + null_rows


# -- harness -------------------------------------------------------------------------


@pytest.fixture(scope="module")
def runner() -> Generator[tuple[asyncio.Runner, Database]]:
    url = os.environ.get("MIZZLE_TEST_URL")
    if not url:
        pytest.skip("set MIZZLE_TEST_URL to run integration tests")
    with asyncio.Runner() as loop:
        db = loop.run(Database.connect(url, schema=[Items], pool_size=2))

        async def setup() -> None:
            async with db.driver.acquire() as conn:
                await conn.execute(DDL, [])

        loop.run(setup())
        yield loop, db

        async def teardown() -> None:
            async with db.driver.acquire() as conn:
                await conn.execute("DROP TABLE fuzz_items", [])
            await db.close()

        loop.run(teardown())


async def load(db: Database, rows: list[NewRow]) -> list[Row]:
    async with db.driver.acquire() as conn:
        await conn.execute("TRUNCATE fuzz_items RESTART IDENTITY", [])
    if not rows:
        return []
    inserted = await db.insert(Items).values(rows).returning()
    return [{"id": i.id, "n": i.n, "s": i.s, "f": i.f} for i in inserted]


EXAMPLES = int(os.environ.get("MIZZLE_FUZZ_EXAMPLES", "300"))
SETTINGS = settings(
    max_examples=EXAMPLES,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)


@SETTINGS
@given(rows=rows_strategy, cond=conditions)
def test_where_matches_sql_semantics(
    runner: tuple[asyncio.Runner, Database], rows: list[NewRow], cond: Cond
) -> None:
    loop, db = runner

    async def scenario() -> None:
        stored = await load(db, rows)
        want = sorted(r["id"] for r in stored if cond.eval(r) is True)
        got = await db.select(Items.id).from_(Items).where(cond.expr).order_by(Items.id)
        assert [i for (i,) in got] == want, f"WHERE {cond}"
        # The same filter through a relational query and through NOT: rows where the
        # condition is UNKNOWN must appear in neither.
        rel = await db.query(Items).find_many(where=cond.expr, order_by=Items.id)
        assert [r.id for r in rel] == want
        negated = await db.select(Items.id).from_(Items).where(not_(cond.expr))
        assert sorted(i for (i,) in negated) == sorted(
            r["id"] for r in stored if cond.eval(r) is False
        )

    loop.run(scenario())


@SETTINGS
@given(
    rows=rows_strategy,
    column=st.sampled_from(["n", "s", "f"]),
    descending=st.booleans(),
    nulls=st.sampled_from([None, "first", "last"]),
    limit=st.none() | st.integers(0, 5),
    offset=st.none() | st.integers(0, 5),
)
def test_order_limit_offset(
    runner: tuple[asyncio.Runner, Database],
    rows: list[NewRow],
    column: str,
    descending: bool,
    nulls: Nulls,
    limit: int | None,
    offset: int | None,
) -> None:
    loop, db = runner
    col = {"n": Items.n, "s": Items.s, "f": Items.f}[column]
    order: OrderBy = (desc if descending else asc)(col, nulls=nulls)

    async def scenario() -> None:
        stored = await load(db, rows)
        want = [r["id"] for r in expected_order(stored, column, descending, nulls)]
        start = offset or 0
        want = want[start : None if limit is None else start + limit]
        query = db.select(Items.id).from_(Items).order_by(order, asc(Items.id))
        if limit is not None:
            query = query.limit(limit)
        if offset is not None:
            query = query.offset(offset)
        assert [i for (i,) in await query] == want
        rel = await db.query(Items).find_many(
            order_by=[order, asc(Items.id)], limit=limit, offset=offset
        )
        assert [r.id for r in rel] == want

    loop.run(scenario())


@SETTINGS
@given(rows=rows_strategy, cond=conditions)
def test_aggregates_and_group_by(
    runner: tuple[asyncio.Runner, Database], rows: list[NewRow], cond: Cond
) -> None:
    loop, db = runner

    async def scenario() -> None:
        stored = await load(db, rows)
        hits = [r for r in stored if cond.eval(r) is True]
        ns = [r["n"] for r in hits if r["n"] is not None]
        [(total, non_null, low, high, summed)] = (
            await db.select(count(), count(Items.n), min_(Items.n), max_(Items.n), sum_(Items.n))
            .from_(Items)
            .where(cond.expr)
        )
        assert (total, non_null) == (len(hits), len(ns))
        assert (low, high) == ((min(ns), max(ns)) if ns else (None, None))
        assert summed == (sum(ns) if ns else None)

        groups = (
            await db.select({"n": Items.n, "c": count()})
            .from_(Items)
            .where(cond.expr)
            .group_by(Items.n)
        )
        want: dict[object, int] = {}
        for r in hits:
            want[r["n"]] = want.get(r["n"], 0) + 1
        assert {g["n"]: g["c"] for g in groups} == want

    loop.run(scenario())
