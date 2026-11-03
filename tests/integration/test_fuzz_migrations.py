"""Random schemas through the migration engine.

Schemas also get random extensions, views (plain and materialized) over their tables,
row-level security, policies, grants, a trigger function with triggers (UPDATE OF columns,
transition tables, INSTEAD OF on views), and a SQL function some views call; mutations
change all of them, rename and move tables, rename columns, and make columns generated.
Half of the schemas use ``casing="snake_case"`` (camelCase attributes), and the enum type
may live in the other schema.

A: empty -> schema, applied, introspected: no drift.
B: schema S1 (with a few rows) -> random mutation S2, applied: no drift, untouched data kept.
C: rendering is deterministic and re-diffing the migrated database finds nothing to do.
The CLI test runs the same evolution through ``mizzle generate`` and ``mizzle migrate``.
"""

import asyncio
import contextlib
import copy
import dataclasses
import datetime as dt
import enum
import os
import re
import tempfile
import types
import uuid
from collections.abc import Callable, Generator
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import cast

import asyncpg
import pytest
from hypothesis import HealthCheck, event, given, note, settings
from hypothesis import strategies as st

from mizzle import (
    QueryBuilder,
    Table,
    View,
    check,
    extension,
    foreign_key,
    function,
    grant,
    index,
    is_not_null,
    policy,
    primary_key,
    rls,
    sql,
    trigger,
    unique,
    unique_index,
)
from mizzle.core.errors import DatabaseError
from mizzle.core.naming import Casing
from mizzle.core.query.select import Select
from mizzle.core.schema.column import ColumnBase, Computed, Identity, Reference, ReferentialAction
from mizzle.core.schema.constraints import Constraint, PolicyCommand, Privilege
from mizzle.core.schema.objects import SchemaObject, TriggerEvent
from mizzle.core.snapshot import Snapshot, empty_snapshot
from mizzle.core.sql.nodes import ExprBase
from mizzle.core.steps import Statement
from mizzle.dialects.postgres import PostgresDialect
from mizzle.dialects.postgres.ddl import qualified, quote
from mizzle.dialects.postgres.driver import PostgresDriver
from mizzle.kit.cli import main
from mizzle.kit.diff import RenameKind, diff
from mizzle.kit.journal import MigrationsDir
from mizzle.kit.migrate import apply_statements
from mizzle.kit.snapshot import build_snapshot
from mizzle.pg import (
    array,
    bigint,
    bigserial,
    boolean,
    bytea,
    date,
    double_precision,
    inet,
    integer,
    interval,
    json,
    jsonb,
    numeric,
    pg_enum,
    real,
    serial,
    smallint,
    smallserial,
    text,
    time,
    timestamp,
    tsvector,
    varchar,
)
from mizzle.pg import uuid as uuid_col

dialect = PostgresDialect()
SCHEMAS = ["public", "fz_s"]
ENUM = "fz_mood"

# -- column kinds ----------------------------------------------------------------------


@dataclass(frozen=True)
class Kind:
    make: Callable[[type[enum.Enum]], ColumnBase]
    defaults: tuple[object, ...] = ()  # Python literals or sql(...) expressions
    samples: tuple[str, str, str] = ("NULL", "NULL", "NULL")  # SQL literals for test rows
    check: str = "num_nulls({c}) <= 1"
    btree: bool = True  # usable in keys and indexes
    text: bool = False  # lower(col) works
    serial: bool = False
    base: str = ""  # kind of a column referencing this one (serial -> integer)
    generated: str | None = None  # an expression for a generated column of this kind


def _k(
    make: Callable[[], ColumnBase],
    *defaults: object,
    samples: tuple[str, str, str] = ("NULL", "NULL", "NULL"),
    check: str = "num_nulls({c}) <= 1",
    btree: bool = True,
    text: bool = False,
    serial: bool = False,
    base: str = "",
    generated: str | None = None,
) -> Kind:
    return Kind(
        lambda _enum: make(), defaults, samples, check, btree, text, serial, base, generated
    )


INTS = ("1", "2", "3")
STRS = ("'a'", "'b'", "'it''s'")
NUMCHECK = "{c} >= 0"
TEXTCHECK = "length({c}) < 100"

KINDS: dict[str, Kind] = {
    "smallint": _k(smallint, 0, -3, sql("(1 + 2)"), samples=INTS, check=NUMCHECK, generated="2"),
    "integer": _k(integer, 0, 7, -1, samples=INTS, check=NUMCHECK, generated="length('abc')"),
    "bigint": _k(bigint, 0, 9_000_000_000, samples=INTS, check=NUMCHECK, generated="(4 * 5)"),
    "numeric": _k(numeric, Decimal("1.50"), 0, Decimal("-2.5"), samples=INTS, check=NUMCHECK),
    "numeric(10,2)": _k(
        lambda: numeric(10, 2), Decimal("1.50"), Decimal("-3"), samples=INTS, check=NUMCHECK
    ),
    "real": _k(real, 1.5, -0.25, samples=("1.5", "2.5", "3.5"), check=NUMCHECK),
    "double": _k(double_precision, 2.5, 0.0, samples=("1.5", "2.5", "3.5"), check=NUMCHECK),
    "text": _k(
        text, "", "it's", "x", samples=STRS, check=TEXTCHECK, text=True, generated="repeat('g', 2)"
    ),
    "varchar(20)": _k(lambda: varchar(20), "", "abc", samples=STRS, check=TEXTCHECK, text=True),
    "varchar": _k(varchar, "z", samples=STRS, check=TEXTCHECK, text=True),
    "boolean": _k(boolean, True, False, samples=("true", "false", "true"), generated="true"),
    "tsvector": _k(
        tsvector,
        "'a' 'b'",  # as PostgreSQL prints it back
        sql("to_tsvector('simple'::regconfig, 'x y'::text)"),  # defaults compare as printed
        samples=("'a b'", "'c:1A'", "''"),
        generated="to_tsvector('english', 'cats sat')",
    ),
    "date": _k(
        date,
        dt.date(2020, 1, 2),
        sql("CURRENT_DATE"),
        samples=("'2020-01-01'", "'2021-02-03'", "'1999-12-31'"),
    ),
    "timestamp": _k(
        timestamp,
        dt.datetime(2020, 1, 2, 3, 4, 5),
        dt.datetime(2020, 1, 2, 3, 4, 5, 500000),  # PostgreSQL prints .5
        sql("now()"),
        samples=("'2020-01-01 10:00'", "'2021-02-03 11:00'", "'1999-12-31 23:59:59'"),
    ),
    "timestamptz": _k(
        lambda: timestamp(tz=True),
        sql("now()"),
        sql("CURRENT_TIMESTAMP"),
        # Printed back in the session time zone; mizzle compares them in UTC.
        dt.datetime(2020, 1, 2, 3, 4, 5, tzinfo=dt.UTC),
        dt.datetime(2020, 1, 2, 3, 4, 5, 250000, dt.timezone(dt.timedelta(hours=5, minutes=45))),
        samples=("'2020-01-01 10:00+00'", "'2021-02-03 11:00+02'", "'1999-12-31 23:59:59+00'"),
    ),
    "time": _k(
        time,
        dt.time(12, 30),
        dt.time(1, 2, 3, 400000),
        sql("LOCALTIME"),
        samples=("'10:00'", "'11:30'", "'23:59'"),
    ),
    "timetz": _k(
        lambda: time(tz=True),
        sql("CURRENT_TIME"),
        dt.time(3, 4, 5, tzinfo=dt.timezone(dt.timedelta(hours=-3, minutes=-30))),
        samples=("'10:00+00'", "'11:30+02'", "'23:59-05'"),
    ),
    "uuid": _k(
        uuid_col,
        sql("gen_random_uuid()"),
        uuid.UUID("12345678-1234-5678-1234-567812345678"),
        samples=(
            "'00000000-0000-0000-0000-000000000001'",
            "'00000000-0000-0000-0000-000000000002'",
            "'00000000-0000-0000-0000-000000000003'",
        ),
    ),
    "bytea": _k(bytea, sql("'\\x00ff'::bytea"), samples=("'\\x01'", "'\\x02'", "'\\x0304'")),
    "interval": _k(
        interval, sql("'1 day'::interval"), samples=("'1 day'", "'2 hours'", "'3 mins'")
    ),
    "inet": _k(inet, sql("'10.0.0.1'::inet"), samples=("'10.0.0.1'", "'10.0.0.2'", "'::1'")),
    "jsonb": _k(jsonb[object], {"a": 1}, [1, "x"], samples=("'{\"a\": 1}'", "'[1]'", "'\"s\"'")),
    "json": _k(json[object], {"k": [1]}, samples=("'{\"a\": 1}'", "'[1]'", "'\"s\"'"), btree=False),
    "text[]": _k(
        lambda: array(text()),
        sql("'{}'::text[]"),
        ["a", "b"],
        ["x y", 'q"\\', "", "null", "it's"],
        samples=("'{a,b}'", "'{}'", "'{c}'"),
    ),
    "integer[]": _k(
        lambda: array(integer()),
        sql("ARRAY[1, 2]"),
        [1, 2],
        samples=("'{1,2}'", "'{}'", "'{3}'"),
    ),
    "enum": Kind(
        lambda e: pg_enum(e, type_name=ENUM),
        ("happy", "sad"),
        samples=("'happy'", "'sad'", "'happy'"),
    ),
    "enum[]": Kind(
        lambda e: array(pg_enum(e, type_name=ENUM)),
        (sql(f"'{{}}'::{ENUM}[]"), ["sad", "happy"]),
        samples=("'{happy}'", "'{sad,happy}'", "'{}'"),
    ),
    "serial": _k(serial, serial=True, base="integer", check=NUMCHECK),
    "bigserial": _k(bigserial, serial=True, base="bigint", check=NUMCHECK),
    "smallserial": _k(smallserial, serial=True, base="smallint", check=NUMCHECK),
}
KIND_NAMES = list(KINDS)
PLAIN_KINDS = [k for k in KIND_NAMES if not KINDS[k].serial]

