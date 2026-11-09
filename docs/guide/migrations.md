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

## Enum values

Change the Python enum and run `generate`:

- **A new value** is added in place (`ALTER TYPE ... ADD VALUE`).
- **A renamed value** keeps every row: `--rename status.done=finished` writes
  `ALTER TYPE ... RENAME VALUE`. As for columns, mizzle asks on a terminal and stops in CI.
- **A removed or reordered value**: PostgreSQL cannot drop or move one, so mizzle creates
  the type again, converts its columns, and drops the old type. Views, policies, checks and
  indexes that use those columns are created again around it. A removal needs
  `--allow-data-loss`; a row that still holds the removed value stops the migration, and
  nothing changes. Move those rows first (`UPDATE tasks SET status = 'draft' WHERE ...`).

Something mizzle does not manage that uses the enum (a table's column, a view, a domain, a
function) stops the migration too: the old type cannot be dropped, and nothing changes.
Convert it to `text` in a custom migration before, and back after.

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

Each migration runs in one transaction, so do not write `BEGIN`, `COMMIT` or `ROLLBACK`:
`migrate` refuses a migration that does. A statement that cannot run in a transaction
(`CREATE INDEX CONCURRENTLY`) goes in its own chunk, marked `-- mizzle:no-transaction`:

```sql
-- mizzle:breakpoint
-- mizzle:no-transaction
CREATE INDEX CONCURRENTLY posts_body_idx ON posts (body);
```

Such a chunk cannot be rolled back if a later statement fails, so keep it last.

`check` does not warn about edits to custom migrations. `mizzle drop` deletes the latest
migration if it has not been applied yet. A custom migration keeps the schema snapshot as it
was: schema changes still go into the next generated migration, so a custom migration can
run before the tables that need what it creates.

When your own SQL makes a schema change, change the schema too and record it in the custom
migration, so `generate` does not write it again:

```bash
mizzle generate --custom --record-schema --name split_names
```

After `migrate`, the next `generate` finds no changes.

## Not managed by mizzle

Functions, triggers, domains, composite types, extensions, exclusion constraints,
partitions and grants are left alone by `generate`, `push` and `pull`: they are never
created, changed, dropped or reported as drift (`pull` names them in a warning, and reads a
partitioned table as a plain one). Write them in a custom migration:

```bash
mizzle generate --custom --name extensions
```

```sql
-- Custom migration. Separate statements with: -- mizzle:breakpoint
CREATE EXTENSION IF NOT EXISTS pg_trgm;
-- mizzle:breakpoint
CREATE OR REPLACE FUNCTION touch_updated_at() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN NEW.updated_at = now(); RETURN NEW; END
$$;
```

Generate it before the migration whose tables need it (an index using `gin_trgm_ops`), and
put triggers and grants in one after the tables they are on. A column of another type (a
domain, a composite, `citext`) is `custom("type")`; see
[Schema](schema.md#columns-of-other-types).

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

Migrations manage more than tables: enums, views, row-level security, policies and roles.
mizzle collects module-level `role` values, like `Table` classes. Table-level objects
(indexes, row-level security, policies) go in `config()`. Functions, triggers, extensions
and grants are not managed: see [Not managed by mizzle](#not-managed-by-mizzle).

```python
from typing import final, override

from mizzle import (
    Constraint,
    QueryBuilder,
    Table,
    View,
    count,
    eq,
    index,
    many,
    one,
    policy,
    rls,
    role,
    sql,
    unique_index,
)
from mizzle.pg import bigint, integer, serial, text, timestamp, varchar

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
            # gin_trgm_ops needs pg_trgm: a custom migration creates it before this one.
            index("users_name_trgm_idx").using("gin", "gin_trgm_ops").on(cls.full_name),
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

```

`mizzle generate` writes, besides the tables:

```sql
-- create role docs_app_user
-- create index users_name_trgm_idx
-- create view post_counts
-- create materialized view daily_posts
-- create index daily_posts_day_key
-- enable row level security on posts
-- create policy posts_own on posts
```

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

A role used in a `policy` is created by the migration, so you do not need to
assign it to a module-level name, but you can.

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

from mizzle import Constraint, QueryBuilder, Table, View, index, many, one, policy, rls, sql, unique_index
from mizzle.pg import bigint, integer, serial, text, timestamp, varchar


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
```

Run `mizzle generate` right after: it prints `No schema changes.`

It reads tables, columns, enums (also in other schemas), indexes, views and materialized
views, row-level security and policies, and warns about what it leaves out (see
[Not managed by mizzle](#not-managed-by-mizzle)). A column of a type mizzle has no column
type for (a domain, a range, `citext`...) is written as `custom("type")`. Each
foreign key becomes a `one()` relation and its reverse a `many()` (or `one()` when the key
is unique). Roles come out as names; wrap them in `role(...)` if migrations should manage
them. View queries and policy conditions come back as `sql(...)`, the way PostgreSQL
prints them. Review the file before you commit it.

`schema_filter = ["public", "auth"]` in `mizzle.toml` sets which schemas `pull` and `push`
read.

### Following a schema another app owns

In microservices, one app often owns the schema and applies its migrations. Other apps on
the same database only follow it. In a follower, run:

```bash
mizzle pull --update                 # after the owner applied a migration
mizzle pull --update --name add_tags # name the record
```

`--update`:

- rewrites your schema module from the database (it is generated: do not edit it by hand),
- writes a new migration folder as a record of what changed. Every statement in it is
  commented out, because the database already has the changes, so `migrate` runs nothing,
- prints `Up to date with the database.` and writes nothing when nothing changed.

The first `pull --update` in a new project writes the schema module and a baseline. A
follower does not run `generate` or `migrate`: the owner's migrations are the ones applied.
