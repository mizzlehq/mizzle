"""Diff two snapshots into an ordered list of migration steps."""

from __future__ import annotations

import copy
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Literal, Protocol

from mizzle.core.errors import MigrationError
from mizzle.core.snapshot import CheckSnap, IndexSnap, Snapshot, TableSnap, key
from mizzle.core.steps import (
    AddCheck,
    AddColumn,
    AddEnumValue,
    AddForeignKey,
    AddPrimaryKey,
    AddUnique,
    AlterColumnDefault,
    AlterColumnNullability,
    AlterColumnType,
    ConstraintKind,
    CreateEnum,
    CreateIndex,
    CreateSchema,
    CreateTable,
    DropColumn,
    DropConstraint,
    DropEnum,
    DropIndex,
    DropTable,
    RenameColumn,
    RenameConstraint,
    RenameIndex,
    RenameTable,
    Step,
)

type RenameKind = Literal["table", "column"]


class RenameResolver(Protocol):
    """Decide which removed names were renamed to which added names.

    ``scope`` is ``"schema"`` for tables and ``"schema.table"`` for columns. Returns
    ``{old: new}``; anything not in the result is a real drop/add.
    """

    def __call__(
        self, kind: RenameKind, scope: str, removed: list[str], added: list[str], /
    ) -> dict[str, str]: ...


@dataclass
class StrictRenames:
    """Non-interactive resolver: uses only explicit renames and refuses to guess.

    ``renames`` keys: ``"table_old"`` / ``"schema.table_old"`` for tables and
    ``"table.col_old"`` / ``"schema.table.col_old"`` for columns; values are new names.
    """

    renames: Mapping[str, str] = field(default_factory=dict[str, str])
    default_schema: str = "public"
    # Called for leftovers instead of raising (the CLI asks the user when on a terminal).
    ask: RenameResolver | None = None

    def _lookup(self, scope: str, old: str) -> str | None:
        full = f"{scope}.{old}"
        if full in self.renames:
            return self.renames[full]
        prefix = f"{self.default_schema}."
        if full.startswith(prefix) and full.removeprefix(prefix) in self.renames:
            return self.renames[full.removeprefix(prefix)]
        return None

    def __call__(
        self, kind: RenameKind, scope: str, removed: list[str], added: list[str]
    ) -> dict[str, str]:
        result: dict[str, str] = {}
        for old in removed:
            new = self._lookup(scope, old)
            if new is not None:
                if new not in added:
                    raise MigrationError(
                        f"rename {scope}.{old} -> {new}: {new} is not a new {kind}"
                    )
                result[old] = new
        left_removed = [r for r in removed if r not in result]
        left_added = [a for a in added if a not in result.values()]
        if left_removed and left_added and self.ask is not None:
            return result | self.ask(kind, scope, left_removed, left_added)
        if left_removed and left_added:
            raise MigrationError(
                f"{kind}s {left_removed} were removed and {left_added} added in {scope}: "
                f"pass --rename {scope.split('.')[-1] + '.' if kind == 'column' else ''}"
                f"{left_removed[0]}={left_added[0]} if it is a rename, or --no-renames"
            )
        return result


def no_renames(
    _kind: RenameKind, _scope: str, _removed: list[str], _added: list[str], /
) -> dict[str, str]:
    """Treat every removed/added pair as a real drop and create."""
    return {}


@dataclass
class _Plan:
    schemas: list[Step] = field(default_factory=list[Step])
    enums: list[Step] = field(default_factory=list[Step])
    renames: list[Step] = field(default_factory=list[Step])
    drop_fks: list[Step] = field(default_factory=list[Step])
    drop_constraints: list[Step] = field(default_factory=list[Step])
    drop_indexes: list[Step] = field(default_factory=list[Step])
    create_tables: list[Step] = field(default_factory=list[Step])
    add_columns: list[Step] = field(default_factory=list[Step])
    alter_columns: list[Step] = field(default_factory=list[Step])
    drop_columns: list[Step] = field(default_factory=list[Step])
    add_constraints: list[Step] = field(default_factory=list[Step])
    create_indexes: list[Step] = field(default_factory=list[Step])
    add_fks: list[Step] = field(default_factory=list[Step])
    drop_tables: list[Step] = field(default_factory=list[Step])
    drop_enums: list[Step] = field(default_factory=list[Step])

    def steps(self) -> list[Step]:
        # Order matters: renames first (later steps use new names), drops of dependent
        # objects before what they depend on, foreign keys after every table exists.
        return [
            *self.schemas,
            *self.enums,
            *self.renames,
            *self.drop_fks,
            *self.drop_constraints,
            *self.drop_indexes,
            *self.create_tables,
            *self.add_columns,
            *self.alter_columns,
            *self.drop_columns,
            *self.add_constraints,
            *self.create_indexes,
            *self.add_fks,
            *self.drop_tables,
            *self.drop_enums,
        ]


