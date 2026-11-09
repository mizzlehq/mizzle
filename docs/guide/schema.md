# Schema

A table is a class. Each column is a class attribute built from `mizzle.pg`. The class is
your schema for migrations, and its instances are your rows.

```python
from typing import final

from mizzle import Table
from mizzle.pg import serial, text, varchar


@final
class Users(Table, name="users"):
    id = serial().primary_key()
    email = varchar(255).not_null().unique()
    bio = text()
```

- `name=` is the table name. Without it, mizzle uses the class name in snake_case.
- Mark tables `@final`: a table cannot inherit from another table.
- `Users.email` is the column (use it in queries). `user.email` on a row is the value: a
  `str` here, and `user.bio` is a `str | None`.
- A column's attribute name is its database name. Pass a name to rename it in the
  database only: `text("full_name")`. `Database.connect(..., casing="snake_case")` maps
  `createdAt` to `created_at` for every column.

## Column types

```python
from typing import Any, final

from mizzle import Table
from mizzle.pg import (
    array, bigint, bigserial, boolean, bytea, date, double_precision, inet, integer,
    interval, json, jsonb, numeric, real, smallint, smallserial, text, time, timestamp,
    uuid, varchar,
)


@final
class Everything(Table, name="everything"):
    id = bigserial().primary_key()
    small = smallint()
    whole = integer()
    big = bigint()
    counter = smallserial()
    price = numeric(10, 2)
    ratio = real()
    score = double_precision()
    name = text()
    code = varchar(8)
    active = boolean()
    born = date()
    seen_at = timestamp(tz=True)
    local_at = timestamp(precision=3)
    opens = time()
    timeout = interval()
    key = uuid()
    blob = bytea()
    ip = inet()
    tags = array(text())
    settings = jsonb[dict[str, Any]]()
    raw = json[list[int]]()
```

| Function | PostgreSQL type | Python type |
|---|---|---|
| `smallint()`, `integer()`, `bigint()` | `smallint`, `integer`, `bigint` | `int` |
| `smallserial()`, `serial()`, `bigserial()` | auto-increment integers, `NOT NULL` | `int` |
| `numeric(precision, scale)` | `numeric(p, s)` | `Decimal` |
| `real()`, `double_precision()` | `real`, `double precision` | `float` |
| `text()`, `varchar(n)` | `text`, `varchar(n)` | `str` |
| `boolean()` | `boolean` | `bool` |
| `date()` | `date` | `datetime.date` |
| `timestamp(tz=False, precision=None)` | `timestamp`, `timestamptz` | `datetime.datetime` |
| `time(tz=False, precision=None)` | `time`, `timetz` | `datetime.time` |
| `interval()` | `interval` | `datetime.timedelta` |
| `uuid()` | `uuid` | `uuid.UUID` |
| `bytea()` | `bytea` | `bytes` |
| `inet()` | `inet` | `IPv4Address`, `IPv6Address`, ... (`mizzle.pg.IPAddress`) |
| `array(item)` | `item[]` | `list[item type]` |
| `jsonb[T]()`, `json[T]()` | `jsonb`, `json` | `T` (a hint for your editor; not checked) |
| `pg_enum(EnumClass)` | an enum type | your `enum.Enum` class |
| `vector(n)`, `halfvec(n)` | pgvector `vector(n)`, `halfvec(n)` | `list[float]` |
| `sparsevec(n)` | pgvector `sparsevec(n)` | `dict[int, float]` |

- Every column is nullable until you call `.not_null()`, so its value type is `T | None`.
  `serial` columns are `NOT NULL` already.
- `timestamp(tz=True)` values always come back as aware datetimes in UTC.
- `precision=` keeps 0 to 6 digits of fractional seconds (default 6).
- The pgvector types need the `vector` extension. See [Extensions](extensions.md).
- Other PostgreSQL types (ranges, geometry shapes other than `point` and `line`, `hstore`,
  `citext`, `tsvector`, ...) are not built in; use `custom("type")`.

## Nullability, defaults and on_update

```python
import datetime as dt
import uuid as uuid_lib
from typing import final

from mizzle import Table, sql
from mizzle.pg import date, integer, serial, text, timestamp, uuid


@final
class Articles(Table, name="articles"):
    id = serial().primary_key()
    title = text().not_null()                     # str
    subtitle = text()                             # str | None
    views = integer().not_null().default(0)       # database default
    created_at = timestamp(tz=True).not_null().default(sql("now()"))
    slug = uuid().not_null().default_fn(uuid_lib.uuid4)  # Python default, per row
    updated_at = timestamp(tz=True).not_null().default(sql("now()")).on_update_now()
    edited_on = date().on_update(dt.date.today)   # Python value on every update
```

