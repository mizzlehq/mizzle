"""Differential testing of joins, subqueries, CTEs, grouping and relational queries: random
rows (with NULL foreign keys and orphans) and random queries run through mizzle on PostgreSQL
and through a Python model of SQL. Any difference is a bug in query building or decoding.
"""

import asyncio
import os
from collections.abc import Callable, Generator, Sequence
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from typing import Literal, final

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from mizzle import (
    Database,
    Table,
    and_,
    asc,
    avg,
    count,
    desc,
    eq,
    exists,
    gt,
    gte,
    in_,
    is_not_null,
    is_null,
    lt,
    lte,
    many,
    max_,
    min_,
    ne,
    not_,
    not_exists,
    not_in,
    nullable,
    one,
    or_,
    placeholder,
    sum_,
)
from mizzle.core.query.builder import QueryBuilder
from mizzle.core.query.relational import WithOptions
from mizzle.core.query.select import JoinKind, Select, SelectBase, Source
from mizzle.core.schema.column import ColumnBase
from mizzle.core.sql.nodes import Condition, Expr, OrderBy
from mizzle.pg import integer, serial, text
from tests.integration.test_fuzz_queries import cmp, sql_and, sql_in, sql_not, sql_or


@final
class Parent(Table, name="fj_parent"):
    id = serial().primary_key()
    n = integer()
    s = text()
    children = many(lambda: Child)


@final
class Child(Table, name="fj_child"):
    id = serial().primary_key()
    parent_id = integer()  # no foreign key: orphans are allowed
    n = integer()
    s = text()
    parent = one(lambda: Parent, fields=lambda: [Child.parent_id], references=lambda: [Parent.id])
    toys = many(lambda: Toy)


@final
class Toy(Table, name="fj_toy"):
    id = serial().primary_key()
    child_id = integer()
    n = integer()
    child = one(lambda: Child, fields=lambda: [Toy.child_id], references=lambda: [Child.id])


# camelCase attributes, snake_case columns, in a schema of their own.
@final
class Owner(Table, name="owner", schema="fj_s"):
    id = serial().primary_key()
    displayName = text()
    score = integer()
    pets = many(lambda: Pet)


@final
class Pet(Table, name="pet", schema="fj_s"):
    id = serial().primary_key()
    ownerId = integer()
    petName = text()
    owner = one(lambda: Owner, fields=lambda: [Pet.ownerId], references=lambda: [Owner.id])


DDL = """
DROP TABLE IF EXISTS fj_parent, fj_child, fj_toy;
DROP SCHEMA IF EXISTS fj_s CASCADE;
CREATE TABLE fj_parent (id serial PRIMARY KEY, n integer, s text COLLATE "C");
CREATE TABLE fj_child (
    id serial PRIMARY KEY, parent_id integer, n integer, s text COLLATE "C"
);
CREATE TABLE fj_toy (id serial PRIMARY KEY, child_id integer, n integer);
CREATE SCHEMA fj_s;
CREATE TABLE fj_s.owner (id serial PRIMARY KEY, display_name text COLLATE "C", score integer);
CREATE TABLE fj_s.pet (id serial PRIMARY KEY, owner_id integer, pet_name text COLLATE "C");
"""

type Val = int | str | None
type Rec = dict[str, Val]
type Env = dict[str, Rec | None]  # table alias -> its row (None: missing side of a join)
type Truth = bool | None
type Nulls = Literal["first", "last"] | None
type Order = tuple[str, bool, Nulls]  # column, descending, nulls
type Tree = dict[str, Val | Tree | list[Tree]]
type Data = dict[str, list[Rec]]  # alias -> stored rows


@dataclass(frozen=True)
class Cond:
    expr: Expr[bool]
    eval: Callable[[Env], Truth]
    text: str

    def __repr__(self) -> str:
        return self.text


def x_query(db: QueryBuilder) -> SelectBase:
    """The CTE ``x`` used by the tests: children, renamed."""
    return db.select({"cid": Child.id, "pid": Child.parent_id, "n": Child.n, "s": Child.s}).from_(
        Child
    )


X = x_query(Database()).cte("x")  # only its column references are used

