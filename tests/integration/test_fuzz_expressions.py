"""Differential testing of expressions (arithmetic, CASE, coalesce, nullif, greatest/least,
string functions and ``+``, SIMILAR TO, date/time arithmetic), set operations (also ordered
by expressions), window functions (also named windows) and self-joins through table aliases:
random rows and random expressions run through mizzle on PostgreSQL and through a Python
model of SQL. Any difference is a bug in query building or decoding.
"""

import asyncio
import datetime
import os
from collections import Counter
from collections.abc import Callable, Generator
from dataclasses import dataclass
from typing import Literal, TypedDict, final

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from mizzle import (
    Database,
    Table,
    abs_,
    alias,
    and_,
    asc,
    case_,
    coalesce,
    concat,
    count,
    dense_rank,
    desc,
    eq,
    first_value,
    greatest,
    gt,
    gte,
    is_not_null,
    is_null,
    lag,
    lead,
    least,
    length,
    lower,
    lt,
    lte,
    ne,
    not_,
    nullable,
    nullif,
    or_,
    rank,
    row_number,
    rows_between,
    similar_to,
    sum_,
    upper,
)
from mizzle.core.query.select import Compound, Select
from mizzle.core.sql.functions import Case
from mizzle.core.sql.nodes import Expr
from mizzle.pg import date, integer, interval, serial, text, timestamp
from tests.integration.test_fuzz_queries import cmp, sql_and, sql_not, sql_or, sql_similar


@final
class Items(Table, name="fuzz_expr"):
    id = serial().primary_key()
    a = integer()
    b = integer()
    s = text()
    g = integer()  # partition / group key
    t = timestamp()
    d = date()
    e = date()
    iv = interval()


DDL = """
DROP TABLE IF EXISTS fuzz_expr;
CREATE TABLE fuzz_expr (id serial PRIMARY KEY, a integer, b integer, s text COLLATE "C",
                        g integer, t timestamp, d date, e date, iv interval);
"""


class Row(TypedDict):
    id: int
    a: int | None
    b: int | None
    s: str | None
    g: int | None
    t: datetime.datetime | None
    d: datetime.date | None
    e: datetime.date | None
    iv: datetime.timedelta | None


type NewRow = dict[str, int | str | datetime.date | datetime.timedelta | None]
type Truth = bool | None  # SQL three-valued logic: None is UNKNOWN
type Int = Expr[int | None]
type Str = Expr[str | None]


@dataclass(frozen=True)
class Term[V]:
    """An SQL expression, the same computation in Python, and a readable form."""

    expr: Expr[V | None]
    eval: Callable[[Row], V | None]
    text: str

    def __repr__(self) -> str:
        return self.text


@dataclass(frozen=True)
class Cond:
    expr: Expr[bool]
    eval: Callable[[Row], Truth]
    text: str

    def __repr__(self) -> str:
        return self.text


# -- the Python model ----------------------------------------------------------------


def arith(op: str, x: int | None, y: int | None) -> int | None:
    if x is None or y is None:
        return None
    match op:
        case "+":
            return x + y
        case "-":
            return x - y
        case "*":
            return x * y
        case _:  # integer division truncates toward zero, as in SQL
            quotient = abs(x) // abs(y)
            return quotient if (x < 0) == (y < 0) else -quotient


def first_not_null[V](*values: V | None) -> V | None:
    return next((v for v in values if v is not None), None)


def extreme(pick: Callable[[list[int]], int], *values: int | None) -> int | None:
    present = [v for v in values if v is not None]  # greatest/least ignore NULLs
    return pick(present) if present else None


# -- random expressions --------------------------------------------------------------

ints = st.integers(-3, 3)
words = st.text("abAB ", max_size=3)


def column(table: type[Items], name: Literal["a", "b", "g"]) -> Term[int]:
    col = {"a": table.a, "b": table.b, "g": table.g}[name]
    return Term(col, lambda r: r[name], name)


