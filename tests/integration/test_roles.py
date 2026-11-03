"""Roles in migrations: created when missing, options kept in sync, dropped only when a
previous snapshot managed them, and never touched when declared ``existing``."""

from collections.abc import AsyncGenerator
from typing import final, override

import pytest

from mizzle import Constraint, Table, policy, rls, role, sql
from mizzle.core.snapshot import empty_snapshot
from mizzle.core.steps import AlterRole, CreateRole, DropRole, Step
from mizzle.dialects.postgres import PostgresDialect
from mizzle.dialects.postgres.driver import PostgresDriver
from mizzle.kit.diff import diff
from mizzle.kit.snapshot import build_snapshot
from mizzle.pg import serial, text
from mizzle.supabase import AUTHENTICATED, auth_uid, crud_policy

dialect = PostgresDialect()
EDITOR = role("mizzle_test_editor", login=True)
READER = role("mizzle_test_reader")
GUEST = role("mizzle_test_guest", existing=True)  # made by the fixture, not by migrations


@final
class Notes(Table, name="role_notes"):
    id = serial().primary_key()
    owner = text()

    @classmethod
    @override
    def config(cls) -> list[Constraint]:
        return [
            rls(),
            policy("notes_editors", to=[EDITOR, GUEST], using=sql("true")),
            *crud_policy("notes_readers", to=[READER], read=True),
        ]


async def run(driver: PostgresDriver, steps: list[Step]) -> None:
    async with driver.acquire() as conn:
        for statement in dialect.render(steps):
            await conn.execute(statement.sql, [])


async def role_options(driver: PostgresDriver, name: str) -> tuple[bool, bool] | None:
    async with driver.acquire() as conn:
        rows = await conn.fetch_dicts(
            "SELECT rolcanlogin, rolcreatedb FROM pg_roles WHERE rolname = $1", [name]
        )
    return (bool(rows[0]["rolcanlogin"]), bool(rows[0]["rolcreatedb"])) if rows else None


@pytest.fixture
async def clean(driver: PostgresDriver) -> AsyncGenerator[None]:
    names = ("mizzle_test_editor", "mizzle_test_reader", "mizzle_test_guest")
    drop = "DROP TABLE IF EXISTS role_notes; " + "".join(
        f"DROP ROLE IF EXISTS {n}; " for n in names
    )
    async with driver.acquire() as conn:
        await conn.execute(drop, [])
        await conn.execute("CREATE ROLE mizzle_test_guest", [])
    yield
    async with driver.acquire() as conn:
        await conn.execute(drop, [])


async def test_roles_are_created_kept_in_sync_and_dropped(
    driver: PostgresDriver, clean: None
) -> None:
    desired = build_snapshot([Notes], dialect)
    assert sorted(desired.get("roles", {})) == ["mizzle_test_editor", "mizzle_test_reader"]
    steps = diff(empty_snapshot("postgresql"), desired)
    assert isinstance(steps[0], CreateRole)  # roles come first
    await run(driver, steps)
    assert await role_options(driver, "mizzle_test_editor") == (True, False)

    # No drift: the live database (which lists every role on the server) needs nothing.
    async with driver.acquire() as conn:
        live = await dialect.introspect(conn, ["public"], list(desired.get("roles", {})))
    remaining = [
        s
        for s in diff(live, desired, compare_expressions=False)
        if isinstance(s, CreateRole | AlterRole | DropRole)
    ]
    assert remaining == []

    # Changed options are applied; running "create" again is harmless (shared server).
    changed = build_snapshot([Notes], dialect)
    roles = changed.get("roles", {})
    roles["mizzle_test_editor"]["create_db"] = True
    await run(driver, diff(desired, changed))
    assert await role_options(driver, "mizzle_test_editor") == (True, True)
    await run(driver, [CreateRole(roles["mizzle_test_editor"])])

    # Dropping the table and its roles: policies go before the roles they name.
    steps = diff(changed, empty_snapshot("postgresql"))
    assert isinstance(steps[-1], DropRole)
    await run(driver, steps)
    assert await role_options(driver, "mizzle_test_editor") is None
    assert await role_options(driver, "mizzle_test_guest") is not None  # existing: kept


async def test_unmanaged_roles_are_never_read_or_dropped(
    driver: PostgresDriver, clean: None
) -> None:
    async with driver.acquire() as conn:
        live = await dialect.introspect(conn, ["public"])  # e.g. what pull reads
        managed = await dialect.introspect(conn, ["public"], ["mizzle_test_guest"])
    assert "roles" not in live  # a pulled baseline holds no server roles
    assert list(managed.get("roles", {})) == ["mizzle_test_guest"]
    steps = diff(managed, build_snapshot([Notes], dialect), compare_expressions=False)
    assert not [s for s in steps if isinstance(s, DropRole)]


def test_supabase_roles_are_only_referenced() -> None:
    @final
    class Mine(Table, name="mine"):
        id = serial().primary_key()

        @classmethod
        @override
        def config(cls) -> list[Constraint]:
            return [rls(), policy("own", to=[AUTHENTICATED], using=auth_uid())]

    snapshot = build_snapshot([Mine], dialect)
    assert "roles" not in snapshot
    policies = snapshot["tables"]["public.mine"].get("policies", {})
    assert policies["own"]["roles"] == ["authenticated"]
