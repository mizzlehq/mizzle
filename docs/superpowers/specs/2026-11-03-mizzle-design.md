# mizzle — Design Spec

Date: 2026-11-03
Status: Approved in conversation, pending written review

## 1. Goal

`mizzle` is a Drizzle-style, all-in-one ORM for Python, built for production use:

- Declare the schema in Python classes.
- Query with a typed SQL-like builder and a typed relational query API.
- Manage the database with a migration CLI (`generate`, `migrate`, `push`, `pull`, `check`, `drop`, `studio`, `typegen`).

### Decisions

| Topic | Decision |
|---|---|
| Databases | PostgreSQL only in v1. All database-specific code lives behind a `Dialect` interface so other SQL databases can be added without changing core or kit. |
| Concurrency | Async only. |
| Driver | `asyncpg`. |
| Query styles | Both in v1: SQL-like builder and relational queries. |
| CLI | All Drizzle Kit commands, including `studio`. |
| Typing | Result rows are statically typed with no code generation. Insert/update payloads are runtime-checked; static checking via optional `mizzle typegen` stub. |
| Core approach | Own SQL expression tree + per-dialect compiler (not built on SQLAlchemy). |
| Python | 3.12+. t-string support for raw SQL on 3.14+. |

### Non-goals (v1)

- Sync API.
- Databases other than PostgreSQL.
- Active-record behavior (`user.save()`), identity map, unit of work, lazy loading.

## 2. Architecture

Four layers. A layer only imports from layers below it.

```
mizzle/
  __init__.py           # public API re-exports
  core/                 # dialect-free
    schema/             # Table, Column, constraints, indexes, enums, relations
    sql/                # expression tree: nodes, operators, raw sql()
    query/              # select/insert/update/delete builders, relational query
    compiler.py         # base Compiler: visits tree, emits (sql_text, params)
    errors.py
  dialects/
    base.py             # Dialect interface
    postgres/
      types.py          # jsonb, uuid, arrays, enums, timestamptz, numeric...
      compiler.py       # $N params, RETURNING, ON CONFLICT, JSON aggregation
      ddl.py            # migration steps -> CREATE/ALTER/DROP SQL
      introspect.py     # live DB -> snapshot (pull, push)
      driver.py         # asyncpg pool, transactions, error mapping
  pg.py                 # public import path for Postgres column types
  db.py                 # Database: connect, transactions, select/insert/..., query()
  kit/
    config.py           # [tool.mizzle] loader
    snapshot.py         # schema -> snapshot JSON
    diff.py             # snapshot A vs B -> ordered list of migration steps
    journal.py          # migrations/meta/journal.json
    commands/           # generate, migrate, push, pull, check, drop, studio, typegen
    cli.py              # argparse entry point: `mizzle`
  studio/               # optional extra
tests/
  unit/
  integration/
  typing/
```

### Dialect interface

`dialects/base.py` defines `Dialect` with five parts:

1. `compiler` — turns the SQL tree into `(sql_text, params)`.
2. `type_map` — maps core column types to database types and Python codecs.
3. `ddl` — renders database-neutral migration steps to SQL, and marks which statements cannot run in a transaction.
4. `introspect(conn) -> Snapshot` — reads a live database.
5. `driver` — pool, connections, transactions, and mapping of driver errors to mizzle errors.

`core/` and `kit/` only use this interface. They never import from `dialects/postgres/`. A new database means a new `dialects/<name>/` package implementing the same five parts.

### Data flow

1. User code builds a query. This creates a tree of immutable node objects. No SQL exists yet.
2. `await query` asks the dialect compiler for `(sql_text, params)`.
3. The driver executes it.
4. Rows are decoded into table instances, tuples, or dicts, depending on the select shape.

Builders are immutable: each method returns a new builder, so partial queries can be reused safely.

## 3. Schema API