@st.composite
def int_leaf(draw: st.DrawFn, table: type[Items] = Items) -> Term[int]:
    if draw(st.integers(0, 4)) == 0:
        text_term = draw(str_terms(table))
        return Term(
            length(text_term.expr),
            lambda r: None if (v := text_term.eval(r)) is None else len(v),
            f"length({text_term})",
        )
    return column(table, draw(st.sampled_from(["a", "b", "g"])))


def int_ops(
    table: type[Items],
) -> Callable[[st.SearchStrategy[Term[int]]], st.SearchStrategy[Term[int]]]:
    def extend(children: st.SearchStrategy[Term[int]]) -> st.SearchStrategy[Term[int]]:
        return st.one_of(
            st.builds(binary, st.sampled_from("+-*/"), children, children | ints, st.booleans()),
            st.builds(coalesced, children, children | ints),
            st.builds(nulled, children, children | ints),
            st.builds(extremes, st.booleans(), children, children | ints),
            st.builds(absolute, children),
            st.builds(
                cased,
                st.lists(
                    st.tuples(int_conds(table, children, deep=False), children | ints),
                    min_size=1,
                    max_size=2,
                ),
                st.none() | children | ints,
            ),
        )

    return extend


def binary(op: str, x: Term[int], y: Term[int] | int, swap: bool) -> Term[int]:
    if isinstance(y, int):
        k = y
        if swap:  # k op x: the reflected operators
            exprs = {"+": k + x.expr, "-": k - x.expr, "*": k * x.expr, "/": k / nullif(x.expr, 0)}
            return Term(
                exprs[op],
                lambda r: arith(op, k, divisor(x.eval(r)) if op == "/" else x.eval(r)),
                f"({k} {op} {x})",
            )
        if op == "/" and k == 0:
            k = 1
        exprs = {"+": x.expr + k, "-": x.expr - k, "*": x.expr * k, "/": x.expr / k}
        return Term(exprs[op], lambda r: arith(op, x.eval(r), k), f"({x} {op} {k})")
    other = y
    right = nullif(other.expr, 0) if op == "/" else other.expr
    exprs = {"+": x.expr + right, "-": x.expr - right, "*": x.expr * right, "/": x.expr / right}
    return Term(
        exprs[op],
        lambda r: arith(op, x.eval(r), divisor(other.eval(r)) if op == "/" else other.eval(r)),
        f"({x} {op} {other})",
    )


def divisor(value: int | None) -> int | None:
    return None if value == 0 else value  # the SQL side divides by nullif(y, 0)


def coalesced(x: Term[int], y: Term[int] | int) -> Term[int]:
    if isinstance(y, int):
        k = y
        return Term(
            nullable(coalesce(x.expr, k)),
            lambda r: first_not_null(x.eval(r), k),
            f"coalesce({x}, {k})",
        )
    other = y
    return Term(
        coalesce(x.expr, other.expr),
        lambda r: first_not_null(x.eval(r), other.eval(r)),
        f"coalesce({x}, {other})",
    )


def nulled(x: Term[int], y: Term[int] | int) -> Term[int]:
    def model(r: Row) -> int | None:
        value, other = x.eval(r), y if isinstance(y, int) else y.eval(r)
        return None if value is not None and other is not None and value == other else value

    other_expr = y if isinstance(y, int) else y.expr
    return Term(nullif(x.expr, other_expr), model, f"nullif({x}, {y})")


def extremes(largest: bool, x: Term[int], y: Term[int] | int) -> Term[int]:
    fn, pick = (greatest, max) if largest else (least, min)
    other_expr = y if isinstance(y, int) else y.expr
    return Term(
        fn(x.expr, other_expr),
        lambda r: extreme(pick, x.eval(r), y if isinstance(y, int) else y.eval(r)),
        f"{fn.__name__}({x}, {y})",
    )


def absolute(x: Term[int]) -> Term[int]:
    return Term(abs_(x.expr), lambda r: None if (v := x.eval(r)) is None else abs(v), f"abs({x})")


def value_of(item: Term[int] | int, r: Row) -> int | None:
    return item if isinstance(item, int) else item.eval(r)


