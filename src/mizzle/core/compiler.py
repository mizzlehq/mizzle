"""Base SQL compiler. Walks a node tree and emits SQL text plus bind parameters.

Dialects subclass ``Compiler`` and override hooks (``param_marker``, ``returning_clause``...).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from mizzle.core.errors import QueryError
from mizzle.core.naming import Casing
from mizzle.core.schema.column import MISSING, Column
from mizzle.core.sql.nodes import (
    Between,
    BinaryOp,
    BoolOp,
    Excluded,
    Exists,
    Func,
    Identifier,
    InList,
    Node,
    Not,
    OrderBy,
    Param,
    Placeholder,
    Postfix,
    Raw,
    Star,
)

if TYPE_CHECKING:
    from mizzle.core.query.insert import Insert, OnConflict
    from mizzle.core.query.returning import Returning
    from mizzle.core.query.select import CteColumn, Select, Slot, Source
    from mizzle.core.query.update import Delete, Update
    from mizzle.core.schema.table import Table


@dataclass(frozen=True)
class CompiledQuery:
    sql: str
    params: tuple[Any, ...]
    placeholders: tuple[Placeholder | None, ...]  # per param slot; set where a placeholder sits

    @property
    def has_placeholders(self) -> bool:
        return any(p is not None for p in self.placeholders)

    def bind(self, values: Mapping[str, Any]) -> CompiledQuery:
        """Fill placeholder slots with ``values`` (by placeholder name)."""
        names = {p.name for p in self.placeholders if p is not None}
        if missing := names - values.keys():
            raise QueryError(f"missing values for placeholders: {', '.join(sorted(missing))}")
        if extra := values.keys() - names:
            raise QueryError(f"unknown placeholders: {', '.join(sorted(extra))}")
        params = tuple(
            value
            if p is None
            else (p.column.encode(values[p.name]) if p.column else values[p.name])
            for value, p in zip(self.params, self.placeholders, strict=True)
        )
        return CompiledQuery(self.sql, params, (None,) * len(params))


class Compiler:
    """Single-use per ``compile()`` call: it accumulates params while walking the tree."""

    def __init__(self, casing: Casing | None = None) -> None:
        self.casing: Casing | None = casing
        self._params: list[Any] = []
        self._placeholders: list[Placeholder | None] = []

    def compile(self, node: Node) -> CompiledQuery:
        self._params, self._placeholders = [], []
        text = self.visit(node)
        return CompiledQuery(text, tuple(self._params), tuple(self._placeholders))

    def visit(self, node: Node) -> str:
        # Walk the MRO so subclasses (e.g. jsonb -> Column) reuse their parent's visitor.
        for cls in type(node).__mro__:
            method = getattr(self, f"visit_{cls.__name__}", None)
            if method is not None:
                return cast(Callable[[Node], str], method)(node)
        raise QueryError(f"{type(self).__name__} cannot compile {type(node).__name__}")

    # -- hooks ------------------------------------------------------------------------

    def param_marker(self, index: int) -> str:
        return "?"

    def quote(self, ident: str) -> str:
        return '"' + ident.replace('"', '""') + '"'

    def bind(self, value: Any, placeholder: Placeholder | None = None) -> str:
        self._params.append(value)
        self._placeholders.append(placeholder)
        return self.param_marker(len(self._params))

    def table_name(self, table: type[Table]) -> str:
        info = table.__table__
        name = self.quote(info.name)
        return f"{self.quote(info.schema)}.{name}" if info.schema else name

    def column_name(self, column: Column[Any]) -> str:
        return self.quote(column.db_name(self.casing))

    def operand(self, node: Node) -> str:
        """An expression used inside a larger one; boolean groups get parentheses."""
        text = self.visit(node)
        return f"({text})" if isinstance(node, BoolOp | Not) else text

    def value(self, column: Column[Any], value: Any) -> str:
        """A value written into a column (INSERT / UPDATE SET)."""
        return self.visit(value) if isinstance(value, Node) else self.bind(column.encode(value))

    # -- expressions ------------------------------------------------------------------

    def visit_Column(self, node: Column[Any]) -> str:
        if node.table is None:
            raise QueryError(f"column {node.attr!r} is not attached to a table")
        return f"{self.quote(node.table.__table__.name)}.{self.column_name(node)}"

    def visit_Param(self, node: Param[Any]) -> str:
        value = node.column.encode(node.value) if node.column is not None else node.value
        return self.bind(value)

    def visit_Placeholder(self, node: Placeholder) -> str:
        return self.bind(None, node)

    def visit_BinaryOp(self, node: BinaryOp) -> str:
        return f"{self.operand(node.left)} {node.op} {self.operand(node.right)}"

    def visit_BoolOp(self, node: BoolOp) -> str:
        return f" {node.op} ".join(self.operand(item) for item in node.items)

    def visit_Not(self, node: Not) -> str:
        return f"NOT ({self.visit(node.item)})"

    def visit_Postfix(self, node: Postfix) -> str:
        return f"{self.operand(node.item)} {node.op}"

    def visit_InList(self, node: InList) -> str:
        if isinstance(node.values, tuple):
            if not node.values:  # IN () is a syntax error; the meaning is "matches nothing"
                return "true" if node.negate else "false"
            inner = ", ".join(self.visit(v) for v in node.values)
        else:
            inner = self.visit(node.values)
        op = "NOT IN" if node.negate else "IN"
        return f"{self.operand(node.item)} {op} ({inner})"

    def visit_Between(self, node: Between) -> str:
        item, low, high = self.operand(node.item), self.operand(node.low), self.operand(node.high)
        return f"{item} BETWEEN {low} AND {high}"

    def visit_Exists(self, node: Exists) -> str:
        return f"{'NOT ' if node.negate else ''}EXISTS ({self.visit(node.query)})"

    def visit_Func(self, node: Func[Any]) -> str:
        args = ", ".join(self.visit(a) for a in node.args)
        return f"{node.name}({'DISTINCT ' if node.distinct else ''}{args})"

    def visit_Star(self, node: Star) -> str:
        return "*"

    def visit_OrderBy(self, node: OrderBy) -> str:
        text = f"{self.visit(node.expr)} {'DESC' if node.descending else 'ASC'}"
        return f"{text} NULLS {node.nulls.upper()}" if node.nulls else text

    def visit_Raw(self, node: Raw) -> str:
        return "".join(p if isinstance(p, str) else self.visit(p) for p in node.parts)

    def visit_Identifier(self, node: Identifier) -> str:
        return self.quote(node.name)

    def visit_Excluded(self, node: Excluded[Any]) -> str:
        return f"excluded.{self.column_name(node.column)}"

    # -- SELECT -----------------------------------------------------------------------

    def source(self, source: Source) -> str:
        if isinstance(source, type):
            return self.table_name(source)
        return self.quote(source.name)

    def select_slot(self, slot: Slot) -> str:
        if isinstance(slot, type):
            return ", ".join(self.visit(c) for c in slot.__table__.columns.values())
        return self.visit(slot)

    def count_value(self, value: int | Placeholder) -> str:
        return self.visit(value) if isinstance(value, Placeholder) else self.bind(value)

    def visit_CteColumn(self, node: CteColumn) -> str:
        return f"{self.quote(node.cte.name)}.{self.quote(node.name)}"

    def visit_Select(self, node: Select[Any]) -> str:
        # Clauses are emitted strictly left to right so $n numbering follows the text.
        parts: list[str] = []
        if node.ctes:
            ctes = ", ".join(f"{self.quote(c.name)} AS ({self.visit(c.query)})" for c in node.ctes)
            parts.append(f"WITH {ctes}")
        parts.append("SELECT DISTINCT" if node.distinct_ else "SELECT")
        if isinstance(node.projection, dict):
            items = [f"{self.visit(e)} AS {self.quote(k)}" for k, e in node.projection.items()]
        else:
            items = [self.select_slot(slot) for slot in node.slots()]
        parts.append(", ".join(items))
        parts.append(f"FROM {self.source(node.source)}")
        for join in node.joins:
            parts.append(f"{join.kind} JOIN {self.source(join.source)} ON {self.visit(join.on)}")
        if node.where_ is not None:
            parts.append(f"WHERE {self.visit(node.where_)}")
        if node.group_by_:
            parts.append("GROUP BY " + ", ".join(self.visit(e) for e in node.group_by_))
        if node.having_ is not None:
            parts.append(f"HAVING {self.visit(node.having_)}")
        if node.order_by_:
            parts.append("ORDER BY " + ", ".join(self.visit(e) for e in node.order_by_))
        if node.limit_ is not None:
            parts.append(f"LIMIT {self.count_value(node.limit_)}")
        if node.offset_ is not None:
            parts.append(f"OFFSET {self.count_value(node.offset_)}")
        if node.lock is not None:
            lock = "FOR UPDATE"
            if node.lock.of:
                lock += " OF " + ", ".join(self.quote(t.__table__.name) for t in node.lock.of)
            if node.lock.skip_locked:
                lock += " SKIP LOCKED"
            if node.lock.nowait:
                lock += " NOWAIT"
            parts.append(lock)
        return " ".join(parts)

    # -- INSERT / UPDATE / DELETE -----------------------------------------------------

    def returning_clause(self, table: type[Table]) -> str:
        raise QueryError(f"{type(self).__name__} does not support RETURNING")

    def on_conflict_clause(self, table: type[Table], conflict: OnConflict) -> str:
        raise QueryError(f"{type(self).__name__} does not support ON CONFLICT")

    def set_list(self, table: type[Table], values: dict[str, Any]) -> str:
        columns = table.__table__.columns
        return ", ".join(
            f"{self.column_name(columns[k])} = {self.value(columns[k], v)}"
            for k, v in values.items()
        )

    def visit_Insert(self, node: Insert[Any]) -> str:
        if not node.rows:
            raise QueryError(f"insert into {node.table.__name__} has no values()")
        columns = node.table.__table__.columns
        used = [attr for attr in columns if any(attr in row for row in node.rows)]
        head = f"INSERT INTO {self.table_name(node.table)}"
        if not used and len(node.rows) == 1:
            text = f"{head} DEFAULT VALUES"
        else:
            used = used or [next(iter(columns))]  # several all-default rows: VALUES (DEFAULT)
            names = ", ".join(self.column_name(columns[a]) for a in used)
            rows = ", ".join(
                "("
                + ", ".join(self._insert_value(columns[a], row.get(a, MISSING)) for a in used)
                + ")"
                for row in node.rows
            )
            text = f"{head} ({names}) VALUES {rows}"
        if node.conflict is not None:
            text += " " + self.on_conflict_clause(node.table, node.conflict)
        return text

    def _insert_value(self, column: Column[Any], value: Any) -> str:
        return "DEFAULT" if value is MISSING else self.value(column, value)

    def _require_filter(self, node: Update[Any] | Delete[Any], verb: str) -> None:
        if node.where_ is None and not node.all_rows_:
            raise QueryError(
                f"{verb} on {node.table.__name__} has no .where(); "
                "call .all_rows() to affect every row"
            )

    def visit_Update(self, node: Update[Any]) -> str:
        if not node.values_:
            raise QueryError(f"update({node.table.__name__}) needs .set(...)")
        self._require_filter(node, "update")
        text = f"UPDATE {self.table_name(node.table)} SET {self.set_list(node.table, node.values_)}"
        if node.where_ is not None:
            text += f" WHERE {self.visit(node.where_)}"
        return text

    def visit_Delete(self, node: Delete[Any]) -> str:
        self._require_filter(node, "delete")
        text = f"DELETE FROM {self.table_name(node.table)}"
        if node.where_ is not None:
            text += f" WHERE {self.visit(node.where_)}"
        return text

    def visit_Returning(self, node: Returning[Any]) -> str:
        return f"{self.visit(node.query)} {self.returning_clause(node.table)}"
