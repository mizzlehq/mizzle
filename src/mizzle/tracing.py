"""Tracing hooks: wrap statements and transactions in spans.

Pass ``Database.connect(..., tracer=...)`` a ``Tracer``. mizzle opens a span around every
statement and transaction, so the span covers the work and ends with the error when one is
raised::

    from opentelemetry import trace
    from mizzle.tracing import OpenTelemetryTracer

    db = await Database.connect(url, tracer=OpenTelemetryTracer(trace.get_tracer("mizzle")))

For another backend, implement ``statement(sql, params)`` and ``transaction(isolation)`` as
context managers yielding your span object.
"""

from __future__ import annotations

from collections.abc import Generator, Mapping
from contextlib import AbstractContextManager, contextmanager
from typing import Protocol

from mizzle.dialects.base import IsolationLevel


class Tracer(Protocol):
    """A span source. See :class:`OpenTelemetryTracer`."""

    def statement(
        self,
        sql: str,
        params: tuple[object, ...] | None,
        tags: Mapping[str, object],
    ) -> AbstractContextManager[object]: ...
    def transaction(
        self, isolation: IsolationLevel | None = None
    ) -> AbstractContextManager[object]: ...


class Span(Protocol):
    """The span object an OpenTelemetry tracer yields."""

    def set_attribute(self, key: str, value: object) -> None: ...


class SpanTracer(Protocol):
    """The subset of ``opentelemetry.trace.Tracer`` mizzle uses."""

    def start_as_current_span(self, name: str) -> AbstractContextManager[Span]: ...


class OpenTelemetryTracer:
    """A ``Tracer`` emitting OpenTelemetry spans.

    Pass the tracer to use, e.g. ``OpenTelemetryTracer(trace.get_tracer("mizzle"))``. The
    ``db.statement`` span carries the SQL (and, with ``log_params=True``, the parameter
    values); ``db.transaction`` carries the isolation level when one was requested. A
    raised error ends the span as failed.
    """

    _tracer: SpanTracer

    def __init__(self, tracer: SpanTracer) -> None:
        self._tracer = tracer

    @contextmanager
    def statement(
        self, sql: str, params: tuple[object, ...] | None, tags: Mapping[str, object]
    ) -> Generator[object]:
        with self._tracer.start_as_current_span("db.statement") as span:
            span.set_attribute("db.system", "postgresql")
            span.set_attribute("db.statement", sql)
            if params is not None:
                span.set_attribute("db.statement.parameters", str(params))
            for key, value in tags.items():
                span.set_attribute(f"mizzle.tag.{key}", value)
            yield span

    @contextmanager
    def transaction(self, isolation: IsolationLevel | None = None) -> Generator[object]:
        with self._tracer.start_as_current_span("db.transaction") as span:
            if isolation is not None:
                span.set_attribute("db.transaction.isolation", isolation)
            yield span
