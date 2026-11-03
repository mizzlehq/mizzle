"""Build a Snapshot from Python table classes."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import cast

from mizzle.core.errors import SchemaError
from mizzle.core.naming import Casing
from mizzle.core.schema.column import ColumnBase, ReferentialAction
from mizzle.core.schema.constraints import (
    Check,
    ForeignKey,
    Index,
    Policy,
    PrimaryKey,
    RowLevelSecurity,
    UniqueConstraint,
)
from mizzle.core.schema.objects import Extension, Function, SchemaObject, Trigger
from mizzle.core.schema.table import Table, validate_tables
from mizzle.core.schema.view import View
from mizzle.core.snapshot import (
    CheckSnap,
    ColumnSnap,
    EnumSnap,
    ExtensionSnap,
    ForeignKeySnap,
    FunctionSnap,
    GeneratedSnap,
    IdentitySnap,
    IndexSnap,
    PolicySnap,
    PrimaryKeySnap,
    RlsSnap,
    Snapshot,
    TableSnap,
    TriggerSnap,
    UniqueSnap,
    ViewSnap,
    empty_snapshot,
    key,
)
from mizzle.db import Session
from mizzle.dialects.base import Dialect


def build_snapshot(
    tables: Sequence[type[Table]],
    dialect: Dialect,
    casing: Casing | None = None,
    *,
    prev_id: str | None = None,
    objects: Sequence[SchemaObject] = (),
) -> Snapshot:
    """``tables`` may include views; ``objects`` are extensions, functions and triggers."""
    validate_tables(tables, casing)
    snapshot = empty_snapshot(dialect.name, str(uuid.uuid4()))
    snapshot["prev_id"] = prev_id
    default_schema = dialect.default_schema

    def schema_of(table: type[Table]) -> str:
        return table.__table__.schema or default_schema

    def names(columns: Sequence[ColumnBase]) -> list[str]:
        return [c.db_name(casing) for c in columns]

    def add_schema(schema: str) -> None:
        if schema != default_schema and schema not in snapshot["schemas"]:
            snapshot["schemas"].append(schema)

    for table in tables:
        if issubclass(table, View):
            _add_view(snapshot, table, dialect, casing)
            add_schema(schema_of(table))
            continue
        info = table.__table__
        schema, tname = schema_of(table), info.name

        def trunc(name: str) -> str:
            # Shorten the middle, keeping the "_key" / "_fkey" / ... suffix: cutting it off
            # can give a unique and a foreign key on the same columns the same name.
            over = len(name) - len(dialect.truncate_name(name))
            if over == 0:
                return name
            base, _, suffix = name.rpartition("_")
            return dialect.truncate_name(f"{base[:-over]}_{suffix}")

        snap = TableSnap(
            schema=schema,
            name=tname,
            columns={},
            primary_key=None,
            uniques={},
            checks={},
            foreign_keys={},
            indexes={},
        )
        for column in info.columns.values():
            cname = column.db_name(casing)
            if column.enum is not None:
                _add_enum(snapshot, default_schema, column)
            col = snap["columns"][cname] = ColumnSnap(
                name=cname,
                type=dialect.column_type(column),
                not_null=not column.nullable,
                default=dialect.column_default(column, casing),
            )
            if (identity := column.identity) is not None:
                increment = identity.increment or 1
                start = identity.start
                col["identity"] = IdentitySnap(
                    always=identity.always,
                    # The database's defaults: ascending from 1, descending from -1.
                    start=start if start is not None else (1 if increment > 0 else -1),
                    increment=increment,
                )
            if (computed := column.computed) is not None:
                col["generated"] = GeneratedSnap(
                    expression=dialect.expression(computed.expression, casing),
                    stored=computed.stored,
                )
            if column.is_primary_key:
                snap["primary_key"] = PrimaryKeySnap(name=trunc(f"{tname}_pkey"), columns=[cname])
            if column.is_unique:
                name = column.unique_name or trunc(f"{tname}_{cname}_key")
                snap["uniques"][name] = UniqueSnap(name=name, columns=[cname])
            for i, condition in enumerate(column.checks):
                name = trunc(f"{tname}_{cname}_check{i or ''}")
                expression = dialect.expression(condition, casing)
                snap["checks"][name] = CheckSnap(name=name, expression=expression)
            if column.reference is not None:
                target = column.reference.target()
                assert target.table is not None  # checked by validate_tables
                name = trunc(f"{tname}_{cname}_fkey")
                snap["foreign_keys"][name] = ForeignKeySnap(
                    name=name,
                    columns=[cname],
                    ref_schema=schema_of(target.table),
                    ref_table=target.table.__table__.name,
                    ref_columns=[target.db_name(casing)],
                    on_delete=_action(column.reference.on_delete),
                    on_update=_action(column.reference.on_update),
                )

        for constraint in table.config():
            match constraint:
                case Index():
                    snap["indexes"][constraint.name] = _index(constraint, dialect, casing)
                case UniqueConstraint():
                    cols = names(constraint.columns)
                    name = constraint.name or trunc(f"{tname}_{'_'.join(cols)}_key")
                    snap["uniques"][name] = UniqueSnap(name=name, columns=cols)
                case PrimaryKey():
                    name = constraint.name or trunc(f"{tname}_pkey")
                    snap["primary_key"] = PrimaryKeySnap(
                        name=name, columns=names(constraint.columns)
                    )
                    for cname in names(constraint.columns):  # the database enforces this
                        snap["columns"][cname]["not_null"] = True
                case ForeignKey():
                    cols = names(constraint.columns)
                    refs = list(constraint.references())
                    ref_table = refs[0].table
                    if ref_table is None or any(r.table is not ref_table for r in refs):
                        raise SchemaError(
                            f"{table.__name__}: a foreign key must reference one table"
                        )
                    name = constraint.name or trunc(f"{tname}_{'_'.join(cols)}_fkey")
                    snap["foreign_keys"][name] = ForeignKeySnap(
                        name=name,
                        columns=cols,
                        ref_schema=schema_of(ref_table),
                        ref_table=ref_table.__table__.name,
                        ref_columns=names(refs),
                        on_delete=_action(constraint.on_delete),
                        on_update=_action(constraint.on_update),
                    )
                case Check():
                    expression = dialect.expression(constraint.condition, casing)
                    snap["checks"][constraint.name] = CheckSnap(
                        name=constraint.name, expression=expression
                    )
                case Policy():
                    snap.setdefault("policies", {})[constraint.name] = _policy(
                        constraint, dialect, casing
                    )
                case RowLevelSecurity():
                    if constraint.enabled or constraint.force:
                        snap["rls"] = RlsSnap(enabled=constraint.enabled, force=constraint.force)

        if pk := snap["primary_key"]:  # CREATE TABLE drops a UNIQUE that repeats the key
            snap["uniques"] = {
                n: u for n, u in snap["uniques"].items() if u["columns"] != pk["columns"]
            }
        add_schema(schema)
        snapshot["tables"][key(schema, tname)] = snap
        for column in info.columns.values():  # e.g. vector columns need pgvector
            if (needed := dialect.type_extension(column.sql_type)) is not None:
                snapshot.setdefault("extensions", {}).setdefault(needed, ExtensionSnap(name=needed))

    for obj in [*objects, *(o.execute for o in objects if isinstance(o, Trigger))]:
        match obj:
            case Extension():
                ext = ExtensionSnap(name=obj.name)
                if obj.schema is not None:  # created with it (see render)
                    ext["schema"] = obj.schema
                if obj.version is not None:
                    ext["version"] = obj.version
                snapshot.setdefault("extensions", {})[obj.name] = ext
            case Function():
                fn = _function(obj, default_schema)
                functions = snapshot.setdefault("functions", {})
                existing = functions.get(key(fn["schema"], fn["name"]))
                if existing is not None and existing != fn:
                    raise SchemaError(f"function {fn['name']!r} is declared twice")
                functions[key(fn["schema"], fn["name"])] = fn
                add_schema(fn["schema"])
            case Trigger():
                on = snapshot["tables"].get(key(schema_of(obj.on), obj.on.__table__.name))
                if on is None:
                    raise SchemaError(
                        f"trigger {obj.name!r} is on {obj.on.__name__}, which is not a table "
                        "in the schema"
                    )
                when = obj.when
                on.setdefault("triggers", {})[obj.name] = TriggerSnap(
                    name=obj.name,
                    timing=obj.timing,
                    events=list(obj.events),
                    for_each=obj.for_each,
                    function=key(obj.execute.schema or default_schema, obj.execute.name),
                    when=dialect.expression(when, casing) if when is not None else None,
                )
    return snapshot


def _index(constraint: Index, dialect: Dialect, casing: Casing | None) -> IndexSnap:
    where = constraint.predicate
    keys = constraint.columns
    plain = [k for k in keys if isinstance(k, ColumnBase)]
    index = IndexSnap(
        name=constraint.name,
        columns=[c.db_name(casing) for c in plain] if len(plain) == len(keys) else [],
        unique=constraint.unique,
        where=dialect.expression(where, casing) if where is not None else None,
    )
    opclass = constraint.opclass
    if len(plain) != len(keys):  # an expression index; opclasses inline
        index["expression"] = ", ".join(
            dialect.expression(k, casing) + (f" {opclass}" if opclass else "") for k in keys
        )
    elif opclass is not None:
        index["opclasses"] = [opclass] * len(keys)
    if constraint.method is not None and constraint.method != "btree":
        index["method"] = constraint.method
    if constraint.options:
        index["options"] = dict(constraint.options)
    return index


def _policy(policy: Policy, dialect: Dialect, casing: Casing | None) -> PolicySnap:
    using, with_check = policy.using, policy.with_check
    return PolicySnap(
        name=policy.name,
        command=policy.command,
        permissive=policy.permissive,
        roles=list(policy.roles),
        using=dialect.expression(using, casing) if using is not None else None,
        with_check=dialect.expression(with_check, casing) if with_check is not None else None,
    )


def _function(fn: Function, default_schema: str) -> FunctionSnap:
    return FunctionSnap(
        schema=fn.schema or default_schema,
        name=fn.name,
        args=fn.args,
        returns=fn.returns,
        language=fn.language,
        body=fn.body,
        volatility=fn.volatility,
        security_definer=fn.security_definer,
    )


def _add_view(
    snapshot: Snapshot, view: type[View], dialect: Dialect, casing: Casing | None
) -> None:
    info = view.__table__
    schema = info.schema or dialect.default_schema
    query = view.query(Session(dialect=dialect, driver=None, casing=casing))
    snap = ViewSnap(
        schema=schema,
        name=info.name,
        definition=dialect.view_definition(query, casing),
        materialized=view.__materialized__,
        columns={},
    )
    for column in info.columns.values():
        name = column.db_name(casing)
        snap["columns"][name] = ColumnSnap(
            name=name, type=dialect.column_type(column), not_null=False, default=None
        )
    for constraint in view.config():
        if not isinstance(constraint, Index) or not view.__materialized__:
            raise SchemaError(
                f"{view.__name__}.config(): views take only indexes, on materialized views"
            )
        snap.setdefault("indexes", {})[constraint.name] = _index(constraint, dialect, casing)
    snapshot.setdefault("views", {})[key(schema, info.name)] = snap


def _action(action: ReferentialAction | None) -> ReferentialAction | None:
    # "no action" is the database default; introspection reports it as None.
    return None if action == "no action" else action


def _add_enum(snapshot: Snapshot, schema: str, column: ColumnBase) -> None:
    assert column.enum is not None
    name = column.sql_type.removesuffix("[]")
    values = [str(cast(object, member.value)) for member in column.enum]
    existing = snapshot["enums"].get(key(schema, name))
    if existing is not None and existing["values"] != values:
        raise SchemaError(f"enum type {name!r} is declared twice with different values")
    snapshot["enums"][key(schema, name)] = EnumSnap(schema=schema, name=name, values=values)