def cased(
    branches: list[tuple[Cond, Term[int] | int]], default: Term[int] | int | None
) -> Term[int]:
    first_cond, first = branches[0]
    case: Case[int | None] = (
        case_().when(first_cond.expr, first)
        if isinstance(first, int)
        else case_().when(first_cond.expr, first.expr)
    )
    for cond, value in branches[1:]:
        case = case.when(cond.expr, value if isinstance(value, int) else value.expr)
    expr: Int = (
        case
        if default is None
        else case.else_(default if isinstance(default, int) else default.expr)
    )

    def model(r: Row) -> int | None:
        for cond, value in branches:
            if cond.eval(r) is True:
                return value_of(value, r)
        return None if default is None else value_of(default, r)

    text = " ".join(f"WHEN {c} THEN {v}" for c, v in branches)
    return Term(expr, model, f"CASE {text} ELSE {default} END")


OPS = {"=": eq, "<>": ne, "<": lt, "<=": lte, ">": gt, ">=": gte}


def int_conds(
    table: type[Items], terms: st.SearchStrategy[Term[int]], *, deep: bool = True
) -> st.SearchStrategy[Cond]:
    def compare(op: str, x: Term[int], y: Term[int] | int) -> Cond:
        right = y if isinstance(y, int) else y.expr
        return Cond(
            OPS[op](x.expr, right), lambda r: cmp(op, x.eval(r), value_of(y, r)), f"{x} {op} {y}"
        )

    def null_check(x: Term[int], negate: bool) -> Cond:
        if negate:
            return Cond(is_not_null(x.expr), lambda r: x.eval(r) is not None, f"{x} NOT NULL")
        return Cond(is_null(x.expr), lambda r: x.eval(r) is None, f"{x} IS NULL")

    leaf = st.builds(compare, st.sampled_from(sorted(OPS)), terms, terms | ints) | st.builds(
        null_check, terms, st.booleans()
    )
    if not deep:
        return leaf

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
            build, st.sampled_from(["and", "or", "not"]), st.lists(children, min_size=1, max_size=2)
        )

    return st.recursive(leaf, combine, max_leaves=3)


@st.composite
def str_terms(draw: st.DrawFn, table: type[Items] = Items) -> Term[str]:
    term: Term[str] = Term(table.s, lambda r: r["s"], "s")
    for _ in range(draw(st.integers(0, 3))):
        kind = draw(
            st.sampled_from(["lower", "upper", "concat", "coalesce", "nullif", "plus", "radd"])
        )
        word = draw(words)
        inner = term
        match kind:
            case "lower":
                term = Term(
                    lower(inner.expr),
                    lambda r, t=inner: None if (v := t.eval(r)) is None else v.lower(),
                    f"lower({inner})",
                )
            case "upper":
                term = Term(
                    upper(inner.expr),
                    lambda r, t=inner: None if (v := t.eval(r)) is None else v.upper(),
                    f"upper({inner})",
                )
            case "concat":  # NULL parts count as empty
                term = Term(
                    nullable(concat(inner.expr, word)),
                    lambda r, t=inner, w=word: (t.eval(r) or "") + w,
                    f"concat({inner}, {word!r})",
                )
            case "coalesce":
                term = Term(
                    nullable(coalesce(inner.expr, word)),
                    lambda r, t=inner, w=word: first_not_null(t.eval(r), w),
                    f"coalesce({inner}, {word!r})",
                )
            case "plus":  # || : NULL if either side is NULL
                other = draw(st.sampled_from([word, "self"]))
                if other == "self":
                    term = Term(
                        inner.expr + inner.expr,
                        lambda r, t=inner: None if (v := t.eval(r)) is None else v + v,
                        f"({inner} || {inner})",
                    )
                else:
                    term = Term(
                        inner.expr + word,
                        lambda r, t=inner, w=word: None if (v := t.eval(r)) is None else v + w,
                        f"({inner} || {word!r})",
                    )
            case "radd":
                term = Term(
                    word + inner.expr,
                    lambda r, t=inner, w=word: None if (v := t.eval(r)) is None else w + v,
                    f"({word!r} || {inner})",
                )
            case _:
                term = Term(
                    nullif(inner.expr, word),
                    lambda r, t=inner, w=word: None if t.eval(r) == w else t.eval(r),
                    f"nullif({inner}, {word!r})",
                )
    return term


