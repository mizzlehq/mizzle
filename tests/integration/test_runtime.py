"""Runtime and operations: ping, pool stats, connect hooks, slow queries, retries."""

import json
import logging
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from typing import Any

import pytest

from mizzle import (
    ConnectionError,
    Database,
    DatabaseError,
    MizzleError,
    QueryError,
    eq,
    in_,
    placeholder,
    sql,
)
from mizzle.dialects.base import Connection, IsolationLevel
from mizzle.dialects.postgres import PostgresDialect
from mizzle.dialects.postgres.driver import PostgresDriver
from tests.models import Posts, Users


async def setting(db: Database, name: str) -> str:
    [row] = await db.execute(sql(f"SELECT current_setting('{name}') AS value"))
    return str(row["value"])


async def test_ping_and_pool_stats(pg_url: str) -> None:
    async with await Database.connect(pg_url, schema=[Users, Posts], pool_size=3) as db:
        assert await db.ping() is True
        stats = db.pool_stats()
        assert stats.max_size == 3
        assert stats.min_size == 1
        assert stats.in_use == stats.size - stats.idle
        assert stats.wait_ms >= 0


async def test_on_connect_runs_for_each_new_connection(pg_url: str) -> None:
    opened = 0

    async def hook(conn: Connection) -> None:
        nonlocal opened
        opened += 1
        await conn.execute("SET application_name = 'hooked'", [])

    async with await Database.connect(
        pg_url, schema=[Users, Posts], pool_size=1, on_connect=hook
    ) as db:
        assert await setting(db, "application_name") == "hooked"
    assert opened >= 1


async def test_server_settings_apply_to_every_connection(pg_url: str) -> None:
    async with await Database.connect(
        pg_url, schema=[Users, Posts], pool_size=2, server_settings={"application_name": "srv"}
    ) as db:
        assert await setting(db, "application_name") == "srv"


async def test_slow_query_warning(pg_url: str, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="mizzle"):
        async with await Database.connect(pg_url, schema=[Users, Posts], slow_query_ms=0.0) as db:
            await db.execute(sql("SELECT 1"))
    assert any("slow query" in record.getMessage() for record in caplog.records)


