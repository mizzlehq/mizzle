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


def test_generated_and_key_columns_are_not_client_settable() -> None:
    assert "id" not in insert_model(Users).model_fields  # serial
    assert "id" not in update_model(Users).model_fields  # primary key
    assert "id" in select_model(Users).model_fields
    assert _errors(insert_model(Users), {"email": "a", "id": 5})[0]["type"] == "extra_forbidden"


def test_include_is_an_allowlist() -> None:
    SignUp = insert_model(Users, include=["email", "bio"])
    assert set(SignUp.model_fields) == {"email", "bio"}
    attack = _errors(SignUp, {"email": "a", "role": "admin"})
    assert attack[0]["loc"] == ("role",) and attack[0]["type"] == "extra_forbidden"
    assert "id" in update_model(Users, include=["id", "bio"]).model_fields  # explicit opt-in
    with pytest.raises(SchemaError):
        insert_model(Users, include=["nope"])


def test_models_accept_every_value_the_column_holds() -> None:
    # C35: inet/cidr were IPv4-only; Point, Line and BitString broke schema building.
    import ipaddress

    from asyncpg import BitString, Line, Point

    from mizzle.pg import cidr, inet, line, point, varbit

    class Net(Table, name="net"):
        id = serial().primary_key()
        ip = inet().not_null()
        net = cidr()
        p = point()
        b = line()
        bits = varbit()

    Model = select_model(Net)
    for ip in ("::1", "10.1.2.3", "10.1.2.3/24"):
        Model.model_validate(
            {
                "id": 1,
                "ip": ip,
                "net": "2001:db8::/32",
                "p": None,
                "b": None,
                "bits": None,
            }
        )
    row = Model.model_validate(
        {
            "id": 1,
            "ip": ipaddress.ip_interface("10.1.2.3/24"),
            "net": ipaddress.ip_network("10.0.0.0/8"),
            "p": Point(1, 2),
            "b": Line(1, -1, 0),
            "bits": BitString.from_int(5, 3),
        }
    )
    assert row.model_dump()["p"] == Point(1, 2)
    with pytest.raises(pydantic.ValidationError):
        Model.model_validate(
            {"id": 1, "ip": "::1", "net": None, "p": "(1,2)", "b": None, "bits": None}
        )
    insert_model(Net).model_validate({"ip": "::1"})


def test_dict_composites_write_and_validate() -> None:
    # Q9: pg_composite(name, dict) read dicts but only accepted tuples (writes and models).
    from mizzle.pg import pg_composite

    class Home(Table, name="home"):
        id = serial().primary_key()
        at = pg_composite("addr", dict)

    Home.at.validate({"city": "Oslo"})
    row = select_model(Home).model_validate({"id": 1, "at": {"city": "Oslo"}})
    assert row.model_dump()["at"] == {"city": "Oslo"}


def test_geometry_and_bit_strings_round_trip_json() -> None:
    # Q14: BitString had no serializer and no JSON input form.
    from asyncpg import BitString, Point

    from mizzle.pg import point, varbit

    class Spots(Table, name="spots"):
        id = serial().primary_key()
        at = point()
        bits = varbit()

    Model = select_model(Spots)
    row = Model(id=1, at=Point(1.5, -2), bits=BitString.from_int(5, 4))
    assert Model.model_validate_json(row.model_dump_json()) == row
    assert row.model_dump(mode="json")["bits"] == "0101"