```python
import enum
from mizzle import Table, one, many, index, unique, sql
from mizzle.pg import serial, text, varchar, integer, timestamp, jsonb, pg_enum

class Role(enum.StrEnum):
    ADMIN = "admin"
    USER = "user"

class Users(Table, name="users"):
    id = serial().primary_key()                        # Column[int]
    email = varchar(255).not_null().unique()           # Column[str]
    bio = text()                                       # Column[str | None]
    role = pg_enum(Role).not_null().default(Role.USER) # Column[Role]
    created_at = timestamp(tz=True).not_null().default(sql("now()"))

    posts = many(lambda: Posts)

class Posts(Table, name="posts"):
    id = serial().primary_key()
    author_id = integer().not_null().references(lambda: Users.id, on_delete="cascade")
    title = text().not_null()
    meta = jsonb[dict[str, str]]()

    author = one(lambda: Users, fields=[author_id], references=[Users.id])

    @classmethod
    def config(cls):
        return [index("posts_author_idx").on(cls.author_id)]
```

### Rules

- **Typing mechanism.** Columns are descriptors with overloaded `__get__`. Access on the class (`Users.id`) returns `Column[int]` for building queries. Access on an instance (`user.id`) returns `int`. Result rows are instances of the table class. No code generation is needed for row types.
- **Nullability.** Columns are nullable by default. `.not_null()` and `.primary_key()` return a column type without `None`.
- **Names.** Attribute name is the column name. Override with a positional name: `text("email_address")`. A `casing="snake_case"` option on `Database` and in kit config converts attribute names.
- **Table name.** Given by `name=`. If omitted, the class name converted to snake_case.
- **Postgres schema.** `class Users(Table, name="users", schema="auth")`.
- **Forward references.** `lambda:` in `.references()`, `one()`, and `many()`.
- **Relations** (`one`, `many`) only drive relational queries. Database foreign keys come only from `.references()`. `many()` finds its matching `one()` on the other table; if that is ambiguous, `relation_name=` on both sides is required (raises `SchemaError` otherwise).
- **Defaults.** `.default(value)`, `.default_fn(callable)` (Python side, per insert), `.default(sql("..."))` (database side).
- **Constraints.** Column level: `.primary_key()`, `.unique()`, `.references()`, `.check(sql(...))`. Table level in `config()`: `index`, `unique_index`, `primary_key` (composite), `foreign_key` (composite), `check`.
- **`jsonb[T]`.** `T` is a static type hint only. No runtime validation.
- **Generic vs dialect types.** `int`, `text`, `bool`, `float`, `numeric`, `date`, `timestamp` live in core. `mizzle.pg` re-exports them and adds Postgres-only types (`jsonb`, `uuid`, arrays, `pg_enum`, `inet`, `interval`...).
- **Validation.** Schema errors (duplicate column names, relation with no matching foreign key, bad reference) raise `SchemaError` when the schema is registered with `Database` or loaded by kit, not at first query.

## 4. Query API

### Connection

```python
from mizzle import Database
db = await Database.connect(url, schema=[Users, Posts], pool_size=10)
await db.close()
```

`Database` is also an async context manager.

### Operators

Functions, not overloaded Python operators: `eq, ne, gt, gte, lt, lte, and_, or_, not_, in_, not_in, like, ilike, is_null, is_not_null, between, exists`, plus `asc, desc, count, sum_, avg, min_, max_`. Overloading `==` on columns is rejected because it breaks hashing and `in` checks.

### SQL-like builder

```python
await db.select().from_(Users).where(eq(Users.role, Role.ADMIN))            # list[Users]
await db.select(Users.id, Users.email).from_(Users)                         # list[tuple[int, str]]
await db.select({"id": Users.id, "n": count()}).from_(Users).group_by(...)  # list[dict[str, Any]]
await (db.select(Users, Posts).from_(Users)
       .left_join(Posts, eq(Posts.author_id, Users.id)))                    # list[tuple[Users, Posts | None]]

await db.insert(Users).values(email="a@b.c").returning()                    # list[Users]
await db.insert(Users).values([{...}, {...}])                               # bulk
await db.insert(Users).values(...).on_conflict_do_update(target=Users.email, set_={...})
await db.insert(Users).values(...).on_conflict_do_nothing()
await db.update(Users).set(bio="hi").where(eq(Users.id, 1)).returning()
await db.delete(Users).where(lt(Users.created_at, cutoff))
```

Select also supports `order_by`, `limit`, `offset`, `group_by`, `having`, `distinct`, `for_update`, subqueries, and CTEs (`db.with_(...)`).

