# Migrations

mizzle diffs your Python schema against the last migration's snapshot and writes the SQL.
You read it, commit it, and run it. Every option of every command is on the
[CLI](../cli.md) page; this page is the workflow.

## The loop

```bash
mizzle generate --name init   # writes migrations/0001_init/migration.sql
mizzle migrate                # applies what is not applied yet
mizzle check                  # in CI: the folder and journal agree
```

`generate` writes one folder per migration: `migration.sql` (the SQL to run) and
`snapshot.json` (the schema after it). `meta/journal.json` lists them in order. Commit the
whole `migrations/` folder.

The first lines of each `migration.sql` say what it does:

```sql
-- create table users
-- create table posts
-- add foreign key posts_author_id_fkey
CREATE TABLE "users" (
    "id" serial NOT NULL,
    "email" character varying(255) NOT NULL,
    ...
```

`migrate` records applied migrations in `__mizzle_migrations`. Each migration runs in its
own transaction: it applies fully or not at all. (One exception: `ALTER TYPE ... ADD VALUE`
for a new enum value runs on its own, since PostgreSQL cannot use the value in the
transaction that adds it.) Running `migrate` again prints `Database is up to date.`

`migrate` raises `MigrationTampered` when a `migration.sql` changed after it was applied:
restore the file and put the change in a new migration. It also stops when a migration
is unapplied while a later one is applied (two merged branches); `mizzle check` shows
which.

`mizzle check` fails when a folder is missing from the journal, or when two branches
generated migrations from the same parent. It warns when a `migration.sql` was edited
after `generate`.

## push: for development databases

```bash
mizzle push --dry-run   # print the SQL only
mizzle push             # asks first; --force to skip the question
```

`push` compares the schema with the live database and applies the difference. No files
are written. Use it on a scratch database while you shape the schema, then `generate` the
real migration. Without a terminal, `push` refuses to run unless you pass `--force`.

## Renames

A column that disappears while another one appears can be a rename or a drop plus a
create. mizzle never guesses. On a terminal it asks. Elsewhere (CI, scripts) it stops:

```text
error: columns ['name'] were removed and ['full_name'] added in public.users:
pass --rename users.name=full_name if it is a rename, or --no-renames
```

```bash
mizzle generate --rename users.name=full_name   # ALTER TABLE ... RENAME COLUMN
mizzle generate --rename users=people           # a table
mizzle generate --rename app.users=archive.users  # move to another schema, data kept
mizzle generate --no-renames                    # it really is drop + create
```

## Data loss

Steps that can lose data (dropping a table or column, narrowing a type) stop `generate`
and `push`:

```text
error: these changes can lose data:
  - change type of users.email from character varying(255) to character varying(100)
rerun with --allow-data-loss if this is intended
```

On a terminal it asks instead. Widening a type (`varchar(40)` to `varchar(64)` or `text`,
`integer` to `bigint`) does not ask.

## Custom migrations

For SQL mizzle does not write, such as data backfills:

```bash
mizzle generate --custom --name backfill
```

This writes an empty `migration.sql`. Add your statements, separated by
`-- mizzle:breakpoint`:

```sql
-- Custom migration. Separate statements with: -- mizzle:breakpoint
UPDATE users SET full_name = initcap(full_name);
-- mizzle:breakpoint
UPDATE posts SET body = trim(body);
```

`check` does not warn about edits to custom migrations. `mizzle drop` deletes the latest
migration if it has not been applied yet.

## Many servers at once

Run `mizzle migrate` on every app server at startup. An advisory lock lets one process
apply the migrations; the others wait, then find nothing to do:

```text
Applying 0001_init
Applying 0002_rename_name
Applied 2 migration(s).
```

```text
Database is up to date.
```

`--lock-timeout 30` sets how long (seconds) a process waits for that lock (default 60).
After that it fails with `MigrationLockTimeout`.

## Database objects

Migrations manage more than tables. Declare objects in your schema module; mizzle collects
module-level `extension`, `function`, `trigger` and `role` values, like `Table` classes.
Table-level objects (indexes, row-level security, policies, grants) go in `config()`.

