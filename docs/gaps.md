# Known gaps

What mizzle does not do, and what to do instead. mizzle manages tables and what hangs on
them, and leaves the rest to SQL you write.

## Only PostgreSQL, only async

mizzle talks to PostgreSQL 14 to 18 through asyncpg. There is no MySQL or SQLite dialect,
and no synchronous API. From synchronous code, run the query in an event loop:

```python
import asyncio


async def count_users() -> int:
    async with await Database.connect(url, schema=[Users]) as db:
        return len(await db.select().from_(Users))


total = asyncio.run(count_users())
```

## Database objects mizzle does not manage

Functions, triggers, domains, composite types, extensions, exclusion constraints,
partitions and grants are not part of the schema API. `generate`, `push` and `pull` never
create, change or drop them, and never report them as drift. Write them in a custom
migration:

```bash
mizzle generate --custom --name functions
```

```sql
-- Custom migration. Separate statements with: -- mizzle:breakpoint
CREATE EXTENSION IF NOT EXISTS pg_trgm;
-- mizzle:breakpoint
CREATE OR REPLACE FUNCTION touch_updated_at() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN NEW.updated_at = now(); RETURN NEW; END
$$;
-- mizzle:breakpoint
CREATE TRIGGER posts_touch BEFORE UPDATE ON posts
  FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
-- mizzle:breakpoint
GRANT SELECT ON posts TO reporting;
```

Generate it before the migration whose tables need it (an extension, a function an index
uses), and after the tables a trigger or grant is on. For a value set when a row changes,
`.on_update_now()` on the column needs no trigger (see [Schema](guide/schema.md)).

### Columns of a domain, composite or extension type

Create the type in a custom migration, then declare the column with `custom("type")`:

```sql
CREATE DOMAIN posint AS integer CHECK (VALUE > 0);
```

```python
from mizzle.pg import custom


@final
class Orders(Table, name="orders"):
    id = serial().primary_key()
    qty = custom("posint", py_type=int)
```

See [Columns of other types](guide/schema.md#columns-of-other-types) for `decode=` and
`encode=`.

### A partitioned table

Create the table and its partitions in a custom migration that records the schema, and
declare the parent as a plain table. Queries go through the parent; the partitions are
left alone.

```python
@final
class Events(Table, name="events"):
    id = bigint().not_null()
    at = timestamp(tz=True).not_null()

    @classmethod
    @override
    def config(cls) -> list[Constraint]:
        return [primary_key(cls.id, cls.at)]
```

```bash
mizzle generate --custom --record-schema --name events
```

```sql
-- Custom migration. Separate statements with: -- mizzle:breakpoint
CREATE TABLE events (id bigint NOT NULL, at timestamptz NOT NULL,
  CONSTRAINT events_pkey PRIMARY KEY (id, at)) PARTITION BY RANGE (at);
-- mizzle:breakpoint
CREATE TABLE events_2026 PARTITION OF events FOR VALUES FROM ('2026-01-01') TO ('2027-01-01');
```

## No down migrations

Migrations only go forward. To undo one, generate a new migration that
changes the schema back. On a development database, `mizzle reset` drops what mizzle
manages and runs every migration again.

## Values of custom() columns

- **No operators for ranges, geometry and other extension types.** `contains`,
  `contained_by` and `overlaps` work on arrays and jsonb. For other types, write the
  operator with `sql()`:

    ```python
    staying = await db.select().from_(Places).where(sql("{} @> {}::int", Places.nights, 3))
    ```

- **A tuple does not compare with a composite.** asyncpg cannot send an anonymous record.
  Write the type:

    ```python
    rows = await db.select().from_(Prices).where(
        eq(Prices.pair, sql("{}::price_pair", (Decimal("1.5"), day)))
    )
    ```

- **Nested rows hold the JSON form.** In a relational query (`with_=`), a custom column is
  the value's JSON form (`str`, `int`, `Decimal`, `dict`...), not what a plain select
  gives (`Range`, `Record`...). `decode=` gets that form too. Read such a column with a
  plain select when you need the driver's value.

## Savepoints belong to one task

Inside a savepoint (`tx.transaction()`), only the task that opened it may run statements:
`asyncio.gather` of statements inside a savepoint raises `QueryError`. Run them one after
another, or gather on the transaction itself, outside the savepoint.

## Studio

- `like` filters compare the text form, so on a `citext` column they are case-sensitive.
  Use `ilike`.
- Read-only Studio refuses to start as a role that can read server files, run programs or
  signal other backends (a superuser, `pg_read_server_files`...). Connect it as a plain
  role with `SELECT` rights.
- Composite columns are shown read-only.

## Less common server settings

- With `standard_conforming_strings = off` (off by default since PostgreSQL 9.1), a
  backslash inside an `N'...'` string, or a string continued on the next line, is read as
  an escape. Write such defaults as `E'...'` strings.
- Before PostgreSQL 17, changing a generated column's expression drops the column and adds
  it again, and re-creates the views that use it.
- A policy that names its own table with its schema (`app.items.owner`) is compared as
  written while another session holds a lock on the table, so `doctor` may report it once
  as changed. Write the column without the table name (`owner`).

## Schemas other roles can write to

mizzle's own SQL names PostgreSQL's built-ins with their schema. Your queries, and the
asyncpg driver's type lookups, use the search path. See
[Production](production.md#schemas-other-roles-can-write-to).
