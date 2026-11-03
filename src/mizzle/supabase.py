"""Supabase helpers for row-level security: ``from mizzle.supabase import auth_uid, crud_policy``.

    class Notes(Table, name="notes"):
        id = serial().primary_key()
        owner_id = uuid().not_null()

        @classmethod
        @override
        def config(cls) -> list[Constraint]:
            return [
                rls(),
                *crud_policy("notes_owner", to=[AUTHENTICATED], read=True,
                             modify=eq(cls.owner_id, auth_uid())),
            ]

The roles below already exist in every Supabase project.
"""

from __future__ import annotations

from mizzle.core.schema.constraints import Policy, policy
from mizzle.core.sql.nodes import Condition, Raw
from mizzle.core.sql.raw import sql

ANON = "anon"
AUTHENTICATED = "authenticated"
SERVICE_ROLE = "service_role"
POSTGRES = "postgres"
SUPABASE_AUTH_ADMIN = "supabase_auth_admin"


def auth_uid() -> Raw:
    """The signed-in user's id. Wrapped in a subquery so PostgreSQL evaluates it once per
    statement instead of once per row, as Supabase recommends."""
    return sql("(select auth.uid())")


def auth_jwt() -> Raw:
    """The request's JWT claims as jsonb."""
    return sql("(select auth.jwt())")


def auth_role() -> Raw:
    """The request's role name (anon, authenticated, ...)."""
    return sql("(select auth.role())")


def _condition(rule: Condition | bool) -> Condition:
    if isinstance(rule, bool):
        return sql("true") if rule else sql("false")
    return rule


def crud_policy(
    name: str,
    *,
    to: list[str],
    read: Condition | bool | None = None,
    modify: Condition | bool | None = None,
) -> list[Policy]:
    """Policies for the usual cases, like Drizzle's ``crudPolicy``: ``read`` controls
    SELECT; ``modify`` controls INSERT, UPDATE and DELETE. ``True`` allows every row,
    ``False`` none, and ``None`` adds no policy (so, with RLS on, nothing is allowed).
    Names are ``{name}_select``, ``{name}_insert``, ``{name}_update`` and ``{name}_delete``."""
    policies: list[Policy] = []
    if read is not None:
        policies.append(policy(f"{name}_select", for_="select", to=to, using=_condition(read)))
    if modify is not None:
        rule = _condition(modify)
        policies += [
            policy(f"{name}_insert", for_="insert", to=to, with_check=rule),
            policy(f"{name}_update", for_="update", to=to, using=rule, with_check=rule),
            policy(f"{name}_delete", for_="delete", to=to, using=rule),
        ]
    return policies