```python
from typing import final, override

from mizzle import (
    Constraint,
    QueryBuilder,
    Table,
    View,
    count,
    eq,
    extension,
    function,
    grant,
    index,
    many,
    one,
    policy,
    rls,
    role,
    sql,
    trigger,
    unique_index,
)
from mizzle.pg import bigint, integer, serial, text, timestamp, varchar

trgm = extension("pg_trgm")

app_user = role("docs_app_user")  # created and kept in sync by migrations
reporting = role("docs_reporting", existing=True)  # made by someone else: only referenced


@final
class Users(Table, name="users"):
    id = serial().primary_key()
    email = varchar(255).not_null().unique()
    full_name = text().not_null()
    posts = many(lambda: Posts)

    @classmethod
    @override
    def config(cls) -> list[Constraint]:
        return [
            index("users_name_trgm_idx").using("gin", "gin_trgm_ops").on(cls.full_name),
            grant(["select", "update"], to=[app_user]),
        ]


@final
class Posts(Table, name="posts"):
    id = serial().primary_key()
    author_id = integer().not_null().references(lambda: Users.id, on_delete="cascade")
    body = text().not_null()
    created_at = timestamp(tz=True).not_null().default(sql("now()"))
    updated_at = timestamp(tz=True).not_null().default(sql("now()"))
    author = one(lambda: Users, fields=lambda: [Posts.author_id], references=lambda: [Users.id])

    @classmethod
    @override
    def config(cls) -> list[Constraint]:
        return [
            rls(),
            grant(["select", "insert", "update", "delete"], to=[app_user]),
            policy(
                "posts_own",
                to=[app_user],
                using=eq(cls.author_id, sql("current_setting('app.user_id')::int")),
            ),
        ]


@final
class PostCounts(View, name="post_counts"):
    author_id = integer().not_null()
    posts = bigint().not_null()

    @classmethod
    @override
    def query(cls, db: QueryBuilder):
        return (
            db.select({"author_id": Posts.author_id, "posts": count()})
            .from_(Posts)
            .group_by(Posts.author_id)
        )

    @classmethod
    @override
    def config(cls) -> list[Constraint]:
        return [grant("select", to=[reporting])]


@final
class DailyPosts(View, name="daily_posts", materialized=True):
    day = timestamp(tz=True).not_null()
    posts = bigint().not_null()

    @classmethod
    @override
    def query(cls, db: QueryBuilder):
        return sql(
            "SELECT date_trunc('day', created_at) AS day, count(*) AS posts FROM posts GROUP BY 1"
        )

    @classmethod
    @override
    def config(cls) -> list[Constraint]:
        return [unique_index("daily_posts_day_key").on(cls.day)]


touch = function(
    "touch_updated_at",
    returns="trigger",
    body="BEGIN NEW.updated_at = now(); RETURN NEW; END",
)
posts_touch = trigger("posts_touch", on=Posts, execute=touch, events=["update"])
```

`mizzle generate` writes, besides the tables:

```sql
-- create role docs_app_user
-- create extension pg_trgm
-- create index users_name_trgm_idx
-- create function touch_updated_at
-- create view post_counts
-- create materialized view daily_posts
-- create index daily_posts_day_key
-- enable row level security on posts
-- create policy posts_own on posts
-- create trigger posts_touch on posts
-- grant select, update on users to docs_app_user
-- grant delete, insert, select, update on posts to docs_app_user
-- grant select on post_counts to docs_reporting
```

### Extensions

