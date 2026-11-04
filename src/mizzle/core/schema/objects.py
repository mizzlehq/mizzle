"""Database objects besides tables: extensions, functions and triggers.

Assign them to module-level names in a schema module; the migration kit collects them
like ``Table`` classes::

    trgm = extension("pg_trgm")
    touch = function("touch", returns="trigger", body="BEGIN NEW.at = now(); RETURN NEW; END")
    users_touch = trigger("users_touch", on=Users, events=["update"], execute=touch)
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from mizzle.core.errors import SchemaError
from mizzle.core.schema.column import ColumnBase
from mizzle.core.schema.constraints import Role
from mizzle.core.schema.table import Table
from mizzle.core.sql.nodes import Condition

type Volatility = Literal["volatile", "stable", "immutable"]
type Parallel = Literal["unsafe", "restricted", "safe"]
type TriggerTiming = Literal["before", "after", "instead of"]
type TriggerEvent = Literal["insert", "update", "delete", "truncate"]

_EVENT_ORDER: tuple[TriggerEvent, ...] = ("insert", "delete", "update", "truncate")


@dataclass(frozen=True)
class Extension:
    name: str
    schema: str | None = None  # None: wherever the database puts it (or already has it)
    version: str | None = None  # None: the default version, and any installed one


@dataclass(frozen=True)
class Function:
    name: str
    returns: str
    body: str
    language: str = "plpgsql"
    args: str = ""  # "a integer, b text", as PostgreSQL prints it
    schema: str | None = None
    volatility: Volatility = "volatile"
    security_definer: bool = False
    strict: bool = False
    leakproof: bool = False
    parallel: Parallel = "unsafe"
    cost: float | None = None  # None: the language's default
    settings: tuple[tuple[str, str], ...] = ()  # SET name = value


@dataclass(frozen=True)
class Trigger:
    name: str
    on: type[Table]  # a table, or a view for INSTEAD OF triggers
    execute: Function
    timing: TriggerTiming = "before"
    events: tuple[TriggerEvent, ...] = ("insert", "update")
    for_each: Literal["row", "statement"] = "row"
    when: Condition | None = None
    update_of: tuple[ColumnBase, ...] = ()  # UPDATE OF these columns
    old_table: str | None = None  # REFERENCING OLD TABLE AS old_table
    new_table: str | None = None


@dataclass(frozen=True)
class Domain:
    name: str
    base_type: str  # as PostgreSQL prints it: integer, character varying(255), ...
    schema: str | None = None
    not_null: bool = False
    default: str | None = None  # an SQL expression
    checks: tuple[str, ...] = ()  # CHECK expressions; VALUE is the domain's value
    collation: str | None = None


type SchemaObject = Extension | Function | Trigger | Role | Domain


def extension(name: str, *, schema: str | None = None, version: str | None = None) -> Extension:
    """``CREATE EXTENSION IF NOT EXISTS``. Migrations create it before anything else."""
    return Extension(name, schema, version)


def function(
    name: str,
    *,
    returns: str,
    body: str,
    language: str = "plpgsql",
    args: str = "",
    schema: str | None = None,
    volatility: Volatility = "volatile",
    security_definer: bool = False,
    strict: bool = False,
    leakproof: bool = False,
    parallel: Parallel = "unsafe",
    cost: float | None = None,
    settings: Mapping[str, str] | None = None,
) -> Function:
    """``CREATE OR REPLACE FUNCTION name(args) RETURNS returns LANGUAGE language AS body``.

    ``body`` is kept verbatim. A ``language="sql"`` body that starts with ``RETURN`` or
    ``BEGIN ATOMIC`` is a SQL-standard body (written without quotes). ``args`` and
    ``returns`` use PostgreSQL's names (``integer``, not ``int``) so the database prints
    them back the same. Functions with the same name and different ``args`` are overloads.
    ``settings`` are ``SET`` options, values as SQL: ``{"search_path": "public, pg_temp"}``.
    """
    for setting in settings or {}:
        if not setting.replace("_", "").replace(".", "").isalnum():
            raise SchemaError(f"function {name!r}: invalid setting name {setting!r}")
    return Function(
        name,
        returns,
        body,
        language,
        args,
        schema,
        volatility,
        security_definer,
        strict,
        leakproof,
        parallel,
        cost,
        tuple(sorted((settings or {}).items())),
    )


def trigger(
    name: str,
    *,
    on: type[Table],
    execute: Function,
    timing: TriggerTiming = "before",
    events: Sequence[TriggerEvent] = ("insert", "update"),
    for_each: Literal["row", "statement"] = "row",
    when: Condition | None = None,
    update_of: Sequence[ColumnBase] = (),
    old_table: str | None = None,
    new_table: str | None = None,
) -> Trigger:
    """``CREATE TRIGGER name {timing} {events} ON table FOR EACH {for_each} [WHEN (...)]
    EXECUTE FUNCTION execute()``.

    ``update_of=[Users.email]``: ``UPDATE OF email``. ``old_table``/``new_table`` name
    transition tables (``REFERENCING NEW TABLE AS ...``; AFTER triggers). ``on`` may be a
    view, with ``timing="instead of"``."""
    if not events:
        raise SchemaError(f"trigger {name!r} needs at least one event")
    if update_of and "update" not in events:
        raise SchemaError(f"trigger {name!r}: update_of needs the update event")
    ordered: tuple[TriggerEvent, ...] = tuple(e for e in _EVENT_ORDER if e in events)
    return Trigger(
        name, on, execute, timing, ordered, for_each, when, tuple(update_of), old_table, new_table
    )


def domain(
    name: str,
    *,
    as_: str,
    schema: str | None = None,
    not_null: bool = False,
    default: str | None = None,
    check: str | None = None,
    checks: Sequence[str] = (),
    collation: str | None = None,
) -> Domain:
    """``CREATE DOMAIN name AS as_``. ``as_`` is the base SQL type; ``check``/``checks`` add
    CHECK constraints, where ``VALUE`` is the domain's value. A column uses the domain by
    name (see ``pg_domain``)."""
    return Domain(
        name,
        as_,
        schema,
        not_null,
        default,
        (*checks, *([check] if check is not None else [])),
        collation,
    )
