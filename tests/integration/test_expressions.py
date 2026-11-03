import datetime
from decimal import Decimal

from mizzle import (
    Database,
    abs_,
    alias,
    asc,
    case_,
    cast,
    coalesce,
    concat,
    count,
    current_date,
    date_trunc,
    dense_rank,
    desc,
    eq,
    exists,
    extract,
    first_value,
    func,
    greatest,
    gt,
    in_,
    is_null,
    lag,
    last_value,
    lead,
    least,
    length,
    lower,
    matches,
    max_,
    not_ilike,
    not_like,
    not_regex,
    now,
    nullif,
    phraseto_tsquery,
    plainto_tsquery,
    rank,
    regex,
    round_,
    row_number,
    rows_between,
    sum_,
    to_tsquery,
    to_tsvector,
    trim,
    ts_rank,
    upper,
    websearch_to_tsquery,
)
from mizzle.core.sql.nodes import Expr
from mizzle.pg import integer, numeric, pg_enum, text
from tests.models import Posts, Role, Users


async def seed(db: Database) -> None:
    await db.insert(Users).values(
        [
            {"email": "Ann@x", "bio": "cats and dogs", "role": Role.ADMIN},
            {"email": "bob@x", "bio": None},
            {"email": "cid@x", "bio": "  "},
        ]
    )
    await db.insert(Posts).values(
        [
            {"author_id": 1, "title": "The cats are sleeping"},
            {"author_id": 1, "title": "Dogs running fast"},
            {"author_id": 1, "title": "A cat and a dog"},
            {"author_id": 2, "title": "Nothing here"},
        ]
    )


async def test_case_coalesce_and_arithmetic(db: Database) -> None:
    await seed(db)
    kind = case_().when(eq(Users.role, Role.ADMIN), "admin").when(is_null(Users.bio), "quiet")
    rows = await (
        db.select(
            Users.id,
            kind,
            kind.else_("other"),
            coalesce(Users.bio, "-"),
            nullif(trim(coalesce(Users.bio, "")), ""),
            Users.id * 10 + 1,
            100 / (Users.id + 1),
            greatest(Users.id, 2),
            least(Users.id, 2),
            case_().when(gt(Users.id, 1), Users.role).else_(Role.ADMIN),
        )
        .from_(Users)
        .order_by(Users.id)
    )
    assert rows == [
        (1, "admin", "admin", "cats and dogs", "cats and dogs", 11, 50, 2, 1, Role.ADMIN),
        (2, "quiet", "quiet", "-", None, 21, 33, 2, 2, Role.USER),
        (3, None, "other", "  ", None, 31, 25, 3, 2, Role.USER),
    ]


async def test_expressions_in_where_group_by_having_order_by(db: Database) -> None:
    await seed(db)
    bio = coalesce(Users.bio, "none")
    assert await db.select(Users.id).from_(Users).where(eq(bio, "none")) == [(2,)]
    assert await db.select(Users.id).from_(Users).where(gt(Users.id * 2, 4)) == [(3,)]
    has_bio = case_().when(is_null(Users.bio), False).else_(True)
    groups = (
        await db.select(has_bio, count())
        .from_(Users)
        .group_by(has_bio)
        .having(gt(count(), 0))
        .order_by(desc(has_bio))
    )
    assert groups == [(True, 2), (False, 1)]
    ordered = await db.select(Users.id).from_(Users).order_by(desc(length(bio)), Users.id)
    assert ordered == [(1,), (2,), (3,)]


async def test_expressions_in_update_and_insert(db: Database) -> None:
    await seed(db)
    updated = (
        await db.update(Users)
        .set(email=concat(upper(Users.email), "!"), bio=coalesce(Users.bio, "new"))
        .where(eq(Users.id, 2))
        .returning()
    )
    assert (updated[0].email, updated[0].bio) == ("BOB@X!", "new")
    [post] = await db.insert(Posts).values(author_id=2, title=lower(concat("Hi ", 42))).returning()
    assert post.title == "hi 42"


async def test_functions(db: Database) -> None:
    await seed(db)
    [row] = await (
        db.select(
            lower(Users.email),
            upper(Users.email),
            length(Users.email),
            trim(coalesce(Users.bio, "")),
            concat(Users.email, "#", Users.id, Users.bio),
            abs_(Users.id - 5),
            round_(cast(Users.id, numeric().not_null()) / 3, 2),
            cast(Users.id, text()),
            cast(Users.role, text().not_null()),
            cast(cast(Users.role, text().not_null()), pg_enum(Role)),
            func[int]("char_length", Users.email),
            func[str]("repeat", Users.email, 2),
        )
        .from_(Users)
        .where(eq(Users.id, 1))
    )
    assert row == (
        "ann@x",
        "ANN@X",
        5,
        "cats and dogs",
        "Ann@x#1cats and dogs",
        4,
        Decimal("0.33"),
        "1",
        "admin",
        Role.ADMIN,
        5,
        "Ann@xAnn@x",
    )


