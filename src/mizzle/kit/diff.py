"""Diff two snapshots into an ordered list of migration steps."""

from __future__ import annotations

import copy
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Literal, Protocol

from mizzle.core.errors import MigrationError
from mizzle.core.snapshot import (
    CheckSnap,
    ColumnSnap,
    CompositeSnap,
    DomainSnap,
    ExclusionSnap,
    GrantsSnap,
    IndexSnap,
    Snapshot,
    TableSnap,
    TriggerSnap,
    ViewSnap,
    function_key,
    key,
    keyword_default,
)
from mizzle.core.steps import (
    AddCheck,
    AddColumn,
    AddEnumValue,
    AddExclusion,
    AddForeignKey,
    AddPrimaryKey,
    AddUnique,
    AlterColumnCollation,
    AlterColumnDefault,
    AlterColumnGenerated,
    AlterColumnIdentity,
    AlterColumnNullability,
    AlterColumnType,
    AlterExtension,
    AlterPolicy,
    AlterRole,
    AlterTableStorage,
    ConstraintKind,
    CreateComposite,
    CreateDomain,
    CreateEnum,
    CreateExtension,
    CreateFunction,
    CreateIndex,
    CreatePartition,
    CreatePolicy,
    CreateRole,
    CreateSchema,
    CreateTable,
    CreateTrigger,
    CreateView,
    DropColumn,
    DropComposite,
    DropConstraint,
    DropDomain,
    DropEnum,
    DropExtension,
    DropFunction,
    DropIndex,
    DropPartition,
    DropPolicy,
    DropRole,
    DropTable,
    DropTrigger,
    DropView,
    GrantPrivileges,
    RenameColumn,
    RenameConstraint,
    RenameIndex,
    RenameTable,
    RestoreGrants,
    SaveGrants,
    SetColumnComment,
    SetRowLevelSecurity,
    SetTableComment,
    SetTableLogged,
    SetTableSchema,
    Step,
)

type RenameKind = Literal["table", "column"]


