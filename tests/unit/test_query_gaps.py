"""SQL of: SIMILAR TO, date/time arithmetic, text +, named windows, ts_rank weights,
subqueries in FROM, relational queries on an alias, ORDER BY expressions after UNION,
raw SQL reused next to different types, and OVERRIDING ... VALUE."""

import datetime
from decimal import Decimal

import pytest

from mizzle import (
    abs_,
    alias,
    and_,
    asc,
    between,
    cast,
    coalesce,
    count,
    desc,
    eq,
    exists,
    func,
    in_,
    lag,
    lower,
    not_similar_to,
    placeholder,
    row_number,
    rows_between,
    similar_to,
    sql,
    sum_,
    to_tsquery,
    to_tsvector,
    ts_rank,
    ts_rank_cd,
)
from mizzle.core.errors import QueryError
from mizzle.core.query.base import row_decoder
from mizzle.core.query.builder import QueryBuilder
from mizzle.core.schema.table import Table
from mizzle.core.sql.nodes import ExprBase, Node
from mizzle.db import dialect_for_url
from mizzle.dialects.postgres import PostgresDialect
from mizzle.dialects.postgres.compiler import PostgresCompiler
from mizzle.pg import custom, date, integer, serial, text
from tests.models import Posts, Role, Users
from tests.unit.conftest import FakeRunner


class Days(Table, name="days"):
    id = serial().primary_key()
    day = date().not_null()
    other = date()
    n = integer()
    label = text()


def selected(qb: QueryBuilder, expr: ExprBase) -> tuple[str, tuple[object, ...]]:
    sql_text, params = qb.select(expr).from_(Users).to_sql()
    return sql_text.removeprefix("SELECT ").removesuffix(' FROM "users"'), params


def test_similar_to(qb: QueryBuilder) -> None:
    q = qb.select(Users.id).from_(Users)
    assert (
        q.where(similar_to(Users.email, "(a|b)%"))
        .to_sql()[0]
        .endswith('WHERE "users"."email" SIMILAR TO $1')
    )
    assert (
        q.where(not_similar_to(Users.bio, Users.email))
        .to_sql()[0]
        .endswith('WHERE "users"."bio" NOT SIMILAR TO "users"."email"')
    )


def test_date_time_arithmetic(qb: QueryBuilder) -> None:
    week = datetime.timedelta(days=7)
    assert selected(qb, Users.created_at + week) == (
        '("users"."created_at" + CAST($1 AS interval))',
        (week,),
    )
    assert selected(qb, Users.created_at - Users.created_at)[0] == (
        '("users"."created_at" - "users"."created_at")'
    )
    stamp = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
    assert selected(qb, Users.created_at - stamp) == ('("users"."created_at" - $1)', (stamp,))
    sql_text, _ = qb.select(Days.day - Days.other, Days.day + week).from_(Days).to_sql()
    assert sql_text.startswith(
        'SELECT ("days"."day" - "days"."other"), ("days"."day" + CAST($1 AS interval))'
    )


def test_text_concatenation(qb: QueryBuilder) -> None:
    assert selected(qb, Users.email + " " + Users.bio) == (
        '(("users"."email" || CAST($1 AS text)) || "users"."bio")',
        (" ",),
    )
    assert selected(qb, "<" + lower(Users.email)) == (
        '(CAST($1 AS text) || lower("users"."email"))',
        ("<",),
    )
    assert selected(qb, coalesce(Users.bio, "") + "!")[0] == (
        '(coalesce("users"."bio", $1) || CAST($2 AS text))'
    )
    assert selected(qb, Users.id + 1)[0] == '("users"."id" + $1)'  # numbers still add


def test_named_windows(qb: QueryBuilder) -> None:
    q = (
        qb.select(
            Users.id,
            row_number().over("w"),
            sum_(Users.id).over("w", frame=rows_between(-1, 0)),
            lag(Users.id).over(order_by=Users.id),
        )
        .from_(Users)
        .window("w", partition_by=Users.role, order_by=desc(Users.id))
        .window("all")
        .order_by(Users.id)
    )
    assert q.to_sql()[0] == (
        'SELECT "users"."id", row_number() OVER "w", sum("users"."id") OVER ("w" ROWS BETWEEN '
        '1 PRECEDING AND CURRENT ROW), lag("users"."id", 1) OVER (ORDER BY "users"."id") '
        'FROM "users" WINDOW "w" AS (PARTITION BY "users"."role" ORDER BY "users"."id" DESC), '
        '"all" AS () ORDER BY "users"."id"'
    )


