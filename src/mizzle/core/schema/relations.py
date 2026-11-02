"""Relations for relational queries: ``one()`` and ``many()``.

Relations only describe how to load related rows. Database foreign keys come from
``.references()`` / ``foreign_key()``.

    class Users(Table):
        posts = many(lambda: Posts)

    class Posts(Table):
        author_id = integer().not_null()
        author = one(lambda: Users, fields=[author_id], references=lambda: [Users.id])
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Self, overload

from mizzle.core.errors import RelationNotLoaded, SchemaError

if TYPE_CHECKING:
    from mizzle.core.schema.column import Column
    from mizzle.core.schema.table import Table

type ColumnsArg = Sequence[Column[Any]] | Callable[[], Sequence[Column[Any]]]


@dataclass(frozen=True)
class Link:
    """A resolved relation: rows of ``target`` whose ``target_columns`` equal the
    ``source_columns`` of a ``source`` row."""

    source: type[Table]
    target: type[Table]
    source_columns: tuple[Column[Any], ...]
    target_columns: tuple[Column[Any], ...]
    many: bool


class Relation[T: Table]:
    def __init__(self, target: Callable[[], type[T]], relation_name: str | None) -> None:
        self._target = target
        self.relation_name = relation_name
        self.attr = ""
        self.owner: type[Table] | None = None
        self._link: Link | None = None

    def __set_name__(self, owner: type[Table], name: str) -> None:
        self.attr = name
        self.owner = owner

    @property
    def target(self) -> type[T]:
        return self._target()

    @property
    def qualified_name(self) -> str:
        return f"{self.owner.__name__ if self.owner else '?'}.{self.attr}"

    def _not_loaded(self, owner: type[Any]) -> RelationNotLoaded:
        return RelationNotLoaded(
            f"{owner.__name__}.{self.attr} was not loaded; add {self.attr!r} to with_"
        )

    def link(self) -> Link:
        if self._link is None:
            self._link = self._resolve()
        return self._link

    def _resolve(self) -> Link:
        raise NotImplementedError


def _columns(arg: ColumnsArg | None) -> tuple[Column[Any], ...]:
    if arg is None:
        return ()
    return tuple(arg() if callable(arg) else arg)


class One[T: Table](Relation[T]):
    """A single related row. On a row it is ``T | None``."""

    def __init__(
        self,
        target: Callable[[], type[T]],
        fields: ColumnsArg | None,
        references: ColumnsArg | None,
        relation_name: str | None,
    ) -> None:
        super().__init__(target, relation_name)
        self._fields = fields
        self._references = references

    @property
    def fields(self) -> tuple[Column[Any], ...]:
        return _columns(self._fields)

    @property
    def references(self) -> tuple[Column[Any], ...]:
        return _columns(self._references)

    @overload
    def __get__(self, obj: None, owner: type[Any]) -> Self: ...
    @overload
    def __get__(self, obj: object, owner: type[Any]) -> T | None: ...
    def __get__(self, obj: object | None, owner: type[Any]) -> Self | T | None:
        if obj is None:
            return self
        raise self._not_loaded(owner)

    def _resolve(self) -> Link:
        assert self.owner is not None
        target = self.target
        if self.fields:
            if len(self.fields) != len(self.references):
                raise SchemaError(f"{self.qualified_name}: fields and references differ in length")
            return Link(self.owner, target, self.fields, self.references, many=False)
        # Inverse side of a one-to-one: the target declares the fields.
        back = _find_back_reference(self, target)
        return Link(self.owner, target, back.references, back.fields, many=False)


class Many[T: Table](Relation[T]):
    """Related rows. On a row it is ``list[T]``."""

    @overload
    def __get__(self, obj: None, owner: type[Any]) -> Self: ...
    @overload
    def __get__(self, obj: object, owner: type[Any]) -> list[T]: ...
    def __get__(self, obj: object | None, owner: type[Any]) -> Self | list[T]:
        if obj is None:
            return self
        raise self._not_loaded(owner)

    def _resolve(self) -> Link:
        assert self.owner is not None
        back = _find_back_reference(self, self.target)
        return Link(self.owner, self.target, back.references, back.fields, many=True)


def _find_back_reference(relation: Relation[Any], target: type[Table]) -> One[Any]:
    """The ``one(..., fields=...)`` on ``target`` that points back at ``relation.owner``."""
    candidates = [
        r
        for r in target.__table__.relations.values()
        if isinstance(r, One)
        and r.target is relation.owner
        and r.fields
        and (relation.relation_name is None or r.relation_name == relation.relation_name)
    ]
    if not candidates:
        raise SchemaError(
            f"{relation.qualified_name}: {target.__name__} has no one(...) with fields "
            f"pointing back to {relation.owner.__name__ if relation.owner else '?'}"
        )
    if len(candidates) > 1:
        raise SchemaError(
            f"{relation.qualified_name}: several relations on {target.__name__} match; "
            "set relation_name on both sides"
        )
    return candidates[0]


def one[T: Table](
    target: Callable[[], type[T]],
    *,
    fields: ColumnsArg | None = None,
    references: ColumnsArg | None = None,
    relation_name: str | None = None,
) -> One[T]:
    return One(target, fields, references, relation_name)


def many[T: Table](target: Callable[[], type[T]], *, relation_name: str | None = None) -> Many[T]:
    return Many(target, relation_name)