class RenameResolver(Protocol):
    """Decide which removed names were renamed to which added names.

    ``scope`` is ``"schema"`` for tables and ``"schema.table"`` for columns. Returns
    ``{old: new}``; anything not in the result is a real drop/add. Scope ``"*"`` asks
    whether tables moved to another schema: names are then ``"schema.table"``.
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
    # The renames that matched something; any other was a typo or a stale flag (C20).
    used: set[str] = field(default_factory=set[str])

    def unused(self) -> list[str]:
        return [old for old in self.renames if old not in self.used]

    def _lookup(self, scope: str, old: str) -> str | None:
        if scope == "*":  # a move: "schema.table" names
            keys = [old]
        else:
            full = f"{scope}.{old}"
            prefix = f"{self.default_schema}."
            keys = [full, full.removeprefix(prefix)] if full.startswith(prefix) else [full]
        for k in keys:
            if k in self.renames:
                self.used.add(k)
                return self.renames[k]
        return None

    def __call__(
        self, kind: RenameKind, scope: str, removed: list[str], added: list[str]
    ) -> dict[str, str]:
        result: dict[str, str] = {}
        for old in removed:
            new = self._lookup(scope, old)
            if kind == "table" and scope != "*" and new is not None and "." in new:
                continue  # "schema.table": a move, asked for with scope "*"
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
    domains: list[Step] = field(default_factory=list[Step])
    composites: list[Step] = field(default_factory=list[Step])
    renames: list[Step] = field(default_factory=list[Step])
    drop_triggers: list[Step] = field(default_factory=list[Step])
    drop_policies: list[Step] = field(default_factory=list[Step])
    drop_views: list[Step] = field(default_factory=list[Step])
    drop_functions: list[Step] = field(default_factory=list[Step])
    drop_fks: list[Step] = field(default_factory=list[Step])
    drop_constraints: list[Step] = field(default_factory=list[Step])
    drop_indexes: list[Step] = field(default_factory=list[Step])
    drop_rebuilt: list[Step] = field(default_factory=list[Step])  # columns added again
    drop_early: list[Step] = field(default_factory=list[Step])  # tables whose names are reused
    create_tables: list[Step] = field(default_factory=list[Step])
    create_partitions: list[Step] = field(default_factory=list[Step])
    add_columns: list[Step] = field(default_factory=list[Step])
    alter_columns: list[Step] = field(default_factory=list[Step])
    table_options: list[Step] = field(default_factory=list[Step])
    drop_columns: list[Step] = field(default_factory=list[Step])
    add_constraints: list[Step] = field(default_factory=list[Step])
    create_indexes: list[Step] = field(default_factory=list[Step])
    add_fks: list[Step] = field(default_factory=list[Step])
    create_functions: list[Step] = field(default_factory=list[Step])
    drop_replaced: list[Step] = field(default_factory=list[Step])  # tables a view replaces
    create_views: list[Step] = field(default_factory=list[Step])
    rls: list[Step] = field(default_factory=list[Step])
    create_policies: list[Step] = field(default_factory=list[Step])
    create_triggers: list[Step] = field(default_factory=list[Step])
    comments: list[Step] = field(default_factory=list[Step])
    revokes: list[Step] = field(default_factory=list[Step])
    grants: list[Step] = field(default_factory=list[Step])
    drop_partitions: list[Step] = field(default_factory=list[Step])
    drop_tables: list[Step] = field(default_factory=list[Step])
    drop_composites: list[Step] = field(default_factory=list[Step])
    drop_domains: list[Step] = field(default_factory=list[Step])
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
            *self.domains,  # a domain's base type can be an enum
            *self.composites,  # a composite's fields can be enums or domains
            *self.renames,
            *self.drop_triggers,
            *self.drop_policies,
            *self.drop_views,
            *self.drop_functions,
            *self.drop_fks,
            *self.drop_constraints,
            *self.drop_indexes,
            *self.drop_rebuilt,
            *self.drop_early,
            *self.create_tables,
            *self.create_partitions,
            *self.add_columns,
            *self.alter_columns,
            *self.table_options,
            *self.drop_columns,
            *self.add_constraints,
            *self.create_indexes,
            *self.add_fks,
            *self.create_functions,
            *self.drop_replaced,
            *self.create_views,
            *self.rls,
            *self.create_policies,
            *self.create_triggers,
            *self.comments,
            *self.revokes,
            *self.grants,
            *self.drop_partitions,
            *self.drop_tables,
            *self.drop_composites,
            *self.drop_domains,
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


def _domain_changed(old: DomainSnap, new: DomainSnap, compare_expressions: bool) -> bool:
    if any(
        old.get(field) != new.get(field)
        for field in ("base_type", "not_null", "default", "collation")
    ):
        return True
    if compare_expressions:
        return sorted(old.get("checks", [])) != sorted(new.get("checks", []))
    return len(old.get("checks", [])) != len(new.get("checks", []))


def _diff_domains(old: Snapshot, new: Snapshot, plan: _Plan, compare_expressions: bool) -> None:
    old_domains = old.get("domains", {})
    new_domains = new.get("domains", {})
    for k, domain in new_domains.items():
        before = old_domains.get(k)
        if before is None:
            plan.domains.append(CreateDomain(domain))
        elif _domain_changed(before, domain, compare_expressions):
            raise MigrationError(
                f"domain {domain['name']}: changing its definition needs a custom migration"
            )
    for k, domain in old_domains.items():
        if k not in new_domains:
            plan.drop_domains.append(DropDomain(domain["schema"], domain["name"]))


def _diff_composites(old: Snapshot, new: Snapshot, plan: _Plan) -> None:
    """A composite type is created or dropped whole; changing its fields needs a custom
    migration (PostgreSQL can ALTER TYPE ADD/DROP/ALTER ATTRIBUTE, but not atomically from a
    snapshot)."""
    old_types = old.get("composites", {})
    new_types = new.get("composites", {})
    created = {k: composite for k, composite in new_types.items() if k not in old_types}
    for k in _composite_order(created):  # a composite's fields must exist before it does
        plan.composites.append(CreateComposite(created[k]))
    for k, composite in new_types.items():
        before = old_types.get(k)
        if before is not None and before["fields"] != composite["fields"]:
            raise MigrationError(
                f"composite type {composite['name']}: changing its fields needs a custom migration"
            )
    dropped = {k: composite for k, composite in old_types.items() if k not in new_types}
    for k in reversed(_composite_order(dropped)):  # the dependants drop first
        plan.drop_composites.append(DropComposite(dropped[k]["schema"], dropped[k]["name"]))


def _composite_order(composites: Mapping[str, CompositeSnap]) -> list[str]:
    """Keys in an order where a composite comes after every composite its fields use (a
    cycle is impossible in PostgreSQL, but is finished off rather than looping)."""
    by_name: dict[str, list[str]] = {}
    for k, composite in composites.items():
        by_name.setdefault(composite["name"], []).append(k)

    def resolve(sql_type: str) -> str | None:
        if sql_type in composites:
            return sql_type
        matches = by_name.get(sql_type, [])
        return matches[0] if len(matches) == 1 else None

    needs = {
        k: {r for f in composite["fields"] if (r := resolve(f["type"])) is not None} - {k}
        for k, composite in composites.items()
    }
    order: list[str] = []
    done: set[str] = set()
    while len(order) < len(composites):
        ready = [k for k in sorted(composites) if k not in done and needs[k] <= done]
        if not ready:
            ready = [k for k in sorted(composites) if k not in done]
        order.append(ready[0])
        done.add(ready[0])
    return order


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
    for e in table.get("exclusions", {}).values():
        for el in e["elements"]:
            if "column" in el:
                el["column"] = mapping.get(el["column"], el["column"])


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
            _follow(tables, schema, old_name, schema, new_name)

    # Tables that left one schema for another (maybe renamed, maybe with other columns):
    # every leftover pair across schemas is offered, so an explicit "schema.table" rename
    # is honoured and an unresolved pair is asked about, not dropped and created (C20).
    # Same-schema pairs were settled above, so only a table with a partner in another
    # schema is offered.
    gone_all = [k for k in tables if k not in new["tables"]]
    came_all = [k for k in new["tables"] if k not in tables]
    gone_schemas = {tables[k]["schema"] for k in gone_all}
    came_schemas = {new["tables"][k]["schema"] for k in came_all}
    gone = [k for k in gone_all if came_schemas - {tables[k]["schema"]}]
    came = [k for k in came_all if gone_schemas - {new["tables"][k]["schema"]}]
    if gone and came:
        moves = resolve("table", "*", gone, came)
        for old_key, new_key in moves.items():
            moved, dest = tables.pop(old_key), new["tables"][new_key]
            old_schema, old_name = moved["schema"], moved["name"]
            plan.renames.append(SetTableSchema(old_schema, old_name, dest["schema"]))
            if dest["name"] != old_name:
                plan.renames.append(RenameTable(dest["schema"], old_name, dest["name"]))
            moved["schema"], moved["name"] = dest["schema"], dest["name"]
            tables[new_key] = moved
            _follow(tables, old_schema, old_name, dest["schema"], dest["name"])

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


def _follow(
    tables: dict[str, TableSnap], schema: str, name: str, new_schema: str, new_name: str
) -> None:
    """Foreign keys follow a renamed or moved table automatically."""
    for other in tables.values():
        for fk in other["foreign_keys"].values():
            if fk["ref_schema"] == schema and fk["ref_table"] == name:
                fk["ref_schema"], fk["ref_table"] = new_schema, new_name


def _default(text: str | None) -> str | None:
    return None if text is None else keyword_default(text)


def _relation_names(*tables: TableSnap) -> set[str]:
    """Names of the indexes the tables have (their own, and their keys')."""
    names: set[str] = set()
    for t in tables:
        names |= set(t["indexes"]) | set(t["uniques"])
        if t["primary_key"]:
            names.add(t["primary_key"]["name"])
    return names


def _rebuilt(before: ColumnSnap, after: ColumnSnap) -> bool:
    """Whether a column must be dropped and added again: PostgreSQL cannot make a column
    generated, switch STORED/VIRTUAL, or make a VIRTUAL column a plain one."""
    old, new = before.get("generated"), after.get("generated")
    if old is None:
        return new is not None
    return (new is None and not old["stored"]) or (
        new is not None and old["stored"] != new["stored"]
    )


def _without_columns(table: TableSnap, names: list[str]) -> TableSnap:
    """``table`` as it is after DROP COLUMN ``names``: the indexes and constraints that use
    them go too."""
    out = copy.deepcopy(table)

    def uses(columns: list[str], *texts: str | None) -> bool:
        return any(c in names for c in columns) or any(_mentions(t, names) for t in texts)

    out["columns"] = {n: c for n, c in out["columns"].items() if n not in names}
    if (pk := out["primary_key"]) and uses(pk["columns"]):
        out["primary_key"] = None
    out["uniques"] = {n: u for n, u in out["uniques"].items() if not uses(u["columns"])}
    out["foreign_keys"] = {n: f for n, f in out["foreign_keys"].items() if not uses(f["columns"])}
    out["checks"] = {n: c for n, c in out["checks"].items() if not uses([], c["expression"])}
    out["indexes"] = {
        n: i
        for n, i in out["indexes"].items()
        if not uses(i["columns"], i.get("expression"), i["where"])
    }
    if exclusions := out.get("exclusions"):
        out["exclusions"] = {
            n: e
            for n, e in exclusions.items()
            if not uses([el.get("column", "") for el in e["elements"]], e.get("where"))
        }
    return out


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


# A quoted date or timestamp with no offset after it: '2026-01-01' or '2026-01-01 10:00'.
_NAIVE_MOMENT = re.compile(r"'\d{4}-\d{2}-\d{2}(?: [\d:.]+)?'")


def _diff_partitions(old: TableSnap, new: TableSnap, plan: _Plan) -> None:
    """Partitions of an existing table: add the new ones, drop the gone ones. A change to
    the key or to a partition's bound is not expressible (it needs a detach/attach)."""
    schema, name = new["schema"], new["name"]
    if old.get("partition_by") != new.get("partition_by"):
        raise MigrationError(f"table {name}: changing the partition key needs a custom migration")
    before, after = old.get("partitions", {}), new.get("partitions", {})
    for pname, bound in after.items():
        if pname not in before:
            plan.create_partitions.append(CreatePartition(schema, name, pname, bound))
        elif before[pname] != bound:
            # A date or time literal without a UTC offset is read in the session TimeZone,
            # so a changed database timezone looks like a changed bound (B64).
            hint = (
                ". A timestamp bound without a UTC offset is read in the session TimeZone; "
                "if the database timezone changed, write the offset (for example "
                "'2026-01-01 00:00:00+00')"
                if _NAIVE_MOMENT.search(bound)
                else ""
            )
            raise MigrationError(
                f"table {name}: changing the bound of partition {pname} needs a custom "
                f"migration (database: {before[pname]}; schema: {bound}){hint}"
            )
    for pname in before:
        if pname not in after:
            plan.drop_partitions.append(DropPartition(schema, pname))


def _diff_table(
    old: TableSnap,
    new: TableSnap,
    plan: _Plan,
    compare_expressions: bool,
    enums: frozenset[str] | None = None,
    normalized: bool = False,
) -> None:
    schema, name = new["schema"], new["name"]
    # Checks, index predicates and generated expressions are compared when both sides are as
    # the database prints them (normalized), even with compare_expressions off (C24).
    expressions = compare_expressions or normalized
    _diff_partitions(old, new, plan)
    if rebuilt := [
        c
        for c, col in new["columns"].items()
        if c in old["columns"] and _rebuilt(old["columns"][c], col)
    ]:
        plan.drop_rebuilt += [DropColumn(schema, name, c) for c in rebuilt]
        kept = _without_columns(old, rebuilt)
        # Dropped explicitly (DROP COLUMN would take them silently): keys other foreign keys
        # use are then re-created around it, and the rest is added again by the diff below.
        if (pk := old["primary_key"]) and kept["primary_key"] is None:
            plan.drop_constraints.append(DropConstraint(schema, name, pk["name"], "primary_key"))
        groups: list[tuple[ConstraintKind, Mapping[str, object], Mapping[str, object]]] = [
            ("unique", old["uniques"], kept["uniques"]),
            ("check", old["checks"], kept["checks"]),
            ("foreign_key", old["foreign_keys"], kept["foreign_keys"]),
            ("exclusion", old.get("exclusions", {}), kept.get("exclusions", {})),
        ]
        for kind, was, now in groups:
            bucket = plan.drop_fks if kind == "foreign_key" else plan.drop_constraints
            bucket += [DropConstraint(schema, name, n, kind) for n in was if n not in now]
        plan.drop_indexes += [
            DropIndex(schema, n) for n in old["indexes"] if n not in kept["indexes"]
        ]
        old = kept

    for cname, column in new["columns"].items():
        before = old["columns"].get(cname)
        if before is None:
            plan.add_columns.append(AddColumn(schema, name, column))
            continue
        old_default = before["default"]
        old_identity, new_identity = before.get("identity"), column.get("identity")
        if old_identity is not None and new_identity is None:  # before DROP NOT NULL
            plan.alter_columns.append(
                AlterColumnIdentity(schema, name, cname, old_identity, None, column["type"])
            )
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
                    collation=column.get("collation"),
                    via_text=before["type"].removesuffix("[]") in (enums or ()),
                )
            )
        old_generated, new_generated = before.get("generated"), column.get("generated")
        if old_generated != new_generated and (
            expressions  # else only whether it is generated, and how, counts
            or old_generated is None
            or new_generated is None
            or old_generated["stored"] != new_generated["stored"]
        ):
            plan.alter_columns.append(
                AlterColumnGenerated(schema, name, cname, old_generated, new_generated, column)
            )
        if before["not_null"] != column["not_null"]:
            plan.alter_columns.append(
                AlterColumnNullability(schema, name, cname, column["not_null"])
            )
        if _default(old_default) != _default(column["default"]):
            plan.alter_columns.append(AlterColumnDefault(schema, name, cname, column["default"]))
        if before.get("collation") != column.get("collation") and before["type"] == column["type"]:
            plan.alter_columns.append(
                AlterColumnCollation(schema, name, cname, column["type"], column.get("collation"))
            )
        if new_identity is not None and old_identity != new_identity:  # after SET NOT NULL
            plan.alter_columns.append(
                AlterColumnIdentity(schema, name, cname, old_identity, new_identity, column["type"])
            )
    for cname in old["columns"]:
        if cname not in new["columns"]:
            plan.drop_columns.append(DropColumn(schema, name, cname))

    if bool(old.get("unlogged")) != bool(new.get("unlogged")):
        plan.table_options.append(SetTableLogged(schema, name, bool(new.get("unlogged"))))
    old_options, new_options = old.get("options", {}), new.get("options", {})
    changed_options = {k: v for k, v in new_options.items() if old_options.get(k) != v}
    removed_options = tuple(k for k in old_options if k not in new_options)
    if changed_options or removed_options:
        plan.table_options.append(AlterTableStorage(schema, name, changed_options, removed_options))

    def drop(kind: ConstraintKind) -> Callable[[str], None]:
        bucket = plan.drop_fks if kind == "foreign_key" else plan.drop_constraints
        return lambda n: bucket.append(DropConstraint(schema, name, n, kind))

    def rename(old_name: str, new_name: str) -> None:
        plan.renames.append(RenameConstraint(schema, name, old_name, new_name))

    def same(a: Mapping[str, object], b: Mapping[str, object]) -> bool:
        return _without_name(a) == _without_name(b)

    def same_check(a: CheckSnap, b: CheckSnap) -> bool:
        if a.get("not_valid") != b.get("not_valid"):
            return False
        return not expressions or a["expression"] == b["expression"]

    def same_exclusion(a: ExclusionSnap, b: ExclusionSnap) -> bool:
        if a["method"] != b["method"] or len(a["elements"]) != len(b["elements"]):
            return False
        if a.get("deferrable") != b.get("deferrable"):
            return False
        if a.get("initially_deferred") != b.get("initially_deferred"):
            return False
        for x, y in zip(a["elements"], b["elements"], strict=True):
            if x["operator"] != y["operator"] or x.get("column") != y.get("column"):
                return False
        if not compare_expressions:  # the database reformats expressions
            return ("where" in a) == ("where" in b)
        return a.get("where") == b.get("where") and [
            e.get("expression") for e in a["elements"]
        ] == [e.get("expression") for e in b["elements"]]

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
    _diff_named(
        old.get("exclusions", {}),
        new.get("exclusions", {}),
        same_exclusion,
        drop("exclusion"),
        lambda e: plan.add_constraints.append(AddExclusion(schema, name, e)),
        rename,
    )

    def same_index(a: IndexSnap, b: IndexSnap) -> bool:
        if a["columns"] != b["columns"] or a["unique"] != b["unique"]:
            return False
        for extra in ("method", "opclasses", "options", "nulls_not_distinct"):
            if a.get(extra) != b.get(extra):
                return False
        if not expressions:  # the database reformats expressions; compare shapes
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
    normalized: bool = False,
) -> list[Step]:
    """Steps that turn the ``old`` schema into the ``new`` one.

    ``compare_expressions=False`` ignores CHECK expressions and index predicates, whose text
    a database reformats; use it when ``old`` comes from introspection (``push``).
    ``normalized=True`` says ``new`` went through ``normalize_snapshot``, so checks, index
    predicates and generated expressions are compared after all.
    """
    plan = _Plan()
    plan.schemas = [CreateSchema(s) for s in new["schemas"] if s not in old["schemas"]]
    _diff_enums(old, new, plan)
    # Columns of these types change type through text (C23). A type in the default schema
    # may be spelled without it, so the bare name counts too.
    enum_names = frozenset(old["enums"]) | frozenset(e["name"] for e in old["enums"].values())
    _diff_domains(old, new, plan, compare_expressions)
    _diff_composites(old, new, plan)
    old_tables = _apply_renames(old, new, resolve, plan)

    for k, table in new["tables"].items():
        before = old_tables.get(k)
        if before is None:
            plan.create_tables.append(CreateTable(table))
            plan.create_partitions += [
                CreatePartition(table["schema"], table["name"], pname, bound)
                for pname, bound in table.get("partitions", {}).items()
            ]
            plan.create_indexes += [
                CreateIndex(table["schema"], table["name"], i) for i in table["indexes"].values()
            ]
            plan.add_constraints += [
                AddCheck(table["schema"], table["name"], c)
                for c in table["checks"].values()
                if c.get("not_valid")  # CREATE TABLE cannot declare it NOT VALID
            ]
            plan.add_fks += [
                AddForeignKey(table["schema"], table["name"], fk)
                for fk in table["foreign_keys"].values()
            ]
        else:
            _diff_table(before, table, plan, compare_expressions, enum_names, normalized)
        _diff_grants(table, before.get("grants", {}) if before else {}, plan, compare_expressions)
        _diff_comments(before if before is not None else _empty_like(table), table, plan)

    def new_in(schema: str) -> list[TableSnap]:
        return [t for t in new["tables"].values() if t["schema"] == schema]

    for k, table in old_tables.items():
        if k not in new["tables"]:
            # Its foreign keys would block changes to the tables they reference.
            plan.drop_fks += [
                DropConstraint(table["schema"], table["name"], n, "foreign_key")
                for n in table["foreign_keys"]
            ]
            # A view of the same name takes its place: the table must go first.
            replaced = k in new.get("views", {})
            drops = plan.drop_replaced if replaced else plan.drop_tables
            if _relation_names(table) & _relation_names(*new_in(table["schema"])):
                drops = plan.drop_early  # an index name is used again (a table recreated)
            drops.append(DropTable(table["schema"], table["name"]))
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
    touched |= _diff_views(old, new, plan, touched, compare_expressions)
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


