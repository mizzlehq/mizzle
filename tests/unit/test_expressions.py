import datetime
from decimal import Decimal

import pytest

from mizzle import (
    abs_,
    alias,
    case_,
    cast,
    coalesce,
    concat,
    count,
    current_date,
    date_trunc,
    desc,
    eq,
    extract,
    first_value,
    func,
    greatest,
    gt,
    in_,
    lag,
    last_value,
    lead,
    least,
    length,
    lower,
    matches,
    max_,
    not_ilike,
    not_like,
    not_regex,
    now,
    nth_value,
    ntile,
    nullif,
    placeholder,
    plainto_tsquery,
    range_between,
    rank,
    regex,
    round_,
    row_number,
    rows_between,
    sum_,
    to_tsquery,
    to_tsvector,
    trim,
    ts_rank,
    upper,
    websearch_to_tsquery,
)
from mizzle.core.errors import QueryError
from mizzle.core.query.builder import QueryBuilder
from mizzle.core.sql.nodes import Expr, ExprBase
from mizzle.pg import integer, numeric, pg_enum, serial, text
from tests.models import Posts, Role, Users
from tests.unit.conftest import FakeRunner


def where(qb: QueryBuilder, condition: Expr[bool]) -> tuple[str, tuple[object, ...]]:
    sql, params = qb.select(Users.id).from_(Users).where(condition).to_sql()
    return sql.split(" WHERE ", 1)[1], params


def selected(qb: QueryBuilder, expr: ExprBase) -> tuple[str, tuple[object, ...]]:
    sql, params = qb.select(expr).from_(Users).to_sql()
    return sql.removeprefix("SELECT ").removesuffix(' FROM "users"'), params


# -- arithmetic -----------------------------------------------------------------------


def test_arithmetic_binds_like_the_column(qb: QueryBuilder) -> None:
    assert selected(qb, Users.id * 2 + 1) == (
        '(("users"."id" * $1) + CAST($2 AS bigint))',
        (2, 1),
    )
    assert selected(qb, 10 - Users.id) == ('($1 - "users"."id")', (10,))
    assert selected(qb, Users.id / Users.id) == ('("users"."id" / "users"."id")', ())
    assert where(qb, gt(Users.id + 1, 5)) == ('("users"."id" + $1) > $2', (1, 5))


def test_arithmetic_without_a_column_casts_values(qb: QueryBuilder) -> None:
    assert selected(qb, count() + 1) == ("(count(*) + CAST($1 AS bigint))", (1,))
    assert selected(qb, Decimal("2.5") * cast(Users.id, numeric().not_null()) - Decimal(1))[0] == (
        '(($1 * CAST("users"."id" AS numeric)) - CAST($2 AS numeric))'
    )


# -- CASE, coalesce and friends -------------------------------------------------------


def test_case(qb: QueryBuilder) -> None:
    expr = case_().when(eq(Users.role, Role.ADMIN), "boss").when(gt(Users.id, 5), "late")
    assert selected(qb, expr) == (
        'CASE WHEN "users"."role" = $1 THEN CAST($2 AS text) '
        'WHEN "users"."id" > $3 THEN CAST($4 AS text) END',
        ("admin", "boss", 5, "late"),
    )
    assert selected(qb, expr.else_("other"))[0].endswith("ELSE CAST($5 AS text) END")


def test_case_values_take_a_columns_type(qb: QueryBuilder) -> None:
    # A column in any branch types the plain values in the others (no cast; enums encoded).
    expr = case_().when(eq(Users.id, 1), Role.ADMIN).else_(Users.role)
    assert selected(qb, expr) == (
        'CASE WHEN "users"."id" = $1 THEN $2 ELSE "users"."role" END',
        (1, "admin"),
    )


def test_case_decodes_with_the_column(runner: FakeRunner, qb: QueryBuilder) -> None:
    expr = case_().when(eq(Users.id, 1), Users.role).else_(Role.USER)
    runner.rows = [("admin",)]
    decode = qb.select(expr).from_(Users).decoder()
    assert decode(runner.rows[0]) == (Role.ADMIN,)


