"""MERGE builder (PostgreSQL 15+).

``MERGE INTO target USING source ON condition WHEN ...`` upserts or reconciles a table in
one statement. Each ``when_*`` clause may carry its own condition; at least one is needed.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal, Self, override

from mizzle.core.compiler import CompiledQuery
from mizzle.core.errors import QueryError
from mizzle.core.query.base import evolve
from mizzle.core.query.insert import check_values, missing_defaults
from mizzle.core.query.returning import ReturningMixin
from mizzle.core.query.select import Cte, Source
from mizzle.core.schema.table import Table
from mizzle.core.sql.nodes import Condition

type MergeAction = Literal["update", "delete", "insert", "nothing"]


@dataclass(frozen=True)
class MergeWhen:
    """One ``WHEN [NOT] MATCHED ... THEN ...`` clause."""

    matched: bool
    action: MergeAction
    by_source: bool = False  # WHEN NOT MATCHED BY SOURCE
    condition: Condition | None = None
    set_: Mapping[str, object] = field(default_factory=dict[str, object])  # UPDATE
    insert_: Mapping[str, object] = field(default_factory=dict[str, object])  # INSERT


@dataclass(frozen=True, eq=False)
class Merge[T: Table](ReturningMixin[T]):
    """Awaiting returns the number of affected rows; ``.returning()`` returns the rows.

    ``WHEN NOT MATCHED BY SOURCE`` (and MERGE ``RETURNING``) need PostgreSQL 17."""

    source_: Source | None = None
    on_: Condition | None = None
    when_: tuple[MergeWhen, ...] = ()
    # A WITH clause passed with db.with_(...): emitted before the MERGE.
    ctes: tuple[Cte, ...] = ()

    def using(self, source: Source) -> Self:
        """The table or subquery to merge from: ``MERGE ... USING source ON ...``."""
        return evolve(self, source_=source)

    def on(self, condition: Condition) -> Self:
        """The match condition, usually comparing a target column with a source column."""
        return evolve(self, on_=condition)

    def when_matched_then_update(
        self, set_: Mapping[str, object], where: Condition | None = None
    ) -> Self:
        """``WHEN MATCHED [AND where] THEN UPDATE SET ...``. Values may be expressions
        (the source row) or plain Python values."""
        if not set_:
            raise QueryError("when_matched_then_update() needs a non-empty set(...)")
        check_values(self.table, set_)
        return self._add(MergeWhen(True, "update", condition=where, set_=dict(set_)))

    def when_matched_then_delete(self, where: Condition | None = None) -> Self:
        """``WHEN MATCHED [AND where] THEN DELETE``."""
        return self._add(MergeWhen(True, "delete", condition=where))

    def when_matched_then_do_nothing(self, where: Condition | None = None) -> Self:
        """``WHEN MATCHED [AND where] THEN DO NOTHING``."""
        return self._add(MergeWhen(True, "nothing", condition=where))

    def when_not_matched_then_insert(
        self, values: Mapping[str, object], where: Condition | None = None
    ) -> Self:
        """``WHEN NOT MATCHED [AND where] THEN INSERT (...) VALUES (...)``. The values are
        usually the source row's columns. ``default_fn`` columns may not be omitted: the
        source has many rows and a Python default would run once for all of them."""
        if not values:
            raise QueryError("when_not_matched_then_insert() needs a non-empty values")
        check_values(self.table, values)
        columns = self.table.__table__.columns
        for attr, column in columns.items():
            if column.required_on_insert and attr not in values:
                raise QueryError(f"{column.qualified_name} is required")  # B48
        if missing := missing_defaults(self.table, values):
            # The factory would run once and every inserted row would share the value.
            raise QueryError(
                f"when_not_matched_then_insert() cannot apply the Python default of "
                f"{', '.join(missing)}: it would run once for every row. Include the column "
                "in the values, or give it a database-side default"
            )
        return self._add(MergeWhen(False, "insert", condition=where, insert_=dict(values)))

    def when_not_matched_then_do_nothing(self, where: Condition | None = None) -> Self:
        """``WHEN NOT MATCHED [AND where] THEN DO NOTHING``."""
        return self._add(MergeWhen(False, "nothing", condition=where))

    def when_not_matched_by_source_then_update(
        self, set_: Mapping[str, object], where: Condition | None = None
    ) -> Self:
        """``WHEN NOT MATCHED BY SOURCE [AND where] THEN UPDATE SET ...`` (PostgreSQL 17)."""
        if not set_:
            raise QueryError("when_not_matched_by_source_then_update() needs a non-empty set(...)")
        check_values(self.table, set_)
        return self._add(
            MergeWhen(False, "update", by_source=True, condition=where, set_=dict(set_))
        )

    def when_not_matched_by_source_then_delete(self, where: Condition | None = None) -> Self:
        """``WHEN NOT MATCHED BY SOURCE [AND where] THEN DELETE`` (PostgreSQL 17)."""
        return self._add(MergeWhen(False, "delete", by_source=True, condition=where))

    def _add(self, clause: MergeWhen) -> Self:
        return evolve(self, when_=(*self.when_, clause))

    @override
    async def run_compiled(self, compiled: CompiledQuery) -> int:
        return await self.runner.run(compiled)
