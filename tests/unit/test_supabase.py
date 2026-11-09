"""mizzle.supabase: role names, auth helpers and crud_policy()."""

from mizzle.core.sql.nodes import Node
from mizzle.dialects.postgres.compiler import PostgresCompiler
from mizzle.supabase import ANON, AUTHENTICATED, auth_uid, crud_policy


def text(node: Node | None) -> str | None:
    return None if node is None else PostgresCompiler().compile(node).sql


def test_crud_policy_expands_to_one_policy_per_command() -> None:
    policies = crud_policy("notes", to=[AUTHENTICATED], read=True, modify=auth_uid())
    by_name = {p.name: p for p in policies}
    assert list(by_name) == ["notes_select", "notes_insert", "notes_update", "notes_delete"]
    assert text(by_name["notes_select"].using) == "true"
    assert by_name["notes_insert"].using is None
    assert text(by_name["notes_insert"].with_check) == "(select auth.uid())"
    assert text(by_name["notes_update"].using) == "(select auth.uid())"
    assert text(by_name["notes_delete"].using) == "(select auth.uid())"
    assert by_name["notes_delete"].with_check is None


def test_crud_policy_parts_are_optional() -> None:
    assert [p.name for p in crud_policy("x", to=[ANON], read=False)] == ["x_select"]
    assert [p.name for p in crud_policy("x", to=[ANON], modify=True)] == [
        "x_insert",
        "x_update",
        "x_delete",
    ]
    assert crud_policy("x", to=[ANON]) == []