- `.not_null()` makes the column `NOT NULL` and drops `None` from its type.
- `.default(value)` is a database default: a literal (`0`, `"draft"`, an enum member) or SQL
  with `sql("now()")`. It is part of the migration.
- `.default_fn(fn)` calls `fn()` in Python for each inserted row that does not set the
  column. It is not in the database.
- `.on_update(fn)` sets the column to `fn()` on every `update()` (and upsert `DO UPDATE`)
  that does not set it. `.on_update_now()` uses the database's `now()`.
- A `NOT NULL` column with no default is required on insert; mizzle raises `QueryError`
  before sending anything if you leave it out.

## Primary keys, unique and check

```python
from typing import final, override

from mizzle import Constraint, Table, check, primary_key, sql, unique
from mizzle.pg import integer, serial, text


@final
class Tags(Table, name="tags"):
    id = serial().primary_key()
    name = text().not_null().unique("tags_name_key")
    weight = integer().not_null().default(1).check(sql("weight > 0"))


@final
class ArticleTags(Table, name="article_tags"):
    article_id = integer().not_null().references(lambda: Articles.id, on_delete="cascade")
    tag_id = integer().not_null().references(
        lambda: Tags.id, on_delete="restrict", on_update="cascade"
    )
    position = integer().not_null().default(0)

    @classmethod
    @override
    def config(cls) -> list[Constraint]:
        return [
            primary_key(cls.article_id, cls.tag_id),
            unique(cls.article_id, cls.position, name="article_tags_position_key"),
            check("article_tags_position_not_negative", sql("position >= 0")),
        ]
```

- `.primary_key()` on one column, or `primary_key(a, b)` in `config()` for a composite key.
  Only one of the two per table.
- `.unique()` on one column (an optional constraint name), or `unique(a, b, name=...)` for
  several columns.
- `.check(condition)` on a column, or `check(name, condition)` in `config()`. The condition
  is `sql(...)` or a mizzle condition such as `gt(...)`.
- Unnamed constraints get PostgreSQL-style names (`tags_pkey`, `tags_name_key`,
  `tags_weight_check`).

## Foreign keys

`.references(lambda: Target.column)` makes a foreign key (see `ArticleTags` above). The
lambda lets a table point at a table defined later in the file. `on_delete` and `on_update` take `"cascade"`, `"restrict"`, `"no action"`, `"set null"` or
`"set default"`. A key over several columns goes in `config()`:

```python
from typing import final, override

from mizzle import Constraint, Table, foreign_key
from mizzle.pg import integer, text


@final
class Versions(Table, name="versions"):
    article_id = integer().not_null()
    tag_id = integer().not_null()
    note = text()

    @classmethod
    @override
    def config(cls) -> list[Constraint]:
        return [
            foreign_key(
                [cls.article_id, cls.tag_id],
                lambda: [ArticleTags.article_id, ArticleTags.tag_id],
                on_delete="cascade",
            ),
        ]
```

A delete or insert that breaks a foreign key raises `ForeignKeyViolation`.

## Indexes

```python
from typing import final, override

from mizzle import Constraint, Table, eq, index, lower, sql, unique_index
from mizzle.pg import array, boolean, serial, text


@final
class Accounts(Table, name="accounts"):
    id = serial().primary_key()
    email = text().not_null()
    handle = text().not_null()
    country = text().not_null()
    deleted = boolean().not_null().default(False)
    labels = array(text()).not_null().default(sql("'{}'::text[]"))

    @classmethod
    @override
    def config(cls) -> list[Constraint]:
        return [
            index("accounts_country_idx").on(cls.country),
            index("accounts_country_handle_idx").on(cls.country, cls.handle),
            unique_index("accounts_email_lower_key").on(lower(cls.email)),
            unique_index("accounts_handle_live_key").on(cls.handle).where(eq(cls.deleted, False)),
            index("accounts_labels_idx").using("gin").on(cls.labels),
            index("accounts_handle_prefix_idx")
            .using("btree", [None, "text_pattern_ops"])
            .on(cls.country, cls.handle),
            index("accounts_email_brin_idx").using("brin").on(cls.id).with_(pages_per_range=32),
        ]
```

- `index(name).on(...)` takes columns or expressions (`lower(cls.email)`,
  `sql("(price * 2)")`).
