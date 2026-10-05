import base64
import json
from collections.abc import Mapping, Sequence

from google.protobuf import json_format
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
)
from opentelemetry.proto.common.v1.common_pb2 import (
    AnyValue,
    ArrayValue,
    InstrumentationScope,
    KeyValue,
    KeyValueList,
)
from opentelemetry.proto.resource.v1.resource_pb2 import Resource
from opentelemetry.proto.trace.v1.trace_pb2 import (
    ResourceSpans,
    ScopeSpans,
    Span,
    Status,
)

from nodeartifact.server.models import SpanRecord
from nodeartifact.server.otlp import OtlpTraceDecoder

TRACE_ID = "5b8efff798038103d269b633813fc60c"
OTHER_TRACE_ID = "0af7651916cd43dd8448eb211c80319c"
ROOT_ID = "eee19b7ec3c1b174"
CHILD_ID = "eee19b7ec3c1b173"
GRANDCHILD_ID = "eee19b7ec3c1b172"
START_NS = 1_700_000_000_000_000_000

type Value = (
    str
    | bool
    | int
    | float
    | bytes
    | list[Value]
    | tuple[Value, ...]
    | dict[str, Value]
    | None
)


def any_value(value: Value) -> AnyValue:
    match value:
        case None:
            return AnyValue()
        case bool():
            return AnyValue(bool_value=value)
        case int():
            return AnyValue(int_value=value)
        case float():
            return AnyValue(double_value=value)
        case str():
            return AnyValue(string_value=value)
        case bytes():
            return AnyValue(bytes_value=value)
        case list() | tuple():
            return AnyValue(
                array_value=ArrayValue(values=[any_value(item) for item in value])
            )
        case dict():
            return AnyValue(kvlist_value=KeyValueList(values=key_values(value)))
    raise TypeError(f"no AnyValue for {value!r}")


def key_values(attributes: Mapping[str, Value]) -> list[KeyValue]:
    return [
        KeyValue(key=key, value=any_value(value)) for key, value in attributes.items()
    ]


def event(
    name: str, time_ns: int, attributes: Mapping[str, Value] | None = None
) -> Span.Event:
    return Span.Event(
        name=name, time_unix_nano=time_ns, attributes=key_values(attributes or {})
    )


def span(
    trace_id: str,
    span_id: str,
    name: str,
    *,
    parent: str = "",
    start: int = START_NS,
    end: int = START_NS + 1_000_000,
    kind: Span.SpanKind.ValueType = Span.SPAN_KIND_INTERNAL,
    attributes: Mapping[str, Value] | None = None,
    status: Status.StatusCode.ValueType = Status.STATUS_CODE_UNSET,
    message: str = "",
    events: Sequence[Span.Event] = (),
) -> Span:
    return Span(
        trace_id=bytes.fromhex(trace_id),
        span_id=bytes.fromhex(span_id),
        parent_span_id=bytes.fromhex(parent),
        name=name,
        kind=kind,
        start_time_unix_nano=start,
        end_time_unix_nano=end,
        attributes=key_values(attributes or {}),
        events=list(events),
        status=Status(code=status, message=message),
    )


def request(
    *spans: Span,
    service: str = "checkout",
    scope: str = "tests",
    scope_version: str = "1.0",
) -> ExportTraceServiceRequest:
    return ExportTraceServiceRequest(
        resource_spans=[
            ResourceSpans(
                resource=Resource(attributes=key_values({"service.name": service})),
                scope_spans=[
                    ScopeSpans(
                        scope=InstrumentationScope(name=scope, version=scope_version),
                        spans=list(spans),
                    )
                ],
            )
        ]
    )


def as_json(message: ExportTraceServiceRequest) -> bytes:
    data = json_format.MessageToDict(message)
    for resource_spans in data.get("resourceSpans", []):
        for scope_spans in resource_spans.get("scopeSpans", []):
            for item in scope_spans.get("spans", []):
                for key in ("traceId", "spanId", "parentSpanId"):
                    if key in item:
                        item[key] = base64.b64decode(item[key]).hex()
    return json.dumps(data).encode()


def records(*spans: Span, service: str = "checkout") -> list[SpanRecord]:
    body = request(*spans, service=service).SerializeToString()
    return OtlpTraceDecoder().from_protobuf(body).spans