def _without_name(item: Mapping[str, object]) -> dict[str, object]:
    return {k: v for k, v in item.items() if k != "name"}


def _diff_enums(old: Snapshot, new: Snapshot, plan: _Plan) -> None:
    for k, enum in new["enums"].items():
        before = old["enums"].get(k)
        if before is None:
            plan.enums.append(CreateEnum(enum))
            continue
        old_values, new_values = before["values"], enum["values"]
        if removed := [v for v in old_values if v not in new_values]:
            raise MigrationError(
                f"enum {enum['name']}: removing values {removed} needs a custom migration"
            )
        if [v for v in new_values if v in old_values] != old_values:
            raise MigrationError(f"enum {enum['name']}: reordering values needs a custom migration")
        for i, value in enumerate(new_values):
            if value in old_values:
                continue
            after = new_values[i - 1] if i > 0 else None
            before_value = new_values[i + 1] if after is None and len(new_values) > 1 else None
            plan.enums.append(
                AddEnumValue(enum["schema"], enum["name"], value, after, before_value)
            )
    for k, enum in old["enums"].items():
        if k not in new["enums"]:
            plan.drop_enums.append(DropEnum(enum["schema"], enum["name"]))


def _rename_columns_in(table: TableSnap, mapping: Mapping[str, str]) -> None:
    def rn(cols: list[str]) -> list[str]:
        return [mapping.get(c, c) for c in cols]

    table["columns"] = {
        mapping.get(n, n): {**c, "name": mapping.get(n, n)} for n, c in table["columns"].items()
    }
    if table["primary_key"]:
        table["primary_key"]["columns"] = rn(table["primary_key"]["columns"])
    for u in table["uniques"].values():
        u["columns"] = rn(u["columns"])
    for i in table["indexes"].values():
        i["columns"] = rn(i["columns"])
    for fk in table["foreign_keys"].values():
        fk["columns"] = rn(fk["columns"])


def _apply_renames(
    old: Snapshot, new: Snapshot, resolve: RenameResolver, plan: _Plan
) -> dict[str, TableSnap]:
    """Return old tables keyed and named as in ``new``, with renames recorded as steps."""
    tables = copy.deepcopy(old["tables"])

    schemas = {t["schema"] for t in [*tables.values(), *new["tables"].values()]}
    for schema in sorted(schemas):
        removed = [
            t["name"] for k, t in tables.items() if t["schema"] == schema and k not in new["tables"]
        ]
        added = [
            t["name"] for k, t in new["tables"].items() if t["schema"] == schema and k not in tables
        ]
        for old_name, new_name in resolve("table", schema, removed, added).items():
            plan.renames.append(RenameTable(schema, old_name, new_name))
            table = tables.pop(key(schema, old_name))
            table["name"] = new_name
            tables[key(schema, new_name)] = table
            for other in tables.values():  # FKs follow a renamed table automatically
                for fk in other["foreign_keys"].values():
                    if fk["ref_schema"] == schema and fk["ref_table"] == old_name:
                        fk["ref_table"] = new_name

    for k, table in tables.items():
        target = new["tables"].get(k)
        if target is None:
            continue
        scope = f"{table['schema']}.{table['name']}"
        removed = [c for c in table["columns"] if c not in target["columns"]]
        added = [c for c in target["columns"] if c not in table["columns"]]
        mapping = resolve("column", scope, removed, added)
        if not mapping:
            continue
        for old_name, new_name in mapping.items():
            plan.renames.append(RenameColumn(table["schema"], table["name"], old_name, new_name))
        _rename_columns_in(table, mapping)
        for other in tables.values():
            for fk in other["foreign_keys"].values():
                if fk["ref_schema"] == table["schema"] and fk["ref_table"] == table["name"]:
                    fk["ref_columns"] = [mapping.get(c, c) for c in fk["ref_columns"]]
    return tables


def _diff_named[T: Mapping[str, object]](
    old: Mapping[str, T],
    new: Mapping[str, T],
    same: Callable[[T, T], bool],
    on_drop: Callable[[str], None],
    on_add: Callable[[T], None],
    on_rename: Callable[[str, str], None],
) -> None:
    """Diff name -> definition maps. A drop and an add with the same definition is a rename."""
    removed = [n for n in old if n not in new]
    added = [n for n in new if n not in old]
    for name in old:
        if name in new and not same(old[name], new[name]):
            on_drop(name)
            on_add(new[name])
    for name in removed:
        match = next((a for a in added if same(old[name], new[a])), None)
        if match is not None:
            added.remove(match)
            on_rename(name, match)
        else:
            on_drop(name)
    for name in added:
        on_add(new[name])