def test_ts_rank_weights_and_normalization(qb: QueryBuilder) -> None:
    vector, query = to_tsvector(Users.email), to_tsquery("a")
    assert selected(qb, ts_rank(vector, query, weights=[0, 0.5, 1, 2], normalization=32)) == (
        'ts_rank(CAST($1 AS real[]), to_tsvector("users"."email"), '
        "to_tsquery(CAST($2 AS text)), 32)",
        ([0.0, 0.5, 1.0, 2.0], "a"),
    )
    assert selected(qb, ts_rank_cd(vector, query, normalization=4))[0] == (
        'ts_rank_cd(to_tsvector("users"."email"), to_tsquery(CAST($1 AS text)), 4)'
    )
    with pytest.raises(QueryError, match="4 numbers"):
        ts_rank(vector, query, weights=[1, 2])


def test_subquery_in_from_and_join(qb: QueryBuilder) -> None:
    counts = (
        qb.select({"author": Posts.author_id, "n": count()})
        .from_(Posts)
        .where(eq(Posts.title, "x"))
        .group_by(Posts.author_id)
        .subquery("counts")
    )
    author, n = counts.c("author", int), counts.c("n", int)
    q = qb.select(Users.email, n).from_(Users).join(counts, eq(author, Users.id)).where(eq(n, 2))
    assert q.to_sql() == (
        'SELECT "users"."email", "counts"."n" FROM "users" INNER JOIN (SELECT "posts"."author_id" '
        'AS "author", count(*) AS "n" FROM "posts" WHERE "posts"."title" = $1 GROUP BY '
        '"posts"."author_id") AS "counts" ON "counts"."author" = "users"."id" '
        'WHERE "counts"."n" = $2',
        ("x", 2),
    )
    roles = qb.select(Users.id, Users.role).from_(Users).subquery("r")
    r2 = alias(roles, "r2")
    q2 = qb.select(r2.c("role", Role)).from_(r2)
    assert q2.to_sql()[0] == (
        'SELECT "r2"."role" FROM (SELECT "users"."id", "users"."role" FROM "users") AS "r2"'
    )


async def test_subquery_and_cte_columns_decode_like_their_source(
    qb: QueryBuilder, runner: FakeRunner
) -> None:
    roles = qb.select({"r": Users.role, "e": lower(Users.email)}).from_(Users)
    for source in (roles.subquery("s"), roles.cte("s")):
        runner.rows = [("admin", "x")]
        query = qb.with_(source) if not source.inline else qb
        got = await query.select(source.c("r", Role), source.c("e", str)).from_(source)
        assert got == [(Role.ADMIN, "x")]
        assert type(got[0][0]) is Role


def test_relational_query_on_an_alias(qb: QueryBuilder) -> None:
    u = alias(Users, "u")
    q = qb.query(u).find_many(
        columns=[u.id, Users.email],
        where=and_(eq(u.id, 1), eq(Users.role, Role.ADMIN)),
        order_by=desc(u.id),
        with_={"posts": {"columns": [Posts.id]}},
    )
    assert q.to_sql() == (
        'SELECT "t1"."id", "t1"."email", (SELECT coalesce(json_agg(json_build_array("t2"."id")), '
        '\'[]\'::json) FROM "posts" AS "t2" WHERE "t2"."author_id" = "t1"."id") FROM "users" '
        'AS "t1" WHERE "t1"."id" = $1 AND "t1"."role" = $2 ORDER BY "t1"."id" DESC',
        (1, "admin"),
    )