def test_coalesce_nullif_greatest_least(qb: QueryBuilder) -> None:
    assert selected(qb, coalesce(Users.bio, "none")) == ('coalesce("users"."bio", $1)', ("none",))
    assert selected(qb, coalesce(Users.bio, Users.email)) == (
        'coalesce("users"."bio", "users"."email")',
        (),
    )
    assert selected(qb, coalesce(max_(Users.id), 0)) == ('coalesce(max("users"."id"), $1)', (0,))
    assert selected(qb, coalesce(count(), 0)) == ("coalesce(count(*), CAST($1 AS bigint))", (0,))
    assert selected(qb, nullif(Users.email, "")) == ('nullif("users"."email", $1)', ("",))
    assert selected(qb, greatest(Users.id, 3, 4)) == ('greatest("users"."id", $1, $2)', (3, 4))
    assert selected(qb, least(Users.id, 3)) == ('least("users"."id", $1)', (3,))
    with pytest.raises(QueryError):
        coalesce()  # pyright: ignore[reportCallIssue]


def test_min_max_decode_enums(runner: FakeRunner, qb: QueryBuilder) -> None:
    decode = qb.select(max_(Users.role), coalesce(max_(Users.role), Role.USER)).from_(Users)
    assert decode.decoder()(("admin", "user")) == (Role.ADMIN, Role.USER)


def test_cast(qb: QueryBuilder) -> None:
    assert selected(qb, cast(Users.id, text())) == ('CAST("users"."id" AS text)', ())
    assert selected(qb, cast(Users.email, integer()))[0] == 'CAST("users"."email" AS integer)'
    assert selected(qb, cast(Users.id, serial()))[0] == 'CAST("users"."id" AS integer)'
    status = pg_enum(Role, type_name='My "Role"')
    assert selected(qb, cast(Users.email, status))[0] == 'CAST("users"."email" AS "My ""Role""")'
    decode = qb.select(cast(Users.email, pg_enum(Role))).from_(Users).decoder()
    assert decode(("user",)) == (Role.USER,)


# -- functions ------------------------------------------------------------------------


def test_string_functions(qb: QueryBuilder) -> None:
    assert selected(qb, lower(Users.email))[0] == 'lower("users"."email")'
    assert selected(qb, upper(Users.bio))[0] == 'upper("users"."bio")'
    assert selected(qb, trim(Users.bio))[0] == 'btrim("users"."bio")'
    assert selected(qb, length(Users.bio))[0] == 'length("users"."bio")'
    assert selected(qb, concat(Users.email, " #", Users.id)) == (
        'concat("users"."email", CAST($1 AS text), "users"."id")',
        (" #",),
    )


def test_number_and_date_functions(qb: QueryBuilder) -> None:
    assert selected(qb, abs_(Users.id - 3))[0] == 'abs(("users"."id" - $1))'
    assert selected(qb, round_(cast(Users.id, numeric().not_null())))[0] == (
        'round(CAST("users"."id" AS numeric))'
    )
    assert selected(qb, round_(Users.id, 2)) == ('round(CAST("users"."id" AS numeric), 2)', ())
    assert selected(qb, now())[0] == "now()"
    assert selected(qb, current_date())[0] == "CURRENT_DATE"
    assert selected(qb, date_trunc("month", Users.created_at)) == (
        'date_trunc(\'month\', "users"."created_at")',
        (),
    )
    assert selected(qb, extract("year", Users.created_at))[0] == (
        'extract(year FROM "users"."created_at")'
    )


def test_units_and_digits_are_validated() -> None:
    with pytest.raises(QueryError):
        date_trunc("day'; drop table users; --", Users.created_at)  # pyright: ignore[reportCallIssue, reportArgumentType]
    with pytest.raises(QueryError):
        extract("Year", Users.created_at)  # pyright: ignore[reportCallIssue, reportArgumentType]
    with pytest.raises(QueryError):
        round_(Users.id, True)
    with pytest.raises(QueryError):
        ntile("2")  # pyright: ignore[reportArgumentType]