def _diff_table(old: TableSnap, new: TableSnap, plan: _Plan, compare_expressions: bool) -> None:
    schema, name = new["schema"], new["name"]

    for cname, column in new["columns"].items():
        before = old["columns"].get(cname)
        if before is None:
            plan.add_columns.append(AddColumn(schema, name, column))
            continue
        if before["type"] != column["type"]:
            plan.alter_columns.append(
                AlterColumnType(schema, name, cname, before["type"], column["type"])
            )
        if before["not_null"] != column["not_null"]:
            plan.alter_columns.append(
                AlterColumnNullability(schema, name, cname, column["not_null"])
            )
        if before["default"] != column["default"]:
            plan.alter_columns.append(AlterColumnDefault(schema, name, cname, column["default"]))
    for cname in old["columns"]:
        if cname not in new["columns"]:
            plan.drop_columns.append(DropColumn(schema, name, cname))

    def drop(kind: ConstraintKind) -> Callable[[str], None]:
        bucket = plan.drop_fks if kind == "foreign_key" else plan.drop_constraints
        return lambda n: bucket.append(DropConstraint(schema, name, n, kind))

    def rename(old_name: str, new_name: str) -> None:
        plan.renames.append(RenameConstraint(schema, name, old_name, new_name))

    def same(a: Mapping[str, object], b: Mapping[str, object]) -> bool:
        return _without_name(a) == _without_name(b)

    def same_check(a: CheckSnap, b: CheckSnap) -> bool:
        return not compare_expressions or a["expression"] == b["expression"]

    old_pk = {old["primary_key"]["name"]: old["primary_key"]} if old["primary_key"] else {}
    new_pk = {new["primary_key"]["name"]: new["primary_key"]} if new["primary_key"] else {}
    _diff_named(
        old_pk,
        new_pk,
        same,
        drop("primary_key"),
        lambda pk: plan.add_constraints.append(AddPrimaryKey(schema, name, pk)),
        rename,
    )
    _diff_named(
        old["uniques"],
        new["uniques"],
        same,
        drop("unique"),
        lambda u: plan.add_constraints.append(AddUnique(schema, name, u)),
        rename,
    )
    _diff_named(
        old["checks"],
        new["checks"],
        same_check,
        drop("check"),
        lambda c: plan.add_constraints.append(AddCheck(schema, name, c)),
        rename,
    )
    _diff_named(
        old["foreign_keys"],
        new["foreign_keys"],
        same,
        drop("foreign_key"),
        lambda fk: plan.add_fks.append(AddForeignKey(schema, name, fk)),
        rename,
    )

    def same_index(a: IndexSnap, b: IndexSnap) -> bool:
        if a["columns"] != b["columns"] or a["unique"] != b["unique"]:
            return False
        if not compare_expressions:  # the database reformats expressions; compare shapes
            return ("expression" in a) == ("expression" in b)
        return a["where"] == b["where"] and a.get("expression") == b.get("expression")

    _diff_named(
        old["indexes"],
        new["indexes"],
        same_index,
        lambda n: plan.drop_indexes.append(DropIndex(schema, n)),
        lambda i: plan.create_indexes.append(CreateIndex(schema, name, i)),
        lambda a, b: plan.renames.append(RenameIndex(schema, a, b)),
    )


def diff(
    old: Snapshot,
    new: Snapshot,
    resolve: RenameResolver = no_renames,
    *,
    compare_expressions: bool = True,
) -> list[Step]:
    """Steps that turn the ``old`` schema into the ``new`` one.

    ``compare_expressions=False`` ignores CHECK expressions and index predicates, whose text
    a database reformats; use it when ``old`` comes from introspection (``push``).
    """
    plan = _Plan()
    plan.schemas = [CreateSchema(s) for s in new["schemas"] if s not in old["schemas"]]
    _diff_enums(old, new, plan)
    old_tables = _apply_renames(old, new, resolve, plan)

    for k, table in new["tables"].items():
        before = old_tables.get(k)
        if before is None:
            plan.create_tables.append(CreateTable(table))
            plan.create_indexes += [
                CreateIndex(table["schema"], table["name"], i) for i in table["indexes"].values()
            ]
            plan.add_fks += [
                AddForeignKey(table["schema"], table["name"], fk)
                for fk in table["foreign_keys"].values()
            ]
        else:
            _diff_table(before, table, plan, compare_expressions)
    for k, table in old_tables.items():
        if k not in new["tables"]:
            plan.drop_tables.append(DropTable(table["schema"], table["name"]))
    return plan.steps()
