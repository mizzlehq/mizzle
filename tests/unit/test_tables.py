import pytest

from mizzle.core.errors import SchemaError
from mizzle.core.schema.constraints import Index, PrimaryKey, index, primary_key
from mizzle.core.schema.table import Table, validate_tables
from mizzle.core.schema.types import integer, text
from mizzle.dialects.postgres.types import serial


class Authors(Table, name="authors", schema="blog"):
    id = serial().primary_key()
    name = text().not_null()


class BlogPost(Table):
    id = serial().primary_key()
    author_id = integer().not_null().references(lambda: Authors.id, on_delete="cascade")
    title = text().not_null()

    @classmethod
    def config(cls) -> list[Index]:
        return [index("blog_post_author_idx").on(cls.author_id)]


def test_table_info() -> None:
    assert Authors.__table__.name == "authors"
    assert Authors.__table__.schema == "blog"
    assert BlogPost.__table__.name == "blog_post"
    assert list(BlogPost.__table__.columns) == ["id", "author_id", "title"]


def test_from_db_builds_rows() -> None:
    row = Authors.from_db({"id": 1, "name": "ann"})
    assert row.id == 1
    assert row.name == "ann"
    assert row == Authors.from_db({"id": 1, "name": "ann"})
    assert repr(row) == "Authors(id=1, name='ann')"


def test_table_without_columns_is_rejected() -> None:
    with pytest.raises(SchemaError, match="declares no columns"):

        class Empty(Table):
            pass


def test_valid_schema_passes() -> None:
    validate_tables([Authors, BlogPost])


def test_duplicate_table_names() -> None:
    class Other(Table, name="authors", schema="blog"):
        id = integer()

    with pytest.raises(SchemaError, match="both use table name"):
        validate_tables([Authors, Other])


def test_duplicate_column_names_after_casing() -> None:
    class Clash(Table):
        userId = integer()
        user_id = integer()

    validate_tables([Clash])
    with pytest.raises(SchemaError, match="both map to 'user_id'"):
        validate_tables([Clash], casing="snake_case")


def test_reference_to_unregistered_table() -> None:
    with pytest.raises(SchemaError, match="Authors is not in the schema"):
        validate_tables([BlogPost])


def test_two_column_primary_keys() -> None:
    class Bad(Table):
        a = integer().primary_key()
        b = integer().primary_key()

    with pytest.raises(SchemaError, match="composite key"):
        validate_tables([Bad])


def test_primary_key_declared_twice() -> None:
    class Bad(Table):
        a = integer().primary_key()

        @classmethod
        def config(cls) -> list[PrimaryKey]:
            return [primary_key(cls.a)]

    with pytest.raises(SchemaError, match="declared twice"):
        validate_tables([Bad])


def test_config_with_foreign_column() -> None:
    class Bad(Table):
        a = integer()

        @classmethod
        def config(cls) -> list[Index]:
            return [index("x").on(Authors.id)]

    with pytest.raises(SchemaError, match="from another table"):
        validate_tables([Bad, Authors])
