import json

import pytest

from mizzle.dialects.postgres import driver

CASES = [
    b'{"a": 1, "a": 2}',
    b"123456789012345678901234567890",  # orjson: a float, silently
    b"-9223372036854775809",
    b"[9223372036854775807, 18446744073709551615]",
    b'"\\ud800"',  # a lone surrogate: orjson refuses it
    b"1e400",
    b"[1.0, -0.0, 5e-324, 0.1]",
    b'{"k": "\\u00e9\\ud83d\\ude00", "n": null, "t": true}',
    b'"a\\u0000b"',
]


@pytest.mark.parametrize("text", CASES)
def test_json_values_are_the_same_with_and_without_orjson(
    text: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    # orjson reads json/jsonb values when installed: it must give exactly what json gives.
    assert driver._FAST_LOADS is not None
    expected = repr(json.loads(text))
    assert repr(driver._json_loads(text)) == expected
    monkeypatch.setattr(driver, "_FAST_LOADS", None)
    assert repr(driver._json_loads(text)) == expected
