from collections.abc import Iterator

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from spans import FinishedSpans


@pytest.fixture
def exporter() -> InMemorySpanExporter:
    return InMemorySpanExporter()


@pytest.fixture
def provider(exporter: InMemorySpanExporter) -> Iterator[TracerProvider]:
    tracer_provider = TracerProvider()
    tracer_provider.add_span_processor(SimpleSpanProcessor(exporter))
    yield tracer_provider
    tracer_provider.shutdown()


@pytest.fixture
def spans(exporter: InMemorySpanExporter) -> FinishedSpans:
    return FinishedSpans(exporter)
