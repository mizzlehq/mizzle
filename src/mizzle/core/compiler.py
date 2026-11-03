"""Base SQL compiler. Walks a node tree and emits SQL text plus bind parameters.

Dialects subclass ``Compiler`` and override hooks (``param_marker``, ``returning_clause``...).
"""

from __future__ import annotations

import datetime
import enum
import json
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, cast

from mizzle.core.errors import QueryError
from mizzle.core.naming import Casing
from mizzle.core.schema.column import MISSING, ColumnBase
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
    Nullable,
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
    from mizzle.core.query.select import CteColumn, SelectBase, Slot, Source
    from mizzle.core.query.update import Delete, Update
    from mizzle.core.schema.table import Table


@dataclass(frozen=True)
class CompiledQuery:
    sql: str
    params: tuple[object, ...]
    placeholders: tuple[Placeholder | None, ...]  # per param slot; set where a placeholder sits
    # True when the statement contains raw ``sql(...)``, which might change session state
    # (SET, LISTEN, advisory locks...); the pool then resets the connection after use.
    raw: bool = False
    # True when any SELECT in it (also in a CTE or subquery) takes row locks (FOR UPDATE):
    # it must run on the primary.
    locking: bool = False

    @property
    def has_placeholders(self) -> bool:
        return any(p is not None for p in self.placeholders)

    def bind(self, values: Mapping[str, object]) -> CompiledQuery:
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
        return CompiledQuery(self.sql, params, (None,) * len(params), self.raw, self.locking)


type _Visitor = Callable[[Compiler, Node], str]
# (compiler class, node class) -> unbound visit_* method; found once, then reused.
_VISITORS: dict[tuple[type[Compiler], type[Node]], _Visitor] = {}


# ponytail: never evicted; one entry per (table, set of columns) actually inserted.
_INSERT_SQL: dict[tuple[type[Compiler], Casing | None, type[Table], tuple[str, ...], int], str] = {}


