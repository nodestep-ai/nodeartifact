import base64
import json
from collections.abc import Iterable

from google.protobuf import json_format
from google.protobuf.message import DecodeError
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
)
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue
from opentelemetry.proto.trace.v1.trace_pb2 import Span

from nodeartifact.server.models import (
    EventRecord,
    ExportBatch,
    JsonValue,
    ResourceRecord,
    ScopeRecord,
    SpanRecord,
)

TRACE_ID_BYTES = 16
SPAN_ID_BYTES = 8
MAX_TIMESTAMP_NS = 2**63 - 1
SPAN_ID_KEYS = ("traceId", "spanId", "parentSpanId")
LINK_ID_KEYS = ("traceId", "spanId")


class OtlpDecodeError(ValueError):
    pass


class OtlpTraceDecoder:
    def from_protobuf(self, body: bytes) -> ExportBatch:
        try:
            request = ExportTraceServiceRequest.FromString(body)
        except DecodeError as error:
            raise OtlpDecodeError(f"invalid OTLP protobuf body: {error}") from error
        return self._batch(request)

    def from_json(self, body: bytes) -> ExportBatch:
        try:
            data = json.loads(body)
        except (ValueError, RecursionError) as error:
            raise OtlpDecodeError(f"invalid JSON body: {error}") from error
        if not isinstance(data, dict):
            raise OtlpDecodeError("an OTLP JSON body must be a JSON object")
        self._hex_ids_to_base64(data)
        try:
            request = json_format.ParseDict(
                data, ExportTraceServiceRequest(), ignore_unknown_fields=True
            )
        except json_format.ParseError as error:
            raise OtlpDecodeError(f"invalid OTLP JSON body: {error}") from error
        return self._batch(request)

    def _hex_ids_to_base64(self, data: dict[str, JsonValue]) -> None:
        for resource_spans in self._objects(data.get("resourceSpans")):
            for scope_spans in self._objects(resource_spans.get("scopeSpans")):
                for span in self._objects(scope_spans.get("spans")):
                    self._convert_ids(span, SPAN_ID_KEYS)
                    for link in self._objects(span.get("links")):
                        self._convert_ids(link, LINK_ID_KEYS)

    @staticmethod
    def _objects(value: JsonValue) -> list[dict[str, JsonValue]]:
        if not isinstance(value, list):
            return []
        return [item for item in value if isinstance(item, dict)]

    @staticmethod
    def _convert_ids(owner: dict[str, JsonValue], keys: tuple[str, ...]) -> None:
        for key in keys:
            value = owner.get(key)
            if not isinstance(value, str):
                continue
            try:
                owner[key] = base64.b64encode(bytes.fromhex(value)).decode("ascii")
            except ValueError as error:
                raise OtlpDecodeError(f"{key} must be a hex string") from error

    def _batch(self, request: ExportTraceServiceRequest) -> ExportBatch:
        spans: list[SpanRecord] = []
        rejected = 0
        for resource_spans in request.resource_spans:
            resource = ResourceRecord(
                attributes=self._attributes(resource_spans.resource.attributes),
                schema_url=resource_spans.schema_url,
            )
            for scope_spans in resource_spans.scope_spans:
                scope = ScopeRecord(
                    name=scope_spans.scope.name,
                    version=scope_spans.scope.version,
                    attributes=self._attributes(scope_spans.scope.attributes),
                    schema_url=scope_spans.schema_url,
                )
                for span in scope_spans.spans:
                    record = self._span(span, resource, scope)
                    if record is None:
                        rejected += 1
                    else:
                        spans.append(record)
        return ExportBatch(spans=spans, rejected=rejected)

    def _span(
        self, span: Span, resource: ResourceRecord, scope: ScopeRecord
    ) -> SpanRecord | None:
        parent = span.parent_span_id
        if (
            not self._valid_id(span.trace_id, TRACE_ID_BYTES)
            or not self._valid_id(span.span_id, SPAN_ID_BYTES)
            or len(parent) not in (0, SPAN_ID_BYTES)
            or max(span.start_time_unix_nano, span.end_time_unix_nano)
            > MAX_TIMESTAMP_NS
        ):
            return None
        return SpanRecord(
            trace_id=span.trace_id.hex(),
            span_id=span.span_id.hex(),
            parent_span_id=parent.hex() if any(parent) else None,
            name=span.name,
            kind=span.kind,
            start_ns=span.start_time_unix_nano,
            end_ns=span.end_time_unix_nano,
            status_code=span.status.code,
            status_message=span.status.message,
            attributes=self._attributes(span.attributes),
            events=[
                EventRecord(
                    name=event.name,
                    time_ns=event.time_unix_nano,
                    attributes=self._attributes(event.attributes),
                )
                for event in span.events
            ],
            resource=resource,
            scope=scope,
        )

    @staticmethod
    def _valid_id(value: bytes, size: int) -> bool:
        return len(value) == size and any(value)

    def _attributes(self, items: Iterable[KeyValue]) -> dict[str, JsonValue]:
        return {item.key: self._value(item.value) for item in items}

    def _value(self, value: AnyValue) -> JsonValue:
        match value.WhichOneof("value"):
            case "string_value":
                return value.string_value
            case "bool_value":
                return value.bool_value
            case "int_value":
                return value.int_value
            case "double_value":
                return value.double_value
            case "array_value":
                return [self._value(item) for item in value.array_value.values]
            case "kvlist_value":
                return self._attributes(value.kvlist_value.values)
            case "bytes_value":
                return base64.b64encode(value.bytes_value).decode("ascii")
            case _:
                return None
