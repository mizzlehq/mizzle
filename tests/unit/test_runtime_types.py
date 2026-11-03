"""sparsevec, array-like vector values, aware timestamptz, and errors that name the
PostgreSQL version a feature needs."""

import datetime as dt
from typing import cast, final

import pydantic
import pytest
from asyncpg import exceptions as pg
from asyncpg.pool import PoolConnectionProxy

from mizzle import ConnectionError, DatabaseError, QueryBuilder, QueryError, Table
from mizzle.dialects.postgres.driver import PostgresConnection, map_error
from mizzle.kit.codegen import render_types_module
from mizzle.pg import halfvec, integer, l2_distance, sparsevec, timestamp, vector
from mizzle.pydantic import insert_model, select_model


@final
class Embeddings(Table, name="embeddings"):
    id = integer().primary_key()
    dense = vector(3)
    half = halfvec(2)
    sparse = sparsevec(4)
    at = timestamp(tz=True)


@final
class FakeArray:
    def tolist(self) -> list[float]:
        return [1.0, 2.5, -3.0]


def test_sparsevec_text_form() -> None:
    column = Embeddings.sparse
    assert column.encode({3: 2.5, 0: 1}) == "{1:1,4:2.5}/4"
    assert column.encode({}) == "{}/4"
    assert column.decode("{1:1,4:2.5}/4") == {0: 1.0, 3: 2.5}
    assert column.decode("{1:0.1}/4") == {0: 0.10000000149011612}  # float4, like vector
    assert column.decode("{}/4") == {}
    for bad in ({4: 1.0}, {-1: 1.0}, {True: 1.0}, {"0": 1.0}):
        with pytest.raises(QueryError, match="indices"):
            column.encode(bad)
    with pytest.raises(QueryError, match="numbers"):
        column.encode({0: "1"})


def test_array_like_vector_values(qb: QueryBuilder) -> None:
    _, params = qb.insert(Embeddings).values(id=1, dense=FakeArray(), half=(1, 2)).to_sql()
    assert params == (1, "[1,2.5,-3]", "[1,2]")
    with pytest.raises(QueryError, match="expects"):
        qb.insert(Embeddings).values(id=1, dense="1,2,3").to_sql()
    _, params = (
        qb.select({"d": l2_distance(Embeddings.dense, FakeArray())}).from_(Embeddings).to_sql()
    )
    assert params == ("[1,2.5,-3]",)
    _, params = (
        qb.select({"d": l2_distance({1: 2.0}, Embeddings.sparse)}).from_(Embeddings).to_sql()
    )
    assert params == ("{2:2}/4",)


def test_timestamptz_infinity_round_trips() -> None:
    column = Embeddings.at
    top = dt.datetime.max.replace(tzinfo=dt.UTC)
    assert column.decode(dt.datetime.max) == top  # what asyncpg reads for infinity
    assert column.decode(dt.datetime.min) == dt.datetime.min.replace(tzinfo=dt.UTC)
    assert column.encode(top) is not top and column.encode(top) == dt.datetime.max
    now = dt.datetime.now(dt.UTC)
    assert column.encode(now) is now and column.decode(now) is now
    assert timestamp().decoder is None  # no cost for plain timestamp columns


def test_pydantic_and_typegen() -> None:
    Model = insert_model(Embeddings)
    assert Model.model_validate({"id": 1, "sparse": {"2": 1.5}}).model_dump()["sparse"] == {2: 1.5}
    with pytest.raises(pydantic.ValidationError):
        Model.model_validate({"id": 1, "sparse": [1.0]})
    assert select_model(Embeddings).model_fields["sparse"].annotation == dict[int, float] | None
    source = render_types_module([Embeddings])
    assert "    sparse: dict[int, float] | None" in source
    assert "    dense: list[float] | None" in source
    # Only used names are imported (strict checkers flag the rest).
    assert "from typing import NotRequired, TypedDict\n" in source


@pytest.mark.parametrize(
    ("sql", "word", "needs"),
    [
        ('ALTER TABLE "t" ALTER COLUMN "c" SET EXPRESSION AS (1)', "EXPRESSION", "17"),
        ('CREATE TABLE "t" ("c" integer GENERATED ALWAYS AS (1) VIRTUAL)', "VIRTUAL", "18"),
    ],
)
def test_old_servers_name_the_version_needed(sql: str, word: str, needs: str) -> None:
    exc = pg.PostgresSyntaxError.new(
        {"M": f'syntax error at or near "{word}"', "C": "42601", "S": "ERROR"}
    )
    error = map_error(exc, sql)
    assert isinstance(error, DatabaseError)
    assert f"needs PostgreSQL {needs} or later" in str(error)
    other = pg.PostgresSyntaxError.new({"M": 'syntax error at or near "selec"', "C": "42601"})
    assert "needs PostgreSQL" not in str(map_error(other, "selec 1"))


class _BrokenBegin:
    async def __aenter__(self) -> None:
        raise pg.InternalClientError(
            "cannot switch to state 15; another operation (2) is in progress"
        )

    async def __aexit__(self, *_: object) -> None:
        return None


class _BrokenConnection:
    def transaction(self, **_: object) -> _BrokenBegin:
        return _BrokenBegin()


async def test_begin_on_a_closed_connection_is_a_connection_error() -> None:
    # Seen when the primary restarts under load: BEGIN fails inside asyncpg.
    conn = PostgresConnection(cast(PoolConnectionProxy, cast(object, _BrokenConnection())))
    with pytest.raises(ConnectionError):
        async with conn.transaction():
            pass
