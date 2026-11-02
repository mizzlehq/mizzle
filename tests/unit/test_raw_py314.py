import pytest

from mizzle.core.errors import QueryError
from mizzle.core.sql.raw import sql
from mizzle.dialects.postgres.compiler import PostgresCompiler
from tests.models import Users


def test_tstring_values_become_params() -> None:
    uid = 5
    evil = "'; drop table users; --"
    compiled = PostgresCompiler().compile(
        sql(t"select * from users where id = {uid} and {Users.email} = {evil}")
    )
    assert compiled.sql == 'select * from users where id = $1 and "users"."email" = $2'
    assert compiled.params == (5, evil)


def test_tstring_rejects_format_spec() -> None:
    x = 1
    with pytest.raises(QueryError):
        sql(t"select {x!r}")