async def test_connect_retries_until_the_database_is_back(
    pg_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = PostgresDialect.connect
    attempts = 0

    async def flaky(self: PostgresDialect, url: str, **kwargs: Any) -> PostgresDriver:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise ConnectionError("not up yet")
        return await original(self, url, **kwargs)

    monkeypatch.setattr(PostgresDialect, "connect", flaky)
    db = await Database.connect(
        pg_url, schema=[Users, Posts], connect_retries=5, connect_backoff=0.001
    )
    async with db:
        assert await db.ping() is True
    assert attempts == 3


async def test_listen_decodes_json_payloads(pg_url: str) -> None:
    async with (
        await Database.connect(pg_url, schema=[Users, Posts]) as db,
        db.listen("orders", decoder=json.loads) as events,
    ):
        await db.notify("orders", json.dumps({"id": 7}))
        event = await anext(events)
        assert event.data == {"id": 7}
        assert event.payload == '{"id": 7}'


async def test_transaction_callbacks(db: Database) -> None:
    events: list[str] = []

    async def commit() -> None:
        events.append("commit")

    async def rollback() -> None:
        events.append("rollback")

    async with db.transaction(after_commit=commit, after_rollback=rollback) as tx:
        await tx.insert(Users).values(email="rt8@x.io")
    assert events == ["commit"]

    with pytest.raises(DatabaseError):  # duplicate email -> rollback
        async with db.transaction(after_commit=commit, after_rollback=rollback) as tx:
            await tx.insert(Users).values(email="rt8@x.io")
    assert events == ["commit", "rollback"]
    assert await db.select().from_(Users).where(eq(Users.email, "rt8@x.io")) != []


class _Span:
    def __init__(self) -> None:
        self.attributes: dict[str, object] = {}


class RecordingTracer:
    def __init__(self) -> None:
        self.statements: list[str] = []
        self.transactions = 0

    @contextmanager
    def statement(
        self, sql: str, params: tuple[object, ...] | None, tags: Mapping[str, object]
    ) -> Generator[_Span]:
        self.statements.append(sql)
        yield _Span()

    @contextmanager
    def transaction(self, isolation: object = None) -> Generator[_Span]:
        self.transactions += 1
        yield _Span()


async def test_tracer_wraps_statements_and_transactions(pg_url: str) -> None:
    tracer = RecordingTracer()
    async with (
        await Database.connect(pg_url, schema=[Users, Posts], tracer=tracer) as db,
        db.transaction(),
    ):
        await db.execute(sql("SELECT 1"))
    assert tracer.transactions == 1
    assert "SELECT 1" in tracer.statements


async def test_executemany_pipelines_many_sets(db: Database) -> None:
    ran = await db.executemany(
        sql("INSERT INTO users (email) VALUES ($1)"), [("e1@x.io",), ("e2@x.io",), ("e3@x.io",)]
    )
    assert ran == 3
    found = await db.select(Users.email).from_(Users).order_by(Users.email)
    assert [email for (email,) in found] == ["e1@x.io", "e2@x.io", "e3@x.io"]

    with pytest.raises(QueryError):  # mizzle values, not external parameter sets
        await db.executemany(
            sql("INSERT INTO users (email) VALUES (", placeholder("email"), ")"), [("x",)]
        )


async def test_on_connect_survives_a_reset(pg_url: str) -> None:
    # R1: the reset after a dirty statement ran RESET ALL but not the hook again, so the
    # setting was gone on the same connection (asyncpg only calls init on a new one).
    async def hook(conn: Connection) -> None:
        await conn.execute("SET application_name = 'hooked'", [])

    async with await Database.connect(
        pg_url, schema=[Users, Posts], pool_size=1, on_connect=hook
    ) as db:
        assert await setting(db, "application_name") == "hooked"
        # A raw statement marks the connection dirty, so the next acquire resets it.
        await db.execute(sql("SELECT 1"))
        assert await setting(db, "application_name") == "hooked"
        await db.execute(sql("SELECT 1"))
        assert await setting(db, "application_name") == "hooked"


async def test_nested_after_commit_waits_for_the_outermost_commit(pg_url: str) -> None:
    # R2: releasing a savepoint is not a commit, so the hook must not fire there.
    fired: list[str] = []

    async def hook() -> None:
        fired.append("nested")

    async with await Database.connect(pg_url, schema=[Users, Posts], pool_size=1) as db:
        async with db.transaction() as tx:
            async with tx.transaction(after_commit=hook):
                pass
            assert fired == []  # still only released
        assert fired == ["nested"]  # the outer transaction committed

        fired.clear()
        with pytest.raises(RuntimeError):
            async with db.transaction() as tx:
                async with tx.transaction(after_commit=hook):
                    pass
                raise RuntimeError("outer rolls back")
        assert fired == []  # rolled back: the side effect must not have happened


async def test_after_commit_error_says_the_data_is_saved(pg_url: str) -> None:
    # R3: a hook that raises must not look like a failed write, or the caller retries.
    from mizzle import AfterCommitError

    async def hook() -> None:
        raise RuntimeError("queue is down")

    async with await Database.connect(pg_url, schema=[Users, Posts], pool_size=1) as db:
        with pytest.raises(AfterCommitError, match="the data is saved"):
            async with db.transaction(after_commit=hook) as tx:
                await tx.insert(Users).values(email="committed")
        # It really did commit, which is why retrying would insert a second row.
        committed = await db.select(Users.email).from_(Users).where(eq(Users.email, "committed"))
        assert committed == [("committed",)]


async def test_after_rollback_error_keeps_the_original_error(pg_url: str) -> None:
    # R3: the hook's failure used to replace the error that caused the rollback.
    async def hook() -> None:
        raise RuntimeError("hook is down")

    async with await Database.connect(pg_url, schema=[Users, Posts], pool_size=1) as db:
        with pytest.raises(ValueError, match="the real problem") as caught:
            async with db.transaction(after_rollback=hook):
                raise ValueError("the real problem")
        assert "after_rollback" in str(caught.value.__context__)


async def test_on_connect_does_not_rerun_without_a_reset(pg_url: str) -> None:
    # B1: the hook ran on every release, so every query paid an extra round trip and a
    # non-idempotent hook ran once per query.
    calls = 0

    async def hook(conn: Connection) -> None:
        nonlocal calls
        calls += 1
        await conn.execute("SET application_name = 'hooked'", [])

    async with await Database.connect(
        pg_url, schema=[Users, Posts], pool_size=1, on_connect=hook
    ) as db:
        assert await db.ping() is True
        for _ in range(5):
            await db.ping()
        # Six mizzle-built statements are session-safe: no reset, so no re-run.
        assert calls == 1
        assert await setting(db, "application_name") == "hooked"  # raw: one reset


async def test_on_connect_reruns_after_each_reset(pg_url: str) -> None:
    calls = 0

    async def hook(conn: Connection) -> None:
        nonlocal calls
        calls += 1
        await conn.execute("SET application_name = 'hooked'", [])

    async with await Database.connect(
        pg_url, schema=[Users, Posts], pool_size=1, on_connect=hook
    ) as db:
        assert calls == 1
        for expected in (2, 3, 4):
            # A raw statement marks the connection dirty; the release resets and re-runs.
            await db.execute(sql("SELECT 1"))
            assert calls == expected
            await db.ping()  # session-safe: no extra reset
            assert calls == expected


async def test_on_connect_reruns_in_always_mode(pg_url: str) -> None:
    # R1's remaining gap: with "always", asyncpg's own reset ran and the hook did not.
    calls = 0

    async def hook(conn: Connection) -> None:
        nonlocal calls
        calls += 1
        await conn.execute("SET application_name = 'hooked'", [])

    async with await Database.connect(
        pg_url,
        schema=[Users, Posts],
        pool_size=1,
        reset_connections="always",
        on_connect=hook,
    ) as db:
        assert calls == 1
        for expected in (2, 3, 4):
            assert await setting(db, "application_name") == "hooked"
            assert calls == expected


async def test_savepoint_rollback_keeps_a_released_sibling_hook(pg_url: str) -> None:
    # B2: clearing the whole list dropped hooks of savepoints that had already committed.
    fired: list[str] = []

    async def a() -> None:
        fired.append("a")

    async def b() -> None:
        fired.append("b")

    async with await Database.connect(pg_url, schema=[Users, Posts], pool_size=1) as db:
        async with db.transaction() as tx:
            async with tx.transaction(after_commit=a):
                pass
            with pytest.raises(RuntimeError):
                async with tx.transaction(after_commit=b):
                    raise RuntimeError("this savepoint rolls back")
        assert fired == ["a"]


@pytest.mark.parametrize("view", ["tag", "timeout", "primary"])
async def test_hooks_through_a_session_view_run(pg_url: str, view: str) -> None:
    # B3: _view() made a fresh Session with its own empty queue, so the hook was dropped.
    fired: list[str] = []

    async def hook() -> None:
        fired.append("hook")

    async with await Database.connect(pg_url, schema=[Users, Posts], pool_size=1) as db:
        async with db.transaction() as tx:
            if view == "tag":
                inner = tx.tag(request="r")
            elif view == "timeout":
                inner = tx.with_timeout(5)
            else:
                inner = tx.primary
            async with inner.transaction(after_commit=hook):
                pass
        assert fired == ["hook"]


async def test_after_rollback_hook_on_a_released_savepoint_runs_when_outer_rolls_back(
    pg_url: str,
) -> None:
    # B6: after_commit was held for the outer commit but after_rollback was not queued, so
    # a savepoint's rollback hook silently disappeared when the outer transaction failed.
    fired: list[str] = []

    async def hook() -> None:
        fired.append("rollback")

    async with await Database.connect(pg_url, schema=[Users, Posts], pool_size=1) as db:
        with pytest.raises(RuntimeError, match="outer"):
            async with db.transaction() as tx:
                async with tx.transaction(after_rollback=hook):
                    pass
                raise RuntimeError("outer rolls back")
        assert fired == ["rollback"]

        fired.clear()
        async with db.transaction() as tx, tx.transaction(after_rollback=hook):
            pass
        assert fired == []  # committed: no rollback hook


async def test_a_failing_after_commit_hook_does_not_skip_the_others(pg_url: str) -> None:
    # B6: the first raising hook stopped every later one, including the outer transaction's.
    fired: list[str] = []
    from mizzle import AfterCommitError

    async def bad() -> None:
        fired.append("bad")
        raise RuntimeError("boom")

    async def good() -> None:
        fired.append("good")

    async with await Database.connect(pg_url, schema=[Users, Posts], pool_size=1) as db:
        with pytest.raises(AfterCommitError):
            async with db.transaction(after_commit=bad) as tx, tx.transaction(after_commit=good):
                pass
        assert fired == ["good", "bad"]  # both ran; savepoint hook queued first


async def test_after_rollback_hook_error_keeps_the_original_context(pg_url: str) -> None:
    # B4: overwriting __context__ hid an earlier error, and the hook's traceback was lost.
    async def hook() -> None:
        raise RuntimeError("hook is down")

    async with await Database.connect(pg_url, schema=[Users, Posts], pool_size=1) as db:
        with pytest.raises(ValueError, match="real") as caught:
            async with db.transaction(after_rollback=hook):
                try:
                    raise OSError("earlier")
                except OSError as earlier:
                    raise ValueError("real") from earlier
        # The original context (the OSError) survives; the hook error is a note, and its
        # traceback is reachable through __cause__.
        assert isinstance(caught.value.__context__, OSError)
        assert any("after_rollback" in note for note in caught.value.__notes__)


async def test_connect_retry_gives_up_on_a_wrong_password(pg_url: str) -> None:
    # R4: a bad password does not fix itself; retrying only delays the error.
    import time

    from mizzle.core.errors import ConnectionError as DbConnectionError

    bad = pg_url.replace("mizzle:mizzle@", "mizzle:wrong-password@")
    started = time.monotonic()
    with pytest.raises(DbConnectionError):
        await Database.connect(bad, schema=[Users, Posts], connect_retries=3, connect_backoff=1.0)
    assert time.monotonic() - started < 1.0  # no 1s + 2s + 4s of sleeping


async def test_a_cancel_after_commit_runs_the_commit_hooks(pg_url: str) -> None:
    # B36: a cancel while the connection was released (after COMMIT) ran the rollback
    # hooks and dropped the commit hooks, for data that was saved.
    import asyncio

    ran: list[str] = []
    released = asyncio.Event()

    async def slow_hook(conn: Connection) -> None:
        released.set()
        await asyncio.sleep(0.5)

    async def after_commit() -> None:
        ran.append("commit")

    async def after_rollback() -> None:
        ran.append("rollback")

    async with await Database.connect(
        pg_url,
        schema=[Users, Posts],
        pool_size=1,
        reset_connections="always",
        on_connect=slow_hook,
    ) as db:
        await db.delete(Users).where(eq(Users.email, "b36@x"))

        async def write() -> None:
            async with db.transaction(
                after_commit=after_commit, after_rollback=after_rollback
            ) as tx:
                await tx.insert(Users).values({"email": "b36@x"})

        released.clear()  # the on_connect of the first connection already ran
        task = asyncio.create_task(write())
        await released.wait()  # COMMIT done; the reset's on_connect hook is sleeping
        task.cancel()
        with pytest.raises(asyncio.CancelledError) as caught:
            await task
        assert ran == ["commit"]
        assert any("committed" in note for note in caught.value.__notes__)
        rows = await db.select().from_(Users).where(eq(Users.email, "b36@x"))
        assert len(rows) == 1


async def test_a_rolled_back_savepoint_runs_the_hooks_of_savepoints_inside_it(
    pg_url: str,
) -> None:
    # B37: a savepoint released inside one that then rolled back lost its after_rollback.
    ran: list[str] = []

    def hook(name: str) -> Any:
        async def run() -> None:
            ran.append(name)

        return run

    async with await Database.connect(pg_url, schema=[Users, Posts], pool_size=1) as db:
        async with db.transaction(after_commit=hook("L0-ac")) as tx:
            with pytest.raises(RuntimeError):
                async with tx.transaction(after_rollback=hook("L1-rb")) as s1:
                    async with s1.transaction(after_rollback=hook("L2-rb")):
                        pass
                    raise RuntimeError("s1 fails")
        assert ran == ["L2-rb", "L1-rb", "L0-ac"]


async def test_a_savepoint_on_an_ended_transaction_runs_no_rollback_hook(pg_url: str) -> None:
    # B39: a view kept past its transaction started no savepoint, yet after_rollback ran.
    ran: list[str] = []

    async def hook() -> None:
        ran.append("rollback")

    async with await Database.connect(pg_url, schema=[Users, Posts], pool_size=1) as db:
        async with db.transaction() as tx:
            stale = tx.tag(x=1)
        with pytest.raises(Exception):  # noqa: B017 - the driver's "released" error
            async with stale.transaction(after_rollback=hook):
                pass
        assert ran == []


async def test_savepoints_from_two_tasks_are_refused(pg_url: str) -> None:
    # B39: concurrent savepoints failed with "another operation is in progress", labelled
    # as a COMMIT.
    import asyncio

    async with await Database.connect(pg_url, schema=[Users, Posts], pool_size=1) as db:

        async def savepoint(tx: Any) -> None:
            async with tx.transaction():
                await asyncio.sleep(0.05)

        async with db.transaction() as tx:
            results = await asyncio.gather(savepoint(tx), savepoint(tx), return_exceptions=True)
            errors = [r for r in results if isinstance(r, BaseException)]
            assert len(errors) == 1
            assert isinstance(errors[0], QueryError)
            assert "another task" in str(errors[0])
            await savepoint(tx)  # the transaction is still usable


async def test_a_failing_on_connect_after_a_reset_is_a_warning(
    pg_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    # B38: asyncpg closes a connection whose reset fails, so the check after the release
    # always saw a closed connection and logged at DEBUG.
    calls = 0

    async def flaky(conn: Connection) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("on_connect failed")

    async with await Database.connect(
        pg_url, schema=[Users, Posts], pool_size=1, reset_connections="always", on_connect=flaky
    ) as db:
        with caplog.at_level(logging.WARNING, logger="mizzle"):
            await db.execute(sql("SELECT 1"))
        assert any("could not return" in r.getMessage() for r in caplog.records)


async def test_a_hook_error_is_visible_after_raise_from(pg_url: str) -> None:
    # B40: with `raise X from Y` the context is hidden (__suppress_context__), so a hook
    # error put there never showed. The chain must also not loop back to the original.
    async def hook() -> None:
        raise RuntimeError("hook is down")

    async with await Database.connect(pg_url, schema=[Users, Posts], pool_size=1) as db:
        with pytest.raises(ValueError, match="real") as caught:
            async with db.transaction(after_rollback=hook):
                raise ValueError("real") from KeyError("cause")
        assert any("after_rollback" in note for note in caught.value.__notes__)

        with pytest.raises(ValueError) as plain:
            async with db.transaction(after_rollback=hook):
                raise ValueError("plain")
        seen: set[int] = set()
        error: BaseException | None = plain.value
        while error is not None:
            assert id(error) not in seen, "the exception chain is a cycle"
            seen.add(id(error))
            error = error.__cause__ or error.__context__


async def test_a_caught_failure_does_not_commit(pg_url: str) -> None:
    # C10: PostgreSQL answers COMMIT of an aborted transaction with ROLLBACK and no error,
    # so the block "succeeded", after_commit ran, and nothing was saved.
    ran: list[str] = []

    async def ac() -> None:
        ran.append("commit")

    async def ar() -> None:
        ran.append("rollback")

    async with await Database.connect(pg_url, schema=[Users, Posts], pool_size=1) as db:
        with pytest.raises(DatabaseError, match="aborted the transaction") as caught:
            async with db.transaction(after_commit=ac, after_rollback=ar) as tx:
                await tx.insert(Users).values({"email": "c10@x"})
                with pytest.raises(DatabaseError):
                    await tx.insert(Users).values({"email": "c10@x"})  # unique
        assert ran == ["rollback"]
        assert "COMMIT" in str(caught.value)
        assert await db.select().from_(Users).where(eq(Users.email, "c10@x")) == []

        # A failure inside a savepoint that rolled back leaves the outer one committable.
        async with db.transaction() as tx:
            await tx.insert(Users).values({"email": "c10b@x"})
            with pytest.raises(DatabaseError):
                async with tx.transaction() as sp:
                    await sp.insert(Users).values({"email": "c10b@x"})
        assert len(await db.select().from_(Users).where(eq(Users.email, "c10b@x"))) == 1


async def test_a_sibling_task_cannot_write_into_a_savepoint(pg_url: str) -> None:
    # C11: an insert from another task landed in a savepoint that then rolled back: it
    # returned its id, and the row was silently gone after the outer commit.
    import asyncio

    async with await Database.connect(pg_url, schema=[Users, Posts], pool_size=1) as db:

        async def savepoint(tx: Any) -> None:
            with pytest.raises(RuntimeError):
                async with tx.transaction() as sp:
                    await sp.execute(sql("SELECT 1"))
                    await asyncio.sleep(0.2)
                    raise RuntimeError("undo")

        async def insert(tx: Any) -> None:
            await asyncio.sleep(0.05)
            await tx.insert(Users).values({"email": "c11@x"})

        async with db.transaction() as tx:
            results = await asyncio.gather(savepoint(tx), insert(tx), return_exceptions=True)
            assert results[0] is None
            assert isinstance(results[1], QueryError)
            assert "savepoint open" in str(results[1])

            # Tasks started inside the savepoint may use it.
            async with tx.transaction() as sp:
                await asyncio.gather(
                    sp.insert(Users).values({"email": "c11a@x"}),
                    sp.insert(Users).values({"email": "c11b@x"}),
                )
        emails = await db.select(Users.email).from_(Users).where(eq(Users.email, "c11a@x"))
        assert emails == [("c11a@x",)]


async def test_a_body_error_is_kept_when_the_rollback_fails(pg_url: str) -> None:
    # C13: the body's error was replaced by "ConnectionError ... SQL: COMMIT".
    async with await Database.connect(pg_url, schema=[Users, Posts], pool_size=1) as db:
        with pytest.raises(ValueError, match="body") as caught:
            async with db.transaction() as tx:
                [row] = await tx.execute(sql("SELECT pg_backend_pid() AS pid"))
                async with await Database.connect(
                    pg_url, schema=[Users, Posts], pool_size=1
                ) as other:
                    await other.execute(sql(f"SELECT pg_terminate_backend({row['pid']})"))
                raise ValueError("body")
        assert any("ROLLBACK also failed" in note for note in caught.value.__notes__)


async def test_no_rollback_hook_when_the_transaction_never_began(
    pg_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # C15: a failed acquire ran after_rollback, though nothing had begun.
    from collections.abc import AsyncGenerator
    from contextlib import asynccontextmanager

    from mizzle import ConnectionError as DbConnectionError

    ran: list[str] = []

    async def ar() -> None:
        ran.append("rollback")

    @asynccontextmanager
    async def no_connection() -> AsyncGenerator[None]:
        raise DbConnectionError("too many connections")
        yield

    async with await Database.connect(pg_url, schema=[Users, Posts], pool_size=1) as db:
        monkeypatch.setattr(db.driver, "acquire", no_connection)
        with pytest.raises(DbConnectionError):
            async with db.transaction(after_rollback=ar):
                pass
    assert ran == []


async def test_reset_drops_temp_tables(pg_url: str) -> None:
    # C14: asyncpg's reset keeps temp tables, so their rows reached the next user.
    for mode in ("always", "auto"):
        async with await Database.connect(
            pg_url, schema=[Users, Posts], pool_size=1, reset_connections=mode
        ) as db:
            async with db.transaction() as tx:
                await tx.execute(sql("CREATE TEMP TABLE c14 (x int)"))
                await tx.execute(sql("INSERT INTO c14 VALUES (42)"))
            with pytest.raises(DatabaseError, match="c14"):
                await db.execute(sql("SELECT x FROM c14"))


async def test_ping_returns_false_when_the_server_hangs(
    pg_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # C17: a hung server made ping() raise QueryTimeout, or wait forever without one.
    import asyncio

    async with await Database.connect(pg_url, schema=[Users, Posts], pool_size=1) as db:
        assert await db.ping() is True

        async def hang(*_args: object) -> None:
            await asyncio.sleep(60)

        monkeypatch.setattr(db, "_call", hang)
        assert await db.ping(timeout=0.1) is False


async def test_a_failed_acquire_keeps_the_pool(
    pg_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # C12: one "too many connections" closed every healthy pooled connection.
    import asyncpg

    from mizzle import ConnectionError as DbConnectionError

    async with await Database.connect(pg_url, schema=[Users, Posts], pool_size=1) as db:
        [before] = await db.execute(sql("SELECT pg_backend_pid() AS pid"))

        async def too_many(*_args: object, **_kw: object) -> object:
            raise asyncpg.TooManyConnectionsError("sorry, too many clients already")

        monkeypatch.setattr(asyncpg.Pool, "acquire", too_many)
        with pytest.raises(DbConnectionError, match="could not acquire"):
            await db.execute(sql("SELECT 1"))
        monkeypatch.undo()
        [after] = await db.execute(sql("SELECT pg_backend_pid() AS pid"))
        assert after == before  # the healthy connection was kept


async def test_stream_does_not_leave_its_span_current(pg_url: str) -> None:
    # C16: the stream's span stayed current in the caller between batches and after a
    # break, and finalizing the generator reset its token from another context.
    import asyncio
    import contextvars
    import gc
    from contextlib import contextmanager

    current: contextvars.ContextVar[str | None] = contextvars.ContextVar("span", default=None)
    parents: list[tuple[str, str | None]] = []

    class Tracer:
        @contextmanager
        def statement(
            self,
            sql: str,
            params: tuple[object, ...] | None,
            tags: Mapping[str, object],
        ) -> Generator[object]:
            parents.append((sql[:20], current.get()))
            token = current.set(sql[:20])
            try:
                yield
            finally:
                current.reset(token)

        @contextmanager
        def transaction(self, isolation: IsolationLevel | None = None) -> Generator[object]:
            yield

    loop_errors: list[object] = []
    asyncio.get_running_loop().set_exception_handler(lambda _l, ctx: loop_errors.append(ctx))
    async with await Database.connect(
        pg_url, schema=[Users, Posts], pool_size=2, tracer=Tracer()
    ) as db:
        query = db.compile(sql("SELECT generate_series(1, 50) AS g"))
        n = 0
        async for _batch in db.stream(query, 10):
            assert current.get() is None  # nothing current while the caller runs
            await db.execute(sql("SELECT 1"))
            n += 1
            if n == 2:
                break
        await asyncio.sleep(0.05)
        gc.collect()
        await asyncio.sleep(0.05)
    assert current.get() is None
    assert ("SELECT 1", None) in parents  # the caller's query is not a child of the stream
    assert loop_errors == []


async def test_wrong_error_types_are_mizzle_errors(pg_url: str) -> None:
    # C18: an admin cancel was a QueryTimeout, a view kept after close() raised a raw
    # asyncpg InterfaceError, and a malformed URL a raw ValueError.
    import asyncio

    from mizzle import ConnectionError as DbConnectionError
    from mizzle.core.errors import QueryTimeout

    async with await Database.connect(pg_url, schema=[Users, Posts], pool_size=2) as db:

        async def cancel_the_sleeper() -> None:
            await asyncio.sleep(0.3)
            await db.execute(
                sql(
                    "SELECT pg_cancel_backend(pid) FROM pg_stat_activity "
                    "WHERE query LIKE 'SELECT pg_sleep(5)%' AND pid <> pg_backend_pid()"
                )
            )

        canceller = asyncio.create_task(cancel_the_sleeper())
        with pytest.raises(DatabaseError, match="user request") as caught:
            await db.execute(sql("SELECT pg_sleep(5)"))
        assert not isinstance(caught.value, QueryTimeout)
        await canceller

    view = db.primary
    await db.close()
    with pytest.raises(MizzleError):
        await view.execute(sql("SELECT 1"))

    with pytest.raises(DbConnectionError, match="malformed") as bad:
        await Database.connect(
            "postgresql://u:secretpw@localhost:badport/db", schema=[Users, Posts]
        )
    assert "secretpw" not in str(bad.value)


async def test_a_queued_sibling_statement_does_not_land_in_a_closing_savepoint(
    pg_url: str,
) -> None:
    # R1: the savepoint marker was popped before ROLLBACK TO ran. A sibling statement
    # queued on the lock then ran inside the savepoint and was undone silently.
    import asyncio

    async with await Database.connect(pg_url, schema=[Users, Posts], pool_size=1) as db:

        async def owner(tx: Any) -> None:
            with pytest.raises(RuntimeError):
                async with tx.transaction() as sp:
                    await sp.execute(sql("SELECT pg_sleep(0.3)"))  # sibling queues here
                    raise RuntimeError("undo")

        async def sibling(tx: Any) -> None:
            await asyncio.sleep(0.1)
            await tx.insert(Users).values({"email": "r1@x"})

        async with db.transaction() as tx:
            results = await asyncio.gather(owner(tx), sibling(tx), return_exceptions=True)
        assert results[0] is None
        assert isinstance(results[1], QueryError), results[1]
        assert await db.select().from_(Users).where(eq(Users.email, "r1@x")) == []


async def test_a_cancelled_statement_aborts_the_commit(pg_url: str) -> None:
    # R2: a cancelled statement aborts the transaction, but it was not counted as a
    # failure: COMMIT "succeeded", after_commit ran, and nothing was saved.
    import asyncio

    ran: list[str] = []

    async def ac() -> None:
        ran.append("commit")

    async with await Database.connect(pg_url, schema=[Users, Posts], pool_size=1) as db:
        with pytest.raises(DatabaseError, match="aborted the transaction"):
            async with db.transaction(after_commit=ac) as tx:
                await tx.insert(Users).values({"email": "r2@x"})
                slow = asyncio.ensure_future(tx.execute(sql("SELECT pg_sleep(5)")))
                await asyncio.sleep(0.2)
                slow.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await slow
        assert ran == []
        assert await db.select().from_(Users).where(eq(Users.email, "r2@x")) == []


async def test_a_client_side_encode_error_does_not_abort_the_transaction(pg_url: str) -> None:
    # R4: asyncpg's client-side DataError (sqlstate 22000) was counted as a server
    # failure, so a transaction PostgreSQL never aborted was reported as not committed.
    async with await Database.connect(pg_url, schema=[Users, Posts], pool_size=1) as db:
        async with db.transaction() as tx:
            await tx.insert(Users).values({"email": "r4@x"})
            with pytest.raises(DatabaseError):
                await tx.executemany(sql("SELECT $1::text"), [[123]])
        assert len(await db.select().from_(Users).where(eq(Users.email, "r4@x"))) == 1


async def test_a_statement_after_the_block_is_refused(pg_url: str) -> None:
    # R3: a task's statement queued behind COMMIT ran in autocommit on the connection.
    # R11: a statement on tx after its block gave ConnectionError, not QueryError.
    import asyncio

    async with await Database.connect(pg_url, schema=[Users, Posts], pool_size=1) as db:
        async with db.transaction() as tx:
            late = asyncio.ensure_future(tx.insert(Users).values({"email": "r3@x"}))
        with pytest.raises(QueryError, match="transaction has ended"):
            await late
        with pytest.raises(QueryError, match="transaction has ended"):
            await tx.select().from_(Users)
        assert await db.select().from_(Users).where(eq(Users.email, "r3@x")) == []


async def test_a_cancel_during_commit_runs_no_hook(pg_url: str) -> None:
    # R5: a cancel while COMMIT was in flight ran after_rollback, though the server may
    # have committed. The outcome is unknown, so no hook runs and a note says so.
    import asyncio

    ran: list[str] = []

    async def ac() -> None:
        ran.append("commit")

    async def ar() -> None:
        ran.append("rollback")

    async with await Database.connect(pg_url, schema=[Users, Posts], pool_size=1) as db:
        await db.execute(sql("CREATE TABLE r5_slow (id int)"))
        await db.execute(
            sql(
                "CREATE FUNCTION r5_sleep() RETURNS trigger LANGUAGE plpgsql AS"
                " $$ BEGIN PERFORM pg_sleep(1); RETURN NULL; END $$"
            )
        )
        await db.execute(
            sql(
                "CREATE CONSTRAINT TRIGGER r5 AFTER INSERT ON r5_slow"
                " DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION r5_sleep()"
            )
        )
        try:

            async def write() -> None:
                async with db.transaction(after_commit=ac, after_rollback=ar) as tx:
                    await tx.execute(sql("INSERT INTO r5_slow VALUES (1)"))

            task = asyncio.ensure_future(write())
            await asyncio.sleep(0.3)  # the body is done; COMMIT runs the 1 s trigger
            task.cancel()
            with pytest.raises(asyncio.CancelledError) as caught:
                await task
            assert ran == []
            assert any("may or may not" in note for note in caught.value.__notes__)
        finally:
            await db.execute(sql("DROP TABLE r5_slow"))
            await db.execute(sql("DROP FUNCTION r5_sleep()"))


async def test_acquire_gives_up_after_acquire_timeout(pg_url: str) -> None:
    # R9: with every pooled connection busy, a query waited forever.
    async with (
        await Database.connect(
            pg_url, schema=[Users, Posts], pool_size=1, acquire_timeout=0.2
        ) as db,
        db.transaction(),
    ):  # holds the only connection
        with pytest.raises(ConnectionError, match="in time"):
            await db.select().from_(Users)


@pytest.mark.parametrize(
    "args",
    [{"pool_size": 0}, {"pool_min_size": 5, "pool_size": 2}, {"connect_timeout": 0}],
)
async def test_bad_pool_args_are_a_query_error(pg_url: str, args: dict[str, Any]) -> None:
    # R11: asyncpg raised a bare ValueError.
    with pytest.raises(QueryError, match=r"Database.connect\(\) needs"):
        await Database.connect(pg_url, schema=[Users, Posts], **args)


async def test_a_savepoint_cancelled_waiting_for_the_lock_breaks_the_transaction(
    pg_url: str,
) -> None:
    # R4-1: cancelled while waiting for the lock to send ROLLBACK TO / RELEASE, the
    # savepoint stayed open, its marker was dropped, and COMMIT kept its rows.
    import asyncio

    rolled: list[str] = []

    async def ar() -> None:
        rolled.append("sp")

    async with await Database.connect(pg_url, schema=[Users, Posts], pool_size=1) as db:
        for fail in (True, False):
            rolled.clear()
            with pytest.raises(QueryError, match="cancelled before its ROLLBACK TO"):
                async with db.transaction() as tx:
                    bg: asyncio.Task[Any] | None = None
                    with pytest.raises((TimeoutError, RuntimeError)):
                        async with asyncio.timeout(0.3), tx.transaction(after_rollback=ar) as sp:
                            await sp.insert(Users).values({"email": "r41@x"})
                            bg = asyncio.create_task(sp.execute(sql("SELECT pg_sleep(1)")))
                            await asyncio.sleep(0.05)
                            if fail:
                                raise RuntimeError("savepoint body failed")
                    assert bg is not None
                    await bg
                    await tx.insert(Users).values({"email": "r41b@x"})
            assert rolled == ["sp"]
            assert await db.select().from_(Users).where(in_(Users.email, ["r41@x", "r41b@x"])) == []


async def test_commit_refuses_while_a_task_savepoint_is_open(pg_url: str) -> None:
    # R4-2: COMMIT ran while a child task's savepoint was open: its rows were committed,
    # then the child "rolled back" and its after_rollback ran.
    import asyncio

    async with await Database.connect(pg_url, schema=[Users, Posts], pool_size=2) as db:

        async def child(tx: Any) -> None:
            async with tx.transaction() as sp:
                await sp.insert(Users).values({"email": "r42@x"})
                await asyncio.sleep(0.3)
                raise RuntimeError("undo")

        task: asyncio.Task[None] | None = None
        with pytest.raises(QueryError, match="still open"):
            async with db.transaction() as tx:
                task = asyncio.create_task(child(tx))
                await asyncio.sleep(0.1)
        assert task is not None
        with pytest.raises(RuntimeError):
            await task
        assert await db.select().from_(Users).where(eq(Users.email, "r42@x")) == []


async def test_a_task_started_in_a_rolled_back_savepoint_is_refused(pg_url: str) -> None:
    # R4-3: the task's savepoint marker was gone, the list was empty, so the check
    # passed and its write was committed with the outer transaction.
    import asyncio

    async with await Database.connect(pg_url, schema=[Users, Posts], pool_size=2) as db:
        child: asyncio.Task[Any] | None = None
        async with db.transaction() as tx:
            with pytest.raises(RuntimeError):
                async with tx.transaction() as sp:
                    child = asyncio.ensure_future(sp.insert(Users).values({"email": "r43@x"}))
                    raise RuntimeError("undo")
            assert child is not None
            with pytest.raises(QueryError, match="has ended"):
                await child
            async with db.transaction() as other:  # another transaction is not affected
                await other.execute(sql("SELECT 1"))
        assert await db.select().from_(Users).where(eq(Users.email, "r43@x")) == []


async def test_raw_commit_or_rollback_inside_a_transaction_is_detected(pg_url: str) -> None:
    # R4-4: a raw ROLLBACK / COMMIT ended the transaction unnoticed: success was reported
    # (and after_commit ran), or a "rolled back" block kept its rows.
    ran: list[str] = []

    async def ac() -> None:
        ran.append("commit")

    async def ar() -> None:
        ran.append("rollback")

    async with await Database.connect(pg_url, schema=[Users, Posts], pool_size=1) as db:
        with pytest.raises(DatabaseError, match="raw SQL"):
            async with db.transaction(after_commit=ac, after_rollback=ar) as tx:
                await tx.insert(Users).values({"email": "r44@x"})
                await tx.execute(sql("ROLLBACK"))
        assert ran == ["rollback"]
        with pytest.raises(RuntimeError) as err:
            async with db.transaction() as tx:
                await tx.execute(sql("COMMIT"))
                raise RuntimeError("undo")
        assert any("raw SQL" in note for note in err.value.__notes__)
        with pytest.raises(DatabaseError, match="raw SQL"):
            async with db.transaction() as tx, tx.transaction() as sp:
                await sp.execute(sql("COMMIT"))


async def test_an_unknown_isolation_level_raises_query_error(pg_url: str) -> None:
    # R4-5: a bare KeyError came from the driver's lookup.
    async with await Database.connect(pg_url, schema=[Users, Posts]) as db:
        for level in ("bogus", "read uncommitted"):
            with pytest.raises(QueryError, match="serializable"):
                async with db.transaction(level):  # pyright: ignore[reportArgumentType]
                    pass
