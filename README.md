# mizzle

Drizzle-style, fully typed, async ORM for Python and PostgreSQL.

- Schema in Python classes. Result rows are typed for pyright and mypy, with no code generation.
- SQL-like query builder: `select`, `insert`, `update`, `delete`, joins, CTEs, upserts.
- Safe by default: values are always bind parameters, `update`/`delete` refuse to run
  without `.where()`, and errors never include parameter values.

Status: phase 1 (core) done. Relational queries, the migration CLI and Studio come next.
Design: `docs/superpowers/specs/2026-11-03-mizzle-design.md`.

## Example

```python
import enum
from mizzle import Database, Table, eq, desc, sql
from mizzle.pg import serial, varchar, text, integer, timestamp, pg_enum


class Role(enum.StrEnum):
    ADMIN = "admin"
    USER = "user"


class Users(Table, name="users"):
    id = serial().primary_key()                         # Column[int]
    email = varchar(255).not_null().unique()            # Column[str]
    bio = text()                                        # Column[str | None]
    role = pg_enum(Role).not_null().default(Role.USER)  # Column[Role]
    created_at = timestamp(tz=True).not_null().default(sql("now()"))


class Posts(Table, name="posts"):
    id = serial().primary_key()
    author_id = integer().not_null().references(lambda: Users.id, on_delete="cascade")
    title = text().not_null()


async def main() -> None:
    async with await Database.connect("postgresql://...", schema=[Users, Posts]) as db:
        [ann] = await db.insert(Users).values(email="ann@x.io").returning()   # Users
        admins = await db.select().from_(Users).where(eq(Users.role, Role.ADMIN))  # list[Users]
        pairs = await (
            db.select(Users, Posts)
            .from_(Users)
            .left_join(Posts, eq(Posts.author_id, Users.id))
            .order_by(desc(Users.created_at))
        )  # list[tuple[Users, Posts | None]]

        async with db.transaction() as tx:
            await tx.update(Users).set(bio="hi").where(eq(Users.id, ann.id))
```

## Raw SQL

```python
await db.execute(sql(t"select * from users where id = {user_id}"))   # Python 3.14+
await db.execute(sql("select * from users where id = {}", user_id))  # any version
```

Values become bind parameters. **Never build the text passed to `sql()` from user input**
(for example with an f-string): text is sent to the database as written.

## Development

```bash
docker compose up -d
export MIZZLE_TEST_URL=postgresql://mizzle:mizzle@localhost:54329/mizzle
uv run pytest            # integration tests skip when MIZZLE_TEST_URL is unset
uv run pyright && uv run mypy
uv run ruff check && uv run ruff format --check
```