NUM = {"smallint", "integer", "bigint", "numeric", "numeric(10,2)", "real", "double"}
INT_KINDS = {"smallint", "integer", "bigint"}
STR = {"text", "varchar(20)", "varchar"}
# Kinds Postgres converts between with ``USING col::new_type``. Anything converts to and
# from a string type (I/O conversion). Pairs not listed here (uuid -> integer, ...) have no
# cast at all, so a migration cannot do them without hand-written SQL.
CAST_GROUPS = [
    NUM,
    {"boolean", "integer"},
    {"date", "timestamp", "timestamptz"},
    {"time", "timetz"},
    {"json", "jsonb"},
    {"text[]", "integer[]"},
    {"text[]", "enum[]"},
]


def cast_targets(kind: str) -> list[str]:
    if KINDS[kind].serial:
        return []  # changing to or from serial is refused by design (custom migration)
    if kind in STR:
        return [k for k in PLAIN_KINDS if k != kind]
    found = set(STR) | {k for g in CAST_GROUPS if kind in g for k in g}
    return sorted(found - {kind})


def base_kind(kind: str) -> str:
    return KINDS[kind].base or kind


# -- schema spec -----------------------------------------------------------------------

ACTIONS: list[ReferentialAction | None] = [
    None,
    "cascade",
    "restrict",
    "no action",
    "set null",
    "set default",
]
TABLE_NAMES = ["alpha", "beta", "gamma", "delta", "Odd Name", "t_" + "long" * 11]
VIEW_NAMES = ["v_one", "v_two", "Odd View"]
EXTENSIONS = ["pg_trgm", "citext", "hstore"]
FZ_ROLE = "mizzle_fz_role"
TRIGGER_EVENTS: list[TriggerEvent] = ["insert", "delete", "update", "truncate"]
COMMANDS: list[PolicyCommand] = ["all", "select", "insert", "update", "delete"]
ENUM_EXTRAS = ["glad", "it's", "Mixed Case", "z"]
PRIVILEGES: list[Privilege] = ["select", "insert", "update", "delete", "all"]
# Sequence options of identity columns (start 10, increment 3 and more).
# (min_value, max_value, cache, cycle)
SEQ_OPTIONS: list[tuple[int | None, int | None, int | None, bool]] = [
    (None, None, None, False),
    (None, 30000, 5, False),
    (2, None, None, True),
]
# Options of fz_calc (Function fields): settings and planner options.
CALC_OPTIONS: list[dict[str, object]] = [
    {},
    {"strict": True, "parallel": "safe", "cost": 7.0},
    {"settings": (("search_path", "public, pg_temp"),), "parallel": "restricted"},
]
SERVER = {"virtual": False}  # VIRTUAL generated columns: PostgreSQL 18+ (set by the runner)


def camel(name: str) -> str:
    """The attribute for db column ``name`` under snake_case casing: ``c_a`` -> ``cA``."""
    return re.sub(r"_([a-z])", lambda m: m.group(1).upper(), name)


@dataclass
class Col:
    name: str
    kind: str
    nullable: bool = True
    default: int | None = None  # index into KINDS[kind].defaults
    unique: bool = False
    pk: bool = False  # column-level .primary_key()
    identity: bool | None = None  # GENERATED ALWAYS (True) / BY DEFAULT (False) AS IDENTITY
    generated: bool = False  # GENERATED ALWAYS AS (KINDS[kind].generated) STORED
    virtual: bool = False  # generated VIRTUAL instead of STORED
    seq: int = 0  # identity: SEQ_OPTIONS[seq]
    uid: str = ""  # stays the same when the column is renamed

    def __post_init__(self) -> None:
        self.uid = self.uid or self.name


@dataclass
class Idx:
    name: str
    cols: list[str]
    unique: bool = False
    where: str | None = None  # column: partial index "WHERE col IS NOT NULL"
    expr: str | None = None  # text column: an index on lower(col) instead of cols
    ops: list[str | None] = field(default_factory=list[str | None])  # opclass per text column


@dataclass
class Fk:
    cols: list[str]
    ref: str  # table name
    ref_cols: list[str]
    on_delete: ReferentialAction | None = None
    on_update: ReferentialAction | None = None
    inline: bool = False  # single column: .references() instead of foreign_key()


@dataclass
class Pol:
    name: str
    command: PolicyCommand
    using: str | None = None  # column: "col IS NOT NULL"
    check: str | None = None
    permissive: bool = True
    role: bool = False  # TO the fuzz role, else PUBLIC


@dataclass
class Trg:
    name: str
    events: list[TriggerEvent]
    after: bool = False
    statement: bool = False  # FOR EACH STATEMENT
    when: str | None = None  # column: WHEN (NEW.col IS NOT NULL)
    update_of: list[str] = field(default_factory=list[str])  # UPDATE OF columns
    transition: bool = False  # REFERENCING NEW/OLD TABLE (AFTER, one event)


@dataclass
class Grt:
    privileges: list[Privilege]
    public: bool = False  # TO PUBLIC, else the fuzz role


@dataclass
class Tbl:
    name: str
    schema: str | None
    cols: list[Col]
    pk: list[str] = field(default_factory=list[str])  # table-level primary_key(...)
    uniques: list[list[str]] = field(default_factory=list[list[str]])
    checks: list[str] = field(default_factory=list[str])  # one check per listed column
    indexes: list[Idx] = field(default_factory=list[Idx])
    fks: list[Fk] = field(default_factory=list[Fk])
    rls: int = 0  # 0 off, 1 enabled, 2 enabled and forced, 3 only forced
    policies: list[Pol] = field(default_factory=list[Pol])
    triggers: list[Trg] = field(default_factory=list[Trg])
    grants: list[Grt] = field(default_factory=list[Grt])
    uid: str = ""  # stays the same when the table is renamed or moved

    def __post_init__(self) -> None:
        self.uid = self.uid or self.name

    def col(self, name: str) -> Col | None:
        return next((c for c in self.cols if c.name == name), None)

    def by_uid(self, uid: str) -> Col | None:
        return next((c for c in self.cols if c.uid == uid), None)

    def key_lists(self) -> list[list[str]]:
        """Column lists a foreign key may reference."""
        keys = [self.pk] if self.pk else []
        keys += [[c.name] for c in self.cols if c.pk or c.unique]
        return keys + self.uniques


