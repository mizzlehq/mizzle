"""Build a Snapshot from Python table classes."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Sequence
from typing import cast

from mizzle.core.errors import SchemaError
from mizzle.core.naming import Casing
from mizzle.core.schema.column import ColumnBase, Identity, ReferentialAction
from mizzle.core.schema.constraints import (
    Check,
    Exclusion,
    ForeignKey,
    Grant,
    Index,
    Partition,
    Policy,
    PrimaryKey,
    Role,
    RowLevelSecurity,
    UniqueConstraint,
)
from mizzle.core.schema.objects import (
    Composite,
    Domain,
    Extension,
    Function,
    SchemaObject,
    Trigger,
)
from mizzle.core.schema.table import Table, validate_tables
from mizzle.core.schema.view import View
from mizzle.core.snapshot import (
    CheckSnap,
    ColumnSnap,
    CompositeFieldSnap,
    CompositeSnap,
    DomainSnap,
    EnumSnap,
    ExclusionElementSnap,
    ExclusionSnap,
    ExtensionSnap,
    ForeignKeySnap,
    FunctionSnap,
    GeneratedSnap,
    GrantsSnap,
    IdentitySnap,
    IndexSnap,
    PolicySnap,
    PrimaryKeySnap,
    RlsSnap,
    RoleSnap,
    Snapshot,
    TableSnap,
    TriggerSnap,
    UniqueSnap,
    ViewSnap,
    default_cost,
    empty_snapshot,
    function_key,
    key,
    sequence_defaults,
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
    """``tables`` may include views; ``objects`` are extensions, functions, triggers and
    roles."""
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
            return _truncate(name, dialect)

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
        if info.comment is not None:
            snap["comment"] = info.comment
        if info.unlogged:
            snap["unlogged"] = True
        if info.storage:
            snap["options"] = dict(info.storage)
        if info.partition_by is not None:
            snap["partition_by"] = info.partition_by
        for column in info.columns.values():
            cname = column.db_name(casing)
            if column.enum is not None:
                _add_enum(snapshot, column.enum_schema or default_schema, column)
                add_schema(column.enum_schema or default_schema)
            col = snap["columns"][cname] = ColumnSnap(
                name=cname,
                type=dialect.column_type(column),
                not_null=not column.nullable,
                default=dialect.column_default(column, casing),
            )
            if (identity := column.identity) is not None:
                col["identity"] = _identity(identity, col["type"])
            if (computed := column.computed) is not None:
                col["generated"] = GeneratedSnap(
                    expression=dialect.expression(computed.expression, casing),
                    stored=computed.stored,
                )
            if column.collation is not None:
                col["collation"] = column.collation
            if column.comment_text is not None:
                col["comment"] = column.comment_text
            if column.is_primary_key:
                col_pk = PrimaryKeySnap(name=trunc(f"{tname}_pkey"), columns=[cname])
                if column.pk_deferrable:
                    col_pk["deferrable"] = True
                if column.pk_initially_deferred:
                    col_pk["initially_deferred"] = True
                snap["primary_key"] = col_pk
            if column.is_unique:
                name = column.unique_name or trunc(f"{tname}_{cname}_key")
                u = UniqueSnap(name=name, columns=[cname])
                if column.unique_nulls_not_distinct:
                    u["nulls_not_distinct"] = True
                if column.unique_deferrable:
                    u["deferrable"] = True
                if column.unique_initially_deferred:
                    u["initially_deferred"] = True
                snap["uniques"][name] = u
            for i, condition in enumerate(column.checks):
                name = trunc(f"{tname}_{cname}_check{i or ''}")
                expression = dialect.expression(condition, casing)
                snap["checks"][name] = CheckSnap(name=name, expression=expression)
            if column.reference is not None:
                target = column.reference.target()
                assert target.table is not None  # checked by validate_tables
                name = trunc(f"{tname}_{cname}_fkey")
                fk = ForeignKeySnap(
                    name=name,
                    columns=[cname],
                    ref_schema=schema_of(target.table),
                    ref_table=target.table.__table__.name,
                    ref_columns=[target.db_name(casing)],
                    on_delete=_action(column.reference.on_delete),
                    on_update=_action(column.reference.on_update),
                )
                if column.reference.deferrable:
                    fk["deferrable"] = True
                if column.reference.initially_deferred:
                    fk["initially_deferred"] = True
                if column.reference.not_valid:
                    fk["not_valid"] = True
                snap["foreign_keys"][name] = fk

        for constraint in table.config():
            match constraint:
                case Index():
                    snap["indexes"][constraint.name] = _index(constraint, dialect, casing)
                case UniqueConstraint():
                    cols = names(constraint.columns)
                    name = constraint.name or trunc(f"{tname}_{'_'.join(cols)}_key")
                    u = UniqueSnap(name=name, columns=cols)
                    if constraint.nulls_not_distinct:
                        u["nulls_not_distinct"] = True
                    if constraint.deferrable:
                        u["deferrable"] = True
                    if constraint.initially_deferred:
                        u["initially_deferred"] = True
                    snap["uniques"][name] = u
                case PrimaryKey():
                    name = constraint.name or trunc(f"{tname}_pkey")
                    tbl_pk = PrimaryKeySnap(name=name, columns=names(constraint.columns))
                    if constraint.deferrable:
                        tbl_pk["deferrable"] = True
                    if constraint.initially_deferred:
                        tbl_pk["initially_deferred"] = True
                    snap["primary_key"] = tbl_pk
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
                    fk = ForeignKeySnap(
                        name=name,
                        columns=cols,
                        ref_schema=schema_of(ref_table),
                        ref_table=ref_table.__table__.name,
                        ref_columns=names(refs),
                        on_delete=_action(constraint.on_delete),
                        on_update=_action(constraint.on_update),
                    )
                    if constraint.deferrable:
                        fk["deferrable"] = True
                    if constraint.initially_deferred:
                        fk["initially_deferred"] = True
                    if constraint.not_valid:
                        fk["not_valid"] = True
                    snap["foreign_keys"][name] = fk
                case Check():
                    expression = dialect.expression(constraint.condition, casing)
                    c = CheckSnap(name=constraint.name, expression=expression)
                    if constraint.not_valid:
                        c["not_valid"] = True
                    snap["checks"][constraint.name] = c
                case Exclusion():
                    elements: list[ExclusionElementSnap] = []
                    colnames: list[str] = []
                    for elem_key, operator in constraint.elements:
                        if isinstance(elem_key, ColumnBase):
                            cname = elem_key.db_name(casing)
                            colnames.append(cname)
                            elements.append(ExclusionElementSnap(operator=operator, column=cname))
                        else:
                            elements.append(
                                ExclusionElementSnap(
                                    operator=operator,
                                    expression=dialect.expression(elem_key, casing),
                                )
                            )
                    derived = f"{tname}_{'_'.join(colnames)}_excl" if colnames else f"{tname}_excl"
                    excl = ExclusionSnap(
                        name=constraint.name or trunc(derived),
                        method=constraint.method,
                        elements=elements,
                    )
                    if constraint.predicate is not None:
                        excl["where"] = dialect.expression(constraint.predicate, casing)
                    if constraint.deferrable:
                        excl["deferrable"] = True
                    if constraint.initially_deferred:
                        excl["initially_deferred"] = True
                    snap.setdefault("exclusions", {})[excl["name"]] = excl
                case Policy():
                    snap.setdefault("policies", {})[constraint.name] = _policy(
                        constraint, dialect, casing
                    )
                    for declared in constraint.declared_roles:
                        _add_role(snapshot, declared)
                case RowLevelSecurity():
                    if constraint.enabled or constraint.force:
                        snap["rls"] = RlsSnap(enabled=constraint.enabled, force=constraint.force)
                case Grant():
                    _add_grant(snap.setdefault("grants", {}), constraint)
                    for declared in constraint.declared_roles:
                        _add_role(snapshot, declared)
                case Partition():
                    snap.setdefault("partitions", {})[constraint.name] = constraint.for_values

        if pk := snap["primary_key"]:  # CREATE TABLE drops a UNIQUE that repeats the key
            snap["uniques"] = {
                n: u for n, u in snap["uniques"].items() if u["columns"] != pk["columns"]
            }
        add_schema(schema)
        snapshot["tables"][key(schema, tname)] = snap
        for column in info.columns.values():  # e.g. vector columns need pgvector
            if (needed := dialect.type_extension(column.sql_type)) is not None:
                snapshot.setdefault("extensions", {}).setdefault(needed, ExtensionSnap(name=needed))

    declared_exts: dict[str, ExtensionSnap] = {}
    for obj in [*objects, *(o.execute for o in objects if isinstance(o, Trigger))]:
        match obj:
            case Role():
                _add_role(snapshot, obj)
            case Extension():
                ext = ExtensionSnap(name=obj.name)
                if obj.schema is not None:  # created with it (see render)
                    ext["schema"] = obj.schema
                if obj.version is not None:
                    ext["version"] = obj.version
                # Two declarations that disagree silently kept the last one. One a column
                # type implied (no options) may be replaced by the declared one.
                if declared_exts.get(obj.name, ext) != ext:
                    raise SchemaError(
                        f"extension {obj.name!r} is declared twice with different options"
                    )
                declared_exts[obj.name] = ext
                snapshot.setdefault("extensions", {})[obj.name] = ext
            case Function():
                fn = _function(obj, default_schema)
                functions = snapshot.setdefault("functions", {})
                existing = functions.get(function_key(fn))
                if existing is not None and existing != fn:
                    raise SchemaError(f"function {fn['name']!r} is declared twice")
                functions[function_key(fn)] = fn
                add_schema(fn["schema"])
            case Domain():
                _add_domain(snapshot, obj, default_schema, dialect)
            case Composite():
                _add_composite(snapshot, obj, default_schema, dialect)
            case ColumnBase():  # pg_enum(...) on its own: the enum type (K4-5)
                _add_enum(snapshot, obj.enum_schema or default_schema, obj)
                add_schema(obj.enum_schema or default_schema)
            case Trigger():
                k = key(schema_of(obj.on), obj.on.__table__.name)
                on = snapshot["tables"].get(k) or snapshot.get("views", {}).get(k)
                if on is None:
                    raise SchemaError(
                        f"trigger {obj.name!r} is on {obj.on.__name__}, which is not a table "
                        "or view in the schema"
                    )
                _check_trigger(obj)
                when = obj.when
                trg = TriggerSnap(
                    name=obj.name,
                    timing=obj.timing,
                    events=list(obj.events),
                    for_each=obj.for_each,
                    function=key(obj.execute.schema or default_schema, obj.execute.name),
                    when=dialect.expression(when, casing) if when is not None else None,
                )
                if obj.update_of:
                    trg["columns"] = sorted(c.db_name(casing) for c in obj.update_of)
                if obj.old_table is not None:
                    trg["old_table"] = obj.old_table
                if obj.new_table is not None:
                    trg["new_table"] = obj.new_table
                on.setdefault("triggers", {})[obj.name] = trg
    _imply_deferrable(snapshot)
    return snapshot


def _imply_deferrable(snapshot: Snapshot) -> None:
    """INITIALLY DEFERRED requires DEFERRABLE (PostgreSQL rejects it alone), and the
    database always reports both; store both so a declared schema does not drift."""
    for table in snapshot["tables"].values():
        pk = table["primary_key"]
        if pk is not None and pk.get("initially_deferred"):
            pk["deferrable"] = True
        for unique in table["uniques"].values():
            if unique.get("initially_deferred"):
                unique["deferrable"] = True
        for fk in table["foreign_keys"].values():
            if fk.get("initially_deferred"):
                fk["deferrable"] = True
        for excl in table.get("exclusions", {}).values():
            if excl.get("initially_deferred"):
                excl["deferrable"] = True


def _check_trigger(trigger: Trigger) -> None:
    on = trigger.on
    on_view = issubclass(on, View)
    if issubclass(on, View) and on.__materialized__:
        raise SchemaError(f"trigger {trigger.name!r}: materialized views cannot have triggers")
    if on_view and trigger.for_each == "row" and trigger.timing != "instead of":
        raise SchemaError(
            f"trigger {trigger.name!r} is on view {trigger.on.__name__}: row triggers on a view "
            "are INSTEAD OF (timing='instead of'); BEFORE/AFTER ones are FOR EACH STATEMENT"
        )
    if not on_view and trigger.timing == "instead of":
        raise SchemaError(f"trigger {trigger.name!r}: INSTEAD OF triggers are for views")


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
    opclasses = constraint.opclasses(len(keys))
    if len(plain) != len(keys):  # an expression index; opclasses inline
        index["expression"] = ", ".join(
            dialect.expression(k, casing) + (f" {op}" if op else "")
            for k, op in zip(keys, opclasses, strict=True)
        )
    elif any(opclasses):
        index["opclasses"] = opclasses
    if constraint.method is not None and constraint.method != "btree":
        index["method"] = constraint.method
    if constraint.options:
        index["options"] = dict(constraint.options)
    if constraint.nulls_not_distinct:
        index["nulls_not_distinct"] = True
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
    snap = FunctionSnap(
        schema=fn.schema or default_schema,
        name=fn.name,
        args=fn.args,
        returns=fn.returns,
        language=fn.language,
        body=fn.body,
        volatility=fn.volatility,
        security_definer=fn.security_definer,
    )
    if fn.strict:
        snap["strict"] = True
    if fn.leakproof:
        snap["leakproof"] = True
    if fn.parallel != "unsafe":
        snap["parallel"] = fn.parallel
    if fn.cost is not None and fn.cost != default_cost(fn.language):
        snap["cost"] = float(fn.cost)
    if fn.settings:
        snap["settings"] = dict(fn.settings)
    return snap


def _identity(identity: Identity, sql_type: str) -> IdentitySnap:
    increment = identity.increment or 1
    low, high = sequence_defaults(sql_type, increment)
    snap = IdentitySnap(always=identity.always, start=0, increment=increment)
    if identity.min_value is not None and identity.min_value != low:
        snap["min_value"] = low = identity.min_value
    if identity.max_value is not None and identity.max_value != high:
        snap["max_value"] = high = identity.max_value
    if identity.cache not in (None, 1):
        snap["cache"] = identity.cache
    if identity.cycle:
        snap["cycle"] = True
    # The database's default start: the low end ascending, the high end descending.
    default_start = low if increment > 0 else high
    snap["start"] = identity.start if identity.start is not None else default_start
    return snap


def _add_grant(grants: GrantsSnap, grant: Grant) -> None:
    for role in grant.roles:
        privileges = {*grants.get(role, []), *grant.privileges}
        grants[role] = ["all"] if "all" in privileges else sorted(privileges)


def _truncate(name: str, dialect: Dialect) -> str:
    """A generated name the database can keep. A longer one is shortened in the middle,
    with a hash of the whole name: the "_key" / "_fkey" / ... suffix stays (a unique and a
    foreign key on the same columns differ) and two long names never end up the same."""
    if dialect.truncate_name(name) == name:
        return name
    base, _, suffix = name.rpartition("_")
    tail = f"_{hashlib.sha256(name.encode()).hexdigest()[:8]}_{suffix}"
    room = len(dialect.truncate_name(name)) - len(tail.encode())
    return dialect.truncate_name(base.encode()[:room].decode(errors="ignore") + tail)


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
    if view.__security_invoker__:  # (K6)
        snap["security_invoker"] = True
    if view.__security_barrier__:
        snap["security_barrier"] = True
    if view.__check_option__:
        snap["check_option"] = view.__check_option__
    for column in info.columns.values():
        name = column.db_name(casing)
        snap["columns"][name] = ColumnSnap(
            name=name, type=dialect.column_type(column), not_null=False, default=None
        )
    for constraint in view.config():
        if isinstance(constraint, Grant):
            _add_grant(snap.setdefault("grants", {}), constraint)
            for declared in constraint.declared_roles:
                _add_role(snapshot, declared)
            continue
        if not isinstance(constraint, Index) or not view.__materialized__:
            raise SchemaError(
                f"{view.__name__}.config(): views take grants, and indexes on materialized views"
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


def _add_domain(snapshot: Snapshot, obj: Domain, default_schema: str, dialect: Dialect) -> None:
    schema = obj.schema or default_schema
    snap = DomainSnap(
        schema=schema,
        name=obj.name,
        base_type=dialect.normalize_type(obj.base_type),
    )
    if obj.not_null:
        snap["not_null"] = True
    if obj.default is not None:
        normalized = dialect.normalize_default(obj.default)
        snap["default"] = normalized if normalized is not None else obj.default
    if obj.checks:
        snap["checks"] = list(obj.checks)
    if obj.collation is not None:
        snap["collation"] = obj.collation
    domains = snapshot.setdefault("domains", {})
    existing = domains.get(key(schema, obj.name))
    if existing is not None and existing != snap:
        raise SchemaError(f"domain {obj.name!r} is declared twice")
    domains[key(schema, obj.name)] = snap
    if schema != default_schema and schema not in snapshot["schemas"]:
        snapshot["schemas"].append(schema)


def _add_composite(
    snapshot: Snapshot, obj: Composite, default_schema: str, dialect: Dialect
) -> None:
    schema = obj.schema or default_schema
    snap = CompositeSnap(
        schema=schema,
        name=obj.name,
        fields=[
            CompositeFieldSnap(name=name, type=dialect.normalize_type(sql_type))
            for name, sql_type in obj.fields
        ],
    )
    composites = snapshot.setdefault("composites", {})
    existing = composites.get(key(schema, obj.name))
    if existing is not None and existing != snap:
        raise SchemaError(f"composite {obj.name!r} is declared twice")
    composites[key(schema, obj.name)] = snap
    if schema != default_schema and schema not in snapshot["schemas"]:
        snapshot["schemas"].append(schema)


def _add_role(snapshot: Snapshot, role: Role) -> None:
    if role.existing:  # managed elsewhere: only referenced by name
        return
    snap = RoleSnap(
        name=role.name,
        login=role.login,
        create_db=role.create_db,
        create_role=role.create_role,
        inherit=role.inherit,
    )
    roles = snapshot.setdefault("roles", {})
    if roles.get(role.name, snap) != snap:
        raise SchemaError(f"role {role.name!r} is declared twice with different options")
    roles[role.name] = snap