def int_terms(table: type[Items] = Items) -> st.SearchStrategy[Term[int]]:
    return st.recursive(int_leaf(table), int_ops(table), max_leaves=6)


# Days and microseconds only: asyncpg sends a timedelta as such (no months).
intervals = st.timedeltas(datetime.timedelta(days=-9999), datetime.timedelta(days=9999))
similar_patterns = st.text("abA %_|", max_size=5)
INT_TERMS = int_terms()
CONDS = int_conds(Items, INT_TERMS)
rows_strategy = st.lists(
    st.fixed_dictionaries(
        {
            "a": st.none() | ints,
            "b": st.none() | ints,
            "s": st.none() | words,
            "g": st.none() | st.integers(0, 2),
            "t": st.none()
            | st.datetimes(datetime.datetime(1990, 1, 1), datetime.datetime(2050, 1, 1)),
            "d": st.none() | st.dates(datetime.date(1990, 1, 1), datetime.date(2050, 1, 1)),
            "e": st.none() | st.dates(datetime.date(1990, 1, 1), datetime.date(2050, 1, 1)),
            "iv": st.none() | intervals,
        }
    ),
    max_size=10,
)


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
                await conn.execute("DROP TABLE fuzz_expr", [])
            await db.close()

        loop.run(teardown())


async def load(db: Database, rows: list[NewRow]) -> list[Row]:
    async with db.driver.acquire() as conn:
        await conn.execute("TRUNCATE fuzz_expr RESTART IDENTITY", [])
    if not rows:
        return []
    inserted = await db.insert(Items).values(rows).returning()
    return [
        {
            "id": i.id,
            "a": i.a,
            "b": i.b,
            "s": i.s,
            "g": i.g,
            "t": i.t,
            "d": i.d,
            "e": i.e,
            "iv": i.iv,
        }
        for i in inserted
    ]


EXAMPLES = int(os.environ.get("MIZZLE_FUZZ_EXAMPLES", "300"))
SETTINGS = settings(
    max_examples=EXAMPLES,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)


def nulls_first(value: int | None) -> tuple[bool, int]:
    return (value is not None, value or 0)


@SETTINGS
@given(rows=rows_strategy, term=INT_TERMS, text_term=str_terms(), data=st.data())
def test_expressions_match_python(
    runner: tuple[asyncio.Runner, Database],
    rows: list[NewRow],
    term: Term[int],
    text_term: Term[str],
    data: st.DataObject,
) -> None:
    loop, db = runner
    cond = data.draw(CONDS, label="cond")
    pattern = data.draw(similar_patterns, label="pattern")

    async def scenario() -> None:
        stored = await load(db, rows)
        got = await db.select(Items.id, term.expr, text_term.expr).from_(Items).order_by(Items.id)
        assert got == [(r["id"], term.eval(r), text_term.eval(r)) for r in stored], term

        want = [r["id"] for r in stored if cond.eval(r) is True]
        found = await db.select(Items.id).from_(Items).where(cond.expr).order_by(Items.id)
        assert [i for (i,) in found] == want, cond

        # Ordering by the expression, through a relational query (NULLs first, ties by id).
        order = [asc(term.expr, nulls="first"), asc(Items.id)]
        rel = await db.query(Items).find_many(where=cond.expr, order_by=order)
        hits = [r for r in stored if cond.eval(r) is True]
        assert [r.id for r in rel] == [
            r["id"] for r in sorted(hits, key=lambda r: (nulls_first(term.eval(r)), r["id"]))
        ]

        # Grouping by the expression: the same node in SELECT and GROUP BY binds once.
        groups = await db.select(term.expr, count()).from_(Items).group_by(term.expr)
        assert dict(groups) == Counter(term.eval(r) for r in stored)

        # SIMILAR TO on the text expression.
        similar = await (
            db.select(Items.id)
            .from_(Items)
            .where(similar_to(text_term.expr, pattern))
            .order_by(Items.id)
        )
        assert [i for (i,) in similar] == [
            r["id"] for r in stored if sql_similar(text_term.eval(r), pattern) is True
        ], (text_term, pattern)

    loop.run(scenario())