@dataclass
class Vw:
    name: str
    schema: str | None
    table: str
    cols: list[str]
    where: str | None = None  # column: WHERE col IS NOT NULL
    materialized: bool = False
    indexed: bool = False  # materialized: an index on its first column
    calc: bool = False  # a column calling fz_calc()
    grants: list[Grt] = field(default_factory=list[Grt])
    instead: bool = False  # plain view: an INSTEAD OF INSERT trigger


@dataclass
class Spec:
    enum_values: list[str]
    tables: list[Tbl]
    extensions: list[str] = field(default_factory=list[str])
    views: list[Vw] = field(default_factory=list[Vw])
    fn_variant: int = 0  # body of the trigger function
    calc: int | None = None  # fz_calc(): None absent, else its number of arguments
    calc_options: int = 0  # CALC_OPTIONS[calc_options]
    enum_schema: str | None = None  # the enum type's schema (None: public)
    snake: bool = False  # casing="snake_case": attributes are camelCase

    def table(self, name: str) -> Tbl | None:
        return next((t for t in self.tables if t.name == name), None)

    def by_uid(self, uid: str) -> Tbl | None:
        return next((t for t in self.tables if t.uid == uid), None)

    @property
    def casing(self) -> Casing | None:
        return "snake_case" if self.snake else None

    def attr(self, name: str) -> str:
        return camel(name) if self.snake else name

    @property
    def enum_type(self) -> str:
        return f"{self.enum_schema}.{ENUM}" if self.enum_schema else ENUM


def _dedupe[T](items: list[T]) -> list[T]:
    out: list[T] = []
    for item in items:
        if item not in out:
            out.append(item)
    return out


def sanitize(spec: Spec) -> Spec:
    """Drop whatever a random edit left invalid (missing columns, wrong kinds, ...)."""
    for t in spec.tables:
        t.cols = [c for i, c in enumerate(t.cols) if c.name not in [d.name for d in t.cols[:i]]]
        for c in t.cols:
            kind = KINDS[c.kind]
            if kind.serial:
                c.nullable, c.default = False, None
            if c.kind not in INT_KINDS or c.generated:
                c.identity = None
            if kind.generated is None:
                c.generated = False
            c.virtual = c.virtual and c.generated and SERVER["virtual"]
            if c.identity is not None:
                c.nullable, c.default = False, None
            else:
                c.seq = 0
            if c.generated:
                c.default = None
            if c.default is not None and c.default >= len(kind.defaults):
                c.default = None
            if not kind.btree or c.virtual:  # no keys on VIRTUAL columns
                c.unique = c.pk = False

        def ok(cols: list[str], t: Tbl = t) -> bool:
            found = [t.col(n) for n in cols]
            return (
                bool(cols)
                and len(set(cols)) == len(cols)
                and all(c is not None and KINDS[c.kind].btree and not c.virtual for c in found)
            )

        def virtual(name: str | None, t: Tbl = t) -> bool:
            col = t.col(name) if name is not None else None
            return col is not None and col.virtual

        t.pk = t.pk if ok(t.pk) else []
        flagged = [c for c in t.cols if c.pk]
        for c in flagged[1:] if not t.pk else flagged:
            c.pk = False
        t.uniques = _dedupe([u for u in t.uniques if ok(u)])
        t.checks = _dedupe([n for n in t.checks if t.col(n) is not None])
        indexes: list[Idx] = []
        for i in t.indexes:
            if i.where is not None and (t.col(i.where) is None or virtual(i.where)):
                i.where = None
            expr_col = t.col(i.expr) if i.expr is not None else None
            if i.expr is not None and (
                expr_col is None or not KINDS[expr_col.kind].text or expr_col.virtual
            ):
                continue
            if (i.expr is None and not ok(i.cols)) or i.name in [j.name for j in indexes]:
                continue
            # Operator classes only on text columns (named default or a pattern one).
            kinds = [_kind(t, n) for n in i.cols] if i.expr is None else []
            ops = (i.ops + [None] * len(kinds))[: len(kinds)]
            i.ops = [op if k in STR else None for op, k in zip(ops, kinds, strict=True)]
            if not any(i.ops):
                i.ops = []
            indexes.append(i)
        t.indexes = indexes
    for t in spec.tables:
        fks: list[Fk] = []
        for fk in t.fks:
            target = spec.table(fk.ref)
            if target is None or fk.ref_cols not in target.key_lists():
                continue
            local = [t.col(n) for n in fk.cols]
            remote = [target.col(n) for n in fk.ref_cols]
            if len(set(fk.cols)) != len(fk.cols) or len(local) != len(remote):
                continue
            if any(a is not None and a.generated for a in local):
                continue  # a generated column cannot take ON UPDATE / ON DELETE actions
            if any(b is not None and b.virtual for b in remote):
                continue
            if any(
                a is None or b is None or base_kind(a.kind) != base_kind(b.kind)
                for a, b in zip(local, remote, strict=True)
            ):
                continue
            if fk.cols in [f.cols for f in fks]:  # same constraint name
                continue
            fk.inline = fk.inline and len(fk.cols) == 1
            fks.append(fk)
        t.fks = fks
    sanitize_objects(spec)
    return spec


def sanitize_objects(spec: Spec) -> None:
    spec.extensions = _dedupe(spec.extensions)
    for t in spec.tables:
        policies: list[Pol] = []
        for pol in t.policies:
            if pol.using is not None and (t.col(pol.using) is None or pol.command == "insert"):
                pol.using = None
            if pol.check is not None and (
                t.col(pol.check) is None or pol.command in ("select", "delete")
            ):
                pol.check = None
            if pol.name not in [q.name for q in policies]:
                policies.append(pol)
        t.policies = policies
        triggers: list[Trg] = []
        for trg in t.triggers:
            trg.events = [e for e in TRIGGER_EVENTS if e in trg.events] or ["insert"]
            trg.statement = trg.statement or "truncate" in trg.events
            col = t.col(trg.when) if trg.when is not None else None
            if (
                col is None
                or trg.statement
                or not set(trg.events) <= {"insert", "update"}  # NEW is there
                or (col.generated and not trg.after)  # not yet computed in BEFORE
                or col.virtual
            ):
                trg.when = None
            trg.update_of = (
                _dedupe([n for n in trg.update_of if (c := t.col(n)) and not c.generated])
                if "update" in trg.events
                else []
            )
            # Transition tables: AFTER triggers with one event and no column list.
            trg.transition = (
                trg.transition
                and trg.after
                and len(trg.events) == 1
                and trg.events[0] != "truncate"
                and not trg.update_of
            )
            if trg.name not in [u.name for u in triggers]:
                triggers.append(trg)
        t.triggers = triggers
    views: list[Vw] = []
    for v in spec.views:
        t = spec.table(v.table)
        if t is None or v.name in [w.name for w in views]:
            continue
        v.cols = _dedupe([c for c in v.cols if t.col(c) is not None])
        if not v.cols:
            continue
        if v.where is not None and t.col(v.where) is None:
            v.where = None
        v.indexed = v.indexed and v.materialized and KINDS[_kind(t, v.cols[0])].btree
        v.calc = v.calc and spec.calc is not None
        v.instead = v.instead and not v.materialized
        views.append(v)
    spec.views = views


# -- spec -> Table classes -------------------------------------------------------------


def build_tables(spec: Spec) -> list[type[Table]]:
    return build_schema(spec)[0]