async def test_date_functions(db: Database) -> None:
    await seed(db)
    created = datetime.datetime(2024, 5, 17, 13, 45, tzinfo=datetime.UTC)
    await db.update(Users).set(created_at=created).all_rows()
    month, year = date_trunc("month", Users.created_at), extract("year", Users.created_at)
    rows = await db.select(month, year, count()).from_(Users).group_by(month, year)
    assert rows == [(datetime.datetime(2024, 5, 1, tzinfo=datetime.UTC), Decimal(2024), 3)]
    [(today, at, day_of)] = (
        await db.select(current_date(), now(), extract("dow", now())).from_(Users).limit(1)
    )
    assert isinstance(today, datetime.date) and isinstance(at, datetime.datetime)
    assert 0 <= day_of <= 6
    [(days,)] = await db.select(date_trunc("day", current_date())).from_(Users).limit(1)
    assert days.date() == today


async def test_like_and_regex(db: Database) -> None:
    await seed(db)

    async def ids(condition: Expr[bool]) -> list[int]:
        rows = await db.select(Users.id).from_(Users).where(condition).order_by(Users.id)
        return [i for (i,) in rows]

    assert await ids(not_like(Users.email, "b%")) == [1, 3]
    assert await ids(not_ilike(Users.email, "a%")) == [2, 3]
    assert await ids(regex(Users.email, "^[ab]")) == [2]
    assert await ids(regex(Users.email, "^[ab]", ignore_case=True)) == [1, 2]
    assert await ids(not_regex(Users.email, "^[ab]", ignore_case=True)) == [3]
    assert await ids(not_regex(Users.bio, "dog")) == [3]  # NULL matches neither


async def test_window_functions(db: Database) -> None:
    await seed(db)
    rows = await (
        db.select(
            Posts.id,
            row_number().over(partition_by=Posts.author_id, order_by=desc(Posts.id)),
            rank().over(order_by=Posts.author_id),
            dense_rank().over(order_by=Posts.author_id),
            count().over(partition_by=Posts.author_id),
            sum_(Posts.id).over(order_by=Posts.id, frame=rows_between(-1, 0)),
            lag(Posts.id).over(order_by=Posts.id),
            lead(Posts.id, 2, 0).over(order_by=Posts.id),
            first_value(Posts.title).over(partition_by=Posts.author_id, order_by=Posts.id),
            last_value(Posts.id).over(order_by=Posts.id, frame=rows_between(None, None)),
            max_(Users.role).over(),
        )
        .from_(Posts)
        .join(Users, eq(Users.id, Posts.author_id))
        .order_by(Posts.id)
    )
    first = "The cats are sleeping"
    assert rows == [
        (1, 3, 1, 1, 3, 1, None, 3, first, 4, Role.USER),
        (2, 2, 1, 1, 3, 3, 1, 4, first, 4, Role.USER),
        (3, 1, 1, 1, 3, 5, 2, 0, first, 4, Role.USER),
        (4, 1, 4, 2, 1, 7, 3, 0, "Nothing here", 4, Role.USER),
    ]


async def test_window_function_in_a_cte(db: Database) -> None:
    await seed(db)
    # The newest post per author: number rows in a CTE, filter outside.
    n = row_number().over(partition_by=Posts.author_id, order_by=desc(Posts.id))
    numbered = db.select({"id": Posts.id, "n": n}).from_(Posts).cte("numbered")
    newest = (
        await db.with_(numbered)
        .select(numbered.c("id", int))
        .from_(numbered)
        .where(eq(numbered.c("n", int), 1))
        .order_by(numbered.c("id", int))
    )
    assert newest == [(3,), (4,)]


async def test_set_operations(db: Database) -> None:
    await seed(db)
    users = db.select(Users.id).from_(Users)
    authors = db.select(Posts.author_id).from_(Posts)
    assert await users.union(authors).order_by(Users.id) == [(1,), (2,), (3,)]
    assert len(await users.union_all(authors)) == 7
    assert await users.intersect(authors).order_by(desc(Users.id)) == [(2,), (1,)]
    assert len(await authors.intersect_all(authors)) == 4
    assert await users.except_(authors) == [(3,)]
    assert await authors.except_all(users).order_by(Posts.author_id) == [(1,), (1,)]
    page = users.union(authors).order_by(desc(Users.id)).limit(1).offset(1)
    assert await page == [(2,)]
    chained = users.except_(authors).union(db.select(Users.id).from_(Users).where(eq(Users.id, 1)))
    assert sorted(await chained) == [(1,), (3,)]
    # Whole rows decode as usual, and the result works as a subquery and a CTE.
    admins = db.select().from_(Users).where(eq(Users.role, Role.ADMIN))
    rows = await admins.union(db.select().from_(Users).where(eq(Users.id, 1)))
    assert [(u.id, u.role) for u in rows] == [(1, Role.ADMIN)]
    in_both = (
        await db.select(Users.email).from_(Users).where(in_(Users.id, users.intersect(authors)))
    )
    assert sorted(in_both) == [("Ann@x",), ("bob@x",)]
    ids = users.except_(authors).cte("lonely")
    assert await db.with_(ids).select(ids.c("id", int)).from_(ids) == [(3,)]