# Every column by alias, typed as nullable: an outer join can make any of them NULL.
INTS: dict[str, dict[str, Expr[int | None]]] = {
    "p": {"id": nullable(Parent.id), "n": Parent.n},
    "c": {"id": nullable(Child.id), "parent_id": Child.parent_id, "n": Child.n},
    "t": {"id": nullable(Toy.id), "child_id": Toy.child_id, "n": Toy.n},
    "x": {
        "cid": nullable(X.c("cid", int)),
        "pid": nullable(X.c("pid", int)),
        "n": nullable(X.c("n", int)),
    },
}
STRS: dict[str, dict[str, Expr[str | None]]] = {
    "p": {"s": Parent.s},
    "c": {"s": Child.s},
    "t": {},
    "x": {"s": nullable(X.c("s", str))},
}
COLUMNS: dict[str, dict[str, ColumnBase]] = {
    "p": {"id": Parent.id, "n": Parent.n, "s": Parent.s},
    "c": {"id": Child.id, "parent_id": Child.parent_id, "n": Child.n, "s": Child.s},
    "t": {"id": Toy.id, "child_id": Toy.child_id, "n": Toy.n},
}
# alias -> relation -> (target alias, many, source column, target column)
RELATIONS: dict[str, dict[str, tuple[str, bool, str, str]]] = {
    "p": {"children": ("c", True, "id", "parent_id")},
    "c": {"parent": ("p", False, "parent_id", "id"), "toys": ("t", True, "id", "child_id")},
    "t": {"child": ("c", False, "child_id", "id")},
}
OPS = {"=": eq, "<>": ne, "<": lt, "<=": lte, ">": gt, ">=": gte}
KINDS: list[JoinKind] = ["INNER", "LEFT", "RIGHT", "FULL"]
NULLS: list[Nulls] = [None, "first", "last"]


def get(env: Env, alias: str, name: str) -> Val:
    rec = env[alias]
    return None if rec is None else rec[name]


def as_val(value: object) -> Val:
    assert value is None or isinstance(value, int | str)
    return value


def ival(env: Env, alias: str, name: str) -> int | None:
    value = get(env, alias, name)
    assert not isinstance(value, str)
    return value


def sval(env: Env, alias: str, name: str) -> str | None:
    value = get(env, alias, name)
    assert not isinstance(value, int)
    return value


# -- random conditions ---------------------------------------------------------------

ints = st.integers(-3, 7)
strs = st.text("abAB", max_size=3)


@st.composite
def leaves(draw: st.DrawFn, aliases: tuple[str, ...]) -> Cond:
    alias = draw(st.sampled_from(aliases))
    kinds = ["cmp", "cmp", "cols", "null", "in"] + (["str"] if STRS[alias] else [])
    name = draw(st.sampled_from(sorted(INTS[alias])))
    col = INTS[alias][name]
    op = draw(st.sampled_from(sorted(OPS)))
    match draw(st.sampled_from(kinds)):
        case "cmp":
            k = draw(ints)
            return Cond(
                OPS[op](col, k),
                lambda e: cmp(op, ival(e, alias, name), k),
                f"{alias}.{name} {op} {k}",
            )
        case "cols":
            other = draw(st.sampled_from(aliases))
            other_name = draw(st.sampled_from(sorted(INTS[other])))
            return Cond(
                OPS[op](col, INTS[other][other_name]),
                lambda e: cmp(op, ival(e, alias, name), ival(e, other, other_name)),
                f"{alias}.{name} {op} {other}.{other_name}",
            )
        case "str":
            s_name = draw(st.sampled_from(sorted(STRS[alias])))
            word = draw(strs)
            return Cond(
                OPS[op](STRS[alias][s_name], word),
                lambda e: cmp(op, sval(e, alias, s_name), word),
                f"{alias}.{s_name} {op} {word!r}",
            )
        case "null":
            if draw(st.booleans()):
                return Cond(
                    is_null(col), lambda e: get(e, alias, name) is None, f"{alias}.{name} IS NULL"
                )
            return Cond(
                is_not_null(col),
                lambda e: get(e, alias, name) is not None,
                f"{alias}.{name} NOT NULL",
            )
        case _:
            options = draw(st.lists(st.none() | ints, max_size=4))
            negate = draw(st.booleans())
            return Cond(
                (not_in if negate else in_)(col, options),
                lambda e: sql_in(ival(e, alias, name), list(options), negate),
                f"{alias}.{name} {'NOT ' if negate else ''}IN {options}",
            )


def combine(children: st.SearchStrategy[Cond]) -> st.SearchStrategy[Cond]:
    def build(kind: str, items: list[Cond]) -> Cond:
        if kind == "not":
            item = items[0]
            return Cond(not_(item.expr), lambda e: sql_not(item.eval(e)), f"NOT ({item})")
        joined = f" {kind.upper()} ".join(f"({c})" for c in items)
        if kind == "and":
            return Cond(
                and_(*[c.expr for c in items]),
                lambda e: sql_and([c.eval(e) for c in items]),
                joined,
            )
        return Cond(
            or_(*[c.expr for c in items]), lambda e: sql_or([c.eval(e) for c in items]), joined
        )

    return st.builds(
        build, st.sampled_from(["and", "or", "not"]), st.lists(children, min_size=1, max_size=3)
    )


def conditions(*aliases: str) -> st.SearchStrategy[Cond]:
    return st.recursive(leaves(aliases), combine, max_leaves=5)