def test_func(qb: QueryBuilder) -> None:
    assert selected(qb, func[float]("similarity", Users.email, "x")) == (
        'similarity("users"."email", $1)',
        ("x",),
    )
    assert selected(qb, func[int]("public.my_fn"))[0] == "public.my_fn()"
    for bad in ("drop table x; --", "f(x)", "", "a.b.c", "1abc"):
        with pytest.raises(QueryError):
            func[int](bad)


def test_group_by_an_expression_with_values(qb: QueryBuilder) -> None:
    # The same node selected and grouped by binds once, so PostgreSQL sees one expression.
    bio = coalesce(Users.bio, "none")
    assert qb.select(bio, count()).from_(Users).group_by(bio).to_sql() == (
        'SELECT coalesce("users"."bio", $1), count(*) FROM "users" '
        'GROUP BY coalesce("users"."bio", $1)',
        ("none",),
    )


def test_placeholder_in_expressions(qb: QueryBuilder) -> None:
    q = qb.select(coalesce(Users.role, placeholder("role"))).from_(Users)
    assert q.prepare().compiled.bind({"role": Role.ADMIN}).params == ("admin",)


def test_expressions_in_update_and_insert(qb: QueryBuilder) -> None:
    assert qb.update(Users).set(bio=concat(Users.email, "!")).where(eq(Users.id, 1)).to_sql() == (
        'UPDATE "users" SET "bio" = concat("users"."email", CAST($1 AS text)) '
        'WHERE "users"."id" = $2',
        ("!", 1),
    )
    sql, _ = qb.insert(Posts).values(author_id=1, title=lower(cast(Users.id, text()))).to_sql()
    assert 'VALUES ($1, lower(CAST("users"."id" AS text)))' in sql


def test_like_and_regex(qb: QueryBuilder) -> None:
    assert where(qb, not_like(Users.email, "a%")) == ('"users"."email" NOT LIKE $1', ("a%",))
    assert where(qb, not_ilike(Users.email, "a%"))[0] == '"users"."email" NOT ILIKE $1'
    assert where(qb, regex(Users.email, "^a"))[0] == '"users"."email" ~ $1'
    assert where(qb, regex(Users.email, "^a", ignore_case=True))[0] == '"users"."email" ~* $1'
    assert where(qb, not_regex(Users.email, "^a"))[0] == '"users"."email" !~ $1'
    assert where(qb, not_regex(Users.bio, "^a", ignore_case=True))[0] == '"users"."bio" !~* $1'


# -- window functions -----------------------------------------------------------------


def test_window_functions(qb: QueryBuilder) -> None:
    assert selected(qb, row_number().over())[0] == "row_number() OVER ()"
    assert selected(qb, rank().over(partition_by=Users.role, order_by=desc(Users.id)))[0] == (
        'rank() OVER (PARTITION BY "users"."role" ORDER BY "users"."id" DESC)'
    )
    assert selected(qb, sum_(Users.id).over(order_by=[Users.role, Users.id]))[0] == (
        'sum("users"."id") OVER (ORDER BY "users"."role", "users"."id")'
    )
    assert selected(qb, lag(Users.email).over(order_by=Users.id))[0] == (
        'lag("users"."email", 1) OVER (ORDER BY "users"."id")'
    )
    assert selected(qb, lead(Users.email, 2, "-").over(order_by=Users.id)) == (
        'lead("users"."email", 2, $1) OVER (ORDER BY "users"."id")',
        ("-",),
    )
    assert selected(qb, first_value(Users.id).over(partition_by=[Users.role]))[0] == (
        'first_value("users"."id") OVER (PARTITION BY "users"."role")'
    )
    assert selected(qb, nth_value(Users.id, 2).over())[0] == 'nth_value("users"."id", 2) OVER ()'
    assert selected(qb, ntile(4).over(order_by=Users.id))[0] == (
        'ntile(4) OVER (ORDER BY "users"."id")'
    )


