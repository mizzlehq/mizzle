"""Base SQL compiler. Walks a node tree and emits SQL text plus bind parameters.

Dialects subclass ``Compiler`` and override hooks (``param_marker``, ``returning_clause``...).
"""

from __future__ import annotations

import datetime
import enum
import json
import math
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import TYPE_CHECKING, cast

from mizzle.core.errors import QueryError
from mizzle.core.naming import Casing
from mizzle.core.schema.column import MISSING, ColumnBase
from mizzle.core.sql.alias import TableAlias
from mizzle.core.sql.functions import CaseBase, column_of, sql_type_of
from mizzle.core.sql.nodes import (
    Aggregate,
    AnyAll,
    Between,
    BinaryOp,
    BoolOp,
    Cast,
    CteColumn,
    Cube,
    Excluded,
    Exists,
    ExprBase,
    Fragment,
    Frame,
    Func,
    GroupingSets,
    Identifier,
    InList,
    Node,
    Not,
    Nullable,
    Operation,
    OrderBy,
    Over,
    Param,
    Placeholder,
    Postfix,
    PostfixOp,
    Raw,
    Rollup,
    Star,
    Window,
)

if TYPE_CHECKING:
    from mizzle.core.query.insert import Insert, OnConflict
    from mizzle.core.query.returning import Returning
    from mizzle.core.query.select import CompoundBase, Cte, SelectBase, Slot, Source
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
    # True when a Python .on_update() hook supplied a value: a prepared query must compile
    # again on each execution so the hook runs every time, not once at prepare().
    dynamic: bool = False
    # True when it calls a function named with func(): mizzle cannot know it leaves the
    # session alone (set_config, pg_advisory_lock...), so the connection is reset after it.
    # It may also write (nextval, a user function), so it is never run again after a lost
    # connection (R6). A replica may still run it: a write fails there with an error.
    opaque: bool = False
    # True when the statement writes at any level, including a data-modifying CTE inside a
    # SELECT. Such a SELECT is a write: it must run on the primary, must never be retried,
    # and explain(analyze=True) must refuse it.
    writes: bool = False

    @property
    def session_safe(self) -> bool:
        return not (self.raw or self.opaque)

    @property
    def pure_read(self) -> bool:
        """No row locks and no writes: a replica may run it."""
        return not (self.locking or self.writes)

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
        return CompiledQuery(
            self.sql,
            params,
            (None,) * len(params),
            self.raw,
            self.locking,
            self.dynamic,
            self.opaque,
            self.writes,
        )


_JSON = ("json", "jsonb")


def no_nul(text: str) -> str:
    """``text`` unchanged; a NUL in SQL text failed as a lost connection, then retried (Q18)."""
    if "\x00" in text:
        raise QueryError(f"SQL text cannot contain a NUL character: {text!r}")
    return text


def interval_text(value: datetime.timedelta) -> str:
    """``1 day 01:00:00``, ``-1 days +23:00:00``: PostgreSQL's own interval output."""
    parts: list[str] = []
    if value.days:
        parts.append(f"{value.days} day" + ("" if value.days == 1 else "s"))
    if value.seconds or value.microseconds or not parts:
        hours, rest = divmod(value.seconds, 3600)
        clock = f"{hours:02d}:{rest // 60:02d}:{rest % 60:02d}"
        if value.microseconds:
            clock += f".{value.microseconds:06d}".rstrip("0")
        parts.append(("+" if value.days < 0 else "") + clock)
    return " ".join(parts)


def _plain_column(item: Node) -> bool:
    expr = item.expr if isinstance(item, OrderBy) else item
    return isinstance(expr, ColumnBase | CteColumn)


type _Visitor = Callable[[Compiler, Node], str]
# (compiler class, node class) -> unbound visit_* method; found once, then reused.
_VISITORS: dict[tuple[type[Compiler], type[Node]], _Visitor] = {}


# ponytail: never evicted; one entry per (table, set of columns) actually inserted.
_INSERT_SQL: dict[
    tuple[type[Compiler], Casing | None, type[Table], tuple[str, ...], int, str | None], str
] = {}