- `unique_index(name)` makes it unique.
- `.where(condition)` makes a partial index.
- `.using(method, opclass)` picks the access method (`gin`, `gist`, `brin`, `hash`,
  `hnsw`...) and an operator class for every key, or a list with one per key (`None` is the
  default class).
- `.with_(...)` sets storage parameters (`WITH (pages_per_range = 32)`).

## Enums

```python
import enum
from typing import final

from mizzle import Table
from mizzle.pg import array, pg_enum, serial


class Status(enum.StrEnum):
    DRAFT = "draft"
    PUBLISHED = "published"


@final
class Pages(Table, name="pages"):
    id = serial().primary_key()
    status = pg_enum(Status).not_null().default(Status.DRAFT)   # type "status"
    previous = pg_enum(Status, type_name="status")               # same type
    history = array(pg_enum(Status)).not_null().default([])
```

The type name is the class name in snake_case (`Status` is `status`); `type_name=`
changes it, and `schema=` puts the type in another schema. Rows hold enum members:
`page.status is Status.DRAFT`. Migrations create the type and add new members.

## Columns of other types

`custom("type")` (from `mizzle.pg`) is a column of any type mizzle has no column type for:
a domain, a composite type, a range, a type from an extension. The string is the type as PostgreSQL prints it (`format_type()`), used as is
in DDL. mizzle adds no SQL around the column: values are what the driver (asyncpg) reads
and writes for that type, and the column compares, sorts and deduplicates as the type.

```python
from typing import final

from mizzle import Table
from mizzle.pg import custom, serial


@final
class Places(Table, name="places"):
    id = serial().primary_key()
    name = custom("citext")  # needs the citext extension
    search = custom("tsvector", py_type=str)  # str to type checkers, as matches() takes
    nights = custom("int4range")
    price = custom("money_amount")  # a domain over numeric, made in a custom migration
    pair = custom("price_pair")  # a composite type
```

