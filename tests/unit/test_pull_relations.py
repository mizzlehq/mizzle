"""`mizzle pull` writes one()/many() relations from foreign keys."""

import importlib.util
import sys
from pathlib import Path
from typing import final, override

from mizzle import Constraint, Database, Table, unique
from mizzle.dialects.postgres import PostgresDialect
from mizzle.kit.codegen import render_schema_module
from mizzle.kit.snapshot import build_snapshot
from mizzle.pg import integer, serial, text


@final
class Teams(Table, name="teams"):
    id = serial().primary_key()


@final
class Members(Table, name="members"):
    id = serial().primary_key()
    team_id = integer().not_null().references(lambda: Teams.id)
    mentor_id = integer().references(lambda: Members.id)  # points to its own table


@final
class Profiles(Table, name="profiles"):
    id = serial().primary_key()
    member_id = integer().not_null().unique().references(lambda: Members.id)  # one-to-one


@final
class Messages(Table, name="messages"):
    id = serial().primary_key()
    sender_id = integer().not_null().references(lambda: Members.id)  # two keys, one target
    receiver_id = integer().not_null().references(lambda: Members.id)
    author = integer().references(lambda: Members.id)  # no _id suffix
    paid = integer().references(lambda: Teams.id)  # ends in "id" but not a suffix


@final
class Category(Table, name="Category"):
    id = text().primary_key()


@final
class Thread(Table, name="thread"):
    id = text().primary_key()
    categoryId = text().not_null().references(lambda: Category.id)
    createdAt = text()


def pulled(*tables: type[Table]) -> str:
    return render_schema_module(build_snapshot(tables, PostgresDialect()))


def load(source: str, tmp_path: Path) -> dict[str, type[Table]]:
    path = tmp_path / "pulled.py"
    path.write_text(source)
    spec = importlib.util.spec_from_file_location("pulled_relations", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return {
        name: value
        for name, value in vars(module).items()
        if isinstance(value, type) and issubclass(value, Table) and value is not Table
    }


def test_relation_names(tmp_path: Path) -> None:
    source = pulled(Teams, Members, Profiles, Messages, Category, Thread)
    for line in [
        # one() from the key column; many() from the referencing table, pluralized
        "team = one(lambda: Teams, fields=lambda: [Members.team_id], "
        "references=lambda: [Teams.id])",
        "members = many(lambda: Members)",
        # self-reference and two keys to one table: named and paired by relation_name
        "mentor = one(lambda: Members, fields=lambda: [Members.mentor_id], "
        "references=lambda: [Members.id], relation_name='mentor')",
        "members_by_mentor = many(lambda: Members, relation_name='mentor')",
        "messages_by_sender = many(lambda: Messages, relation_name='sender')",
        "messages_by_receiver = many(lambda: Messages, relation_name='receiver')",
        # a unique key column: the reverse side is one(), singular
        "profile = one(lambda: Profiles)",
        # no suffix: the column name is taken, so the target is appended
        "author_member = one(lambda: Members, fields=lambda: [Messages.author], ",
        "paid_team = one(lambda: Teams, fields=lambda: [Messages.paid], ",
        # camelCase tables get camelCase names
        "category = one(lambda: Category, fields=lambda: [Thread.categoryId], ",
        "threads = many(lambda: Thread)",
    ]:
        assert line in source, line
    assert "from mizzle import Table, many, one" in source.replace("Constraint, ", "")

    # The generated module imports, and every relation resolves and compiles.
    tables = load(source, tmp_path)
    db = Database(schema=list(tables.values()))
    for table in tables.values():
        for relation in table.__table__.relations.values():
            relation.link()
    members = tables["Members"]
    sql_text, _ = (
        db.query(members)
        .find_many(with_={"messages_by_sender": True, "profile": True, "mentor": True})
        .to_sql()
    )
    assert "json_agg" in sql_text


def test_enum_names_keep_case_and_defaults_use_members(tmp_path: Path) -> None:
    import enum

    from mizzle.pg import pg_enum

    class NotificationStatus(enum.StrEnum):
        UNREAD = "UNREAD"
        READ = "READ"

    @final
    class Notes(Table, name="notes"):
        id = serial().primary_key()
        status = (
            pg_enum(NotificationStatus, type_name="NotificationStatus")
            .not_null()
            .default(NotificationStatus.UNREAD)
        )

        @classmethod
        @override
        def config(cls) -> list[Constraint]:
            return [unique(cls.status, name="notes_status_uq")]

    source = pulled(Notes)
    assert "class NotificationStatus(enum.StrEnum):" in source
    assert ".default(NotificationStatus.UNREAD)" in source
    assert "def config(cls) -> list[Constraint]:" in source
    assert "Any" not in source
    load(source, tmp_path)


def test_time_precision_survives_pull(tmp_path: Path) -> None:
    from mizzle.kit.diff import StrictRenames, diff
    from mizzle.pg import time, timestamp

    @final
    class Stamps(Table, name="stamps"):
        id = serial().primary_key()
        at = timestamp(precision=3)  # what Prisma creates
        at_tz = timestamp(tz=True, precision=0)
        clock = time(precision=2)
        clock_tz = time(tz=True, precision=6)
        plain = timestamp()

    snapshot = build_snapshot([Stamps], PostgresDialect())
    source = render_schema_module(snapshot)
    assert "at = timestamp(precision=3)" in source
    assert "at_tz = timestamp(tz=True, precision=0)" in source
    assert "clock = time(precision=2)" in source
    assert "plain = timestamp()" in source
    # Pulling and building again gives the same schema: no migration from a pull.
    again = build_snapshot([load(source, tmp_path)["Stamps"]], PostgresDialect())
    assert diff(snapshot, again, StrictRenames()) == []