def col_eq(a: str, a_name: str, b: str, b_name: str) -> Cond:
    return Cond(
        eq(INTS[a][a_name], INTS[b][b_name]),
        lambda e: cmp("=", ival(e, a, a_name), ival(e, b, b_name)),
        f"{a}.{a_name} = {b}.{b_name}",
    )


def both(first: Cond, second: Cond | None) -> Cond:
    if second is None:
        return first
    return Cond(
        and_(first.expr, second.expr),
        lambda e: sql_and([first.eval(e), second.eval(e)]),
        f"({first}) AND ({second})",
    )


def holds(cond: Cond | None, env: Env) -> bool:
    return cond is None or cond.eval(env) is True


# -- data ----------------------------------------------------------------------------

maybe_int = st.none() | ints
data_strategy = st.fixed_dictionaries(
    {
        "p": st.lists(st.fixed_dictionaries({"n": maybe_int, "s": st.none() | strs}), max_size=5),
        "c": st.lists(
            st.fixed_dictionaries(
                {"parent_id": st.none() | st.integers(1, 6), "n": maybe_int, "s": st.none() | strs}
            ),
            max_size=7,
        ),
        "t": st.lists(
            st.fixed_dictionaries({"child_id": st.none() | st.integers(1, 8), "n": maybe_int}),
            max_size=7,
        ),
    }
)


def columns_of(obj: Table) -> dict[str, Callable[[], Val]]:
    match obj:
        case Parent():
            return {"id": lambda: obj.id, "n": lambda: obj.n, "s": lambda: obj.s}
        case Child():
            return {
                "id": lambda: obj.id,
                "parent_id": lambda: obj.parent_id,
                "n": lambda: obj.n,
                "s": lambda: obj.s,
            }
        case Toy():
            return {"id": lambda: obj.id, "child_id": lambda: obj.child_id, "n": lambda: obj.n}
        case _:
            raise TypeError(obj)


def relations_of(obj: Table) -> dict[str, Callable[[], Sequence[Table] | Table | None]]:
    match obj:
        case Parent():
            return {"children": lambda: obj.children}
        case Child():
            return {"parent": lambda: obj.parent, "toys": lambda: obj.toys}
        case Toy():
            return {"child": lambda: obj.child}
        case _:
            raise TypeError(obj)


def rec(obj: Table | None) -> Rec | None:
    return None if obj is None else {k: read() for k, read in columns_of(obj).items()}


async def load(db: Database, data: Data) -> Data:
    async with db.driver.acquire() as conn:
        await conn.execute("TRUNCATE fj_parent, fj_child, fj_toy RESTART IDENTITY", [])
    stored: Data = {}
    for alias, table in (("p", Parent), ("c", Child), ("t", Toy)):
        rows = await db.insert(table).values(data[alias]).returning() if data[alias] else []
        stored[alias] = [r for r in map(rec, rows) if r is not None]
    return stored


def null_last(value: Val) -> tuple[bool, int | str]:
    return (value is None, 0 if value is None else value)


def ordered(rows: list[Rec], order: Order | None) -> list[Rec]:
    """PostgreSQL defaults: ASC NULLS LAST, DESC NULLS FIRST; ties break by id ascending."""
    by_id = sorted(rows, key=lambda r: null_last(r["id"]))
    if order is None:
        return by_id
    column, descending, nulls = order
    values = [r for r in by_id if r[column] is not None]
    values.sort(key=lambda r: null_last(r[column]), reverse=descending)
    null_rows = [r for r in by_id if r[column] is None]
    nulls_first = nulls == "first" if nulls else descending
    return null_rows + values if nulls_first else values + null_rows


def order_items(alias: str, order: Order | None) -> list[OrderBy] | None:
    if order is None:
        return None
    column, descending, nulls = order
    first = (desc if descending else asc)(COLUMNS[alias][column], nulls=nulls)
    return [first, asc(COLUMNS[alias]["id"])]


# -- harness -------------------------------------------------------------------------


@pytest.fixture(scope="module")
def runner() -> Generator[tuple[asyncio.Runner, Database, Database]]:
    url = os.environ.get("MIZZLE_TEST_URL")
    if not url:
        pytest.skip("set MIZZLE_TEST_URL to run integration tests")
    with asyncio.Runner() as loop:
        db = loop.run(Database.connect(url, schema=[Parent, Child, Toy], pool_size=2))
        snake = loop.run(
            Database.connect(url, schema=[Owner, Pet], casing="snake_case", pool_size=1)
        )

        async def setup() -> None:
            async with db.driver.acquire() as conn:
                await conn.execute(DDL, [])

        loop.run(setup())
        yield loop, db, snake

        async def teardown() -> None:
            async with db.driver.acquire() as conn:
                await conn.execute("DROP TABLE fj_parent, fj_child, fj_toy", [])
                await conn.execute("DROP SCHEMA fj_s CASCADE", [])
            await db.close()
            await snake.close()

        loop.run(teardown())


