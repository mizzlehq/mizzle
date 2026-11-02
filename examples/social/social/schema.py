"""A small social media schema: users, follows, posts, comments and likes."""

import enum
from typing import Any, final, override

from mizzle import Table, check, index, many, one, primary_key, sql
from mizzle.pg import integer, jsonb, pg_enum, serial, text, timestamp, varchar


class Visibility(enum.StrEnum):
    PUBLIC = "public"
    FOLLOWERS = "followers"
    PRIVATE = "private"


@final
class Users(Table, name="users"):
    id = serial().primary_key()
    username = varchar(32).not_null().unique()
    email = varchar(255).not_null().unique()
    display_name = text().not_null()
    bio = text()
    created_at = timestamp(tz=True).not_null().default(sql("now()"))

    posts = many(lambda: Posts)
    comments = many(lambda: Comments)

    @classmethod
    @override
    def config(cls) -> list[Any]:
        return [check("users_username_format", sql("username ~ '^[a-z0-9_]{3,32}$'"))]


@final
class Follows(Table, name="follows"):
    follower_id = integer().not_null().references(lambda: Users.id, on_delete="cascade")
    followee_id = integer().not_null().references(lambda: Users.id, on_delete="cascade")
    created_at = timestamp(tz=True).not_null().default(sql("now()"))

    @classmethod
    @override
    def config(cls) -> list[Any]:
        return [
            primary_key(cls.follower_id, cls.followee_id),
            check("follows_not_self", sql("follower_id <> followee_id")),
            index("follows_followee_idx").on(cls.followee_id),
        ]


@final
class Posts(Table, name="posts"):
    id = serial().primary_key()
    author_id = integer().not_null().references(lambda: Users.id, on_delete="cascade")
    body = text().not_null()
    visibility = pg_enum(Visibility).not_null().default(Visibility.PUBLIC)
    meta = jsonb[dict[str, Any]]()
    created_at = timestamp(tz=True).not_null().default(sql("now()"))

    author = one(lambda: Users, fields=lambda: [Posts.author_id], references=lambda: [Users.id])
    comments = many(lambda: Comments)
    likes = many(lambda: Likes)

    @classmethod
    @override
    def config(cls) -> list[Any]:
        return [
            index("posts_author_created_idx").on(cls.author_id, cls.created_at),
            check("posts_body_not_empty", sql("length(body) > 0")),
        ]


@final
class Comments(Table, name="comments"):
    id = serial().primary_key()
    post_id = integer().not_null().references(lambda: Posts.id, on_delete="cascade")
    author_id = integer().not_null().references(lambda: Users.id, on_delete="cascade")
    body = text().not_null()
    created_at = timestamp(tz=True).not_null().default(sql("now()"))

    post = one(lambda: Posts, fields=lambda: [Comments.post_id], references=lambda: [Posts.id])
    author = one(lambda: Users, fields=lambda: [Comments.author_id], references=lambda: [Users.id])

    @classmethod
    @override
    def config(cls) -> list[Any]:
        return [index("comments_post_idx").on(cls.post_id)]


@final
class Likes(Table, name="likes"):
    user_id = integer().not_null().references(lambda: Users.id, on_delete="cascade")
    post_id = integer().not_null().references(lambda: Posts.id, on_delete="cascade")
    created_at = timestamp(tz=True).not_null().default(sql("now()"))

    post = one(lambda: Posts, fields=lambda: [Likes.post_id], references=lambda: [Posts.id])

    @classmethod
    @override
    def config(cls) -> list[Any]:
        return [primary_key(cls.user_id, cls.post_id), index("likes_post_idx").on(cls.post_id)]


@final
class Bookmarks(Table, name="bookmarks"):
    """Posts a user saved for later, optionally with a private note."""

    user_id = integer().not_null().references(lambda: Users.id, on_delete="cascade")
    post_id = integer().not_null().references(lambda: Posts.id, on_delete="cascade")
    note = text()
    created_at = timestamp(tz=True).not_null().default(sql("now()"))

    post = one(lambda: Posts, fields=lambda: [Bookmarks.post_id], references=lambda: [Posts.id])

    @classmethod
    @override
    def config(cls) -> list[Any]:
        return [
            primary_key(cls.user_id, cls.post_id),
            index("bookmarks_post_idx").on(cls.post_id),
        ]
