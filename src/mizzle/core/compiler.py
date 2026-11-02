"""Base SQL compiler. Walks a node tree and emits SQL text plus bind parameters.

Dialects subclass ``Compiler`` and override hooks (``param_marker``, ``returning_clause``...).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from mizzle.core.errors import QueryError
from mizzle.core.naming import Casing
from mizzle.core.schema.column import Column
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
