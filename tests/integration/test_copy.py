"""COPY: copy_into (bulk load) and copy_to (export)."""

import asyncio
import io
from pathlib import Path

import pydantic
import pytest
from asyncpg import Range
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from mizzle import (
    Database,
    DatabaseError,
    QueryError,
    Table,
    UniqueViolation,
    asc,
    eq,
    placeholder,
    sql,
)
from mizzle.dialects.postgres.driver import PostgresDriver
from mizzle.pg import custom, integer, serial
from tests.integration.test_bulk import Rows, count, db, rows
from tests.integration.test_fuzz_types import Owners, Things, check, runner, thing_values
from tests.models import Role, Users

__all__ = ["db", "runner"]  # fixtures


# -- copy_into ----------------------------------------------------------------------


async def test_copy_into_round_trips(db: Database) -> None:
    sent = [
        {"n": 1, "label": "plain", "small": 7, "doc": {"a": [1, None, "x"]}},
        {"n": 2, "label": "tab\tnew\nline\\ 'q' \"dq\"", "small": None, "doc": None},
        {"n": 3, "label": "", "small": -32768, "doc": [1.5, True]},
    ]
    assert await db.copy_into(Rows, sent) == 3
    got = await db.select().from_(Rows).order_by(asc(Rows.n))
    assert [{k: getattr(r, k) for k in sent[0]} for r in got] == sent
    # Enums (and server defaults for columns left out) through the shared test schema.
    assert await db.copy_into(Users, [{"email": "a@x.io", "role": Role.ADMIN}]) == 1
    [user] = await db.select().from_(Users)
    assert user.role is Role.ADMIN and user.created_at is not None


async def test_copy_into_takes_models(db: Database) -> None:
    class Row(pydantic.BaseModel):
        n: int
        label: str

    assert await db.copy_into(Rows, (Row(n=i, label=str(i)) for i in range(3))) == 3
    assert await count(db) == 3


async def test_copy_into_columns_and_generators(db: Database) -> None:
    assert await db.copy_into(Rows, rows(500), columns=[Rows.n, Rows.label]) == 500
    assert await db.copy_into(Rows, iter([])) == 0
    assert await count(db) == 500


async def test_copy_into_inside_a_transaction(db: Database) -> None:
    with pytest.raises(RuntimeError):
        async with db.transaction() as tx:
            await tx.copy_into(Rows, rows(10))
            assert await tx.select().from_(Rows).where(eq(Rows.n, 3))
            raise RuntimeError
    assert await count(db) == 0


@pytest.mark.parametrize(
    ("bad", "message"),
    [
        ({"n": 1, "label": "x", "nope": 1}, "has no column"),
        ({"n": 1, "label": None}, "NOT NULL"),
        ({"n": "1", "label": "x"}, "expects int"),
        ({"n": 1}, "required"),
        ({"n": 1, "label": "x", "small": 1}, "sets small"),
        ({"label": "x"}, "required"),
        ({"n": 1, "label": sql("'x'")}, "SQL expressions"),
    ],
)
async def test_copy_into_checks_rows_like_insert(
    db: Database, bad: dict[str, object], message: str
) -> None:
    # The bad row comes after good ones, mid-COPY: nothing is stored.
    with pytest.raises(QueryError, match=message):
        await db.copy_into(Rows, [*rows(1000, start=10), bad])
    assert await count(db) == 0


async def test_copy_into_column_checks(db: Database) -> None:
    with pytest.raises(QueryError, match="required"):
        await db.copy_into(Rows, rows(1), columns=[Rows.n])
    with pytest.raises(QueryError, match="not a column"):
        await db.copy_into(Rows, rows(1), columns=[Users.email])
    with pytest.raises(QueryError, match="has no value for small"):
        await db.copy_into(Rows, rows(1), columns=[Rows.n, Rows.label, Rows.small])


async def test_copy_into_errors_hide_values(db: Database) -> None:
    secret = "s3cret-value"
    await db.copy_into(Rows, [{"n": 1, "label": secret}])
    with pytest.raises(UniqueViolation) as unique:
        await db.copy_into(Rows, [{"n": 1, "label": secret}])
    with pytest.raises(DatabaseError, match="value hidden") as overflow:
        await db.copy_into(Rows, [{"n": 2, "label": "x", "small": 99999}])
    for error in (unique.value, overflow.value):
        assert secret not in str(error) and "99999" not in str(error)
    assert await count(db) == 1


class Stays(Table, name="copy_stays"):
    id = serial().primary_key()
    n = integer().not_null()
    nights = custom("int4range")


async def test_copy_into_custom_columns_falls_back_to_insert(
    db: Database, driver: PostgresDriver
) -> None:
    # COPY sends binary values, which an extension type may not have: insert_many instead.
    async with driver.acquire() as conn:
        await conn.execute(
            "DROP TABLE IF EXISTS copy_stays;"
            " CREATE TABLE copy_stays (id serial PRIMARY KEY, n int NOT NULL, nights int4range)",
            [],
        )
    try:
        assert await db.copy_into(Stays, [{"n": 1, "nights": Range(1, 3)}, {"n": 2}]) == 2
        got = await db.select(Stays.n).from_(Stays).order_by(Stays.n)
        assert got == [(1,), (2,)]
    finally:
        async with driver.acquire() as conn:
            await conn.execute("DROP TABLE copy_stays", [])