- Tuple-select typing uses overloads up to 10 columns. More than 10 returns `tuple[Any, ...]`.
- `left_join`, `right_join`, `full_join` change the joined table's tuple slot to `| None` using self-typed overloads.
- `update` and `delete` without `.where()` raise `QueryError` unless `.all_rows()` is called. This prevents accidental full-table writes.

### Relational queries

```python
users = await db.query(Users).find_many(
    where=eq(Users.role, Role.ADMIN),
    with_={"posts": {"with_": {"author": True}, "limit": 5}},
    columns=..., order_by=desc(Users.created_at), limit=20, offset=0,
)
user = await db.query(Users).find_first(where=eq(Users.id, 1))  # Users | None
```

- One SQL statement per call, using lateral subqueries with `json_agg` / `json_build_object`, as Drizzle does.
- Loaded relations are set on the returned instances (`user.posts: list[Posts]`, `post.author: Users`).
- Accessing a relation that was not loaded raises `RelationNotLoaded`. There is no lazy loading and no hidden extra query.

### Raw SQL

- Python 3.14+: `sql(t"select * from users where id = {uid}")` using t-strings.
- Python 3.12/3.13: `sql("select * from users where id = {}", uid)`.
- Interpolated values always become bind parameters. Identifiers use `sql.identifier("name")`. There is no API that pastes a value directly into SQL text.
- `await db.execute(sql(...))` returns `list[dict[str, Any]]`.

### Transactions

```python
async with db.transaction(isolation="serializable") as tx:
    await tx.insert(Users).values(...)
    async with tx.transaction():   # savepoint
        ...
```

Exiting with an exception rolls back. `tx` has the same query API as `db`.

### Other v1 features

- Prepared statements: `q = db.select().from_(Users).where(eq(Users.id, placeholder("id"))).prepare(); await q.execute(id=1)`.
- `.to_sql()` returns `(sql_text, params)` without running the query.
- Logger hook: `Database(..., logger=callable)` receives SQL text and duration. It never receives parameter values unless `log_params=True` is set.

### Insert/update typing

Values are always validated at runtime before the database call: unknown columns, missing required columns (not null, no default), and Python type mismatch raise `QueryError`. Static checking of insert/update values is provided by `mizzle typegen`, which writes a `.pyi` stub next to the schema module with `UsersInsert` / `UsersUpdate` TypedDicts. `mizzle generate` refreshes the stub if it already exists.

## 5. Migration kit

### Config

```toml
[tool.mizzle]
schema = "app.db.schema"        # one module or a list
out = "migrations"
dialect = "postgresql"
url_env = "DATABASE_URL"
casing = "snake_case"           # optional
schema_filter = ["public"]      # optional, Postgres schemas to manage
```

Database URLs are read from the environment variable only. The config file never holds credentials.

### Files

```
migrations/
  0001_create_users/
    migration.sql
    snapshot.json
  0002_add_posts/
    migration.sql
    snapshot.json
  meta/journal.json
```

- `snapshot.json` is the full schema state after that migration. It has a `version`, a `dialect`, a `prev_id`, and an `id` (UUID).
- `journal.json` lists migrations in order with name, SHA-256 hash of `migration.sql`, and statement breakpoint info.
- `migration.sql` separates statements with `-- mizzle:breakpoint`. Statements that cannot run in a transaction are preceded by `-- mizzle:no-transaction`.

### Diff engine

`kit/diff.py` takes two snapshots and returns an ordered list of database-neutral steps: `CreateSchema, CreateEnum, AlterEnumAddValue, CreateTable, DropTable, RenameTable, AddColumn, DropColumn, RenameColumn, AlterColumnType, AlterColumnNullability, AlterColumnDefault, CreateIndex, DropIndex, AddConstraint, DropConstraint`, and their reverses. Ordering respects dependencies (enums before tables, tables before foreign keys, drop foreign keys before drop tables).

Renames cannot be detected from snapshots alone. When a table or column disappears and another appears, `generate` asks: rename or drop + create. Non-interactive runs require `--rename old=new` flags; without them, ambiguous changes stop with an error instead of guessing.

### Commands

