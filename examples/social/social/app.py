"""A small social network API: profiles, follows, posts, comments, likes and bookmarks.

    uv run uvicorn social.app:create_app --factory --reload

Authentication is out of scope: the ``X-User-Id`` header stands in for a real session.
"""

import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse

from mizzle import (
    CheckViolation,
    Database,
    ForeignKeyViolation,
    UniqueViolation,
    and_,
    count,
    desc,
    eq,
    excluded,
    in_,
    lt,
    ne,
    or_,
)
from mizzle.kit import migrate
from mizzle.kit.config import load_dotenv
from social.models import (
    BookmarkIn,
    CommentIn,
    CommentOut,
    CommentView,
    Feed,
    PostDetail,
    PostIn,
    PostOut,
    PostView,
    Profile,
    UserIn,
    UserOut,
)
from social.schema import Bookmarks, Comments, Follows, Likes, Posts, Users, Visibility

ROOT = Path(__file__).resolve().parent.parent
TABLES = [Users, Follows, Posts, Comments, Likes, Bookmarks]


router = APIRouter()


_state: dict[str, Database] = {}  # set by the app's lifespan


def get_db() -> Database:
    return _state["db"]


DB = Annotated[Database, Depends(get_db)]


async def current_user(db: DB, x_user_id: Annotated[int, Header()]) -> Users:
    user = await db.query(Users).find_first(where=eq(Users.id, x_user_id))
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "unknown X-User-Id")
    return user


Me = Annotated[Users, Depends(current_user)]


async def user_by_name(db: Database, username: str) -> Users:
    user = await db.query(Users).find_first(where=eq(Users.username, username))
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no user {username!r}")
    return user


async def follows(db: Database, follower: int, followee: int) -> bool:
    rows = await (
        db.select(Follows.follower_id)
        .from_(Follows)
        .where(and_(eq(Follows.follower_id, follower), eq(Follows.followee_id, followee)))
    )
    return bool(rows)


async def visible_post(db: Database, me: Users, post_id: int) -> Posts:
    """The post, if ``me`` may see it; 404 otherwise (do not reveal private posts)."""
    post = await db.query(Posts).find_first(where=eq(Posts.id, post_id))
    allowed = post is not None and (
        post.author_id == me.id
        or post.visibility is Visibility.PUBLIC
        or (post.visibility is Visibility.FOLLOWERS and await follows(db, me.id, post.author_id))
    )
    if post is None or not allowed:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such post")
    return post


async def counts(
    db: Database, table: type[Likes] | type[Comments], ids: list[int]
) -> dict[int, int]:
    if not ids:
        return {}
    rows = await (
        db.select(table.post_id, count())
        .from_(table)
        .where(in_(table.post_id, ids))
        .group_by(table.post_id)
    )
    return dict(rows)


# -- users ----------------------------------------------------------------------------


@router.post("/users", status_code=status.HTTP_201_CREATED)
async def create_user(db: DB, body: UserIn) -> UserOut:
    [user] = await db.insert(Users).values(body).returning()
    return UserOut.model_validate(user)


@router.get("/users/{username}")
async def profile(db: DB, username: str) -> Profile:
    user = await user_by_name(db, username)
    [(followers,)] = await db.select(count()).from_(Follows).where(eq(Follows.followee_id, user.id))
    [(following,)] = await db.select(count()).from_(Follows).where(eq(Follows.follower_id, user.id))
    [(posts,)] = await db.select(count()).from_(Posts).where(eq(Posts.author_id, user.id))
    return Profile(
        user=UserOut.model_validate(user), followers=followers, following=following, posts=posts
    )


@router.post("/users/{username}/follow", status_code=status.HTTP_204_NO_CONTENT)
async def follow(db: DB, me: Me, username: str) -> None:
    target = await user_by_name(db, username)
    if target.id == me.id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "you cannot follow yourself")
    await (
        db.insert(Follows).values(follower_id=me.id, followee_id=target.id).on_conflict_do_nothing()
    )


@router.delete("/users/{username}/follow", status_code=status.HTTP_204_NO_CONTENT)
async def unfollow(db: DB, me: Me, username: str) -> None:
    target = await user_by_name(db, username)
    await db.delete(Follows).where(
        and_(eq(Follows.follower_id, me.id), eq(Follows.followee_id, target.id))
    )


# -- posts ----------------------------------------------------------------------------


@router.post("/posts", status_code=status.HTTP_201_CREATED)
async def create_post(db: DB, me: Me, body: PostIn) -> PostOut:
    values = {**body.model_dump(exclude_unset=True), "author_id": me.id}
    [post] = await db.insert(Posts).values(values).returning()
    return PostOut.model_validate(post)


@router.get("/posts/{post_id}")
async def post_detail(db: DB, me: Me, post_id: int) -> PostDetail:
    await visible_post(db, me, post_id)
    post = await db.query(Posts).find_first(
        where=eq(Posts.id, post_id),
        with_={
            "author": True,
            "comments": {"order_by": Comments.id, "with_": {"author": True}},
        },
    )
    assert post is not None and post.author is not None
    likes = await counts(db, Likes, [post_id])
    return PostDetail(
        post=PostOut.model_validate(post),
        author=UserOut.model_validate(post.author),
        likes=likes.get(post_id, 0),
        comments=[
            CommentView(
                comment=CommentOut.model_validate(c), author=UserOut.model_validate(c.author)
            )
            for c in post.comments
        ],
    )


