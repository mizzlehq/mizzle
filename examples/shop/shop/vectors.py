"""Product embeddings for similar-product search. Needs pgvector: only servers that have it
run ``migrations_vector`` (mizzle.vector.toml); the app checks at startup."""

from typing import final, override

from mizzle import Constraint, Table, index, one
from mizzle.pg import bigint, halfvec, sparsevec, vector
from shop.schema import Products

DIMENSIONS = 3  # tiny on purpose: tests compute distances by hand


@final
class ProductEmbeddings(Table, name="product_embeddings"):
    product_id = bigint().primary_key().references(lambda: Products.id, on_delete="cascade")
    embedding = vector(DIMENSIONS).not_null()
    half = halfvec(DIMENSIONS)
    keywords = sparsevec(8)

    product = one(
        lambda: Products,
        fields=lambda: [ProductEmbeddings.product_id],
        references=lambda: [Products.id],
    )

    @classmethod
    @override
    def config(cls) -> list[Constraint]:
        return [
            index("product_embeddings_hnsw")
            .using("hnsw", "vector_cosine_ops")
            .on(cls.embedding)
            .with_(m=16, ef_construction=64),
            index("product_embeddings_half_hnsw").using("hnsw", "halfvec_l2_ops").on(cls.half),
        ]