@settings(
    max_examples=30,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(sent=st.lists(thing_values, min_size=1, max_size=20))
def test_copy_into_every_type_round_trips(
    runner: tuple[asyncio.Runner, Database], sent: list[dict[str, object]]
) -> None:
    loop, db = runner
    columns = [c for c in Things.__table__.columns.values() if c.attr != "id"]

    async def scenario() -> None:
        [owner] = await db.insert(Owners).values({}).returning()
        full = [{c.attr: row.get(c.attr) for c in columns} | {"owner_id": owner.id} for row in sent]
        assert await db.copy_into(Things, full) == len(full)
        got = (
            await db.select().from_(Things).where(eq(Things.owner_id, owner.id)).order_by(Things.id)
        )
        for values, row in zip(sent, got, strict=True):
            check(values, row, "copy_into")
        await db.delete(Owners).where(eq(Owners.id, owner.id))

    loop.run(scenario())


# -- copy_to ------------------------------------------------------------------------


async def test_copy_to_exports(db: Database, tmp_path: Path) -> None:
    await db.copy_into(Rows, [{"n": 1, "label": 'a,"b"', "doc": {"k": 1}}])
    await db.copy_into(Rows, rows(3, start=2))
    query = db.select(Rows.n, Rows.label).from_(Rows).where(eq(Rows.label, 'a,"b"'))

    buffer = io.BytesIO()
    assert await db.copy_to(query, buffer, header=True) == 1
    assert buffer.getvalue() == b'n,label\n1,"a,""b"""\n'

    path = tmp_path / "rows.csv"
    assert await db.copy_to(db.select().from_(Rows).order_by(Rows.n), str(path)) == 4
    assert path.read_text().splitlines()[0] == '1,1,"a,""b""",,"{""k"": 1}"'

    chunks: list[bytes] = []

    async def write(chunk: bytes) -> None:
        chunks.append(chunk)

    exported = await db.copy_to(db.query(Rows).find_many(), write, format="text")
    assert exported == 4 and b"".join(chunks).count(b"\n") == 4
    assert await db.copy_to(sql("select 1 as one"), write, format="binary") == 1


async def test_copy_to_values_with_quotes_and_dollars(db: Database) -> None:
    query = sql("select {} as a, {}::float8 as b, '$1' as c", "it's $1 \\ x", 0.1)
    buffer = io.BytesIO()
    assert await db.copy_to(query, buffer) == 1
    assert buffer.getvalue() == b"it's $1 \\ x,0.1,$1\n"


async def test_copy_to_errors(db: Database, tmp_path: Path) -> None:
    with pytest.raises(DatabaseError, match="DivisionByZero"):
        await db.copy_to(sql("select 1 / 0"), io.BytesIO())
    with pytest.raises(QueryError, match="placeholders"):
        await db.copy_to(db.select().from_(Rows).where(eq(Rows.n, placeholder("n"))), io.BytesIO())
    await db.copy_into(Rows, [{"n": 1, "label": "a"}])

    async def full(_: bytes) -> None:
        raise OSError(28, "No space left on device")

    # An output error is raised as it is, not as a lost connection.
    with pytest.raises(OSError) as caught:
        await db.copy_to(db.select().from_(Rows), full)
    assert type(caught.value) is OSError
    with pytest.raises(IsADirectoryError):
        await db.copy_to(db.select().from_(Rows), str(tmp_path))
    assert await db.copy_to(db.select().from_(Rows), str(tmp_path / "ok.csv")) == 1


async def test_copy_to_float_values_are_exact(pg_url: str) -> None:
    # The server quotes COPY's values; with extra_float_digits 0 it rounded 0.1 + 0.2 to 0.3.
    settings = {"extra_float_digits": "0"}
    async with await Database.connect(pg_url, server_settings=settings) as db:
        buffer = io.BytesIO()
        query = sql("select 1 as one where {}::float8 = 0.1::float8 + 0.2::float8", 0.1 + 0.2)
        assert await db.copy_to(query, buffer) == 1


async def test_copy_to_inlines_only_real_placeholders(db: Database) -> None:
    # "$1" inside quoted text and identifiers is not a parameter.
    query = sql("""select {} as a, '$1' as b, 'it''s $1' as c, 2 as "$1" """, "x")
    buffer = io.BytesIO()
    assert await db.copy_to(query, buffer) == 1
    assert buffer.getvalue() == b"x,$1,it's $1,2\n"


@pytest.mark.parametrize(
    "text",
    [
        "select {} /* /* nested */ $1 */",  # read as two comments by a simple scanner
        "select {} -- $1",
        "select {}, $q$ $1 $q$",
        "select {}, E'\\' $1'",  # a backslash: read differently with standard_conforming_strings
        "select {}, 'unclosed $1",
    ],
)
async def test_copy_to_refuses_sql_it_could_misread(db: Database, text: str) -> None:
    # Values are written into the SQL: only where PostgreSQL surely sees "$n" as a
    # parameter. Elsewhere a value could end a comment or a string and run as SQL.
    with pytest.raises(QueryError, match="without comments"):
        await db.copy_to(sql(text, "x */ select 1 --"), io.BytesIO())
