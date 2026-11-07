# Extensions

Any PostgreSQL extension can be part of your schema: `extension("pg_trgm")` in your schema
module makes migrations run `CREATE EXTENSION IF NOT EXISTS`. See
[Migrations](migrations.md#extensions).

This page covers [pgvector](https://github.com/pgvector/pgvector), which mizzle supports
with column types and operators. Full-text search (`tsvector`, `to_tsvector`,
`websearch_to_tsquery`, `ts_rank`...) is built into PostgreSQL; see
[Queries](queries.md#full-text-search).

## pgvector columns

```python
from typing import final, override

from mizzle import Constraint, Table, index
from mizzle.pg import halfvec, serial, sparsevec, text, vector


@final
class Items(Table, name="vecdoc_items"):
    id = serial().primary_key()
    title = text().not_null()
    embedding = vector(3).not_null()
    half = halfvec(3)
    keywords = sparsevec(5)

    @classmethod
    @override
    def config(cls) -> list[Constraint]:
        return [
            index("vecdoc_items_embedding_idx")
            .using("hnsw", "vector_cosine_ops")
            .on(cls.embedding)
            .with_(m=16, ef_construction=64),
            index("vecdoc_items_half_idx").using("hnsw", "halfvec_l2_ops").on(cls.half),
            index("vecdoc_items_ivf_idx")
            .using("ivfflat", "vector_l2_ops")
            .on(cls.embedding)
            .with_(lists=100),
        ]
```

| Type | Holds | Python value |
|---|---|---|
| `vector(n)` | `n` float4 values | `list[float]` |
| `halfvec(n)` | `n` half-precision values | `list[float]` |
| `sparsevec(n)` | mostly-zero vectors | `dict[int, float]`, indices from 0 to n - 1 |

You do not need `extension("vector")`: a vector column makes the migration create it.
`mizzle generate` writes:

```sql
-- create extension vector
-- create table vecdoc_items
-- create index vecdoc_items_embedding_idx
-- create index vecdoc_items_half_idx
-- create index vecdoc_items_ivf_idx
CREATE EXTENSION IF NOT EXISTS vector;
...
CREATE INDEX "vecdoc_items_embedding_idx" ON "vecdoc_items" USING hnsw ("embedding" vector_cosine_ops) WITH (m = '16', ef_construction = '64');
```

The server needs pgvector installed.

### Types from other extensions

Only pgvector's types are detected from a column: `vector`, `halfvec` and `sparsevec` make
the migration create `vector` for you. The other extension types are plain names to
PostgreSQL, so declare their extension yourself:

```python
from mizzle import extension
from mizzle.pg import citext, hstore, ltree

exts = [extension("hstore"), extension("citext"), extension("ltree")]


@final
class Document(Table):
    labels = hstore()
    slug = citext()
    path = ltree()
```

Without the `extension(...)` entry the migration fails with
`type "hstore" does not exist` (and so on). `pull` reads an existing extension back into the
schema module as an `extension(...)`, so an adopted database keeps working.

## Writing and reading vectors

```python
await db.insert(Items).values(
    [
        {"title": "red", "embedding": [1.0, 0.0, 0.0], "keywords": {0: 1.5, 3: 2.0}},
        {"title": "green", "embedding": [0.0, 1.0, 0.0], "half": [0.0, 1.0, 0.0]},
        {"title": "dark red", "embedding": [0.9, 0.1, 0.0], "keywords": {0: 1.0}},
    ]
)
[red] = await db.select().from_(Items).limit(1)
print(red.embedding, red.keywords)  # [1.0, 0.0, 0.0] {0: 1.5, 3: 2.0}
```

Write a list, a tuple, or any object with `tolist()` (a numpy array). Reads give lists.
A sparse vector does not store zeros, so `{0: 0.0}` reads back as `{}`.

## Distances

| Function | pgvector | Meaning |
|---|---|---|
| `l2_distance(a, b)` | `<->` | Euclidean distance |
| `cosine_distance(a, b)` | `<=>` | 1 - cosine similarity |
| `inner_product(a, b)` | `<#>` | the **negative** inner product |

Each side is a column or a Python vector. All three return `Expr[float]`, and smaller is
closer, so order ascending for nearest first:

```python
query = [1.0, 0.05, 0.0]
nearest = (
    await db.select()
    .from_(Items)
    .order_by(cosine_distance(Items.embedding, query))
    .limit(2)
)
print([item.title for item in nearest])  # ['red', 'dark red']
```

Select a distance, or filter on it:

```python
distance = l2_distance(Items.embedding, query)
rows = await (
    db.select(Items.title, distance).from_(Items).where(lt(distance, 0.5)).order_by(distance)
)
for title, dist in rows:  # tuple[str, float]
    print(title, round(dist, 3))  # red 0.05, dark red 0.112
```

`inner_product` is negative, as in pgvector, so that ascending order is most similar
first and indexes apply. Negate it for the inner product itself:

```python
best = (
    await db.select(Items.title, inner_product(Items.embedding, query))
    .from_(Items)
    .order_by(inner_product(Items.embedding, query))  # ascending: most similar first
    .limit(1)
)
[(title, negative)] = best
print(title, -negative)  # red 1.0: negate for the inner product itself
```

`halfvec` and `sparsevec` columns take the same functions. A `{index: value}` operand
needs a `sparsevec` column on the other side:

```python
by_half = await db.select().from_(Items).order_by(l2_distance(Items.half, [0.0, 1.0, 0.0]))
by_keywords = (
    await db.select().from_(Items).order_by(inner_product(Items.keywords, {0: 1.0}))
)
```

## Indexes

`index(name).using(method, opclass).on(column)`, with storage options in `.with_()`:

- **HNSW**: `.using("hnsw", "vector_cosine_ops")`, options `m` and `ef_construction`.
- **IVFFlat**: `.using("ivfflat", "vector_l2_ops")`, option `lists`.

Pick the operator class that matches the distance you order by: `vector_l2_ops` for
`l2_distance`, `vector_cosine_ops` for `cosine_distance`, `vector_ip_ops` for
`inner_product` (`halfvec_*_ops` and `sparsevec_*_ops` for the other types). PostgreSQL
uses the index for `ORDER BY distance LIMIT n`.

Query-time settings such as `hnsw.ef_search` are session settings. Set them for one
transaction:

```python
async with db.transaction() as tx:
    await tx.execute(sql("SET LOCAL hnsw.ef_search = 100"))
    top = (
        await tx.select()
        .from_(Items)
        .order_by(cosine_distance(Items.embedding, query))
        .limit(10)
    )
```

## Notes

- `copy_into` loads tables with vector columns through `insert_many`, since vectors have no
  binary COPY format here. See [Bulk and streaming](bulk.md).
- `mizzle.pydantic` models check a vector's length: `vector(3)` accepts exactly 3 floats.
  See [Integrations](integrations.md#pydantic-models).
- [examples/shop](https://github.com/mizzlehq/mizzle/tree/main/examples/shop) keeps its
  vector tables in a second schema module and migrations folder (`mizzle.vector.toml`), so
  servers without pgvector run the rest.
