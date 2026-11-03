"""Database objects besides tables: extensions, functions and triggers.

Assign them to module-level names in a schema module; the migration kit collects them
like ``Table`` classes::

    trgm = extension("pg_trgm")
    touch = function("touch", returns="trigger", body="BEGIN NEW.at = now(); RETURN NEW; END")
    users_touch = trigger("users_touch", on=Users, events=["update"], execute=touch)
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from mizzle.core.errors import SchemaError
from mizzle.core.schema.constraints import Role
from mizzle.core.schema.table import Table
from mizzle.core.sql.nodes import Condition

type Volatility = Literal["volatile", "stable", "immutable"]
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


@dataclass(frozen=True)
class Trigger:
    name: str
    on: type[Table]
    execute: Function
    timing: TriggerTiming = "before"
    events: tuple[TriggerEvent, ...] = ("insert", "update")
    for_each: Literal["row", "statement"] = "row"
    when: Condition | None = None


type SchemaObject = Extension | Function | Trigger | Role


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
) -> Function:
    """``CREATE OR REPLACE FUNCTION name(args) RETURNS returns LANGUAGE language AS body``.

    ``body`` is kept verbatim. ``args`` and ``returns`` use PostgreSQL's names
    (``integer``, not ``int``) so the database prints them back the same."""
    return Function(name, returns, body, language, args, schema, volatility, security_definer)


def trigger(
    name: str,
    *,
    on: type[Table],
    execute: Function,
    timing: TriggerTiming = "before",
    events: Sequence[TriggerEvent] = ("insert", "update"),
    for_each: Literal["row", "statement"] = "row",
    when: Condition | None = None,
) -> Trigger:
    """``CREATE TRIGGER name {timing} {events} ON table FOR EACH {for_each} [WHEN (...)]
    EXECUTE FUNCTION execute()``."""
    if not events:
        raise SchemaError(f"trigger {name!r} needs at least one event")
    ordered: tuple[TriggerEvent, ...] = tuple(e for e in _EVENT_ORDER if e in events)
    return Trigger(name, on, execute, timing, ordered, for_each, when)