type SetOp = Literal["union", "union_all", "intersect", "intersect_all", "except_", "except_all"]


def combine_model(
    op: SetOp, left: list[int | None], right: list[int | None]
) -> Counter[int | None]:
    lc, rc = Counter(left), Counter(right)
    match op:
        case "union":
            return Counter(set(lc) | set(rc))
        case "union_all":
            return lc + rc
        case "intersect":
            return Counter(set(lc) & set(rc))
        case "intersect_all":
            return lc & rc
        case "except_":
            return Counter(set(lc) - set(rc))
        case _:
            return lc - rc


@SETTINGS
@given(
    rows=rows_strategy,
    left=CONDS,
    right=CONDS,
    op=st.sampled_from(
        ["union", "union_all", "intersect", "intersect_all", "except_", "except_all"]
    ),
    third=st.none() | st.sampled_from(["union", "except_"]),
    descending=st.booleans(),
    limit=st.none() | st.integers(0, 4),
    offset=st.none() | st.integers(0, 3),
    pivot=ints,
)
def test_set_operations_match_python(
    runner: tuple[asyncio.Runner, Database],
    rows: list[NewRow],
    left: Cond,
    right: Cond,
    op: SetOp,
    third: Literal["union", "except_"] | None,
    descending: bool,
    limit: int | None,
    offset: int | None,
    pivot: int,
) -> None:
    loop, db = runner

    def query(cond: Cond) -> Select[tuple[int | None]]:
        return db.select(Items.a).from_(Items).where(cond.expr)

    async def scenario() -> None:
        stored = await load(db, rows)
        left_values = [r["a"] for r in stored if left.eval(r) is True]
        right_values = [r["a"] for r in stored if right.eval(r) is True]
        combined: Compound[tuple[int | None]] = getattr(query(left), op)(query(right))
        want = combine_model(op, left_values, right_values)
        if third is not None:  # chained: (left op right) third (all rows)
            combined = getattr(combined, third)(db.select(Items.a).from_(Items))
            want = combine_model(third, list(want.elements()), [r["a"] for r in stored])
        got = [a for (a,) in await combined]
        assert Counter(got) == +want, f"{left} {op} {right} {third}"

        # ORDER BY / LIMIT / OFFSET apply to the combined rows (NULLs last ascending).
        ordered = combined.order_by((desc if descending else asc)(Items.a))
        if limit is not None:
            ordered = ordered.limit(limit)
        if offset is not None:
            ordered = ordered.offset(offset)
        values = sorted(want.elements(), key=nulls_first)
        present = [v for v in values if v is not None]
        nulls = [v for v in values if v is None]
        expected = (nulls + present[::-1]) if descending else (present + nulls)
        start = offset or 0
        expected = expected[start : None if limit is None else start + limit]
        assert [a for (a,) in await ordered] == expected

        # Ordered by an expression of the result column: distance to pivot, then a DESC.
        by_distance = combined.order_by(asc(abs_(Items.a - pivot), nulls="first"), desc(Items.a))
        if limit is not None:
            by_distance = by_distance.limit(limit)
        if offset is not None:
            by_distance = by_distance.offset(offset)
        closest = sorted(
            want.elements(), key=lambda v: (0, 0, 0) if v is None else (1, abs(v - pivot), -v)
        )
        assert [a for (a,) in await by_distance] == closest[
            start : None if limit is None else start + limit
        ]

    loop.run(scenario())