def build_schema(spec: Spec) -> tuple[list[type[Table]], list[SchemaObject]]:
    """Tables and views, and the extensions, functions and triggers of ``spec``."""
    members = [(f"V{i}", v) for i, v in enumerate(spec.enum_values)]
    mood = cast(type[enum.Enum], enum.Enum("FzMood", members))
    classes: dict[str, type[Table]] = {}
    a = spec.attr  # the attribute of a db column name

    def column(c: Col) -> ColumnBase:
        kind = KINDS[c.kind]
        col = kind.make(mood)
        col.nullable = c.nullable and not c.pk and not kind.serial
        col.is_primary_key = c.pk
        col.is_unique = c.unique
        if col.enum is not None:
            col.enum_schema = spec.enum_schema
        if c.identity is not None:
            start, increment = (None, None) if c.pk else (10, 3)
            col.identity = Identity(c.identity, start, increment, *SEQ_OPTIONS[c.seq])
            col.generated = True
        if c.generated:
            col.computed = Computed(sql(cast(str, kind.generated)), stored=not c.virtual)
            col.generated = True
        if c.default is not None:
            value = kind.defaults[c.default]
            if c.kind == "enum":
                value = mood(value)
            elif c.kind == "enum[]" and isinstance(value, list):
                value = [mood(v) for v in cast(list[str], value)]
            elif c.kind == "enum[]":  # the type's name, with its schema
                value = sql(f"'{{}}'::{spec.enum_type}[]")
            col.default_value = value
        return col

    def ref_target(fk: Fk) -> Callable[[], list[ColumnBase]]:
        return lambda: [classes[fk.ref].__table__.columns[a(n)] for n in fk.ref_cols]

    for t in spec.tables:
        attrs: dict[str, object] = {}
        for c in t.cols:
            attrs[a(c.name)] = column(c)
        for fk in t.fks:
            if fk.inline:
                ref = ref_target(fk)
                column_of = cast(ColumnBase, attrs[a(fk.cols[0])])
                column_of.reference = Reference(
                    lambda ref=ref: ref()[0], fk.on_delete, fk.on_update
                )

        def config(cls: type[Table], t: Tbl = t) -> list[Constraint]:
            table_cols = cls.__table__.columns
            cols = {n: table_cols[a(n)] for n in [c.name for c in t.cols]}
            out: list[Constraint] = []
            if t.pk:
                out.append(primary_key(*(cols[n] for n in t.pk)))
            out += [unique(*(cols[n] for n in u)) for u in t.uniques]
            out += [
                check(f"{t.name[:40]}_ck_{n}", sql(KINDS[_kind(t, n)].check.format(c=quote(n))))
                for n in t.checks
            ]
            out += [rls(enabled=t.rls in (1, 2), force=t.rls >= 2)] if t.rls else []
            out += [
                policy(
                    p.name,
                    for_=p.command,
                    to=[FZ_ROLE] if p.role else ["public"],
                    using=sql(f"{quote(p.using)} IS NOT NULL") if p.using else None,
                    with_check=sql(f"{quote(p.check)} IS NOT NULL") if p.check else None,
                    as_="permissive" if p.permissive else "restrictive",
                )
                for p in t.policies
            ]
            for i in t.indexes:
                ix = unique_index(i.name) if i.unique else index(i.name)
                if i.expr is not None:
                    ix = ix.on(sql(f"lower({quote(i.expr)})"))
                else:
                    ix = ix.on(*(cols[n] for n in i.cols))
                    if i.ops:
                        ix = ix.using("btree", i.ops)
                if i.where is not None:
                    ix = ix.where(sql(f"{quote(i.where)} IS NOT NULL"))
                out.append(ix)
            out += [
                foreign_key(
                    [cols[n] for n in fk.cols],
                    ref_target(fk),
                    on_delete=fk.on_delete,
                    on_update=fk.on_update,
                )
                for fk in t.fks
                if not fk.inline
            ]
            out += [grant(g.privileges, to=["public"] if g.public else [FZ_ROLE]) for g in t.grants]
            return out

        attrs["config"] = classmethod(config)
        cls = types.new_class(
            "T_" + uuid.uuid4().hex[:8],
            (Table,),
            {"name": t.name, "schema": t.schema},
            lambda ns, attrs=attrs: ns.update(attrs),
        )
        classes[t.name] = cast(type[Table], cls)

    objects: list[SchemaObject] = [extension(e) for e in spec.extensions]
    trigger_fn = function(
        "fz_trg", returns="trigger", body=f"BEGIN PERFORM {spec.fn_variant}; RETURN NEW; END"
    )
    calc_args = ["a integer", "b integer"][: spec.calc or 0]
    calc_fn = function(
        "fz_calc",
        returns="integer",
        language="sql",
        args=", ".join(calc_args),
        body="SELECT " + " + ".join(arg.split()[0] for arg in calc_args),
        volatility="immutable",
    )
    calc_fn = dataclasses.replace(calc_fn, **CALC_OPTIONS[spec.calc_options])
    if spec.calc is not None:
        objects.append(calc_fn)
    for t in spec.tables:
        for trg in t.triggers:
            when = sql(f"NEW.{quote(trg.when)} IS NOT NULL") if trg.when else None
            objects.append(
                trigger(
                    trg.name,
                    on=classes[t.name],
                    execute=trigger_fn,
                    timing="after" if trg.after else "before",
                    events=trg.events,
                    for_each="statement" if trg.statement else "row",
                    when=when,
                    update_of=[classes[t.name].__table__.columns[a(n)] for n in trg.update_of],
                    old_table="gone" if trg.transition and trg.events[0] != "insert" else None,
                    new_table="came" if trg.transition and trg.events[0] != "delete" else None,
                )
            )

    views: list[type[Table]] = []
    for v in spec.views:
        source = classes[v.table]

        def query(
            cls: type[View],
            db: QueryBuilder,
            v: Vw = v,
            source: type[Table] = source,
            calc_args: int = spec.calc or 0,
        ) -> ExprBase | Select[dict[str, object]]:
            cols = source.__table__.columns
            fields: dict[str, ExprBase] = {c: cols[a(c)] for c in v.cols}
            if v.calc:
                fields["calc"] = sql(f"fz_calc({', '.join(['1'] * calc_args)})")
            select = db.select(fields).from_(source)
            return select.where(is_not_null(cols[a(v.where)])) if v.where else select

        def view_config(cls: type[Table], v: Vw = v) -> list[Constraint]:
            first = cls.__table__.columns[a(v.cols[0])]
            out: list[Constraint] = [index(f"{v.name}_ix").on(first)] if v.indexed else []
            out += [grant(g.privileges, to=["public"] if g.public else [FZ_ROLE]) for g in v.grants]
            return out

        names = [*v.cols, *(["calc"] if v.calc else [])]
        view_attrs: dict[str, object] = {a(c): text() for c in names}
        view_attrs["query"] = classmethod(query)
        view_attrs["config"] = classmethod(view_config)
        views.append(
            types.new_class(
                "V_" + uuid.uuid4().hex[:8],
                (View,),
                {"name": v.name, "schema": v.schema, "materialized": v.materialized},
                lambda ns, view_attrs=view_attrs: ns.update(view_attrs),
            )
        )
    for v, view in zip(spec.views, views, strict=True):
        if v.instead:
            objects.append(
                trigger(
                    f"{v.name[:20]}_ins",
                    on=view,
                    execute=trigger_fn,
                    timing="instead of",
                    events=["insert"],
                )
            )
    return [*classes.values(), *views], objects


def _kind(t: Tbl, name: str) -> str:
    col = t.col(name)
    assert col is not None
    return col.kind


def snapshot_of(spec: Spec) -> Snapshot:
    tables, objects = build_schema(spec)
    return build_snapshot(tables, dialect, spec.casing, objects=objects)


# -- strategies ------------------------------------------------------------------------


def col_strategy(name: str, kinds: list[str] = KIND_NAMES) -> st.SearchStrategy[Col]:
    def build(
        kind: str,
        nullable: bool,
        default: int | None,
        uq: bool,
        identity: bool | None,
        generated: bool,
        virtual: bool,
        seq: int,
    ) -> Col:
        return Col(
            name,
            kind,
            nullable,
            default,
            uq,
            identity=identity,
            generated=generated,
            virtual=virtual,
            seq=seq,
        )

    return st.builds(
        build,
        st.sampled_from(kinds),
        st.booleans(),
        st.none() | st.integers(0, 2),
        st.integers(0, 4).map(lambda n: n == 0),
        st.sampled_from([None, None, True, False]),
        st.integers(0, 4).map(lambda n: n == 0),
        st.booleans(),
        st.integers(0, len(SEQ_OPTIONS) - 1),
    )


