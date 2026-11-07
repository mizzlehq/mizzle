# Queries

mizzle's query builder reads like SQL. Every query is a value you can pass around and
build on; `await` runs it. Results are typed from the query itself.

The examples on this page use these tables:

```python
import enum
from typing import final

from mizzle import Table, sql
from mizzle.pg import date, integer, numeric, pg_enum, serial, text, timestamp, varchar


class Role(enum.StrEnum):
    ADMIN = "admin"
    MEMBER = "member"


@final
class Users(Table, name="users"):
    id = serial().primary_key()
    email = varchar(255).not_null().unique()
    name = text().not_null()
    nickname = text()
    role = pg_enum(Role).not_null().default(Role.MEMBER)
    manager_id = integer().references(lambda: Users.id)
    created_at = timestamp(tz=True).not_null().default(sql("now()"))


@final
class Posts(Table, name="posts"):
    id = serial().primary_key()
    author_id = integer().not_null().references(lambda: Users.id, on_delete="cascade")
    title = text().not_null()
    body = text().not_null().default("")
    views = integer().not_null().default(0)
    price = numeric(10, 2)
    published_on = date()


@final
class Events(Table, name="events"):
    id = integer().primary_key().generated_always_as_identity()
    name = text().not_null()
    starts_at = timestamp(tz=True).not_null()
    ends_at = timestamp(tz=True).not_null()
    day = date().not_null()
```

`db` is a `Database` (see [Getting started](../getting-started.md)). Everything works the
same on a transaction (`async with db.transaction() as tx`).

## Select

```python
users = await db.select().from_(Users)                         # list[Users]
pairs = await db.select(Users.id, Users.email).from_(Users)    # list[tuple[int, str]]
named = await db.select({"id": Users.id, "email": Users.email}).from_(Users)
# named: list[dict[str, object]]
roles = await db.select(Users.role).from_(Users).distinct()
```

- `select()` with no arguments returns whole rows.
- `select(a, b, ...)` returns tuples, typed per column (up to 10 columns; more come back as
  `tuple[object, ...]`). Pass a table to get its whole row in that slot.
- `select({...})` returns dicts.

## Where

```python
from mizzle import eq, gt, ilike

admins = await db.select().from_(Users).where(eq(Users.role, Role.ADMIN))
busy = await (
    db.select()
    .from_(Posts)
    .where(gt(Posts.views, 10))
    .where(ilike(Posts.title, "%hello%"))  # a second where() is ANDed
)
```

Operators are functions, not `==`: your editor checks that both sides have the same type,
so `eq(Users.id, "1")` is a type error. Values are always sent as bind parameters.

```python
from mizzle import (
    and_, between, eq, gt, gte, ilike, in_, is_not_null, is_null, like, lt, lte, ne, not_,
    not_ilike, not_in, not_like, not_regex, not_similar_to, or_, regex, similar_to,
)

conditions = [
    eq(Users.id, 1),
    ne(Users.id, 1),
    gt(Users.id, 1),
    gte(Users.id, 1),
    lt(Users.id, 1),
    lte(Users.id, 1),
    between(Users.id, 1, 10),
    in_(Users.id, [1, 2, 3]),
    not_in(Users.id, [4, 5]),
    is_null(Users.nickname),
    is_not_null(Users.nickname),
    like(Users.email, "%@example.com"),
    not_like(Users.email, "%@spam.%"),
    ilike(Users.name, "ann%"),
    not_ilike(Users.name, "bot%"),
    regex(Users.email, r"^[a-z]+@", ignore_case=True),
    not_regex(Users.email, r"\d"),
    similar_to(Users.email, "%@(example|test).(com|org)"),
    not_similar_to(Users.email, "%.ru"),
    and_(eq(Users.role, Role.ADMIN), gt(Users.id, 0)),
    or_(eq(Users.id, 1), eq(Users.id, 2)),
    not_(eq(Users.role, Role.ADMIN)),
    eq(Posts.author_id, Users.id),  # column against column
]
```

