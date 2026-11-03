"""Table aliases (``alias(Users, "manager")``), for self-joins and the like."""

from __future__ import annotations

import copy
import types
from typing import ClassVar, cast

from mizzle.core.schema.column import ColumnBase
from mizzle.core.schema.table import Table


class TableAlias:
    """Mixin of the table classes ``alias()`` makes. Their ``__table__.name`` is the alias,
    so columns compile as ``"alias"."col"``; FROM names the real table and rows decode
    into it."""

    __alias_of__: ClassVar[type[Table]]


def alias_table[T: Table](table: type[T], name: str) -> type[T]:
    real: type[Table] = table.__alias_of__ if issubclass(table, TableAlias) else table
    # Copies of the columns, attached to the new class: they compile with the alias name.
    columns: dict[str, ColumnBase] = {
        attr: copy.copy(column) for attr, column in real.__table__.columns.items()
    }
    cls = types.new_class(
        real.__name__, (Table, TableAlias), {"name": name}, lambda ns: ns.update(columns)
    )
    cast(type[TableAlias], cls).__alias_of__ = real
    # Typed as the real table so columns (``alias.id``) and rows keep their types.
    return cast(type[T], cls)


def real_table(table: type[Table]) -> type[Table]:
    """The table an alias stands for (or ``table`` itself)."""
    return table.__alias_of__ if issubclass(table, TableAlias) else table