OPCLASSES = [None, "text_ops", "text_pattern_ops"]  # text_ops is the default one


@st.composite
def index_strategy(draw: st.DrawFn, t: Tbl, name: str) -> Idx:
    names = [c.name for c in t.cols]
    cols = draw(st.lists(st.sampled_from(names), min_size=1, max_size=3, unique=True))
    where = draw(st.none() | st.sampled_from(names))
    expr = draw(st.none() | st.sampled_from(names)) if draw(st.booleans()) else None
    ops = draw(st.lists(st.sampled_from(OPCLASSES), max_size=3))
    return Idx(name, cols, draw(st.booleans()), where, expr, ops)


@st.composite
def fk_strategy(draw: st.DrawFn, t: Tbl, spec: Spec) -> Fk | None:
    """A foreign key from ``t`` to a key of some table, adding matching columns to ``t``."""
    target = draw(st.sampled_from(spec.tables))
    keys = target.key_lists()
    if not keys:
        return None
    ref_cols = draw(st.sampled_from(keys))
    cols: list[str] = []
    for j, ref in enumerate(ref_cols):
        ref_col = target.col(ref)
        if ref_col is None:
            return None
        name = f"fk{len(t.fks)}_{j}_{uuid.uuid4().hex[:4]}"
        t.cols.append(Col(name, base_kind(ref_col.kind), draw(st.booleans())))
        cols.append(name)
    return Fk(
        cols,
        target.name,
        list(ref_cols),
        draw(st.sampled_from(ACTIONS)),
        draw(st.sampled_from(ACTIONS)),
        draw(st.booleans()),
    )


@st.composite
def table_strategy(draw: st.DrawFn, name: str) -> Tbl:
    n = draw(st.integers(1, 6))
    cols = [draw(col_strategy(f"c_{'abcdef'[i]}")) for i in range(n)]  # cA under snake_case
    t = Tbl(name, draw(st.sampled_from([None, None, "fz_s"])), cols)
    names = [c.name for c in cols]
    match draw(st.integers(0, 3)):
        case 0:
            pass
        case 1:
            cols[draw(st.integers(0, n - 1))].pk = True
        case _:
            t.pk = draw(st.lists(st.sampled_from(names), min_size=1, max_size=3, unique=True))
    t.uniques = draw(
        st.lists(st.lists(st.sampled_from(names), min_size=1, max_size=3, unique=True), max_size=2)
    )
    t.checks = draw(st.lists(st.sampled_from(names), max_size=2))
    t.indexes = [
        draw(index_strategy(t, f"{name[:40]}_ix{i}")) for i in range(draw(st.integers(0, 3)))
    ]
    t.grants = draw(st.lists(grant_strategy(), max_size=2))
    return t


def grant_strategy() -> st.SearchStrategy[Grt]:
    return st.builds(
        Grt, st.lists(st.sampled_from(PRIVILEGES), min_size=1, max_size=2), st.booleans()
    )


def enum_values(draw: st.DrawFn, values: list[str]) -> list[str]:
    """``values`` with some new values inserted anywhere (existing order kept)."""
    out = list(values)
    for extra in draw(st.lists(st.sampled_from(ENUM_EXTRAS), unique=True, max_size=2)):
        if extra not in out:
            out.insert(draw(st.integers(0, len(out))), extra)
    return out


@st.composite
def schemas(draw: st.DrawFn) -> Spec:
    names = draw(st.lists(st.sampled_from(TABLE_NAMES), min_size=1, max_size=4, unique=True))
    spec = Spec(enum_values(draw, ["happy", "sad"]), [])
    spec.enum_schema = draw(st.sampled_from([None, None, "fz_s"]))
    spec.snake = draw(st.booleans())
    for name in names:
        spec.tables.append(draw(table_strategy(name)))
    sanitize(spec)
    for t in spec.tables:
        for _ in range(draw(st.integers(0, 2))):
            fk = draw(fk_strategy(t, spec))
            if fk is not None:
                t.fks.append(fk)
    if draw(st.booleans()):  # tables only, half of the time
        draw_objects(draw, spec)
    return sanitize(spec)


def draw_objects(draw: st.DrawFn, spec: Spec) -> None:
    spec.extensions = draw(st.lists(st.sampled_from(EXTENSIONS), unique=True, max_size=2))
    spec.fn_variant = draw(st.integers(0, 2))
    spec.calc = draw(st.sampled_from([None, 1, 2]))
    spec.calc_options = draw(st.integers(0, len(CALC_OPTIONS) - 1))
    for t in spec.tables:
        t.rls = draw(st.sampled_from([0, 0, 1, 2, 3]))
        t.policies = [draw(policy_strategy(t, f"p{i}")) for i in range(draw(st.integers(0, 2)))]
        t.triggers = [draw(trigger_strategy(t, f"tg{i}")) for i in range(draw(st.integers(0, 2)))]
    for name in draw(st.lists(st.sampled_from(VIEW_NAMES), unique=True, max_size=2)):
        spec.views.append(draw(view_strategy(spec, name)))


def _col_or_none(draw: st.DrawFn, t: Tbl) -> str | None:
    return draw(st.none() | st.sampled_from([c.name for c in t.cols]))


@st.composite
def policy_strategy(draw: st.DrawFn, t: Tbl, name: str) -> Pol:
    return Pol(
        name,
        draw(st.sampled_from(COMMANDS)),
        _col_or_none(draw, t),
        _col_or_none(draw, t),
        draw(st.booleans()),
        draw(st.booleans()),
    )


@st.composite
def trigger_strategy(draw: st.DrawFn, t: Tbl, name: str) -> Trg:
    events = draw(st.lists(st.sampled_from(TRIGGER_EVENTS), min_size=1, max_size=3, unique=True))
    names = [c.name for c in t.cols]
    return Trg(
        name,
        events,
        draw(st.booleans()),
        draw(st.booleans()),
        _col_or_none(draw, t),
        draw(st.lists(st.sampled_from(names), max_size=2)),
        draw(st.booleans()),
    )


@st.composite
def view_strategy(draw: st.DrawFn, spec: Spec, name: str) -> Vw:
    t = draw(st.sampled_from(spec.tables))
    names = [c.name for c in t.cols]
    return Vw(
        name,
        draw(st.sampled_from([None, None, "fz_s"])),
        t.name,
        draw(st.lists(st.sampled_from(names), min_size=1, max_size=3, unique=True)),
        _col_or_none(draw, t),
        draw(st.booleans()),
        draw(st.booleans()),
        draw(st.booleans()),
        draw(st.lists(grant_strategy(), max_size=2)),
        draw(st.booleans()),
    )


# Mutation ops (see the match below); column changes are listed more than once.
OPS = [*range(18), 2, 2, 2, 3, 4, 4, 14, *range(18, 38), 30, 31, 31, 33]
# Names a table is renamed to (never one a dropped table had: that would be a rewrite).
RENAMED = ["epsilon", "Renamed T", "t_" + "renamed" * 8]


def rename_table(spec: Spec, t: Tbl, new: str) -> None:
    for other in spec.tables:
        for fk in other.fks:
            if fk.ref == t.name:
                fk.ref = new
    for v in spec.views:
        if v.table == t.name:
            v.table = new
    t.name = new


def rename_column(spec: Spec, t: Tbl, old: str, new: str) -> None:
    def rn(names: list[str]) -> list[str]:
        return [new if n == old else n for n in names]

    def rn1(name: str | None) -> str | None:
        return new if name == old else name

    col = t.col(old)
    assert col is not None
    col.name = new
    t.pk, t.uniques, t.checks = rn(t.pk), [rn(u) for u in t.uniques], rn(t.checks)
    for i in t.indexes:
        i.cols, i.where, i.expr = rn(i.cols), rn1(i.where), rn1(i.expr)
    for fk in t.fks:
        fk.cols = rn(fk.cols)
    for other in spec.tables:
        for fk in other.fks:
            if fk.ref == t.name:
                fk.ref_cols = rn(fk.ref_cols)
    for v in spec.views:
        if v.table == t.name:
            v.cols, v.where = rn(v.cols), rn1(v.where)
    for p in t.policies:
        p.using, p.check = rn1(p.using), rn1(p.check)
    for trg in t.triggers:
        trg.when, trg.update_of = rn1(trg.when), rn(trg.update_of)


