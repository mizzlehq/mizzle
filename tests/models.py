"""Shared test schema."""

import enum
from typing import Any

from mizzle.core.schema.table import Table
from mizzle.core.schema.types import integer, text, timestamp, varchar
from mizzle.core.sql.raw import sql
from mizzle.dialects.postgres.types import jsonb, pg_enum, serial


class Role(enum.StrEnum):
    ADMIN = "admin"
    USER = "user"


class Users(Table, name="users"):
    id = serial().primary_key()
    email = varchar(255).not_null().unique()
    bio = text()
    role = pg_enum(Role).not_null().default(Role.USER)
    created_at = timestamp(tz=True).not_null().default(sql("now()"))


class Posts(Table, name="posts"):
    id = serial().primary_key()
    author_id = integer().not_null().references(lambda: Users.id, on_delete="cascade")
    title = text().not_null()
    meta = jsonb[dict[str, Any]]()
