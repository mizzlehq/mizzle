"""View base class: a table you can only read, defined by a query."""

from __future__ import annotations

from typing import ClassVar, Literal

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
    ``security_invoker=True`` / ``security_barrier=True`` set those view options (row-level
    security), and ``check_option="local"`` or ``"cascaded"`` adds WITH ... CHECK OPTION.
    """

    __materialized__: ClassVar[bool] = False
    __security_invoker__: ClassVar[bool] = False
    __security_barrier__: ClassVar[bool] = False
    __check_option__: ClassVar[Literal["local", "cascaded"] | None] = None

    def __init_subclass__(
        cls,
        *,
        name: str | None = None,
        schema: str | None = None,
        materialized: bool = False,
        security_invoker: bool = False,
        security_barrier: bool = False,
        check_option: Literal["local", "cascaded"] | None = None,
    ) -> None:
        super().__init_subclass__(name=name, schema=schema)
        if materialized and (security_invoker or security_barrier or check_option):
            raise SchemaError(
                f"view {cls.__name__}: materialized views take no security_invoker, "
                "security_barrier or check_option"
            )
        cls.__materialized__ = materialized
        cls.__security_invoker__ = security_invoker  # (K6)
        cls.__security_barrier__ = security_barrier
        cls.__check_option__ = check_option

    @classmethod
    def query(cls, db: QueryBuilder) -> Node:
        """The view's query: a select built with ``db``, or ``sql("SELECT ...")``."""
        del db
        raise SchemaError(f"view {cls.__name__} must define query()")