- `in_(col, [])` matches nothing. `in_` and `not_in` also take a query (see
  [Subqueries](#subqueries)).
- `regex` is PostgreSQL's `~` (`~*` with `ignore_case=True`); `similar_to` is
  `SIMILAR TO`.
- `and_` and `or_` skip `None`, which makes optional filters easy:

```python
rows = await db.select().from_(Users).where(
    and_(
        eq(Users.role, Role.MEMBER),
        ilike(Users.name, f"%{search}%") if search else None,  # skipped when None
    )
)
```

Comparing with `None` raises, because `= NULL` is never true in SQL:

```python
from mizzle import QueryError

try:
    eq(Users.nickname, None)
except QueryError as error:
    print(error)  # comparing with None using '=' is never true in SQL; use is_null()
```

## Joins

```python
from mizzle import eq, nullable

# Inner join: whole rows of both tables, as a tuple.
with_posts = await (
    db.select(Users, Posts).from_(Users).join(Posts, eq(Posts.author_id, Users.id))
)  # list[tuple[Users, Posts]]

# Left join: the joined table may be missing, so its slot is `Posts | None`.
everyone = await (
    db.select(Users, Posts).from_(Users).left_join(Posts, eq(Posts.author_id, Users.id))
)  # list[tuple[Users, Posts | None]]

# Right and full joins make the other side optional too.
right = await (
    db.select(Users, Posts).from_(Users).right_join(Posts, eq(Posts.author_id, Users.id))
)  # list[tuple[Users | None, Posts]]
full = await (
    db.select(Users, Posts).from_(Users).full_join(Posts, eq(Posts.author_id, Users.id))
)  # list[tuple[Users | None, Posts | None]]

# Single columns of an outer-joined table: mark them nullable().
titles = await (
    db.select(Users.name, nullable(Posts.title))
    .from_(Users)
    .left_join(Posts, eq(Posts.author_id, Users.id))
)  # list[tuple[str, str | None]]
```

`right_join` and `full_join` need an explicit `select(A, B)`, so the missing side is typed.
`cross_join()` pairs every row with every row of the other table (no condition):

```python
grid = await db.select(Users.id, Posts.id).from_(Users).cross_join(Posts)
```

For a query that loads related rows as nested objects (users with their posts), use a
[relational query](relational.md) instead of a join.

## Order, limit, offset

```python
from mizzle import asc, desc

page = await (
    db.select()
    .from_(Posts)
    .order_by(desc(Posts.views), asc(Posts.id))
    .limit(20)
    .offset(40)
)
newest_first = await db.select().from_(Posts).order_by(desc(Posts.published_on, nulls="last"))
```

A bare column in `order_by` is ascending. `nulls="first"` or `"last"` sets where NULLs go.

## Group by and aggregates

```python
from mizzle import avg, count, gt, max_, min_, sum_

per_author = await (
    db.select(Posts.author_id, count(), sum_(Posts.views), avg(Posts.views))
    .from_(Posts)
    .group_by(Posts.author_id)
    .having(gt(count(), 1))
    .order_by(Posts.author_id)
)  # list[tuple[int, int, int | Decimal | None, Decimal | None]]

[(total, authors, top, low)] = await db.select(
    count(), count(Posts.author_id, distinct=True), max_(Posts.views), min_(Posts.views)
).from_(Posts)
```

- `count()` is `count(*)`; `count(col, distinct=True)` is `count(DISTINCT col)`.
- `sum_`, `avg`, `min_` and `max_` can be NULL (no rows), so their types include `None`.
  `sum_` of an integer column is `int | Decimal`: PostgreSQL sums `bigint` as `numeric`.
  `avg` of integers is `Decimal`.

## Expressions and functions

```python
from mizzle import (
    abs_, cast, coalesce, concat, func, greatest, least, length, lower, nullif, round_,
    trim, upper,
)

rows2 = await db.select(
    Posts.views * 2,                         # arithmetic: +, -, *, /
    Users.name + " <" + Users.email + ">",   # + on text is ||
    lower(Users.email),
    upper(Users.name),
    trim(Users.name),
    length(Users.name),
    concat(Users.name, " #", Users.id),
    abs_(Posts.views - 100),
    Posts.views / 3,                         # integer division, as in SQL
    round_(Posts.price, 1),                  # Decimal | None
    coalesce(Users.nickname, Users.name),     # str: the last argument is not null
    nullif(Users.nickname, ""),
    greatest(Posts.views, 50),
    least(Posts.views, 50),
    cast(Posts.views, text().not_null()),     # CAST(views AS text): str
    func[str]("md5", Users.email),            # any function, typed by you
).from_(Posts).join(Users, eq(Users.id, Posts.author_id)).order_by(Posts.id)
```

- `+` on text is `||`, and NULL on either side gives NULL. `concat()` treats NULL as empty.
- `cast(expr, column_type)` takes a column type from `mizzle.pg`; the result has its type.
- `func[T]("name", *args)` calls any SQL function. The name is checked, and Python values
  are bind parameters.

Expressions also work as values in updates:

```python
await db.update(Posts).set(views=Posts.views + 1).where(eq(Posts.id, 1))
```

## CASE

```python
from mizzle import case_, gt

heat = (
    case_()
    .when(gt(Posts.views, 100), "hot")
    .when(gt(Posts.views, 50), "warm")
    .else_("cold")
)  # Expr[str]; without else_() it is Expr[str | None]
labelled = await db.select(Posts.title, heat).from_(Posts).order_by(Posts.id)
```

## Dates and times

```python
import datetime as dt
from datetime import timedelta

from mizzle import current_date, date_trunc, extract, gt, now

await db.insert(Events).values(
    name="launch",
    starts_at=dt.datetime(2026, 3, 1, 9, tzinfo=dt.UTC),
    ends_at=dt.datetime(2026, 3, 1, 17, tzinfo=dt.UTC),
    day=dt.date(2026, 3, 1),
)
[(next_day, length_, days_ago, month, year)] = await db.select(
    Events.starts_at + timedelta(days=1),    # datetime
    Events.ends_at - Events.starts_at,       # timedelta (an interval)
    current_date() - Events.day,             # int (days)
    date_trunc("month", Events.starts_at),   # datetime
    extract("year", Events.starts_at),       # Decimal
).from_(Events)
soon = await db.select().from_(Events).where(gt(Events.starts_at, now() - timedelta(days=7)))
```

A `timedelta` is sent as an `interval`. `now()` is the start time of the current
transaction.

## Subqueries

```python
from mizzle import count, desc, eq, exists, in_, not_exists

# IN (subquery)
authors_q = db.select(Posts.author_id).from_(Posts)
writers = await db.select().from_(Users).where(in_(Users.id, authors_q))

# EXISTS, correlated with the outer row
has_posts = db.select(Posts.id).from_(Posts).where(eq(Posts.author_id, Users.id))
silent = await db.select().from_(Users).where(not_exists(has_posts))
loud = await db.select().from_(Users).where(exists(has_posts))

# A subquery in FROM (or in a join): name it and read its columns with c()
counts = (
    db.select({"author": Posts.author_id, "n": count()})
    .from_(Posts)
    .group_by(Posts.author_id)
    .subquery("counts")
)
author, n = counts.c("author", int), counts.c("n", int)
leaders = await (
    db.select(Users.name, n)
    .from_(Users)
    .join(counts, eq(author, Users.id))
    .order_by(desc(n))
)  # list[tuple[str, int]]
```

`c("name", type)` types the column for your editor; the type is not checked at runtime.
Values decode like the inner column they come from, so an enum comes back as the enum.

## CTEs

`query.cte(name)` makes a `WITH` query; `db.with_(...)` adds it to the next statement.

```python
from mizzle import desc, eq, row_number

ranked = db.select(
    {
        "id": Posts.id,
        "author_id": Posts.author_id,
        "n": row_number().over(partition_by=Posts.author_id, order_by=desc(Posts.views)),
    }
).from_(Posts).cte("ranked")

top_posts = await (
    db.with_(ranked)
    .select(ranked.c("id", int), ranked.c("author_id", int))
    .from_(ranked)
    .where(eq(ranked.c("n", int), 1))
    .order_by(ranked.c("author_id", int))
)
```

### Recursive CTEs

`db.recursive(name, seed)` builds the self-reference for you, so the name is written once.
Everyone who reports to a manager, at any depth:

```python
from mizzle import eq

reports = db.recursive(
    "reports", db.select({"id": Users.id}).from_(Users).where(eq(Users.manager_id, boss_id))
).step(
    lambda reports: db.select({"id": Users.id})
    .from_(Users)
    .join(reports, eq(Users.manager_id, reports.c("id", int)))
)
rows = await db.with_(reports).select(reports.c("id", int)).from_(reports)
```

The step runs until it adds no new rows, so it must stop by itself: here, at people with
no reports. A step that always finds rows (`n - 1` with no `where`) never ends.

`query.cte(name, recursive=True)` still works when you already have the step query built
against a CTE of the same name.

## Window functions

```python
from mizzle import count, dense_rank, desc, first_value, lag, lead, rank, row_number
from mizzle import rows_between, sum_

stats = await db.select(
    Posts.id,
    row_number().over(partition_by=Posts.author_id, order_by=desc(Posts.views)),
    rank().over(order_by=desc(Posts.views)),
    dense_rank().over(order_by=Posts.author_id),
    lag(Posts.views).over(order_by=Posts.id),          # int | None
    lead(Posts.views, 1, 0).over(order_by=Posts.id),   # int (default 0)
    first_value(Posts.title).over(partition_by=Posts.author_id, order_by=Posts.id),
    sum_(Posts.views).over(order_by=Posts.id, frame=rows_between(None, 0)),  # running total
    count().over(partition_by=Posts.author_id),
).from_(Posts).order_by(Posts.id)
```

- Window functions: `row_number`, `rank`, `dense_rank`, `percent_rank`, `cume_dist`,
  `ntile(n)`, `lag`, `lead`, `first_value`, `last_value`, `nth_value`. Every aggregate
  (`count`, `sum_`, `avg`, `min_`, `max_`) takes `.over(...)` too.
- `rows_between(start, end)` and `range_between(start, end)` set the frame. Negative is
  `PRECEDING`, `0` is `CURRENT ROW`, positive is `FOLLOWING`, `None` is `UNBOUNDED`.

Name a window once with `.window()` and use it by name:

```python
named_w = await (
    db.select(Posts.id, row_number().over("w"), lag(Posts.views).over("w"))
    .from_(Posts)
    .window("w", partition_by=Posts.author_id, order_by=Posts.id)
    .order_by(Posts.id)
)
```

## Union, intersect, except

```python
staff = db.select(Users.id).from_(Users).where(eq(Users.role, Role.ADMIN))
writers_q = db.select(Posts.author_id).from_(Posts)

both = await staff.union(writers_q).order_by(Users.id)        # no duplicates
every = await staff.union_all(writers_q)                       # duplicates kept
staff_who_write = await staff.intersect(writers_q)
readers_only = await db.select(Users.id).from_(Users).except_(writers_q)
# also: intersect_all(), except_all()
```

Both queries must have the same row type. The result takes `order_by`, `limit` and
`offset`, and can become a CTE or subquery (`.cte(name)`, `.subquery(name)`).

## Aliases

Use a table twice in one query (a self-join) with `alias()`:

```python
from mizzle import alias, eq

manager = alias(Users, "manager")
reports = await (
    db.select(Users.name, manager.name)
    .from_(Users)
    .join(manager, eq(Users.manager_id, manager.id))
    .order_by(Users.id)
)  # list[tuple[str, str]]
```

Rows of an alias decode into the table's own class. Comparisons accept a nullable column
on one side and a non-null one on the other (`manager_id` is `int | None`, `manager.id` is
`int`).

## Insert

```python
count_inserted = await db.insert(Users).values(email="dee@example.com", name="Dee")  # 1
[eve] = await db.insert(Users).values({"email": "eve@example.com", "name": "Eve"}).returning()
many_rows = await db.insert(Users).values(
    [
        {"email": "fay@example.com", "name": "Fay"},
        {"email": "gus@example.com", "name": "Gus"},
    ]
).returning()  # list[Users]
```

`values()` takes keyword arguments, a dict, a list of dicts or a Pydantic model. Awaiting
the insert returns the row count; `.returning()` returns the inserted rows (whole rows, or
`.returning(Users.id, Users.email)` for `list[tuple[int, str]]`). mizzle checks
the values before it sends anything: an unknown column, a wrong type or a missing required
column raises `QueryError`.

### Upserts

```python
from mizzle import excluded, ne

# Skip rows that already exist.
await db.insert(Users).values(email="eve@example.com", name="Eve").on_conflict_do_nothing(
    Users.email
)

# Or update them. excluded() is the row you tried to insert.
[eve2] = await (
    db.insert(Users)
    .values(email="eve@example.com", name="Eve Adams")
    .on_conflict_do_update(
        target=Users.email,
        set_={"name": excluded(Users.name)},
        where=ne(Users.role, Role.ADMIN),  # only update these rows
    )
    .returning()
)
```

`target` is a column or a list of columns with a unique constraint. `on_conflict_do_nothing()`
without a target skips any conflict. Columns with `.on_update(...)` are set by
`DO UPDATE` too.

### Identity columns

A `generated_always_as_identity()` column is assigned by the database. To store your own
value (for example when importing data), add `.overriding_system_value()`:

```python
# Events.id is generated_always_as_identity(): values need overriding_system_value().
await db.insert(Events).values(
    id=100, name="import", starts_at=now(), ends_at=now(), day=current_date()
).overriding_system_value()
```

`.overriding_user_value()` does the opposite: the database ignores the given values.

### Many rows

`insert_many` splits any number of rows into as few statements as possible, inside one
transaction: all rows or none.

```python
rows_to_load = [{"email": f"user{i}@example.com", "name": f"User {i}"} for i in range(5000)]
loaded = await db.insert_many(Users, rows_to_load, batch_size=1000)  # 5000, one transaction
upserted = await db.insert_many(
    Users,
    [{"email": "user1@example.com", "name": "First"}],
    on_conflict=db.insert(Users).on_conflict_do_update(
        target=Users.email, set_={"name": excluded(Users.name)}
    ),
    returning=True,
)  # list[Users]
```

`on_conflict` is an insert without values; every batch gets its conflict clause (see
[Bulk and streaming](bulk.md)).

## Update and delete

```python
changed = await db.update(Users).set(nickname="eve").where(eq(Users.id, eve.id))  # 1
[renamed] = await (
    db.update(Users).set({"name": "Eve A."}).where(eq(Users.id, eve.id)).returning()
)
removed = await db.delete(Posts).where(lt(Posts.views, 30)).returning()  # list[Posts]

try:
    await db.delete(Posts)  # no where()
except QueryError as error:
    print(error)  # ... call .all_rows() to affect every row

await db.update(Posts).set(views=0).all_rows()  # on purpose: every row
```

`update()` and `delete()` refuse to run without `.where()`. A forgotten filter would
rewrite or wipe the whole table, so you must say so with `.all_rows()`. Awaiting returns
the row count; `.returning()` returns the rows.

## Locking rows

```python
async with db.transaction() as tx:
    [post] = await tx.select().from_(Posts).where(eq(Posts.id, 1)).for_update()
    await tx.update(Posts).set(views=post.views + 1).where(eq(Posts.id, post.id))

async with db.transaction() as tx:
    # A job queue: take rows no one else holds, skip the locked ones.
    jobs = await (
        tx.select()
        .from_(Posts)
        .order_by(Posts.id)
        .limit(10)
        .for_update(skip_locked=True)
    )
    # nowait=True fails at once instead of waiting; of=[Posts] locks only Posts rows
    # in a join.
```

`FOR UPDATE` holds the lock until the transaction ends, so use it inside
`db.transaction()`. A query with `for_update()` always runs on the primary, also when you
have read replicas.

## Raw SQL

```python
rows3 = await db.execute(sql("SELECT id, email FROM users WHERE id = {}", user_id))
# list[dict[str, object]]

# sql() also works inside builder queries, as a condition or a value.
recent = await db.select().from_(Users).where(sql("created_at > now() - interval '1 day'"))
ranked2 = await db.select(Users.id, sql("{} * 2", Users.id)).from_(Users)
```

- Each `{}` is a bind parameter (never pasted into the SQL text). A column or expression
  in a slot is compiled in place.
- Without arguments, the text is used as written, so braces in regexes or JSON need no
  escaping.
- On Python 3.14 you can write a t-string: `sql(t"... WHERE id = {user_id}")`.
- `db.execute()` returns rows as dicts.

## Explain a query

```python
plan = await db.explain(db.select().from_(Users).where(eq(Users.email, "a@b")))
# list[dict[str, object]]: one row per plan line

analyzed = await db.explain(db.select().from_(Users), analyze=True)
```

`analyze=True` adds real row counts and timings but runs the statement, so only selects
are allowed. The plan routes like the statement itself (a read may use a replica), and
`explain()` does not take `placeholder()` values.

## Prepared queries and SQL text

```python
from mizzle import placeholder

by_role = (
    db.select(Users.email).from_(Users).where(eq(Users.role, placeholder("role"))).prepare()
)
admin_emails = await by_role.execute(role=Role.ADMIN)

text_, params = db.select().from_(Users).where(eq(Users.id, 1)).to_sql()
# 'SELECT "users"."id", ... FROM "users" WHERE "users"."id" = $1', (1,)
```

`prepare()` compiles once; `execute(**values)` fills the placeholders. `to_sql()` shows
the SQL and parameters of any query.

## Full-text search

```python
from mizzle import desc, matches, to_tsvector, ts_rank, websearch_to_tsquery

document = to_tsvector(Posts.title + " " + Posts.body, config="english")
query = websearch_to_tsquery("cats -dogs", config="english")
found = await (
    db.select(Posts.id, ts_rank(document, query))
    .from_(Posts)
    .where(matches(document, query))
    .order_by(desc(ts_rank(document, query)))
)  # list[tuple[int, float]]
# also: to_tsquery("cat & !dog"), plainto_tsquery(text), phraseto_tsquery(text),
# ts_rank(v, q, weights=[0.1, 0.2, 0.4, 1.0], normalization=1), ts_rank_cd(v, q)
```

- `matches(vector, query)` is `vector @@ query`.
- `websearch_to_tsquery` takes search-box syntax (`"a phrase" or -word`) and never fails
  on bad input.
- `ts_rank_cd` also rewards matching words that are close together.
- For speed, store the vector in a generated `custom("tsvector")` column with a GIN index
  (see [Schema](schema.md#identity-and-generated-columns)).