def original_kind(s1: Spec, t: Tbl, c: Col) -> str:
    before = s1.by_uid(t.uid)
    col = before.by_uid(c.uid) if before is not None else None
    return col.kind if col is not None else c.kind


@st.composite
def mutated(draw: st.DrawFn, s1: Spec) -> Spec:
    spec = copy.deepcopy(s1)
    for _ in range(draw(st.sampled_from([1, 2, 3, 4, 5, 6]))):
        op = draw(st.sampled_from(OPS))
        if op == 15 or not spec.tables:
            # Never re-create a dropped table: under its old name it would be a rewrite.
            free = [n for n in TABLE_NAMES if spec.table(n) is None and s1.table(n) is None]
            if free:
                spec.tables.append(draw(table_strategy(draw(st.sampled_from(free)))))
            sanitize(spec)
            continue
        t = draw(st.sampled_from(spec.tables))
        c = draw(st.sampled_from(t.cols))
        names = [col.name for col in t.cols]
        match op:
            case 0:
                t.cols.append(draw(col_strategy(f"n{uuid.uuid4().hex[:4]}")))
            case 1 if len(t.cols) > 1:
                t.cols.remove(c)
            case 2 if not c.generated and (targets := cast_targets(original_kind(s1, t, c))):
                c.kind = draw(st.sampled_from(targets))  # one cast from s1, not a chain
                c.default = draw(st.none() | st.integers(0, 2))
            case 3:
                c.nullable = not c.nullable
            case 4:
                c.default = draw(
                    st.none() | st.integers(0, 2).filter(lambda d, old=c.default: d != old)
                )
            case 5:
                c.unique = not c.unique
            case 6:
                u = draw(st.lists(st.sampled_from(names), min_size=1, max_size=3, unique=True))
                t.uniques = [x for x in t.uniques if x != u] if u in t.uniques else [*t.uniques, u]
            case 7:
                t.indexes.append(draw(index_strategy(t, f"{t.name[:40]}_ix{uuid.uuid4().hex[:4]}")))
            case 8 if t.indexes:
                t.indexes.remove(draw(st.sampled_from(t.indexes)))
            case 9:
                t.checks = (
                    [n for n in t.checks if n != c.name]
                    if c.name in t.checks
                    else [
                        *t.checks,
                        c.name,
                    ]
                )
            case 10:
                fk = draw(fk_strategy(t, spec))
                if fk is not None:
                    t.fks.append(fk)
            case 11 if t.fks:
                t.fks.remove(draw(st.sampled_from(t.fks)))
            case 12:
                for col in t.cols:
                    col.pk = False
                t.pk = draw(st.lists(st.sampled_from(names), max_size=2, unique=True))
            case 13 if len(spec.tables) > 1:
                spec.tables.remove(t)
            case 14:
                spec.enum_values = enum_values(draw, spec.enum_values)
            case 16:
                c.identity = draw(st.sampled_from([None, True, False]))
            case 17:
                c.generated = False  # DROP EXPRESSION; the reverse needs a custom migration
            case 18:
                ext = draw(st.sampled_from(EXTENSIONS))
                spec.extensions = [e for e in spec.extensions if e != ext] + (
                    [] if ext in spec.extensions else [ext]
                )
            case 19:
                free = [n for n in VIEW_NAMES if n not in [v.name for v in spec.views]]
                if free:
                    spec.views.append(draw(view_strategy(spec, draw(st.sampled_from(free)))))
            case 20 if spec.views:
                spec.views.remove(draw(st.sampled_from(spec.views)))
            case 21 if spec.views:  # a new query (or kind) under the same name
                old_view = draw(st.sampled_from(spec.views))
                spec.views[spec.views.index(old_view)] = draw(view_strategy(spec, old_view.name))
            case 22:
                t.rls = draw(st.sampled_from([0, 1, 2, 3]))
            case 23:
                t.policies.append(draw(policy_strategy(t, f"p{uuid.uuid4().hex[:4]}")))
            case 24 if t.policies:
                pol = draw(st.sampled_from(t.policies))
                if draw(st.booleans()):
                    t.policies.remove(pol)
                else:  # change it in place: roles, expressions, maybe its command
                    changed = draw(policy_strategy(t, pol.name))
                    t.policies[t.policies.index(pol)] = changed
            case 25:
                t.triggers.append(draw(trigger_strategy(t, f"tg{uuid.uuid4().hex[:4]}")))
            case 26 if t.triggers:
                trg = draw(st.sampled_from(t.triggers))
                if draw(st.booleans()):
                    t.triggers.remove(trg)
                else:
                    t.triggers[t.triggers.index(trg)] = draw(trigger_strategy(t, trg.name))
            case 27:
                spec.fn_variant = (spec.fn_variant + 1) % 3
            case 28:  # add, drop, or change the arguments of fz_calc
                spec.calc = draw(st.sampled_from([None, 1, 2]))
            case 29 if spec.views:
                v = draw(st.sampled_from(spec.views))
                v.where = None if v.where else c.name if v.table == t.name else None
            case 30:  # renamed in its schema
                free = [n for n in RENAMED if spec.table(n) is None]
                if free:
                    rename_table(spec, t, draw(st.sampled_from(free)))
            case 31:
                rename_column(spec, t, c.name, f"r{uuid.uuid4().hex[:4]}")
            case 32:  # moved to the other schema
                t.schema = None if t.schema else "fz_s"
            case 33 if not c.generated and c.identity is None:
                c.generated, c.virtual = True, draw(st.booleans())  # dropped and added again
            case 33 if c.generated:
                c.virtual = not c.virtual
            case 34:
                t.grants = draw(st.lists(grant_strategy(), max_size=2))
            case 35 if spec.views:
                v = draw(st.sampled_from(spec.views))
                v.grants = draw(st.lists(grant_strategy(), max_size=2))
                v.instead = draw(st.booleans())
            case 36:
                c.seq = draw(st.integers(0, len(SEQ_OPTIONS) - 1))
                spec.calc_options = draw(st.integers(0, len(CALC_OPTIONS) - 1))
            case 37 if t.indexes:
                draw(st.sampled_from(t.indexes)).ops = draw(
                    st.lists(st.sampled_from(OPCLASSES), max_size=3)
                )
            case _:
                pass
        sanitize(spec)
    return spec


@st.composite
def evolutions(draw: st.DrawFn) -> tuple[Spec, Spec]:
    s1 = draw(schemas())
    return s1, draw(mutated(s1))


# -- database helpers ------------------------------------------------------------------

RESET = f"""
DROP SCHEMA IF EXISTS fz_s CASCADE;
DO $$ DECLARE r record; BEGIN
  FOR r IN SELECT tablename FROM pg_tables WHERE schemaname = 'public' LOOP
    EXECUTE format('DROP TABLE IF EXISTS public.%I CASCADE', r.tablename);
  END LOOP;
  FOR r IN SELECT t.typname FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace
           WHERE n.nspname = 'public' AND t.typtype = 'e' LOOP
    EXECUTE format('DROP TYPE IF EXISTS public.%I CASCADE', r.typname);
  END LOOP;
  FOR r IN SELECT p.oid::regprocedure AS fn FROM pg_proc p
           JOIN pg_namespace n ON n.oid = p.pronamespace
           WHERE n.nspname = 'public' AND p.prokind = 'f' AND NOT EXISTS (
             SELECT 1 FROM pg_depend d WHERE d.objid = p.oid AND d.deptype = 'e') LOOP
    EXECUTE format('DROP FUNCTION IF EXISTS %s CASCADE', r.fn);
  END LOOP;
END $$;
DROP TYPE IF EXISTS {ENUM} CASCADE;
DROP EXTENSION IF EXISTS {", ".join(EXTENSIONS)} CASCADE;
"""