def window_model(stored: list[Row]) -> list[tuple[object, ...]]:
    """Per row (by id): row_number / rank / dense_rank over (PARTITION BY g ORDER BY a),
    count(a), lag(a), lead(a, 2, 99), first_value(a) and a 2-row running sum ordered by id."""
    out: list[tuple[object, ...]] = []
    for r in sorted(stored, key=lambda r: r["id"]):
        part = sorted((p for p in stored if p["g"] == r["g"]), key=lambda p: p["id"])
        by_a = sorted(part, key=lambda p: ((p["a"] is None), p["a"] or 0, p["id"]))
        key = (r["a"] is None, r["a"] or 0)
        keys = [(p["a"] is None, p["a"] or 0) for p in by_a]
        at = part.index(r)
        window = [p["a"] for p in part[max(0, at - 1) : at + 1] if p["a"] is not None]
        out.append(
            (
                r["id"],
                by_a.index(r) + 1,
                1 + sum(k < key for k in keys),
                1 + len({k for k in keys if k < key}),
                sum(p["a"] is not None for p in part),
                part[at - 1]["a"] if at > 0 else None,
                part[at + 2]["a"] if at + 2 < len(part) else 99,
                part[0]["a"],
                sum(window) if window else None,
            )
        )
    return out


@SETTINGS
@given(rows=rows_strategy)
def test_window_functions_match_python(
    runner: tuple[asyncio.Runner, Database], rows: list[NewRow]
) -> None:
    loop, db = runner
    by_a = [asc(Items.a), asc(Items.id)]

    async def scenario() -> None:
        stored = await load(db, rows)
        got = await (
            db.select(
                Items.id,
                row_number().over(partition_by=Items.g, order_by=by_a),
                rank().over(partition_by=Items.g, order_by=Items.a),
                dense_rank().over(partition_by=[Items.g], order_by=asc(Items.a)),
                count(Items.a).over(partition_by=Items.g),
                lag(Items.a).over(partition_by=Items.g, order_by=Items.id),
                lead(Items.a, 2, 99).over(partition_by=Items.g, order_by=Items.id),
                first_value(Items.a).over(partition_by=Items.g, order_by=Items.id),
                sum_(Items.a).over(
                    partition_by=Items.g, order_by=Items.id, frame=rows_between(-1, 0)
                ),
            )
            .from_(Items)
            .order_by(Items.id)
        )
        assert got == window_model(stored)

        # The same through named windows (WINDOW clause), refined in OVER.
        named = await (
            db.select(
                Items.id,
                row_number().over("p", order_by=by_a),
                rank().over("p", order_by=Items.a),
                dense_rank().over("p", order_by=asc(Items.a)),
                count(Items.a).over("p"),
                lag(Items.a).over("by_id"),
                lead(Items.a, 2, 99).over("by_id"),
                first_value(Items.a).over("by_id"),
                sum_(Items.a).over("by_id", frame=rows_between(-1, 0)),
            )
            .from_(Items)
            .window("p", partition_by=Items.g)
            .window("by_id", partition_by=Items.g, order_by=Items.id)
            .order_by(Items.id)
        )
        assert named == got

        # Top row per group through a CTE: the window result filtered outside.
        n = row_number().over(
            partition_by=Items.g, order_by=[desc(Items.a, nulls="last"), asc(Items.id)]
        )
        numbered = db.select({"id": Items.id, "n": n}).from_(Items).cte("numbered")
        top = (
            await db.with_(numbered)
            .select(numbered.c("id", int))
            .from_(numbered)
            .where(eq(numbered.c("n", int), 1))
        )
        best: dict[int | None, Row] = {}
        for r in stored:
            current = best.get(r["g"])
            if current is None or (nulls_first(r["a"]), -r["id"]) > (
                nulls_first(current["a"]),
                -current["id"],
            ):
                best[r["g"]] = r
        assert sorted(i for (i,) in top) == sorted(r["id"] for r in best.values())

    loop.run(scenario())