@router.delete("/posts/{post_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_post(db: DB, me: Me, post_id: int) -> None:
    deleted = await db.delete(Posts).where(and_(eq(Posts.id, post_id), eq(Posts.author_id, me.id)))
    if not deleted:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such post of yours")


@router.post("/posts/{post_id}/comments", status_code=status.HTTP_201_CREATED)
async def comment(db: DB, me: Me, post_id: int, body: CommentIn) -> CommentOut:
    await visible_post(db, me, post_id)
    values = {**body.model_dump(exclude_unset=True), "post_id": post_id, "author_id": me.id}
    [created] = await db.insert(Comments).values(values).returning()
    return CommentOut.model_validate(created)


@router.post("/posts/{post_id}/like", status_code=status.HTTP_204_NO_CONTENT)
async def like(db: DB, me: Me, post_id: int) -> None:
    await visible_post(db, me, post_id)
    await db.insert(Likes).values(user_id=me.id, post_id=post_id).on_conflict_do_nothing()


@router.delete("/posts/{post_id}/like", status_code=status.HTTP_204_NO_CONTENT)
async def unlike(db: DB, me: Me, post_id: int) -> None:
    await db.delete(Likes).where(and_(eq(Likes.user_id, me.id), eq(Likes.post_id, post_id)))


@router.put("/posts/{post_id}/bookmark", status_code=status.HTTP_204_NO_CONTENT)
async def bookmark(db: DB, me: Me, post_id: int, body: BookmarkIn) -> None:
    await visible_post(db, me, post_id)
    values = {**body.model_dump(exclude_unset=True), "user_id": me.id, "post_id": post_id}
    await (
        db.insert(Bookmarks)
        .values(values)
        .on_conflict_do_update(
            target=[Bookmarks.user_id, Bookmarks.post_id],
            set_={"note": excluded(Bookmarks.note)},
        )
    )


# -- feed -----------------------------------------------------------------------------


@router.get("/feed")
async def feed(
    db: DB,
    me: Me,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    before: Annotated[int | None, Query(description="post id to continue from")] = None,
) -> Feed:
    """My posts and posts I may see from people I follow, newest first."""
    followed = db.select(Follows.followee_id).from_(Follows).where(eq(Follows.follower_id, me.id))
    visible = or_(
        eq(Posts.author_id, me.id),
        and_(in_(Posts.author_id, followed), ne(Posts.visibility, Visibility.PRIVATE)),
    )
    posts = await db.query(Posts).find_many(
        where=and_(visible, lt(Posts.id, before) if before is not None else None),
        with_={"author": True},
        order_by=desc(Posts.id),
        limit=limit,
    )
    ids = [p.id for p in posts]
    likes, comments = await counts(db, Likes, ids), await counts(db, Comments, ids)
    items = [
        PostView(
            post=PostOut.model_validate(p),
            author=UserOut.model_validate(p.author),
            likes=likes.get(p.id, 0),
            comments=comments.get(p.id, 0),
        )
        for p in posts
    ]
    return Feed(items=items, next_before=ids[-1] if len(ids) == limit else None)


async def _unique_violation(_request: Request, exc: Exception) -> JSONResponse:
    constraint = exc.constraint if isinstance(exc, UniqueViolation) else None
    return JSONResponse({"detail": f"already exists ({constraint})"}, status_code=409)


async def _check_violation(_request: Request, exc: Exception) -> JSONResponse:
    constraint = exc.constraint if isinstance(exc, CheckViolation) else None
    return JSONResponse({"detail": f"invalid value ({constraint})"}, status_code=422)


async def _foreign_key_violation(_request: Request, _exc: Exception) -> JSONResponse:
    return JSONResponse({"detail": "a referenced row does not exist"}, status_code=404)


def create_app(database_url: str | None = None, *, migrate_on_start: bool | None = None) -> FastAPI:
    """Build the API. Defaults come from the environment / .env (see .env.example)."""
    load_dotenv(ROOT / ".env")
    url = database_url or os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("set DATABASE_URL (see .env.example)")
    run_migrations = (
        migrate_on_start
        if migrate_on_start is not None
        else os.environ.get("MIGRATE_ON_START") == "1"
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
        async with await Database.connect(url, schema=TABLES) as db:
            if run_migrations:  # advisory-locked: safe when many workers start at once
                await migrate(db, ROOT / "migrations")
            _state["db"] = db
            try:
                yield
            finally:
                del _state["db"]

    app = FastAPI(title="mizzle social example", lifespan=lifespan)
    app.add_exception_handler(UniqueViolation, _unique_violation)
    app.add_exception_handler(ForeignKeyViolation, _foreign_key_violation)
    app.add_exception_handler(CheckViolation, _check_violation)
    app.include_router(router)
    return app