EXAMPLES = int(os.environ.get("MIZZLE_FUZZ_EXAMPLES", "200"))
SETTINGS = settings(
    max_examples=EXAMPLES,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
type Harness = tuple[asyncio.Runner, Database, Database]


# -- joins ---------------------------------------------------------------------------


def model_join(
    left: list[tuple[Rec | None, ...]],
    aliases: list[str],
    right: list[Rec],
    alias: str,
    kind: JoinKind,
    on: Cond,
) -> list[tuple[Rec | None, ...]]:
    out: list[tuple[Rec | None, ...]] = []
    matched: set[int] = set()
    for row in left:
        env: Env = dict(zip(aliases, row, strict=True))
        hit = False
        for i, r in enumerate(right):
            if on.eval({**env, alias: r}) is True:
                out.append((*row, r))
                matched.add(i)
                hit = True
        if not hit and kind in ("LEFT", "FULL"):
            out.append((*row, None))
    if kind in ("RIGHT", "FULL"):
        out += [(*(None,) * len(aliases), r) for i, r in enumerate(right) if i not in matched]
    return out


def joined[R](q: Select[R], kind: JoinKind, source: Source, on: Condition) -> Select[R]:
    match kind:
        case "INNER":
            return q.join(source, on)
        case "LEFT":
            return q.left_join(source, on)
        case "RIGHT":
            return q.right_join(source, on)
        case "FULL":
            return q.full_join(source, on)


@dataclass(frozen=True)
class JoinSpec:
    kinds: list[JoinKind]
    ons: list[Cond]
    where: Cond | None


@st.composite
def join_specs(draw: st.DrawFn) -> JoinSpec:
    kinds = draw(st.lists(st.sampled_from(KINDS), min_size=1, max_size=2))
    bases = [col_eq("c", "parent_id", "p", "id"), col_eq("t", "child_id", "c", "id")]
    ons: list[Cond] = []
    for i in range(len(kinds)):
        aliases = ("p", "c", "t")[: i + 2]
        extra = draw(st.none() | conditions(*aliases))
        # Usually the foreign key plus a random extra, sometimes only random. PostgreSQL
        # runs FULL JOIN only with an equality it can hash or merge on.
        only_extra = extra is not None and kinds[i] != "FULL" and not draw(st.integers(0, 4))
        ons.append(extra if only_extra and extra else both(bases[i], extra))
    where = draw(st.none() | conditions(*("p", "c", "t")[: len(kinds) + 1]))
    return JoinSpec(kinds, ons, where)


def row_key(row: tuple[Rec | None, ...]) -> tuple[tuple[bool, int | str], ...]:
    return tuple(null_last(None if r is None else r["id"]) for r in row)


@SETTINGS
@given(data=data_strategy, spec=join_specs())
def test_joins(runner: Harness, data: Data, spec: JoinSpec) -> None:
    loop, db, _ = runner
    aliases = ["p", "c", "t"][: len(spec.kinds) + 1]

    async def scenario() -> None:
        stored = await load(db, data)
        want: list[tuple[Rec | None, ...]] = [(p,) for p in stored["p"]]
        for i, (kind, on) in enumerate(zip(spec.kinds, spec.ons, strict=True)):
            want = model_join(
                want, aliases[: i + 1], stored[aliases[i + 1]], aliases[i + 1], kind, on
            )
        want = [r for r in want if holds(spec.where, dict(zip(aliases, r, strict=True)))]
        want.sort(key=row_key)

        got: list[tuple[Rec | None, ...]]
        if len(spec.kinds) == 1:
            q2 = joined(
                db.select(Parent, Child).from_(Parent), spec.kinds[0], Child, spec.ons[0].expr
            )
            if spec.where is not None:
                q2 = q2.where(spec.where.expr)
            got = [(rec(p), rec(c)) for p, c in await q2.order_by(Parent.id, Child.id)]
        else:
            q3 = db.select(Parent, Child, Toy).from_(Parent)
            q3 = joined(q3, spec.kinds[0], Child, spec.ons[0].expr)
            q3 = joined(q3, spec.kinds[1], Toy, spec.ons[1].expr)
            if spec.where is not None:
                q3 = q3.where(spec.where.expr)
            rows = await q3.order_by(Parent.id, Child.id, Toy.id)
            got = [(rec(p), rec(c), rec(t)) for p, c, t in rows]
        assert got == want, spec

    loop.run(scenario())


# -- subqueries ----------------------------------------------------------------------


@dataclass(frozen=True)
class SubSpec:
    kind: str
    uncorrelated: Cond | None  # over c: for IN subqueries
    correlated: Cond | None  # over p and c: for EXISTS
    outer: Cond | None  # over p: for EXISTS over the outer table itself


subqueries = st.builds(
    SubSpec,
    st.sampled_from(["in", "not_in", "in_n", "not_in_n", "exists", "not_exists", "self"]),
    st.none() | conditions("c"),
    st.none() | conditions("p", "c"),
    st.none() | conditions("p"),
)


@SETTINGS
@given(data=data_strategy, spec=subqueries, correlate=st.booleans())
def test_subqueries(runner: Harness, data: Data, spec: SubSpec, correlate: bool) -> None:
    loop, db, _ = runner
    linked = col_eq("c", "parent_id", "p", "id")
    exists_cond = both(linked, spec.correlated) if correlate else spec.correlated
    in_cond = spec.uncorrelated

    async def scenario() -> None:
        stored = await load(db, data)

        def truth(p: Rec) -> Truth:
            env: Env = {"p": p}
            match spec.kind:
                case "in" | "not_in" | "in_n" | "not_in_n":
                    left, right = ("id", "parent_id") if "_n" not in spec.kind else ("n", "n")
                    hits = [c for c in stored["c"] if holds(in_cond, {"c": c})]
                    options = [ival({"c": c}, "c", right) for c in hits]
                    return sql_in(ival(env, "p", left), options, spec.kind.startswith("not"))
                case "exists" | "not_exists":
                    found = any(holds(exists_cond, {"p": p, "c": c}) for c in stored["c"])
                    return found if spec.kind == "exists" else not found
                case _:  # EXISTS over the outer table itself, uncorrelated
                    return any(holds(spec.outer, {"p": q}) for q in stored["p"])

        match spec.kind:
            case "in" | "not_in":
                sub = db.select(Child.parent_id).from_(Child)
                sub = sub if in_cond is None else sub.where(in_cond.expr)
                expr = (in_ if spec.kind == "in" else not_in)(nullable(Parent.id), sub)
            case "in_n" | "not_in_n":
                sub_n = db.select(Child.n).from_(Child)
                sub_n = sub_n if in_cond is None else sub_n.where(in_cond.expr)
                expr = (in_ if spec.kind == "in_n" else not_in)(Parent.n, sub_n)
            case "exists" | "not_exists":
                children = db.select(Child.id).from_(Child)
                children = children if exists_cond is None else children.where(exists_cond.expr)
                expr = (exists if spec.kind == "exists" else not_exists)(children)
            case _:
                own = db.select(Parent.id).from_(Parent)
                expr = exists(own if spec.outer is None else own.where(spec.outer.expr))

        truths = [(p["id"], truth(p)) for p in stored["p"]]
        want = [i for i, t in truths if t is True]
        got = await db.select(Parent.id).from_(Parent).where(expr).order_by(Parent.id)
        assert [i for (i,) in got] == want, spec
        negated = await db.select(Parent.id).from_(Parent).where(not_(expr)).order_by(Parent.id)
        assert [i for (i,) in negated] == [i for i, t in truths if t is False], spec
        rel = await db.query(Parent).find_many(where=expr, order_by=Parent.id)
        assert [p.id for p in rel] == want, spec

    loop.run(scenario())


# -- CTEs ----------------------------------------------------------------------------


@SETTINGS
@given(
    data=data_strategy,
    inner=st.none() | conditions("c"),
    outer=st.none() | conditions("x"),
    on_extra=st.none() | conditions("p", "x"),
    kind=st.sampled_from(KINDS),
)
def test_ctes(
    runner: Harness,
    data: Data,
    inner: Cond | None,
    outer: Cond | None,
    on_extra: Cond | None,
    kind: JoinKind,
) -> None:
    loop, db, _ = runner
    cid, n, s = X.c("cid", int), X.c("n", int), X.c("s", str)
    on = both(col_eq("x", "pid", "p", "id"), on_extra)

    async def scenario() -> None:
        stored = await load(db, data)
        xs: list[Rec] = [
            {"cid": c["id"], "pid": c["parent_id"], "n": c["n"], "s": c["s"]}
            for c in stored["c"]
            if holds(inner, {"c": c})
        ]
        query = x_query(db)
        cte = (query if inner is None else query.where(inner.expr)).cte("x")

        q = db.with_(cte).select(cid, n, s).from_(cte).order_by(cid)
        got = await (q if outer is None else q.where(outer.expr))
        assert got == [(r["cid"], r["n"], r["s"]) for r in xs if holds(outer, {"x": r})]

        want = model_join([(p,) for p in stored["p"]], ["p"], xs, "x", kind, on)
        want = [r for r in want if holds(outer, {"p": r[0], "x": r[1]})]
        want.sort(
            key=lambda r: (
                null_last(get({"p": r[0]}, "p", "id")),
                null_last(get({"x": r[1]}, "x", "cid")),
            )
        )
        jq = db.with_(cte).select(Parent, nullable(cid), nullable(n)).from_(Parent)
        jq = joined(jq, kind, cte, on.expr)
        jq = jq if outer is None else jq.where(outer.expr)
        rows = await jq.order_by(Parent.id, cid)
        assert [(rec(p), c, v) for p, c, v in rows] == [
            (p, get({"x": x}, "x", "cid"), get({"x": x}, "x", "n")) for p, x in want
        ]

    loop.run(scenario())


# -- grouping, aggregates, DISTINCT --------------------------------------------------


@st.composite
def having_leaves(draw: st.DrawFn) -> Cond:
    aggs: dict[str, Expr[int | None]] = {
        "c": nullable(count()),
        "cn": nullable(count(Child.n)),
        "mn": min_(Child.n),
        "mx": max_(Child.n),
    }
    name = draw(st.sampled_from([*sorted(aggs), "sm"]))
    op, k = draw(st.sampled_from(sorted(OPS))), draw(st.integers(-3, 8))
    expr = OPS[op](sum_(Child.n), k) if name == "sm" else OPS[op](aggs[name], k)
    return Cond(expr, lambda e: cmp(op, ival(e, "g", name), k), f"{name} {op} {k}")


def group_stats(rows: list[Rec]) -> Rec:
    ns = [v for v in (ival({"c": r}, "c", "n") for r in rows) if v is not None]
    return {
        "c": len(rows),
        "cn": len(ns),
        "cd": len(set(ns)),
        "sm": sum(ns) if ns else None,
        "mn": min(ns, default=None),
        "mx": max(ns, default=None),
    }


@SETTINGS
@given(
    data=data_strategy,
    where=st.none() | conditions("c"),
    having=st.none() | st.recursive(having_leaves(), combine, max_leaves=4),
    on_extra=st.none() | conditions("p", "c"),
)
def test_group_by_having(
    runner: Harness, data: Data, where: Cond | None, having: Cond | None, on_extra: Cond | None
) -> None:
    loop, db, _ = runner

    async def scenario() -> None:
        stored = await load(db, data)
        hits = [c for c in stored["c"] if holds(where, {"c": c})]
        groups: dict[Val, list[Rec]] = {}
        for c in hits:
            groups.setdefault(c["parent_id"], []).append(c)
        want = {
            g: stats
            for g, rows in groups.items()
            if holds(having, {"g": (stats := group_stats(rows))})
        }
        q = (
            db.select(
                {
                    "g": Child.parent_id,
                    "c": count(),
                    "cn": count(Child.n),
                    "cd": count(Child.n, distinct=True),
                    "sm": sum_(Child.n),
                    "mn": min_(Child.n),
                    "mx": max_(Child.n),
                    "av": avg(Child.n),
                }
            )
            .from_(Child)
            .group_by(Child.parent_id)
        )
        q = q if where is None else q.where(where.expr)
        q = q if having is None else q.having(having.expr)
        got = {as_val(row["g"]): row for row in await q}
        assert got.keys() == want.keys(), (where, having)
        for g, row in got.items():
            assert {k: v for k, v in row.items() if k not in ("g", "av")} == want[g]
            ns = [v for v in (ival({"c": c}, "c", "n") for c in groups[g]) if v is not None]
            average = row["av"]
            if ns:
                assert isinstance(average, Decimal)
                assert abs(Fraction(average) - Fraction(sum(ns), len(ns))) < Fraction(1, 10**15)
            else:
                assert average is None

        # Each parent with how many children an outer join finds (0 for none).
        on = both(col_eq("c", "parent_id", "p", "id"), on_extra)
        counts = (
            await db.select(Parent.id, count(Child.id))
            .from_(Parent)
            .left_join(Child, on.expr)
            .group_by(Parent.id)
            .order_by(Parent.id)
        )
        assert counts == [
            (p["id"], sum(1 for c in stored["c"] if on.eval({"p": p, "c": c}) is True))
            for p in stored["p"]
        ]

        pairs = db.select(Child.n, Child.s).from_(Child).distinct()
        got_pairs = await (pairs if where is None else pairs.where(where.expr))
        assert len(got_pairs) == len(set(got_pairs))
        assert set(got_pairs) == {(c["n"], c["s"]) for c in hits}

    loop.run(scenario())


def test_subquery_in_relational_where_uses_its_own_table(runner: Harness) -> None:
    # Regression: columns of a subquery over the relational query's own table were bound
    # to the outer alias, turning an uncorrelated EXISTS into a per-row filter.
    loop, db, _ = runner

    async def scenario() -> None:
        await load(db, {"p": [{"n": 0, "s": None}, {"n": 5, "s": None}], "c": [], "t": []})
        any_big = exists(db.select(Parent.id).from_(Parent).where(gt(Parent.n, 3)))
        rel = await db.query(Parent).find_many(where=any_big, order_by=Parent.id)
        assert [p.id for p in rel] == [1, 2]

    loop.run(scenario())


# -- relational queries --------------------------------------------------------------


@dataclass(frozen=True)
class Load:
    alias: str
    where: Cond | None
    order: Order | None  # None (and no limit): rows come in any order
    limit: int | None
    columns: tuple[str, ...] | None
    with_: tuple[tuple[str, "Load"], ...]

    def __repr__(self) -> str:
        parts = [f"where={self.where}", f"order={self.order}", f"limit={self.limit}"]
        parts += [f"columns={self.columns}", f"with_={dict(self.with_)}"]
        return f"{self.alias}({', '.join(parts)})"


@st.composite
def loads(draw: st.DrawFn, alias: str, depth: int, many: bool = True) -> Load:
    where = draw(st.none() | conditions(alias))
    order: Order | None = None
    limit: int | None = None
    if many:
        names = sorted(COLUMNS[alias])
        order = draw(
            st.none() | st.tuples(st.sampled_from(names), st.booleans(), st.sampled_from(NULLS))
        )
        limit = draw(st.none() | st.integers(0, 3))
        if limit is not None and order is None:
            order = ("id", False, None)  # LIMIT needs a deterministic order
    columns = draw(
        st.none() | st.lists(st.sampled_from(sorted(COLUMNS[alias])), min_size=1, unique=True)
    )
    with_: list[tuple[str, Load]] = []
    if depth > 0:
        for name, (target, is_many, _, _) in sorted(RELATIONS[alias].items()):
            if draw(st.booleans()):
                with_.append((name, draw(loads(target, depth - 1, is_many))))
    return Load(
        alias, where, order, limit, None if columns is None else tuple(columns), tuple(with_)
    )


def with_options(load: Load) -> WithOptions:
    options: WithOptions = {}
    if load.where is not None:
        options["where"] = load.where.expr
    if (items := order_items(load.alias, load.order)) is not None:
        options["order_by"] = items
    if load.limit is not None:
        options["limit"] = load.limit
    if load.columns is not None:
        options["columns"] = [COLUMNS[load.alias][c] for c in load.columns]
    if load.with_:
        options["with_"] = {name: with_options(sub) for name, sub in load.with_}
    return options


def unordered(trees: list[Tree]) -> list[Tree]:
    return sorted(trees, key=repr)


def expected_tree(load: Load, row: Rec, stored: Data) -> Tree:
    wanted = load.columns or tuple(COLUMNS[load.alias])
    tree: Tree = {k: v for k, v in row.items() if k in wanted}
    for name, sub in load.with_:
        target, is_many, source_col, target_col = RELATIONS[load.alias][name]
        key = row[source_col]
        related = [
            r
            for r in stored[target]
            if key is not None and r[target_col] == key and holds(sub.where, {target: r})
        ]
        if not is_many:
            tree[name] = expected_tree(sub, related[0], stored) if related else None
            continue
        related = ordered(related, sub.order)[: sub.limit]
        children = [expected_tree(sub, r, stored) for r in related]
        tree[name] = children if sub.order else unordered(children)
    return tree


def got_tree(load: Load, obj: Table) -> Tree:
    wanted = load.columns or tuple(COLUMNS[load.alias])
    tree: Tree = {}
    for name, read in columns_of(obj).items():
        if name in wanted:
            tree[name] = read()
        else:
            with pytest.raises(AttributeError, match="was not selected"):
                read()
    relations = relations_of(obj)
    for name, sub in load.with_:
        value = relations[name]()
        if value is None or isinstance(value, Table):
            tree[name] = None if value is None else got_tree(sub, value)
        else:
            children = [got_tree(sub, v) for v in value]
            tree[name] = children if sub.order else unordered(children)
    return tree


@st.composite
def roots(draw: st.DrawFn) -> tuple[Load, int | None]:
    plan = draw(loads(draw(st.sampled_from(["p", "c", "t"])), draw(st.integers(1, 3))))
    offset = draw(st.none() | st.integers(0, 3))
    if offset is not None and plan.order is None:
        plan = Load(
            plan.alias, plan.where, ("id", False, None), plan.limit, plan.columns, plan.with_
        )
    return plan, offset


@SETTINGS
@given(data=data_strategy, root=roots())
def test_relational(runner: Harness, data: Data, root: tuple[Load, int | None]) -> None:
    loop, db, _ = runner
    plan, offset = root
    table = {"p": Parent, "c": Child, "t": Toy}[plan.alias]
    options = with_options(plan)

    async def scenario() -> None:
        stored = await load(db, data)
        rows = [r for r in stored[plan.alias] if holds(plan.where, {plan.alias: r})]
        rows = ordered(rows, plan.order)
        start = offset or 0
        window = rows[start : None if plan.limit is None else start + plan.limit]
        want = [expected_tree(plan, r, stored) for r in window]

        found = await db.query(table).find_many(
            columns=options.get("columns"),
            where=options.get("where"),
            with_=options.get("with_"),
            order_by=options.get("order_by"),
            limit=plan.limit,
            offset=offset,
        )
        got = [got_tree(plan, obj) for obj in found]
        assert (got if plan.order else unordered(got)) == (
            want if plan.order else unordered(want)
        ), plan

        if plan.order is not None:
            first = await db.query(table).find_first(
                columns=options.get("columns"),
                where=options.get("where"),
                with_=options.get("with_"),
                order_by=options.get("order_by"),
            )
            want_first = expected_tree(plan, rows[0], stored) if rows else None
            assert (None if first is None else got_tree(plan, first)) == want_first, plan

    loop.run(scenario())


# -- snake_case columns, a non-public schema, prepared queries ------------------------


@SETTINGS
@given(
    owners=st.lists(
        st.fixed_dictionaries({"displayName": st.none() | strs, "score": maybe_int}), max_size=5
    ),
    pets=st.lists(
        st.fixed_dictionaries(
            {"ownerId": st.none() | st.integers(1, 6), "petName": st.none() | strs}
        ),
        max_size=7,
    ),
    values=st.lists(st.tuples(ints, st.integers(0, 6), strs), min_size=1, max_size=4),
)
def test_snake_case_schema_prepared(
    runner: Harness,
    owners: list[dict[str, Val]],
    pets: list[dict[str, Val]],
    values: list[tuple[int, int, str]],
) -> None:
    loop, _, db = runner

    async def scenario() -> None:
        async with db.driver.acquire() as conn:
            await conn.execute("TRUNCATE fj_s.owner, fj_s.pet RESTART IDENTITY", [])
        stored_owners = await db.insert(Owner).values(owners).returning() if owners else []
        stored_pets = await db.insert(Pet).values(pets).returning() if pets else []
        os_ = [(o.id, o.displayName, o.score) for o in stored_owners]
        ps = [(p.id, p.ownerId, p.petName) for p in stored_pets]

        joined_q = (
            db.select(Owner, Pet)
            .from_(Owner)
            .left_join(Pet, eq(Pet.ownerId, nullable(Owner.id)))
            .where(or_(is_null(Owner.score), gte(Owner.score, placeholder("lo"))))
            .order_by(Owner.id, Pet.id)
            .limit(placeholder("k"))
            .prepare()
        )
        relational = (
            db.query(Owner)
            .find_many(
                where=gte(Owner.score, placeholder("lo")),
                with_={
                    "pets": {
                        "where": ne(Pet.petName, placeholder("name")),
                        "order_by": desc(Pet.id),
                        "with_": {"owner": {"columns": [Owner.displayName]}},
                    }
                },
                order_by=Owner.id,
            )
            .prepare()
        )
        named = db.select(Pet.ownerId).from_(Pet).where(eq(Pet.petName, placeholder("name")))
        without = (
            db.select(Owner.id).from_(Owner).where(not_in(Owner.id, named)).order_by(Owner.id)
        ).prepare()
        cte = (
            db.select({"oid": Pet.ownerId, "nm": Pet.petName})
            .from_(Pet)
            .where(lte(Pet.ownerId, placeholder("k")))
            .cte("pp")
        )
        via_cte = (
            db.with_(cte)
            .select(Owner.displayName, cte.c("nm", str))
            .from_(Owner)
            .join(cte, eq(nullable(cte.c("oid", int)), nullable(Owner.id)))
            .order_by(Owner.id, cte.c("nm", str))
        ).prepare()

        for lo, k, name in values:
            want_rows = [
                (o, p)
                for o in os_
                if o[2] is None or o[2] >= lo
                for p in ([p for p in ps if p[1] == o[0]] or [None])
            ][:k]
            got = await joined_q.execute(lo=lo, k=k)
            assert [
                (
                    (o.id, o.displayName, o.score),
                    None if p is None else (p.id, p.ownerId, p.petName),
                )
                for o, p in got
            ] == want_rows

            got_rel = await relational.execute(lo=lo, name=name)
            assert [
                (
                    o.id,
                    o.displayName,
                    [(p.id, p.petName, p.owner and p.owner.displayName) for p in o.pets],
                )
                for o in got_rel
            ] == [
                (
                    o[0],
                    o[1],
                    [
                        (p[0], p[2], o[1])
                        for p in reversed(ps)
                        if p[1] == o[0] and p[2] is not None and p[2] != name
                    ],
                )
                for o in os_
                if o[2] is not None and o[2] >= lo
            ]

            owner_ids = [p[1] for p in ps if p[2] == name]
            want_ids = [o[0] for o in os_ if sql_in(o[0], owner_ids, negate=True) is True]
            assert [i for (i,) in await without.execute(name=name)] == want_ids

            pairs = sorted(
                ((o[0], p[2]) for o in os_ for p in ps if p[1] == o[0] and o[0] <= k),
                key=lambda r: (r[0], null_last(r[1])),
            )
            names = {o[0]: o[1] for o in os_}
            assert await via_cte.execute(k=k) == [(names[i], nm) for i, nm in pairs]

    loop.run(scenario())
