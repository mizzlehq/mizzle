# Relational queries

`db.query(Table)` loads rows together with their related rows, as nested objects, in one
SQL statement. It follows the `one()` and `many()` relations of your tables (see
[Schema](schema.md#relations)).

The examples on this page use these tables:

```python
from contextlib import aclosing
from typing import final

from mizzle import (
    QueryError, RelationNotLoaded, Table, alias, desc, eq, exists, gt, ilike, many, one,
)
from mizzle.pg import integer, serial, text


@final
class Users(Table, name="users"):
    id = serial().primary_key()
    name = text().not_null()
    posts = many(lambda: Posts)
    profile = one(lambda: Profiles)


@final
class Profiles(Table, name="profiles"):
    user_id = integer().primary_key().references(lambda: Users.id)
    bio = text()
    user = one(lambda: Users, fields=lambda: [Profiles.user_id], references=lambda: [Users.id])


@final
class Posts(Table, name="posts"):
    id = serial().primary_key()
    author_id = integer().not_null().references(lambda: Users.id)
    title = text().not_null()
    views = integer().not_null().default(0)
    author = one(lambda: Users, fields=lambda: [Posts.author_id], references=lambda: [Users.id])
    comments = many(lambda: Comments)


@final
class Comments(Table, name="comments"):
    id = serial().primary_key()
    post_id = integer().not_null().references(lambda: Posts.id)
    author_id = integer().not_null().references(lambda: Users.id)
    body = text().not_null()
    post = one(lambda: Posts, fields=lambda: [Comments.post_id], references=lambda: [Posts.id])
    author = one(
        lambda: Users, fields=lambda: [Comments.author_id], references=lambda: [Users.id]
    )
```

## find_many and find_first

```python
users = await db.query(Users).find_many(with_={"posts": True})
for user in users:
    print(user.name, [post.title for post in user.posts])  # user.posts: list[Posts]
```

`find_many` returns a list of rows. `find_first` returns one row or `None`:

```python
post = await db.query(Posts).find_first(
    where=eq(Posts.id, 1), with_={"author": True, "comments": True}
)  # Posts | None
if post is not None and post.author is not None:  # post.author: Users | None
    print(post.author.name, len(post.comments))
```

Both take `columns`, `where`, `order_by` and `with_`; `find_many` also takes `limit` and
`offset`:

```python
top = await db.query(Users).find_many(
    columns=[Users.id, Users.name],
    where=gt(Users.id, 0),
    order_by=desc(Users.id),
    limit=10,
    offset=0,
)
```

`where` and `order_by` take the same conditions and orderings as
[select queries](queries.md#where).

## Loading relations with with_

Each key of `with_` is a relation name. `True` loads it with all columns. A dict loads it
with options: `columns`, `where`, `order_by`, `limit`, and its own `with_` for the next
level.

```python
feed = await db.query(Users).find_many(
    with_={
        "profile": True,
        "posts": {
            "columns": [Posts.id, Posts.title],
            "where": gt(Posts.views, 10),
            "order_by": desc(Posts.views),
            "limit": 5,
            "with_": {
                "comments": {
                    "order_by": Comments.id,
                    "with_": {"author": True},
                },
            },
        },
    },
    order_by=Users.id,
)
ann = feed[0]
first = ann.posts[0]
print(first.title, [(c.body, c.author.name if c.author else None) for c in first.comments])
```

- A `many()` relation comes back as a list, empty when there are no related rows.
- A `one()` relation comes back as the row or `None`.
- `limit` and `order_by` apply per parent row: "the 5 most viewed posts of each user".
- `limit` and `order_by` need a `many()` relation. On a `one()` they raise `QueryError`:

```python
try:
    await db.query(Posts).find_many(with_={"author": {"limit": 1}})
except QueryError as error:
    print(error)  # Posts.author is a one() relation; limit/order_by need many()
```

To filter parents by their children, use `exists()` in the top-level `where`:

```python
commented = db.select(Comments.id).from_(Comments).where(eq(Comments.post_id, Posts.id))
discussed = await db.query(Posts).find_many(
    where=exists(commented), with_={"comments": True}, order_by=Posts.id
)
```

## Loading some columns

`columns=` loads only those columns. Reading another one raises `AttributeError`:

```python
names = await db.query(Users).find_many(columns=[Users.name])
print(names[0].name)
try:
    print(names[0].id)
except AttributeError as error:
    print(error)  # Users.id was not selected by this query
```

## RelationNotLoaded

A relation is only on the row when the query asked for it. Reading one that was not
loaded raises `RelationNotLoaded` (no lazy loading, so no hidden queries):

```python
plain = await db.query(Users).find_first(where=eq(Users.id, 1))
assert plain is not None
try:
    print(plain.posts)
except RelationNotLoaded as error:
    print(error)  # Users.posts was not loaded; add 'posts' to with_
```

## Aliases

`db.query()` also takes a table alias. Use the alias's columns in `where`:

```python
u = alias(Users, "u")
matching = await db.query(u).find_many(
    where=ilike(u.name, "a%"), with_={"posts": True}
)  # list[Users]
```

Rows of an alias are instances of the table's own class.

## Streaming

For many rows, `.stream()` reads them in batches from a server-side cursor instead of
loading all at once:

```python
async with aclosing(db.query(Users).find_many(with_={"posts": True}).stream()) as rows:
    async for user in rows:
        print(user.name, len(user.posts))
```

`aclosing` (from `contextlib`) releases the connection right away if you break out of the
loop early. See [Bulk and streaming](bulk.md).

## Typing

- `find_many` returns `list[T]` and `find_first` returns `T | None`, where `T` is your
  table class.
- Columns have their declared types: `post.title` is `str`.
- `many()` relations are typed `list[Target]` and `one()` relations `Target | None`.
  The type does not change with `with_`: your editor accepts `user.posts` either way, and
  an unloaded relation raises `RelationNotLoaded` when read.
- `with_` dicts are checked: an option name that does not exist is a type error. A
  relation name that does not exist raises `QueryError`, listing the available ones.

Relational queries work the same in a transaction:

```python
async with db.transaction() as tx:
    mine = await tx.query(Posts).find_many(where=eq(Posts.author_id, 1))
```