def _diff_comments(before: TableSnap, after: TableSnap, plan: _Plan) -> None:
    """``COMMENT ON`` for the table and every column whose comment changed (a new column has
    no old comment, so its comment is set too). Comments on dropped objects go with them."""
    schema, name = after["schema"], after["name"]
    if before.get("comment") != after.get("comment"):
        plan.comments.append(SetTableComment(schema, name, after.get("comment")))
    for cname, column in after["columns"].items():
        old = before["columns"].get(cname)
        old_comment = old.get("comment") if old is not None else None
        if old_comment != column.get("comment"):
            plan.comments.append(SetColumnComment(schema, name, cname, column.get("comment")))


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
    """Returns the names of functions that are dropped (also to be created again).
    Functions are matched by name and arguments: overloads are separate functions, and
    other arguments make another function (the old one is dropped)."""
    # Keyed again: snapshots written before overloads were supported used "schema.name".
    before = {function_key(f): f for f in old.get("functions", {}).values()}
    after = {function_key(f): f for f in new.get("functions", {}).values()}
    dropped: set[str] = set()
    for k, fn in after.items():
        prev = before.get(k)
        if prev is not None and prev["returns"] != fn["returns"]:
            # CREATE OR REPLACE cannot change it.
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
    for step in [
        *plan.drop_tables,
        *plan.drop_replaced,
        *plan.drop_early,
        *plan.drop_columns,
        *plan.drop_rebuilt,
        *plan.alter_columns,
    ]:
        match step:
            case DropTable(name=name):
                names.add(name)
            case DropColumn(table=table, name=name):
                names |= {table, name}
            case AlterColumnType(table=table, column=column):
                names |= {table, column}
            case _:
                pass
    # Old snapshots still use the names from before a rename.
    for step in plan.renames:
        match step:
            case RenameColumn(table=table, old=old, new=new) if {table, new} & names:
                names.add(old)
            case RenameTable(old=old, new=new) if new in names:
                names.add(old)
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