def test_order_by_expression_after_union(qb: QueryBuilder) -> None:
    left = qb.select(Users.id).from_(Users)
    right = qb.select(Posts.id).from_(Posts)
    # Plain columns: ORDER BY the output column directly.
    assert left.union(right).order_by(desc(Users.id)).to_sql()[0] == (
        '(SELECT "users"."id" FROM "users") UNION (SELECT "posts"."id" FROM "posts") '
        'ORDER BY "id" DESC'
    )
    q = left.union_all(right).order_by(asc(abs_(Users.id - 3), nulls="first"), Users.id).limit(5)
    assert q.to_sql() == (
        'SELECT * FROM ((SELECT "users"."id" FROM "users") UNION ALL (SELECT "posts"."id" FROM '
        '"posts")) AS "_combined" ORDER BY abs(("id" - $1)) ASC NULLS FIRST, "id" LIMIT $2',
        (3, 5),
    )
    cte = qb.select({"k": Users.id}).from_(Users).cte("c")
    k = cte.c("k", int)
    u = alias(Users, "u")
    q2 = (
        qb.with_(cte)
        .select(k)
        .from_(cte)
        .except_(qb.select(u.id).from_(u))
        .order_by(desc(k + 1), u.id)
    )
    assert q2.to_sql()[0].endswith('AS "_combined" ORDER BY ("k" + $1) DESC, "id"')
    # A subquery in that ORDER BY still names its own tables: an outer column must not
    # silently bind to the subquery's table.
    inner = qb.select(Posts.id).from_(Posts).where(eq(Posts.author_id, Users.id))
    q3 = left.union(right).order_by(desc(exists(inner)))
    assert q3.to_sql()[0].endswith(
        'ORDER BY EXISTS (SELECT "posts"."id" FROM "posts" '
        'WHERE "posts"."author_id" = "users"."id") DESC'
    )


def test_raw_sql_reused_next_to_different_types(qb: QueryBuilder) -> None:
    value = sql("{}", "x")
    q = qb.select(Users.id).from_(Users)
    # varchar and text: a parameter for each type; the same type shares one.
    assert q.where(
        and_(eq(Users.email, value), eq(Users.bio, value), eq(Users.bio, value))
    ).to_sql() == (
        'SELECT "users"."id" FROM "users" WHERE "users"."email" = $1 AND "users"."bio" = $2 '
        'AND "users"."bio" = $2',
        ("x", "x"),
    )
    # Selected and grouped by: one parameter, so PostgreSQL sees the same expression.
    expr = sql("lower({})", "A")
    sql_text, params = qb.select(expr, count()).from_(Users).group_by(expr).to_sql()
    assert sql_text.endswith("GROUP BY lower($1)") and params == ("A",)
    # Between an integer and a bigint context.
    n = sql("{}", 1)
    sql_text, params = (
        qb.select(Days.id).from_(Days).where(and_(eq(Days.n, n), eq(Days.id, n))).to_sql()
    )
    assert params == (1, 1)


def test_raw_sql_reused_in_casts(qb: QueryBuilder) -> None:
    nothing = sql("{}", None)
    q = qb.select(cast(nothing, integer()), cast(nothing, text()), cast(nothing, integer()))
    assert q.from_(Users).to_sql() == (
        'SELECT CAST($1 AS integer), CAST($2 AS text), CAST($1 AS integer) FROM "users"',
        (None, None),
    )


def test_user_named_functions_are_not_session_safe(qb: QueryBuilder) -> None:
    # func() may change the session (reset after it) but stays a plain read (retried after a
    # lost connection); built-in functions are safe; raw SQL is neither.
    def flags(node: Node) -> tuple[bool, bool]:
        compiled = PostgresCompiler().compile(node)
        return compiled.session_safe, compiled.raw

    assert flags(qb.select(lower(Users.email)).from_(Users)) == (True, False)
    assert flags(qb.select(func[bool]("pg_is_in_recovery")).from_(Users)) == (False, False)
    assert flags(qb.select(sql("1")).from_(Users)) == (False, True)


def test_generated_row_decoder() -> None:
    # Columns from an offset, a converter (the enum) and NULLs; built once and reused.
    columns = [(n, Users.__table__.columns[n]) for n in ("id", "bio", "role")]
    decode = row_decoder(Users, columns, 2)
    assert row_decoder(Users, columns, 2) is decode
    user = decode(("x", "y", 7, None, "admin"))
    assert (user.id, user.bio, user.role) == (7, None, Role.ADMIN)
    assert not hasattr(user, "email")  # not selected: absent, not None


def test_a_select_with_a_data_modifying_cte_is_a_write(qb: QueryBuilder) -> None:
    # Q1: a data-modifying CTE writes, so the SELECT runs on the primary, is never retried
    # and explain(analyze=True) refuses it.
    moved = qb.delete(Posts).where(eq(Posts.author_id, 1)).returning().cte("moved")
    compiled = PostgresCompiler().compile(qb.with_(moved).select(Users.id).from_(Users))
    assert compiled.writes is True
    assert compiled.locking is False
    assert PostgresCompiler().compile(qb.select(Users.id).from_(Users)).writes is False