def test_window_frames(qb: QueryBuilder) -> None:
    def frame(expr: ExprBase) -> str:
        return selected(qb, expr)[0].split('ORDER BY "users"."id" ', 1)[1]

    total = sum_(Users.id)
    assert frame(total.over(order_by=Users.id, frame=rows_between())) == (
        "ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)"
    )
    assert frame(total.over(order_by=Users.id, frame=rows_between(-2, 3))) == (
        "ROWS BETWEEN 2 PRECEDING AND 3 FOLLOWING)"
    )
    last = last_value(Users.id).over(order_by=Users.id, frame=range_between(None, None))
    assert frame(last) == "RANGE BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING)"
    with pytest.raises(QueryError):
        rows_between(1.5)  # pyright: ignore[reportArgumentType]


def test_window_decodes_with_the_column(qb: QueryBuilder) -> None:
    decode = qb.select(lag(Users.role).over(order_by=Users.id)).from_(Users).decoder()
    assert decode(("admin",)) == (Role.ADMIN,)


# -- full-text search -----------------------------------------------------------------


def test_full_text_search(qb: QueryBuilder) -> None:
    vector = to_tsvector(Users.bio, config="english")
    assert where(qb, matches(vector, websearch_to_tsquery("cats -dogs", config="english"))) == (
        'to_tsvector(\'english\'::regconfig, "users"."bio") @@ '
        "websearch_to_tsquery('english'::regconfig, CAST($1 AS text))",
        ("cats -dogs",),
    )
    assert selected(qb, ts_rank(to_tsvector(Users.email), to_tsquery("a & b"))) == (
        'ts_rank(to_tsvector("users"."email"), to_tsquery(CAST($1 AS text)))',
        ("a & b",),
    )
    assert selected(qb, ts_rank(vector, plainto_tsquery("x"), normalization=1))[0].endswith(
        "plainto_tsquery(CAST($1 AS text)), 1)"
    )
    with pytest.raises(QueryError):
        to_tsvector(Users.bio, config="english'")


# -- set operations -------------------------------------------------------------------


def test_set_operations(qb: QueryBuilder) -> None:
    users = qb.select(Users.id).from_(Users).where(gt(Users.id, 1))
    authors = qb.select(Posts.author_id).from_(Posts).limit(5)
    assert users.union(authors).order_by(desc(Users.id)).limit(3).offset(1).to_sql() == (
        '(SELECT "users"."id" FROM "users" WHERE "users"."id" > $1) UNION '
        '(SELECT "posts"."author_id" FROM "posts" LIMIT $2) ORDER BY "id" DESC LIMIT $3 OFFSET $4',
        (1, 5, 3, 1),
    )
    for method, op in [
        (users.union_all, "UNION ALL"),
        (users.intersect, "INTERSECT"),
        (users.intersect_all, "INTERSECT ALL"),
        (users.except_, "EXCEPT"),
        (users.except_all, "EXCEPT ALL"),
    ]:
        assert f") {op} (" in method(authors).to_sql()[0]
    chained = users.union(authors).except_(users).to_sql()[0]
    assert chained.startswith("((SELECT") and ") EXCEPT (SELECT" in chained


def test_set_operation_as_subquery_and_cte(qb: QueryBuilder) -> None:
    ids = qb.select(Users.id).from_(Users).union(qb.select(Posts.author_id).from_(Posts))
    sql, _ = qb.select(Users.email).from_(Users).where(in_(Users.id, ids)).to_sql()
    assert '"users"."id" IN ((SELECT "users"."id" FROM "users") UNION (SELECT' in sql
    cte = ids.cte("ids")
    sql, _ = qb.with_(cte).select(cte.c("id", int)).from_(cte).to_sql()
    assert sql.startswith('WITH "ids" AS ((SELECT "users"."id" FROM "users") UNION (SELECT')


async def test_set_operation_decodes_rows(runner: FakeRunner, qb: QueryBuilder) -> None:
    runner.rows = [(1, "a", None, "admin", None)]
    rows = await qb.select().from_(Users).union_all(qb.select().from_(Users))
    assert rows[0].role is Role.ADMIN


# -- aliases --------------------------------------------------------------------------


