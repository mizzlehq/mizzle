"""End-to-end API tests on a fresh database (migrated at startup, like production).

Needs the repo's Docker Postgres:  MIZZLE_TEST_URL=postgresql://mizzle:mizzle@localhost:54329/mizzle
"""

import os
import uuid
from collections.abc import AsyncGenerator

import asyncpg
import httpx
import pytest
from social.app import create_app


@pytest.fixture
async def client() -> AsyncGenerator[httpx.AsyncClient]:
    admin_url = os.environ.get("MIZZLE_TEST_URL", "")
    if not admin_url:
        pytest.skip("set MIZZLE_TEST_URL to run the example's API tests")
    name = f"social_{uuid.uuid4().hex[:10]}"
    admin = await asyncpg.connect(admin_url)
    await admin.execute(f'CREATE DATABASE "{name}"')
    app = create_app(f"{admin_url.rpartition('/')[0]}/{name}", migrate_on_start=True)
    try:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
                yield c
    finally:
        await admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
        await admin.close()


async def make_user(client: httpx.AsyncClient, username: str) -> int:
    response = await client.post(
        "/users",
        json={"username": username, "email": f"{username}@example.com", "display_name": username},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert "email" not in body  # private
    return int(body["id"])


def as_user(user_id: int) -> dict[str, str]:
    return {"X-User-Id": str(user_id)}


async def test_social_flow(client: httpx.AsyncClient) -> None:
    ann = await make_user(client, "ann")
    bob = await make_user(client, "bob")
    cat = await make_user(client, "cat")

    # bob follows ann (twice: idempotent), cat follows nobody
    for _ in range(2):
        assert (await client.post("/users/ann/follow", headers=as_user(bob))).status_code == 204
    profile = (await client.get("/users/ann")).json()
    assert (profile["followers"], profile["following"]) == (1, 0)

    posts: dict[str, int] = {}
    for visibility in ("public", "followers", "private"):
        r = await client.post(
            "/posts",
            json={
                "body": f"a {visibility} post",
                "visibility": visibility,
                "meta": {"tags": ["t"]},
            },
            headers=as_user(ann),
        )
        assert r.status_code == 201, r.text
        assert r.json()["author_id"] == ann
        posts[visibility] = r.json()["id"]

    # bob sees public + followers posts in his feed; cat (not following) sees nothing
    bob_feed = (await client.get("/feed", headers=as_user(bob))).json()
    assert [i["post"]["visibility"] for i in bob_feed["items"]] == ["followers", "public"]
    assert (await client.get("/feed", headers=as_user(cat))).json()["items"] == []

    # private and followers-only posts are hidden (404, not 403, so they stay secret)
    assert (await client.get(f"/posts/{posts['private']}", headers=as_user(bob))).status_code == 404
    assert (
        await client.get(f"/posts/{posts['followers']}", headers=as_user(cat))
    ).status_code == 404
    assert (await client.get(f"/posts/{posts['public']}", headers=as_user(cat))).status_code == 200

    # comments, likes (idempotent), bookmarks (upsert)
    public = posts["public"]
    r = await client.post(f"/posts/{public}/comments", json={"body": "nice"}, headers=as_user(cat))
    assert r.status_code == 201
    for user in (bob, cat, cat):
        assert (
            await client.post(f"/posts/{public}/like", headers=as_user(user))
        ).status_code == 204
    for note in ("later", "read again"):
        r = await client.put(f"/posts/{public}/bookmark", json={"note": note}, headers=as_user(bob))
        assert r.status_code == 204
    detail = (await client.get(f"/posts/{public}", headers=as_user(bob))).json()
    assert detail["likes"] == 2
    assert [(c["author"]["username"], c["comment"]["body"]) for c in detail["comments"]] == [
        ("cat", "nice")
    ]

    # feed pagination
    page = (await client.get("/feed?limit=1", headers=as_user(ann))).json()
    assert len(page["items"]) == 1 and page["next_before"] is not None
    rest = (await client.get(f"/feed?before={page['next_before']}", headers=as_user(ann))).json()
    assert len(rest["items"]) == 2

    # only the author can delete; cascades remove likes and comments
    assert (await client.delete(f"/posts/{public}", headers=as_user(bob))).status_code == 404
    assert (await client.delete(f"/posts/{public}", headers=as_user(ann))).status_code == 204


async def test_errors(client: httpx.AsyncClient) -> None:
    ann = await make_user(client, "ann")
    dup = await client.post(
        "/users", json={"username": "ann", "email": "x@example.com", "display_name": "x"}
    )
    assert dup.status_code == 409
    # mass assignment: fields outside the allowlist are rejected
    sneaky = await client.post(
        "/posts", json={"body": "hi", "author_id": 999, "id": 1}, headers=as_user(ann)
    )
    assert sneaky.status_code == 422
    bad_name = await client.post(
        "/users", json={"username": "Not Valid!", "email": "v@example.com", "display_name": "v"}
    )
    assert bad_name.status_code == 422  # the users_username_format CHECK constraint
    assert (await client.get("/feed", headers=as_user(12345))).status_code == 401
    assert (await client.post("/users/ann/follow", headers=as_user(ann))).status_code == 400
    assert (await client.get("/users/nobody")).status_code == 404