def test_ctes_reach_insert_update_delete_and_merge(qb: QueryBuilder) -> None:
    # Q2/Q6: with_() used to be silently dropped on every write but SELECT.
    keep = qb.select(Users.id).from_(Users).cte("keep")
    with_ = qb.with_(keep)
    head = 'WITH "keep" AS (SELECT "users"."id" FROM "users") '

    insert_sql = with_.insert(Posts).values({"author_id": 1, "title": "x"}).to_sql()[0]
    assert insert_sql.startswith(head + 'INSERT INTO "posts"')

    update_sql = with_.update(Posts).set({"title": "y"}).where(eq(Posts.id, 1)).to_sql()[0]
    assert update_sql.startswith(head + 'UPDATE "posts"')

    delete_sql = with_.delete(Posts).where(eq(Posts.id, 1)).to_sql()[0]
    assert delete_sql.startswith(head + 'DELETE FROM "posts"')

    source = qb.select(Users.id, Users.email).from_(Users).subquery("src")
    sid, email = source.c("id", int), source.c("email", str)
    merge_sql = (
        with_.merge(Posts)
        .using(source)
        .on(eq(Posts.author_id, sid))
        .when_matched_then_update({"title": email})
        .to_sql()[0]
    )
    assert merge_sql.startswith(head + 'MERGE INTO "posts"')


def _dollars(cents: int) -> Decimal:
    return Decimal(cents) / 100


def _cents(dollars: Decimal) -> int:
    return int(dollars * 100)


class Priced(Table, name="priced"):
    id = serial().primary_key()
    price = custom("money_amount", py_type=int, decode=_cents, encode=_dollars)
    pair = custom("price_pair")
    flag = custom("flag_domain")  # a domain over boolean


def test_values_next_to_cte_and_subquery_columns_are_encoded(qb: QueryBuilder) -> None:
    # Q8-1: a value compared with a CTE or subquery column was sent raw (150, not 1.50).
    s = qb.select(Priced.id, Priced.price).from_(Priced).subquery("s")
    q = qb.select(s.c("id", int)).from_(s).where(eq(s.c("price", int), 150))
    assert q.to_sql()[1] == (Decimal("1.5"),)
    c = qb.select(Priced.id, Priced.price).from_(Priced).cte("c")
    q = qb.with_(c).select(c.c("id", int)).from_(c)
    assert q.where(in_(c.c("price", int), [1, 2])).to_sql()[1] == (Decimal("0.01"), Decimal("0.02"))
    assert q.where(between(c.c("price", int), 1, 2)).to_sql()[1] == (
        Decimal("0.01"),
        Decimal("0.02"),
    )


def test_values_compared_with_custom_columns_are_not_cast(qb: QueryBuilder) -> None:
    # R9-1: a cast to the custom type cut or rounded the value (varchar(5), numeric(10,2)).
    sql_text, params = qb.select(Priced.id).from_(Priced).where(eq(Priced.pair, (1, 2))).to_sql()
    assert sql_text.endswith('WHERE "priced"."pair" = $1') and params == ((1, 2),)
    where = eq(Priced.price, placeholder("p"))
    assert qb.select(Priced.id).from_(Priced).where(where).to_sql()[0].endswith("= $1")
    assert qb.insert(Priced).values(pair=(1, 2)).to_sql()[0].endswith("VALUES ($1)")


def test_untyped_custom_columns_take_booleans(qb: QueryBuilder) -> None:
    # R8-2: a custom() column without py_type takes any value, True too.
    assert qb.insert(Priced).values(flag=True).to_sql()[1] == (True,)
    assert qb.update(Priced).set(flag=False).all_rows().to_sql()[1] == (False,)


def test_url_scheme_is_case_insensitive() -> None:
    # R8-3: RFC 3986 schemes are case-insensitive; asyncpg takes PostgreSQL:// too.
    assert isinstance(dialect_for_url("PostgreSQL://u@h/d"), PostgresDialect)
    assert isinstance(dialect_for_url("POSTGRES://u@h/d"), PostgresDialect)