def _diff_views(
    old: Snapshot, new: Snapshot, plan: _Plan, touched: set[str], drop_unlisted: bool
) -> set[str]:
    """Views are dropped and created again, never altered: CREATE OR REPLACE VIEW cannot
    remove or retype columns. Their privileges, owner and comment are kept across that.
    Returns the names of views that are dropped."""
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
        if k in after:
            plan.drop_views.append(SaveGrants(v["schema"], v["name"]))
        plan.drop_views.append(DropView(v["schema"], v["name"], v["materialized"]))
    for k in _ordered(after, [k for k in after if k in redo or k not in before]):
        v = after[k]
        plan.create_views.append(CreateView(v))
        plan.create_views += [
            CreateIndex(v["schema"], v["name"], i) for i in v.get("indexes", {}).values()
        ]
        if k in before:
            plan.create_views.append(RestoreGrants(v["schema"], v["name"], v["materialized"]))
    for k, v in after.items():
        prev = before.get(k)
        if prev is not None and k not in redo:
            _diff_view_indexes(prev, v, plan)
        # Restored after a re-create, so the grants of ``prev`` are there either way.
        _diff_grants(v, prev.get("grants", {}) if prev else {}, plan, drop_unlisted)
        old_triggers = prev.get("triggers", {}) if prev is not None and k not in redo else {}
        _diff_triggers(v["schema"], v["name"], old_triggers, v.get("triggers", {}), plan, touched)
    return {before[k]["name"] for k in redo}


