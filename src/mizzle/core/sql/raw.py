"""Raw SQL with safe parameters.

``sql(t"select * from users where id = {uid}")`` on Python 3.14+, or
``sql("select * from users where id = {}", uid)`` on any version.
Values always become bind parameters; nodes (columns, expressions) are compiled in place.
"""

from __future__ import annotations

import string
import sys

from mizzle.core.errors import QueryError
from mizzle.core.sql.nodes import Identifier, Node, Param, Raw

if sys.version_info >= (3, 14):
    from string.templatelib import Interpolation, Template


def _part(value: object) -> Node:
    return value if isinstance(value, Node) else Param(value)


class _Sql:
    if sys.version_info >= (3, 14):

        def __call__(self, template: str | Template, /, *args: object) -> Raw:
            if isinstance(template, str):
                return self._from_str(template, args)
            if args:
                raise QueryError("sql() takes no extra arguments with a t-string")
            return self._from_template(template)

    else:

        def __call__(self, template: str, /, *args: object) -> Raw:
            return self._from_str(template, args)

    @staticmethod
    def _from_str(template: str, args: tuple[object, ...]) -> Raw:
        parts: list[str | Node] = []
        used = 0
        for literal, field, spec, conversion in string.Formatter().parse(template):
            if literal:
                parts.append(literal)
            if field is None:
                continue
            if field != "" or spec or conversion:
                raise QueryError("sql() only supports plain {} slots; use {{ and }} for braces")
            if used >= len(args):
                raise QueryError(f"sql() has more {{}} slots than arguments ({len(args)})")
            parts.append(_part(args[used]))
            used += 1
        if used != len(args):
            raise QueryError(f"sql() got {len(args)} arguments for {used} {{}} slots")
        return Raw(tuple(parts))

    if sys.version_info >= (3, 14):

        @staticmethod
        def _from_template(template: Template) -> Raw:
            parts: list[str | Node] = []
            for item in template:
                if isinstance(item, Interpolation):
                    if item.conversion or item.format_spec:
                        raise QueryError("sql() t-strings do not support !r or :format")
                    parts.append(_part(item.value))
                elif item:
                    parts.append(item)
            return Raw(tuple(parts))

    @staticmethod
    def identifier(name: str) -> Identifier:
        """A safely quoted identifier (table or column name)."""
        return Identifier(name)


sql = _Sql()
