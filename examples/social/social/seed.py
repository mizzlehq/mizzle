"""Fill the dev database with a small, repeatable social network.

uv run python -m social.seed
"""

import asyncio
import datetime
import os
import random
from pathlib import Path

from mizzle import Database, count, desc
from mizzle.kit.config import load_dotenv
from social.schema import Comments, Follows, Likes, Posts, Users, Visibility

FIRST = [
    "ana",
    "ben",
    "cleo",
    "dev",
    "eli",
    "fay",
    "gus",
    "hana",
    "ivan",
    "jade",
    "kai",
    "lena",
    "milo",
    "nora",
    "omar",
    "pia",
    "quin",
    "rosa",
    "sam",
    "tara",
    "uma",
    "vik",
    "wren",
    "yara",
]
TOPICS = [
    "typed SQL",
    "Postgres enums",
    "async Python",
    "migrations",
    "coffee",
    "hiking",
    "film photography",
    "sourdough",
    "mechanical keyboards",
    "open source",
    "rust vs go",
]
OPENERS = ["Hot take:", "TIL", "Thread:", "Unpopular opinion:", "Weekend project:", "Reminder:"]
REPLIES = [
    "So true.",
    "Hard disagree.",
    "Saving this.",
    "Source?",
    "This made my day.",
    "Wait, really?",
    "Same here!",
    "Great write-up, thanks.",
    "Following for updates.",
]


async def main() -> None:
    load_dotenv(Path(".env"))
    rng = random.Random(42)  # same data every run
    now = datetime.datetime.now(datetime.UTC)

    def ago(max_days: int) -> datetime.datetime:
        return now - datetime.timedelta(minutes=rng.randint(0, max_days * 24 * 60))

    tables = [Users, Follows, Posts, Comments, Likes]
    async with await Database.connect(os.environ["DATABASE_URL"], schema=tables) as db:
        async with db.transaction() as tx:
            await tx.delete(Users).all_rows()  # cascades to everything else

            users = (
                await tx.insert(Users)
                .values(
                    [
                        {
                            "username": f"{name}_{rng.randint(10, 99)}",
                            "email": f"{name}@example.com",
                            "display_name": name.capitalize(),
                            "bio": rng.choice([None, f"Into {rng.choice(TOPICS)}."]),
                            "created_at": ago(120),
                        }
                        for name in FIRST
                    ]
                )
                .returning()
            )

            follows = {(a.id, b.id) for a in users for b in rng.sample(users, 6) if a.id != b.id}
            await tx.insert(Follows).values(
                [{"follower_id": a, "followee_id": b, "created_at": ago(90)} for a, b in follows]
            )

            posts = (
                await tx.insert(Posts)
                .values(
                    [
                        {
                            "author_id": rng.choice(users).id,
                            "body": f"{rng.choice(OPENERS)} {rng.choice(TOPICS)} is underrated.",
                            "visibility": rng.choices(list(Visibility), weights=[8, 3, 1])[0],
                            "meta": {"tags": rng.sample(TOPICS, 2)},
                            "created_at": ago(60),
                        }
                        for _ in range(80)
                    ]
                )
                .returning()
            )

            await tx.insert(Comments).values(
                [
                    {
                        "post_id": post.id,
                        "author_id": rng.choice(users).id,
                        "body": rng.choice(REPLIES),
                        "created_at": post.created_at
                        + datetime.timedelta(minutes=rng.randint(1, 600)),
                    }
                    for post in posts
                    for _ in range(rng.randint(0, 5))
                ]
            )

            likes = {(rng.choice(users).id, rng.choice(posts).id) for _ in range(400)}
            await tx.insert(Likes).values([{"user_id": u, "post_id": p} for u, p in likes])

        # Show what we made, using a relational query.
        totals = {t.__name__: (await db.select(count()).from_(t))[0][0] for t in tables}
        print("seeded:", ", ".join(f"{n} {name.lower()}" for name, n in totals.items()))
        top = await db.query(Posts).find_many(
            with_={"author": True, "comments": {"limit": 2, "with_": {"author": True}}},
            order_by=desc(Posts.created_at),
            limit=3,
        )
        for post in top:
            author = post.author.username if post.author else "?"
            print(f"\n@{author}: {post.body}  [{post.visibility}]")
            for comment in post.comments:
                print(f"   ↳ @{comment.author.username if comment.author else '?'}: {comment.body}")


if __name__ == "__main__":
    asyncio.run(main())
