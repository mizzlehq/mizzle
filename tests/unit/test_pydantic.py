from decimal import Decimal
from typing import Any

import pydantic
import pytest

from mizzle.core.errors import SchemaError
from mizzle.core.query.builder import QueryBuilder
from mizzle.core.schema.table import Table
from mizzle.core.schema.types import numeric, text
from mizzle.dialects.postgres.types import array, serial
from mizzle.pydantic import insert_model, select_model, update_model
from tests.models import Posts, Role, Users
from tests.unit.conftest import FakeRunner


class Price(Table, name="price"):
    id = serial().primary_key()
    amount = numeric(5, 2).not_null()
    tags = array(text().not_null())


def test_select_model_requires_every_column() -> None:
    Model = select_model(Users)
    with pytest.raises(pydantic.ValidationError):
        Model(id=1, email="a")
    row = Users.from_db(
        {
            "id": 1,
            "email": "a",
            "bio": None,
            "role": Role.ADMIN,
            "created_at": "2024-01-01T00:00:00Z",
        }
    )
    out = Model.model_validate(row)  # straight from a table row (from_attributes)
    assert out.model_dump()["role"] is Role.ADMIN


def test_insert_model_matches_database_rules() -> None:
    Model = insert_model(Users)
    ok = Model(email="a@x.io")  # id serial, role/created_at have defaults, bio nullable
    assert ok.model_dump(exclude_unset=True) == {"email": "a@x.io"}
    errors = {e["loc"][0]: e["type"] for e in _errors(Model, {})}
    assert errors == {"email": "missing"}
    assert _errors(Model, {"email": "x" * 256})[0]["type"] == "string_too_long"
    assert _errors(Model, {"email": "a", "role": "boss"})[0]["loc"] == ("role",)
    assert _errors(Model, {"email": "a", "extra": 1})[0]["type"] == "extra_forbidden"


def test_update_model_is_partial_and_keeps_not_null() -> None:
    Model = update_model(Users)
    assert Model().model_dump(exclude_unset=True) == {}
    assert Model(bio=None).model_dump(exclude_unset=True) == {"bio": None}


def test_numeric_and_arrays() -> None:
    Model = insert_model(Price)
    assert Model(amount="123.45", tags=["a"]).model_dump()["amount"] == Decimal("123.45")
    assert _errors(Model, {"amount": "1234.5"})  # 5 digits max, 2 after the point
    assert _errors(Model, {"amount": "1", "tags": [1]})


def test_refine_exclude_and_name() -> None:
    Model = insert_model(
        Posts, refine={"title": pydantic.constr(min_length=3)}, exclude=["meta"], name="NewPost"
    )
    assert Model.__name__ == "NewPost"
    assert "meta" not in Model.model_fields
    assert _errors(Model, {"author_id": 1, "title": "ab"})
    with pytest.raises(SchemaError):
        insert_model(Posts, exclude=["nope"])


async def test_models_feed_insert_and_update(qb: QueryBuilder, runner: FakeRunner) -> None:
    body = insert_model(Users)(email="a@x.io", bio=None)
    assert qb.insert(Users).values(body).to_sql() == (
        'INSERT INTO "users" ("email", "bio") VALUES ($1, $2)',
        ("a@x.io", None),
    )
    patch = update_model(Users)(role=Role.ADMIN)
    sql, params = qb.update(Users).set(patch).all_rows().to_sql()
    assert sql == 'UPDATE "users" SET "role" = $1' and params == ("admin",)
    bulk = [insert_model(Users)(email=e) for e in ("a", "b")]
    assert qb.insert(Users).values(bulk).to_sql()[1] == ("a", "b")


def _errors(model: type[pydantic.BaseModel], data: dict[str, Any]) -> list[Any]:
    try:
        model.model_validate(data)
    except pydantic.ValidationError as exc:
        return list(exc.errors())
    return []