class Compiler:
    """Single-use per ``compile()`` call: it accumulates params while walking the tree."""

    casing: Casing | None
    inline_literals: bool
    qualify_columns: bool
    _params: list[object]
    _placeholders: list[Placeholder | None]
    _raw: bool
    _locking: bool
    _scopes: list[dict[type[Table], str]]  # table -> alias, for aliased (relational) queries
    _alias_count: int

    def __init__(
        self,
        casing: Casing | None = None,
        *,
        inline_literals: bool = False,
        qualify_columns: bool = True,
    ) -> None:
        """``inline_literals`` and ``qualify_columns=False`` produce DDL-safe SQL (defaults,
        CHECK constraints, index predicates), where bind parameters are not allowed."""
        self.casing = casing
        self.inline_literals = inline_literals
        self.qualify_columns = qualify_columns
        self._params = []
        self._placeholders = []
        self._raw = False
        self._locking = False
        self._scopes = []
        self._alias_count = 0

    def compile(self, node: Node) -> CompiledQuery:
        self._params, self._placeholders, self._raw, self._locking = [], [], False, False
        text = self.visit(node)
        return CompiledQuery(
            text, tuple(self._params), tuple(self._placeholders), self._raw, self._locking
        )

    def visit(self, node: Node) -> str:
        kind = type(node)
        try:
            visitor = _VISITORS[type(self), kind]
        except KeyError:
            visitor = _VISITORS[type(self), kind] = self._find_visitor(kind)
        return visitor(self, node)

    def _find_visitor(self, kind: type[Node]) -> _Visitor:
        # Walk the MRO so subclasses (e.g. jsonb -> Column) reuse their parent's visitor.
        for cls in kind.__mro__:
            method: object = getattr(type(self), f"visit_{cls.__name__}", None)
            if method is not None:
                return cast(_Visitor, method)
        raise QueryError(f"{type(self).__name__} cannot compile {kind.__name__}")

    # -- hooks ------------------------------------------------------------------------

    def param_marker(self, _index: int, /) -> str:
        return "?"

    def quote(self, ident: str) -> str:
        return '"' + ident.replace('"', '""') + '"'

    def bind(self, value: object, placeholder: Placeholder | None = None) -> str:
        if self.inline_literals:
            if placeholder is not None:
                raise QueryError("placeholders cannot be used in DDL")
            return self.literal(value)
        self._params.append(value)
        self._placeholders.append(placeholder)
        return self.param_marker(len(self._params))

    def literal(self, value: object) -> str:
        """An SQL literal. Only for DDL; queries always use bind parameters."""
        match value:
            case None:
                return "NULL"
            case bool():
                return "true" if value else "false"
            case enum.Enum():
                member_value: object = value.value  # pyright: ignore[reportAny]  # Enum.value is Any in typeshed
                return self.literal(member_value)
            case int() | float() | Decimal():
                return str(value)
            case str():
                return "'" + value.replace("'", "''") + "'"
            case datetime.date() | datetime.time() | uuid.UUID():
                return self.literal(str(value))
            case dict() | list():
                return self.literal(json.dumps(value))
            case _:
                raise QueryError(f"cannot write {type(value).__name__} as an SQL literal")

    def new_alias(self) -> str:
        self._alias_count += 1
        return f"t{self._alias_count}"

    def table_name(self, table: type[Table]) -> str:
        info = table.__table__
        name = self.quote(info.name)
        return f"{self.quote(info.schema)}.{name}" if info.schema else name

    def column_name(self, column: ColumnBase) -> str:
        return self.quote(column.db_name(self.casing))

    def operand(self, node: Node) -> str:
        """An expression used inside a larger one; boolean groups get parentheses."""
        text = self.visit(node)
        return f"({text})" if isinstance(node, BoolOp | Not) else text

    def value(self, column: ColumnBase, value: object) -> str:
        """A value written into a column (INSERT / UPDATE SET)."""
        return self.visit(value) if isinstance(value, Node) else self.bind(column.encode(value))

    # -- expressions ------------------------------------------------------------------

    def visit_Column(self, node: ColumnBase) -> str:
        if node.table is None:
            raise QueryError(f"column {node.attr!r} is not attached to a table")
        if not self.qualify_columns:
            return self.column_name(node)
        for scope in reversed(self._scopes):
            if node.table in scope:
                return f"{self.quote(scope[node.table])}.{self.column_name(node)}"
        return f"{self.quote(node.table.__table__.name)}.{self.column_name(node)}"

    def visit_Param(self, node: Param) -> str:
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

    def visit_Func(self, node: Func) -> str:
        args = ", ".join(self.visit(a) for a in node.args)
        return f"{node.name}({'DISTINCT ' if node.distinct else ''}{args})"

    def visit_Star(self, _node: Star) -> str:
        return "*"

    def visit_OrderBy(self, node: OrderBy) -> str:
        text = f"{self.visit(node.expr)} {'DESC' if node.descending else 'ASC'}"
        return f"{text} NULLS {node.nulls.upper()}" if node.nulls else text

    def visit_Raw(self, node: Raw) -> str:
        self._raw = True
        return "".join(p if isinstance(p, str) else self.visit(p) for p in node.parts)

    def visit_Nullable(self, node: Nullable) -> str:
        return self.visit(node.expr)

    def visit_Identifier(self, node: Identifier) -> str:
        return self.quote(node.name)

    def visit_Excluded(self, node: Excluded) -> str:
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

    def visit_SelectBase(self, node: SelectBase) -> str:
        if not self._scopes:
            return self._select_text(node)
        # A subquery inside a relational query: its own tables shadow the outer aliases.
        sources = (node.source, *(j.source for j in node.joins))
        self._scopes.append({s: s.__table__.name for s in sources if isinstance(s, type)})
        try:
            return self._select_text(node)
        finally:
            self._scopes.pop()

    def _select_text(self, node: SelectBase) -> str:
        if node.lock is not None:
            self._locking = True
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

    def returning_clause(self, _table: type[Table], /) -> str:
        raise QueryError(f"{type(self).__name__} does not support RETURNING")

    def on_conflict_clause(self, _table: type[Table], _conflict: OnConflict, /) -> str:
        raise QueryError(f"{type(self).__name__} does not support ON CONFLICT")

    def set_list(self, table: type[Table], values: Mapping[str, object]) -> str:
        columns = table.__table__.columns
        return ", ".join(
            f"{self.column_name(columns[k])} = {self.value(columns[k], v)}"
            for k, v in values.items()
        )

    def visit_Insert(self, node: Insert[Table]) -> str:
        if not node.rows:
            raise QueryError(f"insert into {node.table.__name__} has no values()")
        # Fast path for the most common write: one row of plain values, no ON CONFLICT.
        # Its SQL depends only on which columns are set, so the text is cached and only
        # the values are bound (in column order, as the full path below binds them).
        if node.conflict is None and len(node.rows) == 1 and not self.inline_literals:
            row = node.rows[0]
            if row and not any(isinstance(v, Node) for v in row.values()):
                key = (type(self), self.casing, node.table, tuple(row), len(self._params))
                text = _INSERT_SQL.get(key)
                if text is None:
                    text = _INSERT_SQL[key] = self._insert_text(node)
                    return text
                columns = node.table.__table__.columns
                for attr, value in row.items():
                    encoder = columns[attr].encoder
                    self._params.append(
                        value if value is None or encoder is None else encoder(value)
                    )
                    self._placeholders.append(None)
                return text
        return self._insert_text(node)

    def _insert_text(self, node: Insert[Table]) -> str:
        columns = node.table.__table__.columns
        used = [attr for attr in columns if any(attr in row for row in node.rows)]
        head = f"INSERT INTO {self.table_name(node.table)}"
        if not used and len(node.rows) == 1:
            text = f"{head} DEFAULT VALUES"
        else:
            used = used or [next(iter(columns))]  # several all-default rows: VALUES (DEFAULT)
            names = ", ".join(self.column_name(columns[a]) for a in used)
            targets = [(a, columns[a]) for a in used]
            rows = ", ".join(
                "("
                + ", ".join([self._insert_value(c, row.get(a, MISSING)) for a, c in targets])
                + ")"
                for row in node.rows
            )
            text = f"{head} ({names}) VALUES {rows}"
        if node.conflict is not None:
            text += " " + self.on_conflict_clause(node.table, node.conflict)
        return text

    def _insert_value(self, column: ColumnBase, value: object) -> str:
        # Inlined value()/bind(): bulk inserts run this for every value.
        if value is MISSING:
            return "DEFAULT"
        if isinstance(value, Node) or self.inline_literals:
            return self.value(column, value)
        if value is not None and column.encoder is not None:
            value = column.encoder(value)
        self._params.append(value)
        self._placeholders.append(None)
        return self.param_marker(len(self._params))

    def _require_filter(self, node: Update[Table] | Delete[Table], verb: str) -> None:
        if node.where_ is None and not node.all_rows_:
            raise QueryError(
                f"{verb} on {node.table.__name__} has no .where(); "
                "call .all_rows() to affect every row"
            )

    def visit_Update(self, node: Update[Table]) -> str:
        if not node.values_:
            raise QueryError(f"update({node.table.__name__}) needs .set(...)")
        self._require_filter(node, "update")
        text = f"UPDATE {self.table_name(node.table)} SET {self.set_list(node.table, node.values_)}"
        if node.where_ is not None:
            text += f" WHERE {self.visit(node.where_)}"
        return text

    def visit_Delete(self, node: Delete[Table]) -> str:
        self._require_filter(node, "delete")
        text = f"DELETE FROM {self.table_name(node.table)}"
        if node.where_ is not None:
            text += f" WHERE {self.visit(node.where_)}"
        return text

    def visit_Returning(self, node: Returning[Table]) -> str:
        return f"{self.visit(node.query)} {self.returning_clause(node.table)}"