async def reset(driver: PostgresDriver) -> None:
    async with driver.acquire() as conn:
        await conn.execute(RESET, [])


async def apply(driver: PostgresDriver, statements: list[Statement]) -> None:
    async with driver.acquire() as conn:
        await apply_statements(conn, statements)


async def introspect(driver: PostgresDriver) -> Snapshot:
    async with driver.acquire() as conn:
        return await dialect.introspect(conn, SCHEMAS)


def migration(old: Snapshot, new: Snapshot) -> list[Statement]:
    return dialect.render(diff(old, new))


async def normalized(driver: PostgresDriver, snapshot: Snapshot) -> Snapshot:
    """View queries and expressions as the database prints them (what push compares)."""
    async with driver.acquire() as conn:
        return await dialect.normalize_snapshot(conn, snapshot)


def assert_no_drift(live: Snapshot, expected: Snapshot) -> None:
    note(f"expected: {expected['tables']}\nlive: {live['tables']}")
    for part in ("views", "functions", "extensions"):
        note(f"expected {part}: {expected.get(part)}\nlive {part}: {live.get(part)}")
    remaining = diff(live, expected, compare_expressions=False)
    assert remaining == [], [s.describe() for s in remaining]
    # compare_expressions=False leaves undeclared extensions alone; they must be gone too.
    installed = {e for e in live.get("extensions", {}) if e in EXTENSIONS}
    assert installed == set(expected.get("extensions", {})) - {"vector"}
    for k, table in expected["tables"].items():
        live_indexes = live["tables"][k]["indexes"]
        for name, ix in table["indexes"].items():  # predicates are not compared above
            assert (live_indexes[name]["where"] is None) == (ix["where"] is None), name


def sqlstate(exc: BaseException) -> str:
    original = exc.original if isinstance(exc, DatabaseError) else exc
    return original.sqlstate if isinstance(original, asyncpg.PostgresError) else ""


def is_data_error(exc: BaseException) -> bool:
    """Rejected because of the rows in the table (class 22 data / 23 integrity)."""
    return sqlstate(exc)[:2] in ("22", "23")


async def insert_rows(driver: PostgresDriver, spec: Spec) -> None:
    async with driver.acquire() as conn:
        for _ in range(2):  # a second pass fills tables whose references were empty
            for t in spec.tables:
                cols = [
                    c
                    for c in t.cols
                    if not KINDS[c.kind].serial and c.identity is None and not c.generated
                ]
                for i in range(3):
                    names = ", ".join(quote(c.name) for c in cols)
                    values = ", ".join(KINDS[c.kind].samples[i] for c in cols)
                    target = qualified(t.schema or "public", t.name)
                    stmt = (
                        f"INSERT INTO {target} ({names}) VALUES ({values})"
                        if cols
                        else f"INSERT INTO {target} DEFAULT VALUES"
                    )
                    # Constraints may reject a row; some rows are enough.
                    with contextlib.suppress(DatabaseError, asyncpg.PostgresError):
                        await conn.execute(stmt, [])


type Rows = dict[str, list[tuple[str | None, ...]]]


async def kept_data(driver: PostgresDriver, s1: Spec, s2: Spec, *, after: bool = False) -> Rows:
    """Text of every column that keeps its kind from s1 to s2 (renamed or not), per table.
    ``after``: read the s2 names (the migration ran)."""
    out: Rows = {}
    async with driver.acquire() as conn:
        for t in s1.tables:
            t2 = s2.by_uid(t.uid)
            if t2 is None or t2.schema != t.schema:
                continue
            pairs = [
                (c, a)
                for c in t.cols
                if (a := t2.by_uid(c.uid)) and a.kind == c.kind and a.generated == c.generated
            ]
            if not pairs:
                continue
            table = t2 if after else t
            select = ", ".join(f"{quote((a if after else c).name)}::text" for c, a in pairs)
            rows = await conn.fetch(
                f"SELECT {select} FROM {qualified(table.schema or 'public', table.name)}", []
            )
            values = [tuple(_opt_str(v) for v in row) for row in rows]
            out[t.uid] = sorted(values, key=repr)
    return out


def resolver(
    s1: Spec, s2: Spec
) -> Callable[[RenameKind, str, list[str], list[str]], dict[str, str]]:
    """Answers renames and moves as the specs made them (tables and columns keep a uid)."""

    def find(spec: Spec, schema: str, name: str) -> Tbl | None:
        return next(
            (t for t in spec.tables if (t.schema or "public") == schema and t.name == name), None
        )

    def resolve(
        kind: RenameKind, scope: str, removed: list[str], added: list[str]
    ) -> dict[str, str]:
        out: dict[str, str] = {}
        for old in removed:
            if kind == "table":
                schema, name = old.split(".", 1) if scope == "*" else (scope, old)
                t1 = find(s1, schema, name)
                t2 = s2.by_uid(t1.uid) if t1 is not None else None
                if t2 is not None:
                    new = f"{t2.schema or 'public'}.{t2.name}" if scope == "*" else t2.name
                    if new in added:
                        out[old] = new
                continue
            schema, name = scope.split(".", 1)
            t2 = find(s2, schema, name)
            t1 = s1.by_uid(t2.uid) if t2 is not None else None
            c1 = t1.col(old) if t1 is not None else None
            c2 = t2.by_uid(c1.uid) if t2 is not None and c1 is not None else None
            if c2 is not None and c2.name in added:
                out[old] = c2.name
        return out

    return resolve


def _opt_str(value: object) -> str | None:
    return None if value is None else str(value)


# -- harness ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def runner() -> Generator[tuple[asyncio.Runner, PostgresDriver, str]]:
    url = os.environ.get("MIZZLE_TEST_URL")
    if not url:
        pytest.skip("set MIZZLE_TEST_URL to run integration tests")
    with asyncio.Runner() as loop:
        driver = loop.run(PostgresDriver.connect(url, pool_size=2))
        loop.run(reset(driver))
        loop.run(role(driver, f"DROP ROLE IF EXISTS {FZ_ROLE}; CREATE ROLE {FZ_ROLE}"))
        SERVER["virtual"] = loop.run(server_version(driver)) >= 180000
        yield loop, driver, url
        loop.run(reset(driver))
        loop.run(role(driver, f"DROP ROLE {FZ_ROLE}"))
        loop.run(driver.close())


async def server_version(driver: PostgresDriver) -> int:
    async with driver.acquire() as conn:
        [row] = await conn.fetch("SELECT current_setting('server_version_num')::int", [])
        return int(cast(int, row[0]))


async def role(driver: PostgresDriver, statement: str) -> None:
    async with driver.acquire() as conn:
        await conn.execute(statement, [])


EXAMPLES = int(os.environ.get("MIZZLE_FUZZ_EXAMPLES", "200"))
SETTINGS = settings(
    max_examples=EXAMPLES,
    deadline=None,
    suppress_health_check=[
        HealthCheck.function_scoped_fixture,
        HealthCheck.too_slow,
        HealthCheck.data_too_large,
    ],
)


@SETTINGS
@given(spec=schemas())
def test_create_has_no_drift(
    runner: tuple[asyncio.Runner, PostgresDriver, str], spec: Spec
) -> None:
    loop, driver, _ = runner
    snap = snapshot_of(spec)
    statements = migration(empty_snapshot("postgresql"), snap)
    note("\n".join(s.sql for s in statements))
    # Deterministic: the same schema built again renders the same SQL and diffs to nothing.
    again = snapshot_of(spec)
    assert migration(empty_snapshot("postgresql"), again) == statements
    assert diff(snap, again) == []

    async def scenario() -> None:
        await reset(driver)
        await apply(driver, statements)
        assert_no_drift(await introspect(driver), await normalized(driver, snap))

    loop.run(scenario())