async def test_self_join_with_alias(db: Database) -> None:
    await seed(db)
    other = alias(Users, "other")
    rows = await (
        db.select(Users, other)
        .from_(Users)
        .left_join(other, eq(other.id, Users.id + 1))
        .order_by(Users.id)
    )
    assert [(u.id, o.id if o else None) for u, o in rows] == [(1, 2), (2, 3), (3, None)]
    assert all(type(u) is Users and (o is None or type(o) is Users) for u, o in rows)
    assert rows[0][1] == (await db.select().from_(Users).where(eq(Users.id, 2)))[0]

    # Same table twice in the FROM, a correlated subquery over an alias, and an alias source.
    older = await (
        db.select(Users.id, other.id)
        .from_(Users)
        .join(other, gt(other.id, Users.id))
        .where(eq(other.role, Role.USER))
        .order_by(Users.id, other.id)
    )
    assert older == [(1, 2), (1, 3), (2, 3)]
    later = db.select(other.id).from_(other).where(gt(other.id, Users.id))
    assert await db.select(Users.id).from_(Users).where(exists(later)).order_by(Users.id) == [
        (1,),
        (2,),
    ]
    assert len(await db.select().from_(other)) == 3


async def test_alias_in_relational_queries(db: Database) -> None:
    await seed(db)
    other = alias(Users, "other")
    later = db.select(other.id).from_(other).where(gt(other.id, Users.id))
    users = await db.query(Users).find_many(
        where=exists(later), with_={"posts": {"where": gt(length(Posts.title), 12)}}
    )
    assert [(u.id, [p.id for p in u.posts]) for u in users] == [(1, [1, 2, 3]), (2, [])]


async def test_full_text_search(db: Database) -> None:
    await seed(db)
    vector = to_tsvector(Posts.title, config="english")

    async def found(query: Expr[str]) -> list[int]:
        rows = (
            await db.select(Posts.id).from_(Posts).where(matches(vector, query)).order_by(Posts.id)
        )
        return [i for (i,) in rows]

    assert await found(to_tsquery("cat & !dog", config="english")) == [1]
    assert await found(plainto_tsquery("dogs", config="english")) == [2, 3]
    assert await found(phraseto_tsquery("running fast", config="english")) == [2]
    assert await found(websearch_to_tsquery('cat or "running fast"', config="english")) == [
        1,
        2,
        3,
    ]
    ranked = await (
        db.select(Posts.id, ts_rank(vector, to_tsquery("cat | dog", config="english")))
        .from_(Posts)
        .where(matches(vector, to_tsquery("cat | dog", config="english")))
        .order_by(desc(ts_rank(vector, to_tsquery("cat | dog", config="english"))), asc(Posts.id))
    )
    assert [i for i, _ in ranked] == [3, 1, 2]
    assert all(isinstance(r, float) and r > 0 for _, r in ranked)
    # Plain text matches too: text @@ tsquery converts with the default configuration.
    rel = await db.query(Posts).find_many(
        where=matches(to_tsvector(Posts.title), plainto_tsquery("nothing")), columns=[Posts.id]
    )
    assert [p.id for p in rel] == [4]


async def test_relational_queries_with_expressions(db: Database) -> None:
    await seed(db)
    users = await db.query(Users).find_many(
        where=eq(coalesce(Users.bio, "none"), "none"), order_by=desc(coalesce(Users.bio, "z"))
    )
    assert [u.id for u in users] == [2]
    ordered = await db.query(Users).find_many(
        order_by=[desc(length(coalesce(Users.bio, ""))), asc(Users.id)],
        with_={
            "posts": {
                "order_by": desc(case_().when(regex(Posts.title, "^D"), 1).else_(0)),
                "where": gt(Posts.id * 2, 2),
            }
        },
    )
    assert [(u.id, [p.id for p in u.posts]) for u in ordered] == [(1, [2, 3]), (3, []), (2, [4])]
    first = await db.query(Users).find_first(where=gt(length(Users.email) + Users.id, 7))
    assert first is not None and first.id == 3


async def test_cast_of_columns_and_enum_decoding(db: Database) -> None:
    await seed(db)
    rows = await (
        db.select(
            coalesce(max_(Users.role), Role.USER),
            cast(count(), integer().not_null()),
            nullif(max_(Users.role), Role.ADMIN),
        )
        .from_(Users)
        .where(eq(Users.id, 1))
    )
    assert rows == [(Role.ADMIN, 1, None)]
