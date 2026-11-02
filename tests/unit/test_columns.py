import enum
from typing import Any

import pytest

from mizzle.core.errors import QueryError
from mizzle.core.naming import snake_case
from mizzle.core.schema.types import integer, numeric, text, varchar
from mizzle.core.sql.nodes import Raw
from mizzle.dialects.postgres.types import array, jsonb, pg_enum, serial


class Color(enum.StrEnum):
    RED = "red"
    DARK_BLUE = "dark_blue"


class Thing:
    id = serial().primary_key()
    count = integer().not_null()
    note = text()
    color = pg_enum(Color).not_null()
    colors = array(pg_enum(Color).not_null())
    meta = jsonb[dict[str, Any]]()


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("BlogPost", "blog_post"),
        ("HTTPLog", "http_log"),
        ("createdAt", "created_at"),
        ("users", "users"),
    ],
)
def test_snake_case(name: str, expected: str) -> None:
    assert snake_case(name) == expected


def test_nullability() -> None:
    assert Thing.note.nullable
    assert not Thing.count.nullable
    assert Thing.id.is_primary_key and not Thing.id.nullable


def test_required_on_insert() -> None:
    assert Thing.count.required_on_insert
    assert not Thing.id.required_on_insert  # serial is generated
    assert not Thing.note.required_on_insert  # nullable
    assert not integer().not_null().default(0).required_on_insert
    assert not integer().not_null().default_fn(lambda: 1).required_on_insert


def test_set_name_records_attr_and_owner() -> None:
    assert Thing.count.attr == "count"
    assert Thing.count.table is Thing
    assert Thing.count.qualified_name == "Thing.count"


def test_db_name_and_casing() -> None:
    col = integer()
    col.attr = "authorId"
    assert col.db_name() == "authorId"
    assert col.db_name("snake_case") == "author_id"
    assert text("explicit").db_name("snake_case") == "explicit"


def test_unloaded_instance_attribute_raises() -> None:
    row = Thing()
    with pytest.raises(AttributeError, match=r"Thing\.count was not selected"):
        _ = row.count


def test_validate_types() -> None:
    Thing.count.validate(5)
    Thing.note.validate(None)
    Thing.count.validate(Raw(("1",)))  # SQL expressions are not type-checked
    with pytest.raises(QueryError, match=r"Thing\.count expects int; got str"):
        Thing.count.validate("5")
    with pytest.raises(QueryError, match="got bool"):
        Thing.count.validate(True)
    with pytest.raises(QueryError, match="NOT NULL"):
        Thing.count.validate(None)
    with pytest.raises(QueryError):
        Thing.color.validate("red")  # must be the enum, not its value
    Thing.meta.validate({"any": "json"})


def test_sql_types() -> None:
    assert varchar(255).sql_type == "varchar(255)"
    assert numeric(10, 2).sql_type == "numeric(10, 2)"
    assert Thing.color.sql_type == "color"
    assert Thing.colors.sql_type == "color[]"
    assert Thing.meta.sql_type == "jsonb"


def test_enum_codec() -> None:
    assert Thing.color.encode(Color.DARK_BLUE) == "dark_blue"
    assert Thing.color.decode("red") is Color.RED
    assert Thing.colors.encode([Color.RED]) == ["red"]
    assert Thing.colors.decode(["red", "dark_blue"]) == [Color.RED, Color.DARK_BLUE]
    assert Thing.color.decode(None) is None