| Type | Reads as | Write |
|---|---|---|
| `citext`, `tsvector`, an enum, a domain over `text` | `str` | a `str` |
| a domain over `numeric` | `Decimal` (the base type's value) | a `Decimal` or `int` |
| `int4range`, `daterange`, ... | `asyncpg.Range` | a `Range` |
| `box` | `asyncpg.Box` | a `Box` or `((x1, y1), (x2, y2))` |
| a composite | `asyncpg.Record` | a tuple of its fields |

```python
from asyncpg import Range

await db.insert(Places).values(name="Dhaka", search="old town", nights=Range(1, 5))
[place] = await db.select().from_(Places).where(eq(Places.name, "DHAKA"))  # citext
place.nights  # <Range [1, 5)>
```

The value type is `object` unless you say otherwise. `py_type=` names it (for type
checkers, `select_model` and the write check), `decode=` turns a value read into it and
`encode=` turns one written into what the driver takes. Type checkers take the type from
`decode`'s return type, else from `encode`'s parameter, so `py_type=` must agree with them.
`select_model` needs `py_type=` (or a field type through `refine=`): the driver's values
(a `Range`, a `Record`...) have no JSON form of their own.

```python
from dataclasses import dataclass


@dataclass(frozen=True)
class Nights:
    first: int
    last: int


def nights_of(span: Range[int]) -> Nights:
    return Nights(span.lower or 0, (span.upper or 1) - 1)


def span_of(nights: Nights) -> Range[int]:
    return Range(nights.first, nights.last, upper_inc=True)


@final
class Stays(Table, name="stays"):
    id = serial().primary_key()
    nights = custom("int4range", py_type=Nights, decode=nights_of, encode=span_of)
```

`Stays.nights` is a `Column[Nights | None]`; filters encode their values too
(`eq(Stays.nights, Nights(1, 4))`). Values are sent as they are, with no cast: PostgreSQL
types them from the column, and checks length and precision as for any value. A tuple
compared with a composite needs the type written out:
`eq(Row.pair, sql("{}::price_pair", (Decimal("1.5"), day)))`.

A relational query (`with_=`) nests related rows as JSON, so a custom column there holds
its JSON form (`to_json`): a `bool` or `int` for a domain over one, a `Decimal` with all
digits for other numbers (a `float` or `Decimal` when that is the `py_type`), a `str` such as `"[1,5)"` for a range, a `dict` such as
`{"amount": Decimal("1.5"), "day": "2020-01-02"}` for a composite. `decode` is applied to
that value too, so a column with `decode` and nested reads needs a `decode` that takes
both, or read the column with a plain select.

The domain, composite type or extension itself is not managed by mizzle: create it in a
custom migration (see [Not managed by mizzle](migrations.md#not-managed-by-mizzle)).
`pull` writes every column of such a type as `custom(...)`.

## Identity and generated columns

```python
from typing import final

from mizzle import Table, sql
from mizzle.pg import bigint, integer, numeric, text


@final
class Invoices(Table, name="invoices"):
    id = integer().primary_key().generated_always_as_identity()
    number = bigint().not_null().generated_by_default_as_identity(start=1000, increment=1)
    qty = integer().not_null()
    unit_price = numeric(10, 2).not_null()
    total = numeric(12, 2).generated_always_as(sql("qty * unit_price"))
    memo = text().not_null().default("")
    shout = text().generated_always_as(sql("upper(memo)"))
```

- `generated_always_as_identity()`: the database always assigns the value. An insert that
  sets it raises `QueryError`, unless the insert calls `.overriding_system_value()` (see
  [Queries](queries.md#insert)).
- `generated_by_default_as_identity()`: the database assigns it unless you give one.
- Both take `start=`, `increment=`, `min_value=`, `max_value=`, `cache=` and `cycle=`. They
  need `smallint`, `integer` or `bigint`, and make the column `NOT NULL`.
- `generated_always_as(sql(...))` computes the column from others (`STORED`).
  `stored=False` makes it `VIRTUAL` (PostgreSQL 18+). Inserts and updates cannot set it.

## Schemas

```python
from typing import final

from mizzle import Table
from mizzle.pg import bigserial, text


@final
class AuditLog(Table, name="audit_log", schema="archive"):
    id = bigserial().primary_key()
    message = text().not_null()
```

Without `schema=` a table lives in the default schema (`public`). Migrations create the
schema. Queries name it: `"archive"."audit_log"`.

## Relations

`one()` and `many()` say how to load related rows in
[relational queries](relational.md). They add no columns and no constraints: the foreign
key comes from `.references()` or `foreign_key()`.

```python
from typing import final

from mizzle import Table, many, one
from mizzle.pg import integer, serial, text


@final
class Authors(Table, name="authors"):
    id = serial().primary_key()
    name = text().not_null()
    books = many(lambda: Books)                # list[Books]
    profile = one(lambda: Profiles)            # Profiles | None (the other side has the key)


@final
class Profiles(Table, name="profiles"):
    author_id = integer().primary_key().references(lambda: Authors.id)
    bio = text()
    author = one(
        lambda: Authors, fields=lambda: [Profiles.author_id], references=lambda: [Authors.id]
    )


@final
class Books(Table, name="books"):
    id = serial().primary_key()
    author_id = integer().not_null().references(lambda: Authors.id)
    title = text().not_null()
    author = one(lambda: Authors, fields=lambda: [Books.author_id], references=lambda: [Authors.id])
```

- `one(Target, fields=[...], references=[...])` is the side that holds the key: rows of
  `Target` whose `references` columns equal this row's `fields`. On a row it is
  `Target | None`.
- `many(Target)` is the other side: it finds the `one(...)` on `Target` that points back.
  On a row it is `list[Target]`.
- `one(Target)` without `fields` is the other side of a one-to-one.

When two relations connect the same tables, name each pair with `relation_name`:

```python
from typing import final

from mizzle import Table, many, one
from mizzle.pg import integer, serial


@final
class People(Table, name="people"):
    id = serial().primary_key()
    sent = many(lambda: Messages, relation_name="sender")
    received = many(lambda: Messages, relation_name="recipient")


@final
class Messages(Table, name="messages"):
    id = serial().primary_key()
    sender_id = integer().not_null().references(lambda: People.id)
    recipient_id = integer().not_null().references(lambda: People.id)
    sender = one(
        lambda: People,
        fields=lambda: [Messages.sender_id],
        references=lambda: [People.id],
        relation_name="sender",
    )
    recipient = one(
        lambda: People,
        fields=lambda: [Messages.recipient_id],
        references=lambda: [People.id],
        relation_name="recipient",
    )
```

## Table.config()

`config()` returns everything that belongs to the table but not to one column:
`primary_key`, `unique`, `check`, `foreign_key`, `index`, `unique_index`, and the
security objects `rls()` and `policy(...)`. Declare it as a
`@classmethod` with `@override`, typed `list[Constraint]`, as in the examples above.

Columns in `config()` must belong to the same table. mizzle checks the whole schema when
you connect (`Database.connect(url, schema=[...])`) and when you run `mizzle generate`.
