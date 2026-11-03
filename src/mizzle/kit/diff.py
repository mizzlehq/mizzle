"""Diff two snapshots into an ordered list of migration steps."""

from __future__ import annotations

import copy
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Literal, Protocol

from mizzle.core.errors import MigrationError
from mizzle.core.snapshot import CheckSnap, IndexSnap, Snapshot, TableSnap, ViewSnap, key
from mizzle.core.steps import (
    AddCheck,
    AddColumn,
    AddEnumValue,
    AddForeignKey,
    AddPrimaryKey,
    AddUnique,
    AlterColumnDefault,
    AlterColumnGenerated,
    AlterColumnIdentity,
    AlterColumnNullability,
    AlterColumnType,
    AlterExtension,
    AlterPolicy,
    AlterRole,
    ConstraintKind,
    CreateEnum,
    CreateExtension,
    CreateFunction,
    CreateIndex,
    CreatePolicy,
    CreateRole,
    CreateSchema,
    CreateTable,
    CreateTrigger,
    CreateView,
    DropColumn,
    DropConstraint,
    DropEnum,
    DropExtension,
    DropFunction,
    DropIndex,
    DropPolicy,
    DropRole,
    DropTable,
    DropTrigger,
    DropView,
    RenameColumn,
    RenameConstraint,
    RenameIndex,
    RenameTable,
    SetRowLevelSecurity,
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
    extensions: list[Step] = field(default_factory=list[Step])
    enums: list[Step] = field(default_factory=list[Step])
    renames: list[Step] = field(default_factory=list[Step])
    drop_triggers: list[Step] = field(default_factory=list[Step])
    drop_policies: list[Step] = field(default_factory=list[Step])
    drop_views: list[Step] = field(default_factory=list[Step])
    drop_functions: list[Step] = field(default_factory=list[Step])
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
    create_functions: list[Step] = field(default_factory=list[Step])
    create_views: list[Step] = field(default_factory=list[Step])
    rls: list[Step] = field(default_factory=list[Step])
    create_policies: list[Step] = field(default_factory=list[Step])
    create_triggers: list[Step] = field(default_factory=list[Step])
    drop_tables: list[Step] = field(default_factory=list[Step])
    drop_enums: list[Step] = field(default_factory=list[Step])
    drop_extensions: list[Step] = field(default_factory=list[Step])
    roles: list[Step] = field(default_factory=list[Step])
    drop_roles: list[Step] = field(default_factory=list[Step])

    def steps(self) -> list[Step]:
        # Order matters: renames first (later steps use new names), drops of dependent
        # objects before what they depend on, foreign keys after every table exists.
        # Extensions -> types -> tables -> functions -> views -> policies and triggers;
        # the reverse when dropping.
        return [
            *self.roles,  # before the policies that name them
            *self.schemas,
            *self.extensions,
            *self.enums,
            *self.renames,
            *self.drop_triggers,
            *self.drop_policies,
            *self.drop_views,
            *self.drop_functions,
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
            *self.create_functions,
            *self.create_views,
            *self.rls,
            *self.create_policies,
            *self.create_triggers,
            *self.drop_tables,
            *self.drop_enums,
            *self.drop_extensions,
            *self.drop_roles,  # after the policies that named them are gone
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
            # Anchor a value added first on an existing one: later new values follow it.
            before_value = (
                next((v for v in new_values[i + 1 :] if v in old_values), None)
                if after is None
                else None
            )
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
        old_default = before["default"]
        old_identity, new_identity = before.get("identity"), column.get("identity")
        if old_identity is not None and new_identity is None:  # before DROP NOT NULL
            plan.alter_columns.append(AlterColumnIdentity(schema, name, cname, old_identity, None))
        if before["type"] != column["type"]:
            if old_default is not None:  # it may not cast to the new type; set it again after
                plan.alter_columns.append(AlterColumnDefault(schema, name, cname, None))
                old_default = None
            plan.alter_columns.append(
                AlterColumnType(
                    schema,
                    name,
                    cname,
                    before["type"],
                    column["type"],
                    generated="generated" in before,
                )
            )
        old_generated, new_generated = before.get("generated"), column.get("generated")
        if old_generated != new_generated and (
            compare_expressions  # else only whether it is generated, and how, counts
            or old_generated is None
            or new_generated is None
            or old_generated["stored"] != new_generated["stored"]
        ):
            plan.alter_columns.append(
                AlterColumnGenerated(schema, name, cname, old_generated, new_generated)
            )
        if before["not_null"] != column["not_null"]:
            plan.alter_columns.append(
                AlterColumnNullability(schema, name, cname, column["not_null"])
            )
        if old_default != column["default"]:
            plan.alter_columns.append(AlterColumnDefault(schema, name, cname, column["default"]))
        if new_identity is not None and old_identity != new_identity:  # after SET NOT NULL
            plan.alter_columns.append(
                AlterColumnIdentity(schema, name, cname, old_identity, new_identity)
            )
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
        for extra in ("method", "opclasses", "options"):
            if a.get(extra) != b.get(extra):
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
            # Its foreign keys would block changes to the tables they reference.
            plan.drop_fks += [
                DropConstraint(table["schema"], table["name"], n, "foreign_key")
                for n in table["foreign_keys"]
            ]
            plan.drop_tables.append(DropTable(table["schema"], table["name"]))
            # They go with the table, but too late for the functions they use.
            plan.drop_triggers += [
                DropTrigger(table["schema"], table["name"], n) for n in table.get("triggers", {})
            ]
            plan.drop_policies += [
                DropPolicy(table["schema"], table["name"], n) for n in table.get("policies", {})
            ]
    _recreate_dependent_fks(old_tables, new, plan)
    _diff_extensions(old, new, plan, drop_unlisted=compare_expressions)
    _diff_roles(old, new, plan, drop_unlisted=compare_expressions)
    touched = _diff_functions(old, new, plan)
    touched |= _changed_tables(plan)
    touched |= _diff_views(old, new, plan, touched)
    for k, table in new["tables"].items():
        if (before := old_tables.get(k)) is not None:
            _diff_security(before, table, plan, touched)
        else:
            _diff_security(_empty_like(table), table, plan, touched)
    return plan.steps()


# -- extensions, functions, views, policies, triggers -------------------------------


def _mentions(text: str | None, names: Iterable[str]) -> bool:
    """Whether SQL ``text`` may use any of ``names``. A plain word search: it can only
    over-report, which recreates an object that did not need it."""
    if not text:
        return False
    return any(re.search(rf"(?<![\w$]){re.escape(n)}(?![\w$])", text) for n in names)


def _empty_like(table: TableSnap) -> TableSnap:
    return TableSnap(
        schema=table["schema"],
        name=table["name"],
        columns={},
        primary_key=None,
        uniques={},
        checks={},
        foreign_keys={},
        indexes={},
    )


def _diff_roles(old: Snapshot, new: Snapshot, plan: _Plan, drop_unlisted: bool) -> None:
    before, after = old.get("roles", {}), new.get("roles", {})
    for name, role in after.items():
        prev = before.get(name)
        if prev is None:
            plan.roles.append(CreateRole(role))
        elif prev != role:
            plan.roles.append(AlterRole(role))
    # A live database lists every role on the server: only roles a previous snapshot
    # managed are ever dropped.
    if drop_unlisted:
        plan.drop_roles += [DropRole(n) for n in before if n not in after]


def _diff_extensions(old: Snapshot, new: Snapshot, plan: _Plan, drop_unlisted: bool) -> None:
    before, after = old.get("extensions", {}), new.get("extensions", {})
    for name, ext in after.items():
        prev = before.get(name)
        if prev is None:
            plan.extensions.append(CreateExtension(ext))
            continue
        # Only what the schema declares is compared: a database may have moved or updated
        # an extension the schema does not pin.
        if "schema" in ext and ext["schema"] != prev.get("schema"):
            plan.extensions.append(AlterExtension(name, schema=ext["schema"]))
        if "version" in ext and ext["version"] != prev.get("version"):
            plan.extensions.append(AlterExtension(name, version=ext["version"]))
    if drop_unlisted:  # else ``old`` is a live database: platforms install their own
        plan.drop_extensions += [DropExtension(n) for n in before if n not in after]


def _diff_functions(old: Snapshot, new: Snapshot, plan: _Plan) -> set[str]:
    """Returns the names of functions that are dropped (also to be created again)."""
    before, after = old.get("functions", {}), new.get("functions", {})
    dropped: set[str] = set()
    for k, fn in after.items():
        prev = before.get(k)
        if prev is not None and (prev["args"], prev["returns"]) != (fn["args"], fn["returns"]):
            # CREATE OR REPLACE cannot change these: it would make a second function.
            plan.drop_functions.append(DropFunction(prev["schema"], prev["name"], prev["args"]))
            dropped.add(prev["name"])
        if prev != fn:
            plan.create_functions.append(CreateFunction(fn))
    for k, prev in before.items():
        if k not in after:
            plan.drop_functions.append(DropFunction(prev["schema"], prev["name"], prev["args"]))
            dropped.add(prev["name"])
    return dropped


def _changed_tables(plan: _Plan) -> set[str]:
    """Names of tables and columns whose drop or type change a view (or a policy, or a
    trigger condition) that uses them would block."""
    names: set[str] = set()
    for step in [*plan.drop_tables, *plan.drop_columns, *plan.alter_columns]:
        match step:
            case DropTable(name=name):
                names.add(name)
            case DropColumn(table=table, name=name):
                names |= {table, name}
            case AlterColumnType(table=table, column=column):
                names |= {table, column}
            case _:
                pass
    return names


def _ordered(views: Mapping[str, ViewSnap], keys: Iterable[str]) -> list[str]:
    """``keys`` with views another one uses first."""
    pending, done = list(keys), list[str]()
    while pending:
        ready = [
            k
            for k in pending
            if not any(
                o != k and _mentions(views[k]["definition"], [views[o]["name"]]) for o in pending
            )
        ]
        ready = ready or pending[:1]  # a cycle (or a false match): keep the given order
        done += ready
        pending = [k for k in pending if k not in ready]
    return done


def _diff_views(old: Snapshot, new: Snapshot, plan: _Plan, touched: set[str]) -> set[str]:
    """Views are dropped and created again, never altered: CREATE OR REPLACE VIEW cannot
    remove or retype columns. Returns the names of views that are dropped."""
    before, after = old.get("views", {}), new.get("views", {})
    redo = {
        k
        for k, v in before.items()
        if k not in after
        or (after[k]["definition"], after[k]["materialized"])
        != (v["definition"], v["materialized"])
    }
    names = set(touched)
    while True:  # views using what changes, and views using those
        names |= {before[k]["name"] for k in redo}
        more = {k for k, v in before.items() if k not in redo and _mentions(v["definition"], names)}
        if not more:
            break
        redo |= more
    for k in reversed(_ordered(before, [k for k in before if k in redo])):
        v = before[k]
        plan.drop_views.append(DropView(v["schema"], v["name"], v["materialized"]))
    for k in _ordered(after, [k for k in after if k in redo or k not in before]):
        v = after[k]
        plan.create_views.append(CreateView(v))
        plan.create_views += [
            CreateIndex(v["schema"], v["name"], i) for i in v.get("indexes", {}).values()
        ]
    for k, v in after.items():
        if k in before and k not in redo:
            _diff_view_indexes(before[k], v, plan)
    return {before[k]["name"] for k in redo}


def _diff_view_indexes(old: ViewSnap, new: ViewSnap, plan: _Plan) -> None:
    schema, name = new["schema"], new["name"]

    def same(a: IndexSnap, b: IndexSnap) -> bool:
        return _without_name(a) == _without_name(b)

    _diff_named(
        old.get("indexes", {}),
        new.get("indexes", {}),
        same,
        lambda n: plan.drop_indexes.append(DropIndex(schema, n)),
        lambda i: plan.create_indexes.append(CreateIndex(schema, name, i)),
        lambda a, b: plan.renames.append(RenameIndex(schema, a, b)),
    )


def _diff_security(old: TableSnap, new: TableSnap, plan: _Plan, touched: set[str]) -> None:
    """Row-level security, policies and triggers of a table that exists in both."""
    schema, name = new["schema"], new["name"]
    off = {"enabled": False, "force": False}
    old_rls, new_rls = old.get("rls", off), new.get("rls", off)
    if old_rls != new_rls:
        plan.rls.append(SetRowLevelSecurity(schema, name, new_rls["enabled"], new_rls["force"]))

    old_policies, new_policies = old.get("policies", {}), new.get("policies", {})
    for pname, policy in new_policies.items():
        prev = old_policies.get(pname)
        if prev is None:
            plan.create_policies.append(CreatePolicy(schema, name, policy))
            continue
        blocked = _mentions(f"{prev['using']} {prev['with_check']}", touched)
        if prev == policy and not blocked:
            continue
        if (
            not blocked
            and (prev["command"], prev["permissive"]) == (policy["command"], policy["permissive"])
            # ALTER POLICY can change an expression, but not remove it
            and (prev["using"] is None or policy["using"] is not None)
            and (prev["with_check"] is None or policy["with_check"] is not None)
        ):
            plan.create_policies.append(AlterPolicy(schema, name, policy))
        else:
            plan.drop_policies.append(DropPolicy(schema, name, pname))
            plan.create_policies.append(CreatePolicy(schema, name, policy))
    plan.drop_policies += [
        DropPolicy(schema, name, n) for n in old_policies if n not in new_policies
    ]

    old_triggers, new_triggers = old.get("triggers", {}), new.get("triggers", {})
    for tname, trigger in new_triggers.items():
        was = old_triggers.get(tname)
        blocked = was is not None and (
            _mentions(was["when"], touched)
            or was["function"].partition(".")[2] in touched  # its function is dropped
        )
        if was is None or blocked:
            if was is not None:
                plan.drop_triggers.append(DropTrigger(schema, name, tname))
            plan.create_triggers.append(CreateTrigger(schema, name, trigger))
        elif was != trigger:
            plan.create_triggers.append(CreateTrigger(schema, name, trigger, replace=True))
    plan.drop_triggers += [
        DropTrigger(schema, name, n) for n in old_triggers if n not in new_triggers
    ]


def _recreate_dependent_fks(old_tables: dict[str, TableSnap], new: Snapshot, plan: _Plan) -> None:
    """A foreign key depends on the key it references: dropping that primary key or unique
    fails while the key is in use, so drop the foreign key first and add it back after.
    Keys match by column set, in any order, as PostgreSQL matches them."""
    dropped: set[tuple[str, str, frozenset[str]]] = set()
    for step in plan.drop_constraints:
        if not isinstance(step, DropConstraint) or step.kind not in ("primary_key", "unique"):
            continue
        table = old_tables[key(step.schema, step.table)]
        pk = table["primary_key"]
        cols = pk["columns"] if step.kind == "primary_key" and pk else []
        if step.kind == "unique":
            cols = table["uniques"][step.name]["columns"]
        dropped.add((step.schema, step.table, frozenset(cols)))
    added = {
        (s.schema, s.table, s.foreign_key["name"])
        for s in plan.add_fks
        if isinstance(s, AddForeignKey)
    }
    for k, table in new["tables"].items():
        before = old_tables.get(k)
        for name, fk in table["foreign_keys"].items():
            ref = (fk["ref_schema"], fk["ref_table"], frozenset(fk["ref_columns"]))
            if before is None or name not in before["foreign_keys"] or ref not in dropped:
                continue
            if (table["schema"], table["name"], name) in added:
                continue
            plan.drop_fks.append(
                DropConstraint(table["schema"], table["name"], name, "foreign_key")
            )
            plan.add_fks.append(AddForeignKey(table["schema"], table["name"], fk))