class Compiler:
    """Single-use per ``compile()`` call: it accumulates params while walking the tree."""

    casing: Casing | None
    inline_literals: bool
    qualify_columns: bool
    _qualify_default: bool
    _params: list[object]
    _placeholders: list[Placeholder | None]
    _raw: bool
    _locking: bool
    _dynamic: bool
    _opaque: bool
    _writes: bool
    # Whether the statement being compiled returns its rows to Python, so output() converts
    # them (read_as); off inside a subquery that feeds a write or an expression.
    _read: bool
    # (id(Param), SQL type it is compared with) -> its marker: a reused node reuses its
    # parameter, unless raw SQL holding it sits next to values of different types.
    # Marker per (node id, context). The node is kept in the value: a temporary Param (an
    # ``any_([...])`` array) freed mid-compile let the next one reuse its id() and so its
    # marker, binding the wrong values (C19).
    _bound: dict[tuple[int, str | None], tuple[Param, str]]
    _context: str | None  # the SQL type a raw operand is compared with (see operand_of)
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
        self._qualify_default = qualify_columns
        self._params = []
        self._placeholders = []
        self._raw = False
        self._locking = False
        self._dynamic = False
        self._opaque = False
        self._writes = False
        self._bound = {}
        self._context = None
        self._scopes = []
        self._alias_count = 0
        # id(column) -> its output name, while a UNION's ORDER BY renders (C42).
        self._output_names: dict[int, str] = {}
        self._read = True

    def compile(self, node: Node) -> CompiledQuery:
        self._params, self._placeholders, self._raw, self._locking = [], [], False, False
        self._dynamic = False
        self._opaque = False
        self._writes = False
        self._bound = {}
        self._context = None
        self._read = True
        text = no_nul(self.visit(node))  # also raw sql() and collate() text (Q4-8)
        return CompiledQuery(
            text,
            tuple(self._params),
            tuple(self._placeholders),
            self._raw,
            self._locking,
            self._dynamic,
            self._opaque,
            self._writes,
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
        return '"' + no_nul(ident).replace('"', '""') + '"'

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
                # Quoted, so an IntEnum default on an enum column is its label '1' (B49); an
                # untyped quoted literal still coerces to a number column.
                return self.literal(str(cast(object, value.value)))
            case float() if not math.isfinite(value):  # inf is a column name (C44)
                return (
                    "'NaN'" if math.isnan(value) else ("'-Infinity'" if value < 0 else "'Infinity'")
                )
            case Decimal() if not value.is_finite():
                if value.is_snan():
                    raise QueryError("cannot write a signaling NaN as an SQL literal")
                return "'NaN'" if value.is_nan() else ("'-Infinity'" if value < 0 else "'Infinity'")
            case int() | float() | Decimal():
                return str(value)
            case datetime.timedelta():  # as PostgreSQL prints it, so defaults compare (C45)
                return self.literal(interval_text(value)) + "::interval"
            case bytes():
                return self.literal("\\x" + value.hex()) + "::bytea"
            case str():
                return "'" + no_nul(value).replace("'", "''") + "'"
            case datetime.date() | datetime.time() | uuid.UUID():
                return self.literal(str(value))
            case dict():
                return self.literal(json.dumps(value))
            case list() | tuple():  # an array, not JSON text (Q11); '{}' is typed by context
                items = cast(Sequence[object], value)
                return f"ARRAY[{', '.join(self.literal(v) for v in items)}]" if items else "'{}'"
            case _:
                raise QueryError(f"cannot write {type(value).__name__} as an SQL literal")

    def column_literal(self, column: ColumnBase, value: object) -> str:
        """``value`` (encoded) as a literal of ``column``'s type: a default, or a value
        compared with the column in DDL."""
        if column.sql_type in _JSON and value is not None:
            return self.literal(json.dumps(value))  # as the driver sends it, not an array (Q11)
        return self.literal(value)

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

    def operand_of(self, node: Node, other: Node) -> str:
        """``node`` next to ``other`` in an operator. PostgreSQL types the parameters of
        raw SQL from ``other``, so one ``sql("{}", x)`` used next to an integer and next
        to text gets a parameter for each."""
        if not isinstance(node, Raw):
            return self.operand(node)
        if isinstance(other, Cast):
            return self._typed_raw(node, other.type)
        column = column_of(other)
        return self._typed_raw(node, None if column is None else column.sql_type)

    def _typed_raw(self, node: Raw, sql_type: str | None) -> str:
        self._context = sql_type
        try:
            return self.visit(node)
        finally:
            self._context = None

    def value(self, column: ColumnBase, value: object) -> str:
        """A value written into a column (INSERT / UPDATE SET)."""
        if isinstance(value, Placeholder) and value.column is None:
            # Encoded by the column it fills, as in where(): an IntEnum or vector value
            # was sent raw (C33).
            value = replace(value, column=column)
        if isinstance(value, Node):
            return self.visit(value)
        return self.typed_param(column, self.bind(column.encode(value)))

    def typed_param(self, _column: ColumnBase | None, marker: str, /) -> str:
        """The parameter ``marker`` of a value for ``column``; a dialect may cast it."""
        return marker

    def output(self, node: Node) -> str:
        """An expression a query returns: converted by ``read_as`` when the rows reach
        Python, as is when they feed an outer statement (INSERT ... SELECT, a subquery)."""
        read, self._read = self._read, False
        text = self.visit(node)
        self._read = read
        return self.read_as(node, text) if read else text

    def read_as(self, _node: Node, text: str, /) -> str:
        """``text``, an expression whose value is returned to Python; a dialect may cast it."""
        return text

    # -- expressions ------------------------------------------------------------------

    def visit_Column(self, node: ColumnBase) -> str:
        if node.table is None:
            raise QueryError(f"column {node.attr!r} is not attached to a table")
        if not self.qualify_columns:
            name = self._output_names.get(id(node))
            return self.column_name(node) if name is None else self.quote(name)
        for scope in reversed(self._scopes):
            if node.table in scope:
                return f"{self.quote(scope[node.table])}.{self.column_name(node)}"
        return f"{self.quote(node.table.__table__.name)}.{self.column_name(node)}"

    def visit_Param(self, node: Param) -> str:
        # The same node twice (an expression both selected and grouped by) binds once, so
        # PostgreSQL sees identical expressions.
        key = (id(node), self._context)
        bound = self._bound.get(key)
        if bound is None:
            value = node.column.encode(node.value) if node.column is not None else node.value
            if self.inline_literals and node.column is not None:
                # In the column's type: ARRAY['a'] was text[] next to varchar[] (Q4-1).
                bound = self._bound[key] = (node, self.column_literal(node.column, value))
            else:
                bound = self._bound[key] = (node, self.typed_param(node.column, self.bind(value)))
        return bound[1]

    def visit_Placeholder(self, node: Placeholder) -> str:
        return self.typed_param(node.column, self.bind(None, node))

    def visit_BinaryOp(self, node: BinaryOp) -> str:
        left, right = node.left, node.right
        lhs = self._operand_for(left, right)
        rhs = self._operand_for(right, left)
        return f"{lhs} {node.op} {rhs}"

    def _operand_for(self, node: Node, other: Node) -> str:
        # `col = ANY([...])`: the array's element type comes from the value it is
        # compared against, so an enum list is a `role[]` parameter, not `text[]`.
        if issubclass(type(node), AnyAll):
            quantified = cast("AnyAll[object]", node)
            value, column = quantified.value, column_of(other)
            listed = quantified.values is not None or isinstance(value, Placeholder)
            if listed and column is not None and column.item is not None:
                # PostgreSQL has no array-of-arrays type for the parameter (Q4-3).
                raise QueryError(
                    f"{column.qualified_name} is an array: ANY/ALL (and in_() with a "
                    "placeholder) cannot compare it with a list of arrays; use in_() with "
                    "a list of values"
                )
            if quantified.values is not None:
                inner = self._quantified_array(quantified.values, other)
                return f"{quantified.quantifier} ({inner})"
            if isinstance(value, Placeholder) and value.column is None and column is not None:
                # A placeholder list is encoded like a literal one (Q5).
                array = replace(value, column=self._array_column(column))
                return f"{quantified.quantifier} ({self.visit(array)})"
        return self.operand_of(node, other)

    def visit_BoolOp(self, node: BoolOp) -> str:
        return f" {node.op} ".join(self.operand(item) for item in node.items)

    def visit_Not(self, node: Not) -> str:
        return f"NOT ({self.visit(node.item)})"

    def visit_Postfix(self, node: Postfix) -> str:
        return f"{self.operand(node.item)} {node.op}"

    def visit_PostfixOp(self, node: PostfixOp[object]) -> str:
        return f"{self.operand(node.item)} {node.op} {self.visit(node.right)}"

    def visit_AnyAll(self, node: AnyAll[object]) -> str:
        if node.values is not None:
            return f"{node.quantifier} ({self._quantified_array(node.values, None)})"
        if node.value is None:
            raise QueryError("ANY/ALL without a value")
        return f"{node.quantifier} ({self.visit(node.value)})"

    def _quantified_array(self, values: tuple[object, ...], other: Node | None) -> str:
        """The array operand of ``ANY``/``ALL``. Next to a column it is a parameter
        typed (and encoded) like that column's array; otherwise the Python values
        decide the cast."""
        items = list(values)
        column = column_of(other) if other is not None else None
        if column is not None:
            return self.visit(Param(items, self._array_column(column)))
        kind = next((t for v in items if (t := sql_type_of(v)) is not None), "text")
        return self.visit(Cast(Param(items), kind + "[]"))

    @staticmethod
    def _array_column(column: ColumnBase) -> ColumnBase:
        return column if column.item is not None else column.array_column()

    def visit_InList(self, node: InList) -> str:
        if isinstance(node.values, tuple):
            if not node.values:  # IN () is a syntax error; the meaning is "matches nothing"
                return "true" if node.negate else "false"
            inner = ", ".join(self.operand_of(v, node.item) for v in node.values)
        else:
            inner = self.visit(node.values)
        op = "NOT IN" if node.negate else "IN"
        return f"{self.operand(node.item)} {op} ({inner})"

    def visit_Between(self, node: Between) -> str:
        item = self.operand_of(node.item, node.low)
        low, high = self.operand_of(node.low, node.item), self.operand_of(node.high, node.item)
        return f"{item} BETWEEN {low} AND {high}"

    def visit_Exists(self, node: Exists) -> str:
        return f"{'NOT ' if node.negate else ''}EXISTS ({self.visit(node.query)})"

    def visit_Func(self, node: Func) -> str:
        self._opaque |= node.opaque
        args = ", ".join(self.visit(a) for a in node.args)
        text = f"{node.name}({'DISTINCT ' if node.distinct else ''}{args})"
        if isinstance(node, Aggregate):
            if node.filter_ is not None:
                text += f" FILTER (WHERE {self.visit(node.filter_)})"
            if node.within_group_:
                ordered = ", ".join(self.visit(item) for item in node.within_group_)
                text += f" WITHIN GROUP (ORDER BY {ordered})"
        return text

    def visit_Star(self, _node: Star) -> str:
        return "*"

    def visit_Rollup(self, node: Rollup) -> str:
        return f"ROLLUP ({', '.join(self.visit(item) for item in node.items)})"

    def visit_Cube(self, node: Cube) -> str:
        return f"CUBE ({', '.join(self.visit(item) for item in node.items)})"

    def visit_GroupingSets(self, node: GroupingSets) -> str:
        rendered = [f"({', '.join(self.visit(item) for item in group)})" for group in node.sets]
        return f"GROUPING SETS ({', '.join(rendered)})"

    def visit_OrderBy(self, node: OrderBy) -> str:
        text = f"{self.visit(node.expr)} {'DESC' if node.descending else 'ASC'}"
        return f"{text} NULLS {node.nulls.upper()}" if node.nulls else text

    def visit_Raw(self, node: Raw) -> str:
        self._raw = True
        # The context types only the Raw's own parameters, not nodes nested in it.
        context = self._context
        parts: list[str] = []
        for part in node.parts:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, Param):
                self._context = context
                parts.append(self.visit(part))
            else:
                self._context = None
                parts.append(self.visit(part))
        self._context = None
        return "".join(parts)

    def visit_Nullable(self, node: Nullable) -> str:
        return self.visit(node.expr)

    def visit_Identifier(self, node: Identifier) -> str:
        return self.quote(node.name)

    def visit_Excluded(self, node: Excluded) -> str:
        return f"excluded.{self.column_name(node.column)}"

    def visit_Operation(self, node: Operation) -> str:
        left, right = node.left, node.right
        return f"({self.operand_of(left, right)} {node.op} {self.operand_of(right, left)})"

    def visit_Cast(self, node: Cast) -> str:
        expr = node.expr
        if (
            self.inline_literals
            and node.type in _JSON
            and isinstance(expr, Param)
            and isinstance(expr.value, list | tuple | dict)
        ):  # JSON text, not CAST(ARRAY[1, 2] AS jsonb) (Q4-2)
            return f"CAST({self.literal(json.dumps(cast(object, expr.value)))} AS {node.type})"
        inner = self._typed_raw(expr, node.type) if isinstance(expr, Raw) else self.visit(expr)
        return f"CAST({inner} AS {node.type})"

    def visit_Fragment(self, node: Fragment) -> str:
        return "".join(p if isinstance(p, str) else self.visit(p) for p in node.parts)

    def visit_CaseBase(self, node: CaseBase) -> str:
        parts = ["CASE"]
        for condition, value in node.whens:
            parts.append(f"WHEN {self.visit(condition)} THEN {self.visit(value)}")
        if node.default is not None:
            parts.append(f"ELSE {self.visit(node.default)}")
        parts.append("END")
        return " ".join(parts)

    def visit_Over(self, node: Over) -> str:
        func, spec = self.visit(node.func), node.window
        if spec.base is not None and not (spec.partition_by or spec.order_by or spec.frame):
            return f"{func} OVER {self.quote(spec.base)}"
        return f"{func} OVER ({self.window(spec)})"

    def window(self, spec: Window) -> str:
        parts: list[str] = []
        if spec.base is not None:
            parts.append(self.quote(spec.base))
        if spec.partition_by:
            parts.append("PARTITION BY " + ", ".join(self.visit(e) for e in spec.partition_by))
        if spec.order_by:
            parts.append("ORDER BY " + ", ".join(self.visit(e) for e in spec.order_by))
        if spec.frame is not None:
            parts.append(self.frame(spec.frame))
        return " ".join(parts)

    def frame(self, frame: Frame) -> str:
        def bound(offset: int | None, unbounded: str) -> str:
            if offset is None:
                return f"UNBOUNDED {unbounded}"
            if offset == 0:
                return "CURRENT ROW"
            return f"{abs(offset)} {'PRECEDING' if offset < 0 else 'FOLLOWING'}"

        start, end = bound(frame.start, "PRECEDING"), bound(frame.end, "FOLLOWING")
        return f"{frame.kind} BETWEEN {start} AND {end}"

    # -- SELECT -----------------------------------------------------------------------

    def source(self, source: Source) -> str:
        if isinstance(source, type):
            if issubclass(source, TableAlias):
                return f"{self.table_name(source.__alias_of__)} AS {self.table_name(source)}"
            return self.table_name(source)
        if source.inline:  # the outer select list converts what it reads (Q6-8)
            read, self._read = self._read, False
            text = f"({self.visit(source.query)}) AS {self.quote(source.alias or source.name)}"
            self._read = read
            return text
        if source.alias is not None:
            return f"{self.quote(source.name)} AS {self.quote(source.alias)}"
        return self.quote(source.name)

    def select_slot(self, slot: Slot) -> str:
        if isinstance(slot, type):
            return ", ".join(self.output(c) for c in slot.__table__.columns.values())
        return self.output(slot)

    def count_value(self, value: int | Placeholder) -> str:
        return self.visit(value) if isinstance(value, Placeholder) else self.bind(value)

    def visit_CteColumn(self, node: CteColumn) -> str:
        if not self.qualify_columns:
            return self.quote(node.name)
        return f"{self.quote(node.cte.alias or node.cte.name)}.{self.quote(node.name)}"

    def visit_CompoundBase(self, node: CompoundBase) -> str:
        parts = [f"({self.visit(node.left)}) {node.op} ({self.visit(node.right)})"]
        if not all(_plain_column(e) for e in node.order_by_):
            # ORDER BY of a UNION... only takes output column names; ordering by an
            # expression of them needs the combined rows as a subquery.
            parts = ["SELECT * FROM (" + parts[0] + ') AS "_combined"']
        if node.order_by_:
            # The combined result has only its output columns: refer to them by name. A
            # column selected under a dict key is named by that key (C42).
            from mizzle.core.query.select import CompoundBase  # import cycle at module level

            first: SelectBase | CompoundBase = node
            while isinstance(first, CompoundBase):
                first = first.left
            names = first.projection if isinstance(first.projection, dict) else {}
            outputs = {id(e): key for key, e in names.items() if isinstance(e, ColumnBase)}
            qualify, self.qualify_columns = self.qualify_columns, False
            saved, self._output_names = self._output_names, outputs
            try:
                parts.append("ORDER BY " + ", ".join(self.visit(e) for e in node.order_by_))
            finally:
                self.qualify_columns = qualify
                self._output_names = saved
        if node.limit_ is not None:
            parts.append(f"LIMIT {self.count_value(node.limit_)}")
        if node.offset_ is not None:
            parts.append(f"OFFSET {self.count_value(node.offset_)}")
        return " ".join(parts)

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
        # A nested SELECT (e.g. EXISTS in a UNION's ORDER BY) names its own tables again.
        qualify, self.qualify_columns = self.qualify_columns, self._qualify_default
        saved, self._output_names = self._output_names, {}
        read = self._read
        try:
            if read and node.distinct_ and any(self._converted(e) for e in node.order_by_):
                return self._sorted_distinct(node)
            return self._select_body(node)
        finally:
            self.qualify_columns = qualify
            self._output_names = saved
            self._read = read

    def _converted(self, node: Node) -> bool:
        """Whether ``read_as`` converts ``node`` where rows reach Python."""
        expr = node.expr if isinstance(node, OrderBy) else node
        return self.read_as(expr, "") != ""

    def _sorted_distinct(self, node: SelectBase) -> str:
        """SELECT DISTINCT orders only by its outputs, so ordering by one read_as converts
        would sort by the converted value: select and order the real values in a subquery
        and convert them around it (Q6-7)."""
        # ponytail: relies on PostgreSQL keeping a FROM subquery's order through a plain
        # projection; repeat the ORDER BY outside (by output name) if a dialect does not.
        named: list[tuple[str | None, ExprBase]]
        if isinstance(node.projection, dict):
            named = list(node.projection.items())
        else:
            named = [
                (None, e)
                for slot in node.slots()
                for e in (slot.__table__.columns.values() if isinstance(slot, type) else (slot,))
            ]
        self._read = False
        hidden = {f"_{i}": e for i, (_, e) in enumerate(named)}
        inner = self._select_body(replace(node, projection=hidden))
        self._read = True
        items = [
            self.read_as(e, f"{self.quote('_d')}.{self.quote(f'_{i}')}")
            + ("" if key is None else f" AS {self.quote(key)}")
            for i, (key, e) in enumerate(named)
        ]
        return f"SELECT {', '.join(items)} FROM ({inner}) AS {self.quote('_d')}"

    def _note_write(self) -> None:
        """Mark the statement as a write; a subclass visitor (MERGE) calls this. What it
        reads from (INSERT ... SELECT, MERGE USING) keeps its types (Q6-4); its RETURNING
        is read as the statement holding it was (visit_Returning)."""
        self._writes = True
        self._unread()

    def _unread(self) -> None:
        """What follows is not read by Python: subqueries in it keep their types."""
        self._read = False

    def _with_ctes(self, ctes: Sequence[Cte]) -> str:
        """The ``WITH`` prefix for a select or a write that carries CTEs (a data-modifying
        CTE inside a write). Empty when there are none."""
        if not ctes:
            return ""
        keyword = "WITH RECURSIVE" if any(c.recursive for c in ctes) else "WITH"
        read = self._read  # the outer select list converts what it reads from them (Q6-8)
        rendered: list[str] = []
        for c in ctes:
            self._read = False
            rendered.append(f"{self.quote(c.name)} AS ({self.visit(c.query)})")
        self._read = read
        return f"{keyword} {', '.join(rendered)} "

    def _select_body(self, node: SelectBase) -> str:
        if node.lock is not None:
            self._locking = True
        # Clauses are emitted strictly left to right so $n numbering follows the text.
        parts: list[str] = []
        if node.ctes:
            parts.append(self._with_ctes(node.ctes).rstrip())
        if node.distinct_ and node.distinct_on_:
            raise QueryError("distinct() and distinct_on() cannot be combined")
        read = self._read  # the select list reaches the reader
        if node.distinct_on_:
            self._read = False
            columns = ", ".join(self.visit(e) for e in node.distinct_on_)
            self._read = read
            parts.append(f"SELECT DISTINCT ON ({columns})")
        elif node.distinct_:
            parts.append("SELECT DISTINCT")
        else:
            parts.append("SELECT")
        if isinstance(node.projection, dict):
            items = [f"{self.output(e)} AS {self.quote(k)}" for k, e in node.projection.items()]
        else:
            items = [self.select_slot(slot) for slot in node.slots()]
        parts.append(", ".join(items))
        from_ = f"FROM {self.source(node.source)}"
        if node.tablesample_ is not None:
            sample = node.tablesample_
            clause = f"TABLESAMPLE {sample.method} ({self.bind(sample.percent)})"
            if sample.seed is not None:
                clause += f" REPEATABLE ({self.bind(sample.seed)})"
            from_ += " " + clause
        parts.append(from_)
        for join in node.joins:
            self._read = False
            on = "" if join.on is None else f" ON {self.visit(join.on)}"
            self._read = read
            lateral = " LATERAL" if join.lateral else ""
            if join.on is None and join.kind != "CROSS":  # LATERAL without a condition
                on = " ON true"
            parts.append(f"{join.kind} JOIN{lateral} {self.source(join.source)}{on}")
        self._unread()  # the rest are expressions
        if node.where_ is not None:
            parts.append(f"WHERE {self.visit(node.where_)}")
        if node.group_by_:
            parts.append("GROUP BY " + ", ".join(self.visit(e) for e in node.group_by_))
        if node.having_ is not None:
            parts.append(f"HAVING {self.visit(node.having_)}")
        if node.windows_:
            windows = (f"{self.quote(n)} AS ({self.window(w)})" for n, w in node.windows_)
            parts.append("WINDOW " + ", ".join(windows))
        if node.order_by_:
            parts.append("ORDER BY " + ", ".join(self.visit(e) for e in node.order_by_))
        if node.limit_ is not None:
            parts.append(f"LIMIT {self.count_value(node.limit_)}")
        if node.offset_ is not None:
            parts.append(f"OFFSET {self.count_value(node.offset_)}")
        if node.lock is not None:
            lock = f"FOR {node.lock.strength}"
            if node.lock.of:
                lock += " OF " + ", ".join(self.quote(t.__table__.name) for t in node.lock.of)
            if node.lock.skip_locked:
                lock += " SKIP LOCKED"
            if node.lock.nowait:
                lock += " NOWAIT"
            parts.append(lock)
        return " ".join(parts)

    # -- INSERT / UPDATE / DELETE -----------------------------------------------------

    def returning_clause(self, _node: Returning[object], /) -> str:
        raise QueryError(f"{type(self).__name__} does not support RETURNING")

    def on_conflict_clause(self, _table: type[Table], _conflict: OnConflict, /) -> str:
        raise QueryError(f"{type(self).__name__} does not support ON CONFLICT")

    def set_list(self, table: type[Table], values: Mapping[str, object]) -> str:
        columns = table.__table__.columns
        items = [
            f"{self.column_name(columns[k])} = {self.value(columns[k], v)}"
            for k, v in values.items()
        ]
        for attr, column in columns.items():  # .on_update() / .on_update_now() hooks
            hook = column.on_update_value
            if hook is None or attr in values:
                continue
            if isinstance(hook, Node):
                value: object = hook
            else:
                value = hook()
                self._dynamic = True
            column.validate(value)
            items.append(f"{self.column_name(column)} = {self.value(column, value)}")
        return ", ".join(items)

    def visit_Insert(self, node: Insert[Table]) -> str:
        self._note_write()
        prefix = self._with_ctes(node.ctes)
        if node.source is not None:
            return prefix + self._insert_select_text(node, node.source)
        if not node.rows:
            raise QueryError(f"insert into {node.table.__name__} has no values()")
        if node.defaulted:
            # A default_fn runs per execution, so a prepared statement compiles again (B43).
            node = node.with_defaults()
            self._dynamic = True
        if node.identity is not None and node.overriding is None:
            raise QueryError(
                f"{node.identity.qualified_name} is GENERATED ALWAYS AS IDENTITY; the "
                "database assigns it. Use .overriding_system_value() to insert explicit values"
            )
        # Fast path for the most common write: one row of plain values, no ON CONFLICT.
        # Its SQL depends only on which columns are set, so the text is cached and only
        # the values are bound (in column order, as the full path below binds them).
        if (
            node.conflict is None
            and len(node.rows) == 1
            and not self.inline_literals
            and not node.ctes
        ):
            row = node.rows[0]
            if row and not any(isinstance(v, Node) for v in row.values()):
                columns = node.table.__table__.columns
                # Table order, as _insert_text lists the columns. Binding in the dict's own
                # order swapped values once with_defaults() appended a default_fn column (C1).
                attrs = tuple(a for a in columns if a in row)
                key = (
                    type(self),
                    self.casing,
                    node.table,
                    attrs,
                    len(self._params),
                    node.overriding,
                )
                text = _INSERT_SQL.get(key)
                if text is None:
                    text = _INSERT_SQL[key] = self._insert_text(node)
                    return text
                for attr in attrs:
                    value = row[attr]
                    encoder = columns[attr].encoder
                    self._params.append(
                        value if value is None or encoder is None else encoder(value)
                    )
                    self._placeholders.append(None)
                return text
        return prefix + self._insert_text(node)

    def _insert_select_text(self, node: Insert[Table], source: SelectBase | CompoundBase) -> str:
        if node.identity is not None and node.overriding is None:
            raise QueryError(
                f"{node.identity.qualified_name} is GENERATED ALWAYS AS IDENTITY; the "
                "database assigns it. Use .overriding_system_value() to insert explicit values"
            )
        names = ", ".join(self.column_name(c) for c in node.source_columns)
        overriding = f" OVERRIDING {node.overriding} VALUE" if node.overriding else ""
        text = f"INSERT INTO {self.source(node.table)} ({names}){overriding} {self.visit(source)}"
        if node.conflict is not None:
            text += " " + self.on_conflict_clause(node.table, node.conflict)
        return text

    def _insert_text(self, node: Insert[Table]) -> str:
        columns = node.table.__table__.columns
        used = [attr for attr in columns if any(attr in row for row in node.rows)]
        head = f"INSERT INTO {self.source(node.table)}"
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
            overriding = f" OVERRIDING {node.overriding} VALUE" if node.overriding else ""
            text = f"{head} ({names}){overriding} VALUES {rows}"
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
        return self.typed_param(column, self.param_marker(len(self._params)))

    def _require_filter(self, node: Update[Table] | Delete[Table], verb: str) -> None:
        if node.where_ is None and not node.all_rows_:
            raise QueryError(
                f"{verb} on {node.table.__name__} has no .where(); "
                "call .all_rows() to affect every row"
            )

    def visit_Update(self, node: Update[Table]) -> str:
        self._note_write()
        if not node.values_:
            raise QueryError(f"update({node.table.__name__}) needs .set(...)")
        self._require_filter(node, "update")
        prefix = self._with_ctes(node.ctes)
        text = f"UPDATE {self.source(node.table)} SET {self.set_list(node.table, node.values_)}"
        if node.from_sources:
            text += " FROM " + ", ".join(self.source(t) for t in node.from_sources)
        if node.where_ is not None:
            text += f" WHERE {self.visit(node.where_)}"
        return prefix + text

    def visit_Delete(self, node: Delete[Table]) -> str:
        self._note_write()
        self._require_filter(node, "delete")
        prefix = self._with_ctes(node.ctes)
        text = f"DELETE FROM {self.source(node.table)}"
        if node.using_:
            text += " USING " + ", ".join(self.source(t) for t in node.using_)
        if node.where_ is not None:
            text += f" WHERE {self.visit(node.where_)}"
        return prefix + text

    def visit_Returning(self, node: Returning[object]) -> str:
        read = self._read
        text = self.visit(node.query)
        self._read = read
        return f"{text} {self.returning_clause(node)}"