def test_self_join_with_alias(qb: QueryBuilder) -> None:
    manager = alias(Users, "manager")
    q = qb.select(Users.email, manager.email).from_(Users).join(manager, eq(manager.id, Users.id))
    assert q.where(eq(manager.role, Role.ADMIN)).to_sql() == (
        'SELECT "users"."email", "manager"."email" FROM "users" '
        'INNER JOIN "users" AS "manager" ON "manager"."id" = "users"."id" '
        'WHERE "manager"."role" = $1',
        ("admin",),
    )


async def test_alias_rows_decode_into_the_table(runner: FakeRunner, qb: QueryBuilder) -> None:
    manager = alias(Users, "manager")
    runner.rows = [(1, "a", None, "admin", None, None, None, None, None, None)]
    [(user, boss)] = await (
        qb.select(Users, manager).from_(Users).left_join(manager, eq(manager.id, Users.id))
    )
    assert type(user) is Users and user.role is Role.ADMIN
    assert boss is None
    runner.rows = [(2, "b", None, "user", None)]
    [row] = await qb.select().from_(manager)
    assert type(row) is Users and row == Users.from_db(
        {"id": 2, "email": "b", "bio": None, "role": Role.USER, "created_at": None}
    )
    assert qb.select().from_(manager).to_sql()[0].endswith('FROM "users" AS "manager"')


def test_alias_does_not_change_the_table() -> None:
    manager = alias(Users, "manager")
    assert alias(manager, "boss").__table__.name == "boss"
    assert Users.__table__.name == "users"
    assert Users.id.table is Users and manager.id.table is manager


def test_alias_in_subquery_and_lock(qb: QueryBuilder) -> None:
    other = alias(Users, "other")
    dupes = qb.select(other.id).from_(other).where(eq(other.email, Users.email))
    sql, _ = qb.select(Users.id).from_(Users).where(in_(Users.id, dupes)).to_sql()
    assert 'IN (SELECT "other"."id" FROM "users" AS "other" WHERE "other"."email" = "users"' in sql
    sql, _ = qb.select().from_(other).for_update(of=[other]).to_sql()
    assert sql.endswith('FOR UPDATE OF "other"')


def test_alias_in_relational_where(qb: QueryBuilder) -> None:
    other = alias(Users, "other")
    dupes = qb.select(other.id).from_(other).where(eq(other.email, Users.email))
    sql, _ = qb.query(Users).find_many(where=in_(Users.id, dupes)).to_sql()
    assert 'FROM "users" AS "other" WHERE "other"."email" = "t1"."email"' in sql


def test_alias_of_a_cte(qb: QueryBuilder) -> None:
    cte = qb.select(Users.id).from_(Users).cte("u")
    other = alias(cte, "u2")
    sql, _ = (
        qb.with_(cte)
        .select(cte.c("id", int), other.c("id", int))
        .from_(cte)
        .join(other, gt(other.c("id", int), cte.c("id", int)))
        .to_sql()
    )
    assert sql.endswith(
        'SELECT "u"."id", "u2"."id" FROM "u" INNER JOIN "u" AS "u2" ON "u2"."id" > "u"."id"'
    )


def test_relational_with_expressions(qb: QueryBuilder) -> None:
    sql, params = (
        qb.query(Users)
        .find_many(where=eq(coalesce(Users.bio, ""), ""), order_by=desc(length(Users.email)))
        .to_sql()
    )
    assert sql.endswith('WHERE coalesce("t1"."bio", $1) = $2 ORDER BY length("t1"."email") DESC')
    assert params == ("", "")


def test_values_get_sql_types() -> None:
    from mizzle.core.sql.functions import bind
    from mizzle.dialects.postgres.compiler import PostgresCompiler

    def compiled(value: object) -> str:
        return PostgresCompiler().compile(bind(value)).sql

    assert compiled(True) == "CAST($1 AS boolean)"
    assert compiled(1.5) == "CAST($1 AS double precision)"
    assert compiled(datetime.datetime(2020, 1, 1)) == "CAST($1 AS timestamp)"
    assert compiled(datetime.datetime.now(datetime.UTC)) == "CAST($1 AS timestamptz)"
    assert compiled(datetime.date(2020, 1, 1)) == "CAST($1 AS date)"
    assert compiled({"a": 1}) == "$1"