def _diff_grants(
    relation: TableSnap | ViewSnap, old: GrantsSnap, plan: _Plan, drop_unlisted: bool
) -> None:
    """Grant what is new and revoke what is gone. Roles the new schema does not mention are
    left alone unless ``old`` is a snapshot (``drop_unlisted``), which only has declared
    ones: a live database also lists grants made outside the schema."""
    schema, name, new = relation["schema"], relation["name"], relation.get("grants", {})
    roles = set(new) | (set(old) if drop_unlisted else set[str]())
    for role in sorted(roles):
        before, after = set(old.get(role, [])), set(new.get(role, []))
        if gone := before - after:
            plan.revokes.append(GrantPrivileges(schema, name, role, tuple(sorted(gone)), True))
        if added := after - before:
            plan.grants.append(GrantPrivileges(schema, name, role, tuple(sorted(added))))


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

    _diff_triggers(schema, name, old.get("triggers", {}), new.get("triggers", {}), plan, touched)


def _diff_triggers(
    schema: str,
    name: str,
    old_triggers: Mapping[str, TriggerSnap],
    new_triggers: Mapping[str, TriggerSnap],
    plan: _Plan,
    touched: set[str],
) -> None:
    for tname, trigger in new_triggers.items():
        was = old_triggers.get(tname)
        blocked = was is not None and (
            _mentions(was["when"], touched)
            or any(c in touched for c in was.get("columns", []))  # UPDATE OF a dropped column
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
    (or a column of it) fails while the key is in use, so drop the foreign key first and
    add it back after.
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
    # Columns dropped and added again (made generated...): DROP COLUMN fails while a foreign
    # key references them.
    rebuilt = {(s.schema, s.table, s.name) for s in plan.drop_rebuilt if isinstance(s, DropColumn)}
    added = {
        (s.schema, s.table, s.foreign_key["name"])
        for s in plan.add_fks
        if isinstance(s, AddForeignKey)
    }
    for k, table in new["tables"].items():
        before = old_tables.get(k)
        for name, fk in table["foreign_keys"].items():
            ref = (fk["ref_schema"], fk["ref_table"], frozenset(fk["ref_columns"]))
            uses_rebuilt = any(
                (fk["ref_schema"], fk["ref_table"], c) in rebuilt for c in fk["ref_columns"]
            )
            if before is None:
                continue
            # Renamed (RENAME CONSTRAINT runs first) still counts as existing.
            same = _without_name(fk)
            if not any(_without_name(f) == same for f in before["foreign_keys"].values()):
                continue
            if ref not in dropped and not uses_rebuilt:
                continue
            if (table["schema"], table["name"], name) in added:
                continue
            plan.drop_fks.append(
                DropConstraint(table["schema"], table["name"], name, "foreign_key")
            )
            plan.add_fks.append(AddForeignKey(table["schema"], table["name"], fk))
