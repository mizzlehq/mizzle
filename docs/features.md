# All features

Every feature in one page, each with a short example. The links go to the full guide. The
examples use `Users` and `Posts` from [Getting started](getting-started.md).

## Schema

**Tables and columns.** A table is a class; columns come from `mizzle.pg`.
[More](guide/schema.md#column-types)

```python
@final
class Users(Table, name="users"):
    id = serial().primary_key()
    email = varchar(255).not_null().unique()
    bio = text()
```

**Defaults and on_update.** Database defaults, Python defaults, and values set on every
update. [More](guide/schema.md#nullability-defaults-and-on_update)

```python
views = integer().not_null().default(0)
updated_at = timestamp(tz=True).not_null().default(sql("now()")).on_update_now()
```

**Keys, unique and check constraints.** [More](guide/schema.md#primary-keys-unique-and-check)

```python
weight = integer().not_null().check(sql("weight > 0"))
# in config(): primary_key(cls.a, cls.b), unique(cls.x, cls.y), check("name", sql(...))
```

**Foreign keys.** Single or composite, with actions and deferrable.
[More](guide/schema.md#foreign-keys)

```python
author_id = integer().not_null().references(lambda: Users.id, on_delete="cascade")
```

**Indexes.** Expression, partial, unique, gin/gist/hnsw with opclasses, storage options.
[More](guide/schema.md#indexes)

```python
index("users_lower_email").on(lower(cls.email)).where(eq(cls.deleted, False))
```

**Enums.** A Python `Enum` becomes a PostgreSQL enum. [More](guide/schema.md#enums)

```python
status = pg_enum(Status).not_null().default(Status.DRAFT)
```

**Columns of other types.** Domains, composites, ranges, `citext` and any extension type,
with `decode=` / `encode=`. [More](guide/schema.md#columns-of-other-types)

```python
nights = custom("int4range")
qty = custom("posint", py_type=int)
```

**Identity and generated columns.** [More](guide/schema.md#identity-and-generated-columns)

```python
id = integer().primary_key().generated_always_as_identity()
total = numeric(12, 2).generated_always_as(sql("qty * unit_price"))
```

**Schemas.** Tables in any PostgreSQL schema. [More](guide/schema.md#schemas)

```python
class AuditLog(Table, name="audit_log", schema="archive"): ...
```

**Relations.** `one` and `many`, for relational queries. [More](guide/schema.md#relations)

```python
posts = many(lambda: Posts)
author = one(lambda: Users, fields=lambda: [Posts.author_id], references=lambda: [Users.id])
```

**Views and materialized views.** [More](guide/migrations.md#views)

```python
class PostCounts(View, name="post_counts"): ...  # columns + a query
await db.refresh(DailyPosts, concurrently=True)
```

**Row-level security, policies and roles.** [More](guide/migrations.md#row-level-security-and-policies)

```python
rls(), policy("own_posts", using=eq(cls.author_id, sql("current_setting('app.user_id')::int")))
```

**Table options.** Comments, unlogged tables, storage parameters.

```python
class Org(Table, name="org", comment="customers", unlogged=False, storage={"fillfactor": 70}): ...
```

## Queries

**Select.** Whole rows, chosen columns as tuples, or named columns as dicts.
[More](guide/queries.md#select)

```python
users = await db.select().from_(Users)
pairs = await db.select(Users.id, Users.email).from_(Users)
```

**Where.** `eq`, `ne`, `gt`, `in_`, `between`, `like`, `ilike`, `is_null`, `and_`, `or_`,
`not_`... [More](guide/queries.md#where)

```python
await db.select().from_(Posts).where(and_(gt(Posts.views, 10), ilike(Posts.title, "%hi%")))
```

**Joins.** Inner, left, right, full; the optional side is typed `| None`.
[More](guide/queries.md#joins)

```python
await db.select(Users, Posts).from_(Users).left_join(Posts, eq(Posts.author_id, Users.id))
```

**Order, limit, offset.** [More](guide/queries.md#order-limit-offset)

```python
await db.select().from_(Posts).order_by(desc(Posts.views, nulls="last")).limit(20).offset(40)
```

**Group by and aggregates.** `count`, `sum_`, `avg`, `min_`, `max_`, `having`.
[More](guide/queries.md#group-by-and-aggregates)

```python
await db.select(Posts.author_id, count()).from_(Posts).group_by(Posts.author_id)
```

**Expressions and functions.** Arithmetic, string functions, `coalesce`, `cast`, any SQL
function with `func`. [More](guide/queries.md#expressions-and-functions)

```python
await db.select(lower(Users.email), coalesce(Users.bio, "")).from_(Users)
```

**CASE.** [More](guide/queries.md#case)

```python
case_().when(gt(Posts.views, 100), "hot").else_("cold")
```

**Dates and times.** `now`, `date_trunc`, `extract`, timedelta arithmetic.
[More](guide/queries.md#dates-and-times)

```python
await db.select(date_trunc("month", Events.starts_at)).from_(Events)
```

**Subqueries.** `in_`, `exists`, and subqueries in `FROM`. [More](guide/queries.md#subqueries)

```python
await db.select().from_(Users).where(in_(Users.id, db.select(Posts.author_id).from_(Posts)))
```

**CTEs, also recursive.** [More](guide/queries.md#ctes)

```python
ranked = db.select({"id": Posts.id}).from_(Posts).cte("ranked")
await db.with_(ranked).select(ranked.c("id", int)).from_(ranked)
```

**Window functions.** `row_number`, `rank`, `lag`, `lead`, running totals.
[More](guide/queries.md#window-functions)

```python
row_number().over(partition_by=Posts.author_id, order_by=desc(Posts.views))
```

**Union, intersect, except.** [More](guide/queries.md#union-intersect-except)

```python
await staff.union(writers).order_by(Users.id)
```

**Aliases.** The same table twice in one query. [More](guide/queries.md#aliases)

```python
manager = alias(Users, "manager")
```

**Full-text search.** [More](guide/queries.md#full-text-search)

```python
doc = to_tsvector(Posts.title, config="english")
await db.select().from_(Posts).where(matches(doc, websearch_to_tsquery("cats -dogs")))
```

**Raw SQL.** Values are always parameters. [More](guide/queries.md#raw-sql)

```python
await db.execute(sql("SELECT id FROM users WHERE id = {}", user_id))
```

**Explain, prepared queries, SQL text.** [More](guide/queries.md#explain-a-query)

```python
by_role = db.select().from_(Users).where(eq(Users.role, placeholder("role"))).prepare()
await by_role.execute(role=Role.ADMIN)
```

## Writes

**Insert, with returning.** [More](guide/queries.md#insert)

```python
[ann] = await db.insert(Users).values(email="ann@example.com").returning()
```

**Upserts.** `on_conflict_do_nothing` and `on_conflict_do_update` with `excluded()`.
[More](guide/queries.md#upserts)

```python
await db.insert(Users).values(email="a@x.io", name="A").on_conflict_do_update(
    target=Users.email, set_={"name": excluded(Users.name)}
)
```

**Update and delete.** A `where` is required; `.all_rows()` to touch every row. Also
`UPDATE ... FROM` and `DELETE ... USING`. [More](guide/queries.md#update-and-delete)

```python
await db.update(Users).set(name="Ann").where(eq(Users.id, 1))
```

**MERGE.** `when_matched_then_update`, `when_not_matched_then_insert`...

```python
await db.merge(Users).using(Imports).on(eq(Users.email, Imports.email)).when_matched_then_update(
    {"name": Imports.name}
).when_not_matched_then_insert({"email": Imports.email, "name": Imports.name})
```

**Bulk insert.** Many rows in batches, one transaction. [More](guide/bulk.md#insert_many)

```python
await db.insert_many(Users, rows, batch_size=1000)
```

**Streaming.** Large results from a server-side cursor. [More](guide/bulk.md#stream)

```python
async for user in db.select().from_(Users).stream(batch_size=1000): ...
```

## Relational queries

**find_many / find_first with nested relations.** Filters, order, limit and columns at
every level, in one SQL statement. [More](guide/relational.md)

```python
users = await db.query(Users).find_many(
    with_={"posts": {"where": gt(Posts.views, 10), "order_by": desc(Posts.views), "limit": 5}}
)
```

## Transactions and errors

**Transactions and savepoints.** [More](guide/transactions.md#dbtransaction)

```python
async with db.transaction() as tx:
    async with tx.transaction() as sp:  # SAVEPOINT
        ...
```

**Isolation levels, read-only, after-commit hooks.** [More](guide/transactions.md#isolation-levels-and-read-only)

```python
async with db.transaction("serializable", readonly=True) as tx: ...
```

**Retry on serialization failure.** [More](guide/transactions.md#retrying-serialization-failures)

```python
await db.retry(transfer, attempts=5)
```

**Row locks.** `for_update`, `skip_locked`, `nowait`. [More](guide/transactions.md#row-locks)

```python
jobs = await tx.select().from_(Jobs).limit(10).for_update(skip_locked=True)
```

**Timeouts.** Per database or per query. [More](guide/transactions.md#timeouts)

```python
await db.with_timeout(0.5).execute(sql("SELECT pg_sleep(2)"))  # raises QueryTimeout
```

**Typed errors.** `UniqueViolation`, `ForeignKeyViolation`, `CheckViolation`,
`QueryTimeout`... [More](guide/transactions.md#errors)

```python
try:
    await db.insert(Users).values(email="ann@example.com")
except UniqueViolation:
    ...
```

## Migrations

**generate and migrate.** SQL migrations from your schema, applied once even with many
servers starting at the same time. [More](guide/migrations.md#the-loop)

```bash
mizzle generate --name init
mizzle migrate
```

**Renames and data-loss checks.** [More](guide/migrations.md#renames)

```bash
mizzle generate --rename users.name=full_name
mizzle generate --allow-data-loss
```

**Custom migrations.** Your own SQL, in the same history. [More](guide/migrations.md#custom-migrations)

```bash
mizzle generate --custom --name backfill
```

**push for development databases.** [More](guide/migrations.md#push-for-development-databases)

```bash
mizzle push --dry-run
```

**pull from an existing database.** Writes the schema module and a baseline.
[More](guide/migrations.md#mizzle-pull)

```bash
mizzle pull
```

**check, status, doctor, drop, reset, squash, seed.** [More](cli.md)

```bash
mizzle check    # in CI: folder and journal agree
mizzle doctor   # drift between the database and the migrations
```

## Integrations

**Pydantic models from tables.** Insert, update and select models. [More](guide/integrations.md#pydantic-models)

```python
UserIn, UserOut = insert_model(Users), select_model(Users)
await db.insert(Users).values(UserIn.model_validate_json(body))
```

**Supabase.** `auth.uid()`, roles and ready-made policies. [More](guide/integrations.md#supabase)

```python
*crud_policy("notes_owner", to=[AUTHENTICATED], read=eq(cls.owner_id, auth_uid()),
             modify=eq(cls.owner_id, auth_uid()))
```

**pgvector.** `vector`, `halfvec`, `sparsevec`, distances and hnsw / ivfflat indexes.
[More](guide/extensions.md)

```python
await db.select().from_(Items).order_by(cosine_distance(Items.embedding, query)).limit(5)
```

**mizzle studio.** A data browser in the browser, with a read-only mode. [More](guide/integrations.md#mizzle-studio)

```bash
mizzle studio --read-only
```

**typegen.** TypedDicts for rows, inserts and updates. [More](guide/integrations.md#typegen)

```bash
mizzle typegen --out app/types.py
```

## Production

**Read replicas, pooling, PgBouncer, timeouts.** [More](production.md)

```python
db = await Database.connect(url, replicas=[replica_url], pool_size=20, query_timeout=5)
await db.primary.select().from_(Users)  # read your own write
```

For what mizzle does not do, see [Known gaps](gaps.md).