| Command | Behavior |
|---|---|
| `mizzle generate [--name N] [--custom] [--rename a=b]` | Diff schema vs last snapshot, write a new migration folder. `--custom` writes an empty `migration.sql` with the current snapshot. |
| `mizzle migrate` | Apply pending migrations. |
| `mizzle push [--force]` | Introspect live DB, diff against schema, apply directly. Development only. Prints planned SQL first. |
| `mizzle pull` | Introspect live DB, write a schema module and an initial migration snapshot. |
| `mizzle check` | Validate journal/snapshot consistency, hashes, and detect forks (two migrations with the same `prev_id`). |
| `mizzle drop` | Delete the latest migration folder and journal entry. Refuses if `DATABASE_URL` is set and that migration is applied there. |
| `mizzle studio` | Start Studio. |
| `mizzle typegen` | Write insert/update `.pyi` stubs. |

Programmatic: `await mizzle.kit.migrate(db, "migrations")`.

### Production safety

- **Locking.** `migrate` takes `pg_advisory_lock` with a fixed key before reading state. Concurrent runners wait (timeout configurable, default 60 s, then `MigrationLockTimeout`).
- **Transactions.** Each migration runs in its own transaction. `no-transaction` statements run alone, outside it, and the migration is recorded only after all statements succeed.
- **Tracking table.** `__mizzle_migrations(id, name, hash, applied_at)` in a configurable schema (default `public`).
- **Tamper check.** If an applied migration's file hash differs from the stored hash, `migrate` stops with `MigrationTampered`.
- **Data-loss guard.** `DropTable`, `DropColumn`, narrowing type changes, and `SET NOT NULL` on a column with nulls need interactive confirmation, or `--allow-data-loss` in non-interactive runs. Otherwise `DataLossBlocked`.

## 6. Studio

- Optional extra: `pip install mizzle[studio]` (adds `starlette`, `uvicorn`).
- Features: list tables, browse rows with filters and paging, edit/insert/delete rows, run SQL.
- Binds to `127.0.0.1` only. Generates a random token on start and requires it on every request. The launch URL contains the token.
- Frontend: static HTML/JS served by the Python app. No Node build step required to install.

## 7. Errors

```
MizzleError
├── SchemaError
├── QueryError
├── RelationNotLoaded
├── DatabaseError                 # wraps driver error, keeps .original
│   ├── UniqueViolation           # .constraint
│   ├── ForeignKeyViolation       # .constraint
│   ├── NotNullViolation          # .column
│   ├── CheckViolation            # .constraint
│   ├── SerializationFailure      # retryable
│   └── ConnectionError
└── MigrationError
    ├── MigrationLockTimeout
    ├── MigrationTampered
    └── DataLossBlocked
```

Error messages include SQL text, never parameter values.

## 8. Testing

- **Unit** (`tests/unit`): exact SQL + params for every builder method and DDL step. No database.
- **Integration** (`tests/integration`): real PostgreSQL 14–18 in Docker, run as a CI matrix.
- **Round-trip:** schema → `generate` → apply → `pull`/introspect → diff must be empty. Runs over a set of fixture schemas covering every column type and constraint.
- **Type tests** (`tests/typing`): files with `assert_type(...)` checked by `pyright --strict` and `mypy --strict` in CI.
- **Property tests:** `hypothesis` generates schema pairs; applying the diff to schema A must yield schema B.
- **Concurrency:** parallel `migrate` runners apply each migration exactly once.

## 9. Tooling and packaging

- `uv` for project and dependency management; `pyproject.toml` with hatchling build backend.
- `ruff` lint + format, `pyright` strict, `mypy` strict, `pytest` + `pytest-asyncio`, coverage gate 90%.
- Runtime dependencies: `asyncpg` only. CLI uses stdlib `argparse`. Studio extra adds `starlette`, `uvicorn`.
- Console script: `mizzle = mizzle.kit.cli:main`.
- Docs: `mkdocs-material`. CI: GitHub Actions.
- `py.typed` marker shipped.

## 10. Build order

Each phase gets its own implementation plan.

1. **Core:** schema, SQL tree, base + Postgres compiler, select/insert/update/delete, raw SQL, driver, transactions, errors, type tests.
2. **Relational queries.**
3. **Kit:** config, snapshot, diff, DDL, introspection, `generate`, `migrate`, `push`, `pull`, `check`, `drop`, `typegen`.
4. **Studio.**
