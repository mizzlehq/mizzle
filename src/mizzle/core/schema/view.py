"""View base class: a table you can only read, defined by a query."""

from __future__ import annotations

from typing import ClassVar

from mizzle.core.errors import SchemaError
from mizzle.core.query.builder import QueryBuilder
from mizzle.core.schema.table import Table
from mizzle.core.sql.nodes import Node


class View(Table, abstract=True):
    """Declare the columns the view returns, like a table, and its query::

        class ActiveUsers(View, name="active_users"):
            id = integer().not_null()
            email = text().not_null()

            @classmethod
            @override
            def query(cls, db: QueryBuilder):
                return db.select(Users.id, Users.email).from_(Users).where(eq(Users.active, True))

    Select from it like a table: ``await db.select().from_(ActiveUsers)``.
    ``materialized=True`` makes a materialized view: ``await db.refresh(ActiveUsers)``.
    """

    __materialized__: ClassVar[bool] = False

    def __init_subclass__(
        cls,
        *,
        name: str | None = None,
        schema: str | None = None,
        materialized: bool = False,
    ) -> None:
        super().__init_subclass__(name=name, schema=schema)
        cls.__materialized__ = materialized

    @classmethod
    def query(cls, db: QueryBuilder) -> Node:
        """The view's query: a select built with ``db``, or ``sql("SELECT ...")``."""
        del db
        raise SchemaError(f"view {cls.__name__} must define query()")