@SETTINGS
@given(evolution=evolutions(), with_rows=st.booleans())
def test_evolve_has_no_drift(
    runner: tuple[asyncio.Runner, PostgresDriver, str],
    evolution: tuple[Spec, Spec],
    with_rows: bool,
) -> None:
    loop, driver, _ = runner
    s1, s2 = evolution
    snap1, snap2 = snapshot_of(s1), snapshot_of(s2)
    create = migration(empty_snapshot("postgresql"), snap1)
    steps = diff(snap1, snap2, resolver(s1, s2))
    statements = dialect.render(steps)
    note("\n".join(s.sql for s in statements))
    event(f"with rows: {with_rows}")
    event(f"casing: {s1.casing}")
    for step in steps:
        event(type(step).__name__)
    # deterministic
    assert dialect.render(diff(snap1, snapshot_of(s2), resolver(s1, s2))) == statements

    async def scenario() -> None:
        await reset(driver)
        await apply(driver, create)
        before: Rows | None = None
        if with_rows:
            await insert_rows(driver, s1)
            before = await kept_data(driver, s1, s2)
        try:
            await apply(driver, statements)
        except Exception as exc:
            # Rows can make a valid change fail (NOT NULL on NULLs, a cast of 'a' to
            # integer, ...). That is Postgres protecting data; retry on empty tables.
            if before is None or not is_data_error(exc):
                raise
            event("rows rejected the change; applied to empty tables")
            before = None
            await reset(driver)
            await apply(driver, create)
            await apply(driver, statements)
        live = await introspect(driver)
        expected = await normalized(driver, snap2)
        assert_no_drift(live, expected)
        if before is not None:
            event(f"kept rows checked: {sum(map(len, before.values())) > 0}")
            assert await kept_data(driver, s1, s2, after=True) == before
        # Running the migration again would find nothing to do.
        assert dialect.render(diff(live, expected, compare_expressions=False)) == []

    loop.run(scenario())


def _schema_module(spec_id: str) -> str:
    return (
        f"from {__name__} import CLI_SPECS, build_schema\n"
        f"tables, objects = build_schema(CLI_SPECS[{spec_id!r}])\n"
        "globals().update({t.__name__: t for t in tables})\n"
        "globals().update({f'object_{i}': o for i, o in enumerate(objects)})\n"
    )


CLI_SPECS: dict[str, Spec] = {}


def _write_schema(root: Path, spec: Spec) -> None:
    spec_id = uuid.uuid4().hex[:10]
    CLI_SPECS[spec_id] = spec
    module = f"fuzz_schema_{spec_id}"
    (root / f"{module}.py").write_text(_schema_module(spec_id))
    casing = 'casing = "snake_case"\n' if spec.snake else ""
    (root / "mizzle.toml").write_text(
        f'schema = "{module}"\nout = "migrations"\nschema_filter = {SCHEMAS!r}\n{casing}'
    )


@settings(SETTINGS, max_examples=max(5, EXAMPLES // 10))
@given(evolution=evolutions())
def test_cli_generate_and_migrate(
    runner: tuple[asyncio.Runner, PostgresDriver, str],
    evolution: tuple[Spec, Spec],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop, driver, url = runner
    s1, s2 = evolution
    loop.run(reset(driver))

    async def drop_history() -> None:
        async with driver.acquire() as conn:
            await conn.execute('DROP TABLE IF EXISTS "__mizzle_migrations"', [])

    loop.run(drop_history())
    monkeypatch.setenv("DATABASE_URL", url)
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        monkeypatch.chdir(root)
        flags = ["--no-renames", "--allow-data-loss"]
        _write_schema(root, s1)
        assert main(["generate", *flags]) == 0
        assert main(["migrate"]) == 0
        _write_schema(root, s2)
        assert main(["generate", *flags]) == 0
        assert main(["migrate"]) == 0
        assert main(["migrate"]) == 0
        assert main(["generate", *flags]) == 0  # nothing changed: no new migration
        migrations = MigrationsDir(root / "migrations", "postgresql").migrations()
        expected = 1 + (diff(snapshot_of(s1), snapshot_of(s2)) != [])
        assert len(migrations) == expected
        live = loop.run(introspect(driver))
        assert_no_drift(live, loop.run(normalized(driver, snapshot_of(s2))))
        assert main(["push", "--dry-run", "--no-renames"]) == 0  # nothing to push
        CLI_SPECS.clear()


# -- regressions for bugs these tests found ----------------------------------------------


def _steps(s1: Spec, s2: Spec) -> list[str]:
    return [s.describe() for s in diff(snapshot_of(s1), snapshot_of(s2))]


def test_regressions_found_by_fuzzing() -> None:
    def pair() -> Spec:
        parent = Tbl("alpha", None, [Col("id", "integer", pk=True, unique=True)])
        child = Tbl("beta", None, [Col("p", "integer")])
        child.fks = [Fk(["p"], "alpha", ["id"], on_update="no action")]
        return Spec(["happy", "sad"], [parent, child])

    snap = snapshot_of(pair())
    # "no action" is the default: introspection reports None, so the snapshot must too.
    assert snap["tables"]["public.beta"]["foreign_keys"]["beta_p_fkey"]["on_update"] is None
    # CREATE TABLE silently drops a UNIQUE that repeats the primary key.
    assert snap["tables"]["public.alpha"]["uniques"] == {}

    # A table-level primary key makes its columns NOT NULL in the database.
    composite = Spec([], [Tbl("gamma", None, [Col("a", "text"), Col("b", "integer")], ["a", "b"])])
    columns = snapshot_of(composite)["tables"]["public.gamma"]["columns"]
    assert columns["a"]["not_null"] and columns["b"]["not_null"]

    # List defaults of array columns are array literals as PostgreSQL prints them, not JSON.
    arrays = Spec([], [Tbl("delta", None, [Col("t", "text[]", default=2)])])
    column = snapshot_of(arrays)["tables"]["public.delta"]["columns"]["t"]
    assert column["default"] == """'{"x y","q\\"\\\\","","null",it''s}'"""

    # Changing the type of a column with a default: drop it first, set it again after.
    s1 = Spec([], [Tbl("delta", None, [Col("c", "text", default=2)])])
    s2 = Spec([], [Tbl("delta", None, [Col("c", "integer", default=1)])])
    assert _steps(s1, s2) == [
        "change default of delta.c",
        "change type of delta.c from text to integer",
        "change default of delta.c",
    ]

    # Dropping a table drops its foreign keys first: they would block changes to the
    # tables they reference (here: dropping alpha.id's key).
    only_parent = pair()
    only_parent.tables = [Tbl("alpha", None, [Col("id", "bigint", pk=True)])]
    assert _steps(pair(), only_parent)[0] == "drop foreign key beta_p_fkey"

    # A foreign key depends on the key it references: re-create it when that key changes.
    unique_only = pair()
    unique_only.tables[0].cols[0].pk = unique_only.tables[0].cols[0].nullable = False
    assert _steps(pair(), unique_only) == [
        "drop foreign key beta_p_fkey",
        "drop primary key alpha_pkey",
        "add unique alpha_id_key",
        "add foreign key beta_p_fkey",
    ]

    # Several enum values added in front: the first is anchored on an existing value.
    moods = [Tbl("alpha", None, [Col("m", "enum")])]
    grown = Spec(["it's", "glad", "happy", "sad"], copy.deepcopy(moods))
    sqls = [
        s.sql for s in migration(snapshot_of(Spec(["happy", "sad"], moods)), snapshot_of(grown))
    ]
    assert sqls == [
        """ALTER TYPE "fz_mood" ADD VALUE 'it''s' BEFORE 'happy'""",
        """ALTER TYPE "fz_mood" ADD VALUE 'glad' AFTER 'it''s'""",
    ]

    # Long generated names keep their suffix, so a unique and a foreign key on the same
    # columns never truncate to the same name.
    long = TABLE_NAMES[-1]
    table = Tbl(long, None, [Col("id", "integer", pk=True), Col("parent_reference_id", "integer")])
    table.fks = [Fk(["parent_reference_id"], long, ["id"])]
    table.uniques = [["parent_reference_id"]]
    snap = snapshot_of(Spec([], [table]))["tables"][f"public.{long}"]
    [unique_name], [fk_name] = snap["uniques"], snap["foreign_keys"]
    assert unique_name.endswith("_key") and fk_name.endswith("_fkey"), (unique_name, fk_name)
    assert len(unique_name) == len(fk_name) == 63
