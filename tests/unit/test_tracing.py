"""Tracing: the OpenTelemetry adapter sets span attributes."""

from collections.abc import Generator
from contextlib import contextmanager

from mizzle.tracing import OpenTelemetryTracer


class FakeSpan:
    def __init__(self) -> None:
        self.attributes: dict[str, object] = {}

    def set_attribute(self, key: str, value: object) -> None:
        self.attributes[key] = value


class FakeOtelTracer:
    def __init__(self) -> None:
        self.spans: list[tuple[str, FakeSpan]] = []

    @contextmanager
    def start_as_current_span(self, name: str) -> Generator[FakeSpan]:
        span = FakeSpan()
        self.spans.append((name, span))
        yield span


def test_opentelemetry_tracer_sets_attributes() -> None:
    otel = FakeOtelTracer()
    tracer = OpenTelemetryTracer(otel)

    with tracer.statement("SELECT 1", ("a",), {"request_id": "r1"}):
        pass
    with tracer.transaction("serializable"):
        pass

    assert otel.spans[0][0] == "db.statement"
    assert otel.spans[0][1].attributes == {
        "db.system": "postgresql",
        "db.statement": "SELECT 1",
        "db.statement.parameters": "('a',)",
        "mizzle.tag.request_id": "r1",
    }
    assert otel.spans[1] == ("db.transaction", otel.spans[1][1])
    assert otel.spans[1][1].attributes == {"db.transaction.isolation": "serializable"}
