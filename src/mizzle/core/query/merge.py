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
from mizzle.core.query.returning import ReturningMixin
from mizzle.core.query.select import Source
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
        self._check_set(set_)
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
        usually the source row's columns."""
        self._check_set(values)
        if not values:
            raise QueryError("when_not_matched_then_insert() needs a non-empty values")
        return self._add(MergeWhen(False, "insert", condition=where, insert_=dict(values)))

    def when_not_matched_then_do_nothing(self, where: Condition | None = None) -> Self:
        """``WHEN NOT MATCHED [AND where] THEN DO NOTHING``."""
        return self._add(MergeWhen(False, "nothing", condition=where))

    def when_not_matched_by_source_then_update(
        self, set_: Mapping[str, object], where: Condition | None = None
    ) -> Self:
        """``WHEN NOT MATCHED BY SOURCE [AND where] THEN UPDATE SET ...`` (PostgreSQL 17)."""
        self._check_set(set_)
        return self._add(
            MergeWhen(False, "update", by_source=True, condition=where, set_=dict(set_))
        )

    def when_not_matched_by_source_then_delete(self, where: Condition | None = None) -> Self:
        """``WHEN NOT MATCHED BY SOURCE [AND where] THEN DELETE`` (PostgreSQL 17)."""
        return self._add(MergeWhen(False, "delete", by_source=True, condition=where))

    def _add(self, clause: MergeWhen) -> Self:
        return evolve(self, when_=(*self.when_, clause))

    def _check_set(self, values: Mapping[str, object]) -> None:
        columns = self.table.__table__.columns
        if unknown := [key for key in values if key not in columns]:
            raise QueryError(f"{self.table.__name__} has no column(s): {', '.join(unknown)}")

    @override
    async def run_compiled(self, compiled: CompiledQuery) -> int:
        return await self.runner.run(compiled)
