from collections.abc import Mapping

from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

DEFAULT_ENDPOINT = "http://127.0.0.1:4318"


def configure(
    endpoint: str = DEFAULT_ENDPOINT,
    *,
    service_name: str = "nodestep",
    headers: Mapping[str, str] | None = None,
    schedule_delay_millis: float | None = None,
) -> TracerProvider:
    """Create a tracer provider that exports spans over OTLP/HTTP.

    The provider has one ``BatchSpanProcessor`` with an ``OTLPSpanExporter``
    sending to ``{endpoint}/v1/traces``. The global OpenTelemetry provider is
    not changed. Pass the result to ``nodeartifact.instrument``, or to
    ``opentelemetry.trace.set_tracer_provider`` to make it the global one.

    Parameters
    ----------
    endpoint
        Base URL of the OTLP/HTTP receiver, such as a ``nodeartifact serve``
        server. A trailing slash is ignored.
    service_name
        The ``service.name`` resource attribute of every span.
    headers
        HTTP headers sent with every export, for example for authentication.
    schedule_delay_millis
        How long the ``BatchSpanProcessor`` waits between exports, in
        milliseconds. ``None`` keeps the SDK default: 5000, or the
        ``OTEL_BSP_SCHEDULE_DELAY`` environment variable when it is set. Use
        ``500`` to watch runs live in a local ``nodeartifact serve``.

    Returns
    -------
    TracerProvider
        The new provider. Call its ``shutdown()`` before the process exits,
        so the last batch is sent.
    """
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    provider = TracerProvider(resource=Resource({SERVICE_NAME: service_name}))
    exporter = OTLPSpanExporter(
        endpoint=f"{endpoint.rstrip('/')}/v1/traces",
        headers=dict(headers) if headers is not None else None,
    )
    provider.add_span_processor(
        BatchSpanProcessor(exporter, schedule_delay_millis=schedule_delay_millis)
    )
    return provider