`extension("pg_trgm")` is `CREATE EXTENSION IF NOT EXISTS`. Migrations create extensions
before anything else. `schema=` and `version=` pin where and which version. Columns that
need an extension (pgvector's `vector`) add it on their own; see
[Extensions](extensions.md).

### Views

A `View` declares its columns like a table, and a `query()`: a select built with `db`, or
`sql("SELECT ...")`. `materialized=True` makes a materialized view. Query views like
tables:

```python
counts = await db.select().from_(PostCounts)  # list[PostCounts]
await db.refresh(DailyPosts)  # REFRESH MATERIALIZED VIEW
await db.refresh(DailyPosts, concurrently=True)  # needs a unique index
```

When a view changes, mizzle drops and creates it again, and keeps its privileges, owner
and comment.

### Row-level security and policies

`rls()` turns on row-level security; `rls(force=True)` applies it to the table owner too.
`policy(name, for_=..., to=[...], using=..., with_check=...)` adds a policy. `for_` is
`"all"` (default), `"select"`, `"insert"`, `"update"` or `"delete"`; `as_="restrictive"`
makes a restrictive policy. With RLS on and no policy for a role, that role sees no rows.

The policy above reads a setting. Set it, and the role, for one transaction:

```python
async with db.transaction() as tx:
    await tx.execute(sql("SELECT set_config('app.user_id', {}, true)", str(ann.id)))
    await tx.execute(sql("SET LOCAL ROLE docs_app_user"))
    mine = await tx.select().from_(Posts)  # only Ann's posts
```

For Supabase's `auth.uid()` and ready-made policies, see
[Integrations](integrations.md#supabase).

### Roles

`role("name", login=, create_db=, create_role=, inherit=)` creates the role if it is
missing and keeps those options in sync. Passwords are not managed: set them outside the
schema. `existing=True` only references a role something else manages. Roles belong to
the whole server, so mizzle never drops a role it does not manage.

A role used in a `policy` or `grant` is created by the migration, so you do not need to
assign it to a module-level name, but you can.

### Grants

`grant(privileges, to=[...])` in a table's or view's `config()`. Privileges are
`"select"`, `"insert"`, `"update"`, `"delete"`, `"truncate"`, `"references"`, `"trigger"`,
`"maintain"` or `"all"`. Migrations grant what is declared and revoke what was declared
before and no longer is. Privileges the schema never declared are left alone, and `push`
never revokes from roles the schema does not name.

A denied query raises `DatabaseError` with `sqlstate` `"42501"`:

```python
try:
    async with db.transaction() as tx:
        await tx.execute(sql("SET LOCAL ROLE docs_app_user"))
        await tx.select().from_(PostCounts)  # docs_app_user has no grant on the view
except DatabaseError as exc:
    assert exc.sqlstate == "42501"  # insufficient_privilege
```

### Functions

`function(name, returns=, body=)` is `CREATE OR REPLACE FUNCTION`. The body is kept as
written. Options: `args="a integer, b text"`, `language=` (default `"plpgsql"`),
`schema=`, `volatility="stable"`, `security_definer=True`, `strict=`, `leakproof=`,
`parallel="safe"`, `cost=` and `settings={"search_path": "public, pg_temp"}`. Use
PostgreSQL's type names (`integer`, not `int`) so the database prints them back the same.
Functions with the same name and other `args` are separate overloads. A
`language="sql"` body that starts with `RETURN` or `BEGIN ATOMIC` is a SQL-standard body.

### Triggers

`trigger(name, on=Table, execute=fn)` runs `fn` `before` (default) or `after` the
`events` (default insert and update), `for_each="row"` (default) or `"statement"`. More
options:

- `when=sql("OLD.price IS DISTINCT FROM NEW.price")`: a `WHEN` condition.
- `update_of=[Items.price]`: `UPDATE OF price`.
- `new_table="added"`, `old_table=`: transition tables, for `after` triggers.
- `on=SomeView, timing="instead of"`: a trigger on a view.

## mizzle pull

`pull` reads an existing database and writes a schema module:

```bash
mizzle pull                          # db/schema.py in a new project
mizzle pull --out db/legacy.py --force
```

In a new project it writes your schema module, plus a baseline migration with nothing to
run (the tables already exist). If your schema module exists, it writes
`pulled_schema.py` next to it instead, for you to compare.

From the database above it writes:

```python
"""Generated by `mizzle pull`. Review before committing."""

from typing import final, override

from mizzle import Constraint, QueryBuilder, Table, View, extension, function, grant, index, many, one, policy, rls, sql, trigger, unique_index
from mizzle.pg import bigint, integer, serial, text, timestamp, varchar


ext_pg_trgm = extension('pg_trgm')


fn_touch_updated_at = function(
    'touch_updated_at',
    returns='trigger',
    body='BEGIN NEW.updated_at = now(); RETURN NEW; END',
)


@final
class Posts(Table, name='posts'):
    id = serial().primary_key()
    author_id = integer().not_null().references(lambda: Users.id, on_delete='cascade')
    body = text().not_null()
    created_at = timestamp(tz=True).not_null().default(sql('now()'))
    updated_at = timestamp(tz=True).not_null().default(sql('now()'))

    author = one(lambda: Users, fields=lambda: [Posts.author_id], references=lambda: [Users.id])

    @classmethod
    @override
    def config(cls) -> list[Constraint]:
        return [
            rls(),
            policy('posts_own', to=['docs_app_user'], using=sql("(author_id = (current_setting('app.user_id'::text))::integer)")),
            grant(['delete', 'insert', 'select', 'update'], to=['docs_app_user']),
        ]


@final
class Users(Table, name='users'):
    id = serial().primary_key()
    email = varchar(255).not_null().unique()
    full_name = text().not_null()

    posts = many(lambda: Posts)

    @classmethod
    @override
    def config(cls) -> list[Constraint]:
        return [
            index('users_name_trgm_idx').using('gin', 'gin_trgm_ops').on(cls.full_name),
            grant(['select', 'update'], to=['docs_app_user']),
        ]


@final
class DailyPosts(View, name='daily_posts', materialized=True):
    day = timestamp(tz=True)
    posts = bigint()

    @classmethod
    @override
    def query(cls, db: QueryBuilder):
        return sql("SELECT date_trunc('day'::text, created_at) AS day,\n    count(*) AS posts\n   FROM posts\n  GROUP BY (date_trunc('day'::text, created_at))")

    @classmethod
    @override
    def config(cls) -> list[Constraint]:
        return [
            unique_index('daily_posts_day_key').on(cls.day),
        ]


@final
class PostCounts(View, name='post_counts'):
    author_id = integer()
    posts = bigint()

    @classmethod
    @override
    def query(cls, db: QueryBuilder):
        return sql('SELECT author_id,\n    count(*) AS posts\n   FROM posts\n  GROUP BY author_id')

    @classmethod
    @override
    def config(cls) -> list[Constraint]:
        return [
            grant(['select'], to=['docs_reporting']),
        ]


trg_posts_posts_touch = trigger(
    'posts_touch',
    on=Posts,
    execute=fn_touch_updated_at,
    timing='before',
    events=['update'],
)
```

Run `mizzle generate` right after: it prints `No schema changes.`

It reads tables, columns, enums (also in other schemas), indexes, views and materialized
views, row-level security, policies, grants, functions, triggers and extensions. Each
foreign key becomes a `one()` relation and its reverse a `many()` (or `one()` when the key
is unique). Roles come out as names; wrap them in `role(...)` if migrations should manage
them. View queries and policy conditions come back as `sql(...)`, the way PostgreSQL
prints them. Review the file before you commit it.

`schema_filter = ["public", "auth"]` in `mizzle.toml` sets which schemas `pull` and `push`
read.