@SETTINGS
@given(rows=rows_strategy, data=st.data(), kind=st.sampled_from(["inner", "left"]))
def test_self_join_through_alias(
    runner: tuple[asyncio.Runner, Database], rows: list[NewRow], data: st.DataObject, kind: str
) -> None:
    loop, db = runner
    other = alias(Items, "other")
    mine = data.draw(int_terms(Items), label="mine")
    theirs = data.draw(int_terms(other), label="theirs")
    op = data.draw(st.sampled_from(sorted(OPS)), label="op")
    on = OPS[op](mine.expr, theirs.expr)

    async def scenario() -> None:
        stored = await load(db, rows)
        pairs = [
            (r, o) for r in stored for o in stored if cmp(op, mine.eval(r), theirs.eval(o)) is True
        ]
        want: list[tuple[int, int | None]] = [(r["id"], o["id"]) for r, o in pairs]
        query = db.select(Items, other).from_(Items)
        if kind == "left":
            got = await query.left_join(other, on)
            want += [(r["id"], None) for r in stored if not any(p[0] is r for p in pairs)]
        else:
            got = await query.join(other, on)
        key: Callable[[tuple[int, int | None]], tuple[int, int]] = lambda p: (p[0], p[1] or 0)  # noqa: E731
        assert sorted(((i.id, o.id if o else None) for i, o in got), key=key) == sorted(
            want, key=key
        ), f"{mine} {op} other.{theirs}"
        assert all(type(o) is Items for _, o in got if o is not None)

    loop.run(scenario())


def plus(
    x: datetime.datetime | datetime.date | None, y: datetime.timedelta | None
) -> datetime.datetime | None:
    """timestamp + interval; a date is midnight of that day."""
    if x is None or y is None:
        return None
    if not isinstance(x, datetime.datetime):
        x = datetime.datetime.combine(x, datetime.time())
    return x + y


def minus[V: (datetime.datetime, datetime.date)](
    x: V | None, y: V | None
) -> datetime.timedelta | None:
    return None if x is None or y is None else x - y


def before(
    x: datetime.datetime | None, y: datetime.datetime | None, *, or_equal: bool = False
) -> bool:
    """``x < y`` (or ``<=``) is true: NULL never is."""
    return x is not None and y is not None and (x <= y if or_equal else x < y)


def neg(value: datetime.timedelta | None) -> datetime.timedelta | None:
    return None if value is None else -value


@SETTINGS
@given(rows=rows_strategy, shift=intervals)
def test_date_time_arithmetic_matches_python(
    runner: tuple[asyncio.Runner, Database], rows: list[NewRow], shift: datetime.timedelta
) -> None:
    loop, db = runner
    origin = datetime.datetime(2000, 1, 1)

    async def scenario() -> None:
        stored = await load(db, rows)
        got = await (
            db.select(
                Items.t + Items.iv,
                Items.t - Items.iv,
                Items.t + shift,
                Items.t - shift,
                Items.d + Items.iv,
                Items.d - shift,
                Items.t - (Items.d + Items.iv),
                Items.t - origin,
                Items.d - Items.e,
            )
            .from_(Items)
            .order_by(Items.id)
        )
        want = [
            (
                plus(r["t"], r["iv"]),
                plus(r["t"], neg(r["iv"])),
                plus(r["t"], shift),
                plus(r["t"], -shift),
                plus(r["d"], r["iv"]),
                plus(r["d"], -shift),
                minus(r["t"], plus(r["d"], r["iv"])),
                minus(r["t"], origin),
                None if (days := minus(r["d"], r["e"])) is None else days.days,
            )
            for r in stored
        ]
        assert got == want

        # In a filter: rows whose timestamp is within the interval after the date.
        found = await (
            db.select(Items.id)
            .from_(Items)
            .where(and_(gte(Items.t, Items.d + shift), lt(Items.t - Items.iv, Items.d + shift)))
            .order_by(Items.id)
        )
        assert [i for (i,) in found] == [
            r["id"]
            for r in stored
            if before(plus(r["d"], shift), r["t"], or_equal=True)
            and before(plus(r["t"], neg(r["iv"])), plus(r["d"], shift))
        ]

    loop.run(scenario())
