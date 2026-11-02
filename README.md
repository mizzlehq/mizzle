# mizzle

Drizzle-style, fully typed, async ORM for Python and PostgreSQL.

- **Schema in Python classes.** Result rows are typed for pyright and mypy, with no code
  generation.
- **Two query styles:** a SQL-like builder (`select`, `insert`, `update`, `delete`, joins,
  CTEs, upserts) and relational queries that load nested data in one SQL statement.
- **Migration kit:** `generate`, `migrate`, `push`, `pull`, `check`, `drop`, `typegen`.
- **Studio:** browse and edit data in your browser (`mizzle studio`).
- **Safe by default:** values are always bind parameters, `update`/`delete` refuse to run
  without `.where()`, migrations are locked, transactional and tamper-checked, and errors
  never include row values.

Built for PostgreSQL first; other SQL databases plug in through the dialect layer
(`src/mizzle/dialects/`). Design: `docs/superpowers/specs/2026-11-03-mizzle-design.md`.

## Install

```bash
pip install mizzle            # ORM + migration CLI
pip install "mizzle[studio]"  # + Studio
```

## Schema

```python
# app/db/schema.py
import enum
from mizzle import Table, index, many, one, sql
from mizzle.pg import integer, pg_enum, serial, text, timestamp, varchar


class Role(enum.StrEnum):
    ADMIN = "admin"
    USER = "user"


class Users(Table, name="users"):
    id = serial().primary_key()  # Column[int]
    email = varchar(255).not_null().unique()  # Column[str]
    bio = text()  # Column[str | None]
    role = pg_enum(Role).not_null().default(Role.USER)  # Column[Role]
    created_at = timestamp(tz=True).not_null().default(sql("now()"))

    posts = many(lambda: Posts)


class Posts(Table, name="posts"):
    id = serial().primary_key()
    author_id = integer().not_null().references(lambda: Users.id, on_delete="cascade")
    title = text().not_null()

    author = one(lambda: Users, fields=lambda: [Posts.author_id], references=lambda: [Users.id])

    @classmethod
    def config(cls):
        return [index("posts_author_idx").on(cls.author_id)]
```

## Queries

```python
from mizzle import Database, desc, eq

async with await Database.connect("postgresql://...", schema=[Users, Posts]) as db:
    [ann] = await db.insert(Users).values(email="ann@x.io").returning()  # Users
    admins = await db.select().from_(Users).where(eq(Users.role, Role.ADMIN))  # list[Users]
    pairs = await (
        db.select(Users, Posts).from_(Users).left_join(Posts, eq(Posts.author_id, Users.id))
    )  # list[tuple[Users, Posts | None]]

    # Relational: one SQL statement, typed nested results.
    users = await db.query(Users).find_many(
        with_={"posts": {"order_by": desc(Posts.id), "limit": 5}},
    )
    users[0].posts[0].title  # str

    async with db.transaction() as tx:
        await tx.update(Users).set(bio="hi").where(eq(Users.id, ann.id))
```

Raw SQL: `sql(t"select * from users where id = {user_id}")` (Python 3.14+) or
`sql("... where id = {}", user_id)`. Values become bind parameters. **Never build the text
passed to `sql()` from user input** (for example with an f-string).

## Migrations

`mizzle.toml` in your project root (or `[tool.mizzle]` in `pyproject.toml`):

```toml
schema = "app.db.schema"   # module(s) with your tables
out = "migrations"         # commit this folder
url_env = "DATABASE_URL"   # the URL is read from the environment
```

```bash
mizzle generate            # write migrations/0001_.../migration.sql + snapshot.json
mizzle migrate             # apply pending migrations (safe with many app servers)
mizzle push                # dev: sync schema straight to the database
mizzle pull                # write a schema module from an existing database
mizzle check               # find broken or clashing migrations
mizzle drop                # delete the latest, unapplied migration
mizzle typegen             # TypedDicts for insert/update values
mizzle studio              # open Studio
```

Renames: on a terminal, `generate` asks whether a new column is a rename. In CI pass
`--rename users.name=full_name`. Changes that can lose data need confirmation or
`--allow-data-loss`. From code: `await mizzle.kit.migrate(db, "migrations")`.

## Development

```bash
docker compose up -d
export MIZZLE_TEST_URL=postgresql://mizzle:mizzle@localhost:54329/mizzle
uv run pytest            # integration tests skip when MIZZLE_TEST_URL is unset
uv run pyright && uv run mypy
uv run ruff check && uv run ruff format --check

cd studio-ui && npm ci && npm run build   # builds the UI into src/mizzle/studio/static
npm run dev                               # UI dev server; proxies /api to `mizzle studio`
```

Releases: push a `v*` tag. `.github/workflows/release.yml` builds the UI, packages it into
the wheel, and publishes to PyPI with trusted publishing.
