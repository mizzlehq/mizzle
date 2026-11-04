"""Base SQL compiler. Walks a node tree and emits SQL text plus bind parameters.

Dialects subclass ``Compiler`` and override hooks (``param_marker``, ``returning_clause``...).
"""

from __future__ import annotations

import datetime
import enum
import json
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, cast

from mizzle.core.errors import QueryError
from mizzle.core.naming import Casing
from mizzle.core.schema.column import MISSING, ColumnBase
from mizzle.core.sql.alias import TableAlias
from mizzle.core.sql.functions import CaseBase, column_of
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
    # Unlike raw, a read using one may still be retried after a lost connection.
    opaque: bool = False
    # True when the statement writes at any level, including a data-modifying CTE inside a
    # SELECT. Such a SELECT is a write: it must run on the primary, must never be retried,
    # and explain(analyze=True) must refuse it.
    writes: bool = False

    @property
    def session_safe(self) -> bool:
        return not (self.raw or self.opaque)

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
    # (id(Param), SQL type it is compared with) -> its marker: a reused node reuses its
    # parameter, unless raw SQL holding it sits next to values of different types.
    _bound: dict[tuple[int, str | None], str]
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

    def compile(self, node: Node) -> CompiledQuery:
        self._params, self._placeholders, self._raw, self._locking = [], [], False, False
        self._dynamic = False
        self._opaque = False
        self._writes = False
        self._bound = {}
        self._context = None
        text = self.visit(node)
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
        # The same node twice (an expression both selected and grouped by) binds once, so
        # PostgreSQL sees identical expressions.
        key = (id(node), self._context)
        marker = self._bound.get(key)
        if marker is None:
            value = node.column.encode(node.value) if node.column is not None else node.value
            marker = self._bound[key] = self.bind(value)
        return marker

    def visit_Placeholder(self, node: Placeholder) -> str:
        return self.bind(None, node)

    def visit_BinaryOp(self, node: BinaryOp) -> str:
        left, right = node.left, node.right
        return f"{self.operand_of(left, right)} {node.op} {self.operand_of(right, left)}"

    def visit_BoolOp(self, node: BoolOp) -> str:
        return f" {node.op} ".join(self.operand(item) for item in node.items)

    def visit_Not(self, node: Not) -> str:
        return f"NOT ({self.visit(node.item)})"

    def visit_Postfix(self, node: Postfix) -> str:
        return f"{self.operand(node.item)} {node.op}"

    def visit_PostfixOp(self, node: PostfixOp[object]) -> str:
        return f"{self.operand(node.item)} {node.op} {self.visit(node.right)}"

    def visit_AnyAll(self, node: AnyAll[object]) -> str:
        return f"{node.quantifier} ({self.visit(node.value)})"

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
        if source.inline:
            return f"({self.visit(source.query)}) AS {self.quote(source.alias or source.name)}"
        if source.alias is not None:
            return f"{self.quote(source.name)} AS {self.quote(source.alias)}"
        return self.quote(source.name)

    def select_slot(self, slot: Slot) -> str:
        if isinstance(slot, type):
            return ", ".join(self.visit(c) for c in slot.__table__.columns.values())
        return self.visit(slot)

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
            # The combined result has only its output columns: refer to them by name.
            qualify, self.qualify_columns = self.qualify_columns, False
            try:
                parts.append("ORDER BY " + ", ".join(self.visit(e) for e in node.order_by_))
            finally:
                self.qualify_columns = qualify
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
        try:
            return self._select_body(node)
        finally:
            self.qualify_columns = qualify

    def _note_write(self) -> None:
        """Mark the statement as a write; a subclass visitor (MERGE) calls this."""
        self._writes = True

    def _with_ctes(self, ctes: Sequence[Cte]) -> str:
        """The ``WITH`` prefix for a select or a write that carries CTEs (a data-modifying
        CTE inside a write). Empty when there are none."""
        if not ctes:
            return ""
        keyword = "WITH RECURSIVE" if any(c.recursive for c in ctes) else "WITH"
        rendered = ", ".join(f"{self.quote(c.name)} AS ({self.visit(c.query)})" for c in ctes)
        return f"{keyword} {rendered} "

    def _select_body(self, node: SelectBase) -> str:
        if node.lock is not None:
            self._locking = True
        # Clauses are emitted strictly left to right so $n numbering follows the text.
        parts: list[str] = []
        if node.ctes:
            parts.append(self._with_ctes(node.ctes).rstrip())
        if node.distinct_ and node.distinct_on_:
            raise QueryError("distinct() and distinct_on() cannot be combined")
        if node.distinct_on_:
            columns = ", ".join(self.visit(e) for e in node.distinct_on_)
            parts.append(f"SELECT DISTINCT ON ({columns})")
        elif node.distinct_:
            parts.append("SELECT DISTINCT")
        else:
            parts.append("SELECT")
        if isinstance(node.projection, dict):
            items = [f"{self.visit(e)} AS {self.quote(k)}" for k, e in node.projection.items()]
        else:
            items = [self.select_slot(slot) for slot in node.slots()]
        parts.append(", ".join(items))
        from_ = f"FROM {self.source(node.source)}"
        if node.tablesample_ is not None:
            sample = node.tablesample_
            clause = f"TABLESAMPLE {sample.method} ({sample.percent:g})"
            if sample.seed is not None:
                clause += f" REPEATABLE ({sample.seed:g})"
            from_ += " " + clause
        parts.append(from_)
        for join in node.joins:
            on = "" if join.on is None else f" ON {self.visit(join.on)}"
            lateral = " LATERAL" if join.lateral else ""
            parts.append(f"{join.kind} JOIN{lateral} {self.source(join.source)}{on}")
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
        self._writes = True
        prefix = self._with_ctes(node.ctes)
        if node.source is not None:
            return prefix + self._insert_select_text(node, node.source)
        if not node.rows:
            raise QueryError(f"insert into {node.table.__name__} has no values()")
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
                key = (
                    type(self),
                    self.casing,
                    node.table,
                    tuple(row),
                    len(self._params),
                    node.overriding,
                )
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
        return prefix + self._insert_text(node)

    def _insert_select_text(self, node: Insert[Table], source: SelectBase | CompoundBase) -> str:
        if node.identity is not None and node.overriding is None:
            raise QueryError(
                f"{node.identity.qualified_name} is GENERATED ALWAYS AS IDENTITY; the "
                "database assigns it. Use .overriding_system_value() to insert explicit values"
            )
        names = ", ".join(self.column_name(c) for c in node.source_columns)
        overriding = f" OVERRIDING {node.overriding} VALUE" if node.overriding else ""
        text = (
            f"INSERT INTO {self.table_name(node.table)} ({names}){overriding} {self.visit(source)}"
        )
        if node.conflict is not None:
            text += " " + self.on_conflict_clause(node.table, node.conflict)
        return text

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
        return self.param_marker(len(self._params))

    def _require_filter(self, node: Update[Table] | Delete[Table], verb: str) -> None:
        if node.where_ is None and not node.all_rows_:
            raise QueryError(
                f"{verb} on {node.table.__name__} has no .where(); "
                "call .all_rows() to affect every row"
            )

    def visit_Update(self, node: Update[Table]) -> str:
        self._writes = True
        if not node.values_:
            raise QueryError(f"update({node.table.__name__}) needs .set(...)")
        self._require_filter(node, "update")
        prefix = self._with_ctes(node.ctes)
        text = f"UPDATE {self.table_name(node.table)} SET {self.set_list(node.table, node.values_)}"
        if node.from_sources:
            text += " FROM " + ", ".join(self.source(t) for t in node.from_sources)
        if node.where_ is not None:
            text += f" WHERE {self.visit(node.where_)}"
        return prefix + text

    def visit_Delete(self, node: Delete[Table]) -> str:
        self._writes = True
        self._require_filter(node, "delete")
        prefix = self._with_ctes(node.ctes)
        text = f"DELETE FROM {self.table_name(node.table)}"
        if node.using_:
            text += " USING " + ", ".join(self.source(t) for t in node.using_)
        if node.where_ is not None:
            text += f" WHERE {self.visit(node.where_)}"
        return prefix + text

    def visit_Returning(self, node: Returning[object]) -> str:
        return f"{self.visit(node.query)} {self.returning_clause(node)}"
