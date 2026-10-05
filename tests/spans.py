from typing import Any

from opentelemetry.sdk.trace import Event, ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)


class FinishedSpans:
    def __init__(self, exporter: InMemorySpanExporter) -> None:
        self._exporter = exporter

    @property
    def all(self) -> list[ReadableSpan]:
        return list(self._exporter.get_finished_spans())

    def named(self, name: str) -> list[ReadableSpan]:
        return sorted(
            (span for span in self.all if span.name == name),
            key=lambda span: span.start_time or 0,
        )

    def one(self, name: str) -> ReadableSpan:
        (span,) = self.named(name)
        return span


def span_id(span: ReadableSpan) -> int:
    assert span.context is not None
    return span.context.span_id


def trace_id(span: ReadableSpan) -> int:
    assert span.context is not None
    return span.context.trace_id


def parent_id(span: ReadableSpan) -> int | None:
    return None if span.parent is None else span.parent.span_id


def attributes(item: ReadableSpan | Event) -> dict[str, Any]:
    return dict(item.attributes or {})
