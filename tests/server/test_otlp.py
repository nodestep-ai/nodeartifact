import json

import pytest
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue
from opentelemetry.proto.trace.v1.trace_pb2 import Span, Status

from nodeartifact.server.models import EventRecord, ExportBatch, SpanKind, StatusCode
from nodeartifact.server.otlp import OtlpDecodeError, OtlpTraceDecoder
from payloads import (
    CHILD_ID,
    ROOT_ID,
    START_NS,
    TRACE_ID,
    Value,
    as_json,
    event,
    request,
    span,
)


@pytest.fixture
def decoder() -> OtlpTraceDecoder:
    return OtlpTraceDecoder()


def sample_request():
    return request(
        span(
            TRACE_ID,
            ROOT_ID,
            "nodestep.graph demo",
            kind=Span.SPAN_KIND_SERVER,
            attributes={"nodestep.graph.name": "demo"},
        ),
        span(
            TRACE_ID,
            CHILD_ID,
            "chat gpt-6-luna",
            parent=ROOT_ID,
            start=START_NS + 100,
            end=START_NS + 900,
            status=Status.STATUS_CODE_ERROR,
            message="boom",
            attributes={"gen_ai.usage.input_tokens": 12},
            events=[event("retry", START_NS + 500, {"attempt": 2})],
        ),
    )


def test_protobuf_request_becomes_span_records(decoder):
    batch = decoder.from_protobuf(sample_request().SerializeToString())

    assert batch.rejected == 0
    root, child = batch.spans
    assert (root.trace_id, root.span_id, root.parent_span_id) == (
        TRACE_ID,
        ROOT_ID,
        None,
    )
    assert root.name == "nodestep.graph demo"
    assert root.kind == SpanKind.SERVER
    assert (root.start_ns, root.end_ns) == (START_NS, START_NS + 1_000_000)
    assert root.status_code == StatusCode.UNSET
    assert root.attributes == {"nodestep.graph.name": "demo"}
    assert root.resource.attributes == {"service.name": "checkout"}
    assert (root.scope.name, root.scope.version) == ("tests", "1.0")
    assert child.parent_span_id == ROOT_ID
    assert (child.status_code, child.status_message) == (StatusCode.ERROR, "boom")
    assert child.attributes == {"gen_ai.usage.input_tokens": 12}
    assert child.events == [
        EventRecord(name="retry", time_ns=START_NS + 500, attributes={"attempt": 2})
    ]


def test_every_attribute_value_type_is_converted(decoder):
    attributes: dict[str, Value] = {
        "text": "hi",
        "flag": True,
        "count": 3,
        "ratio": 0.5,
        "items": ["a", 1],
        "nested": {"inner": [True, None]},
        "raw": b"\x00\xff",
        "empty": None,
    }
    body = request(
        span(TRACE_ID, ROOT_ID, "values", attributes=attributes)
    ).SerializeToString()

    (record,) = decoder.from_protobuf(body).spans

    assert record.attributes == {
        "text": "hi",
        "flag": True,
        "count": 3,
        "ratio": 0.5,
        "items": ["a", 1],
        "nested": {"inner": [True, None]},
        "raw": "AP8=",
        "empty": None,
    }


def test_string_table_index_value_has_no_meaning_in_traces(decoder):
    message = request(span(TRACE_ID, ROOT_ID, "values"))
    target = message.resource_spans[0].scope_spans[0].spans[0]
    target.attributes.append(
        KeyValue(key="indexed", value=AnyValue(string_value_strindex=3))
    )

    (record,) = decoder.from_protobuf(message.SerializeToString()).spans

    assert record.attributes == {"indexed": None}


def test_json_request_decodes_like_protobuf(decoder):
    message = sample_request()

    assert decoder.from_json(as_json(message)) == decoder.from_protobuf(
        message.SerializeToString()
    )


def test_json_follows_otlp_encoding_rules(decoder):
    body = json.dumps(
        {
            "resourceSpans": [
                {
                    "resource": {
                        "attributes": [
                            {
                                "key": "service.name",
                                "value": {"stringValue": "checkout"},
                            }
                        ]
                    },
                    "scopeSpans": [
                        {
                            "scope": {"name": "manual"},
                            "spans": [
                                {
                                    "traceId": TRACE_ID,
                                    "spanId": ROOT_ID,
                                    "parentSpanId": "",
                                    "name": "GET /",
                                    "kind": 2,
                                    "startTimeUnixNano": "1700000000000000000",
                                    "endTimeUnixNano": "1700000000001000000",
                                    "attributes": [
                                        {
                                            "key": "http.status_code",
                                            "value": {"intValue": "200"},
                                        }
                                    ],
                                    "status": {"code": 1},
                                    "futureField": {"anything": 1},
                                }
                            ],
                        }
                    ],
                }
            ]
        }
    ).encode()

    (record,) = decoder.from_json(body).spans

    assert (record.trace_id, record.span_id, record.parent_span_id) == (
        TRACE_ID,
        ROOT_ID,
        None,
    )
    assert record.kind == SpanKind.SERVER
    assert (record.start_ns, record.end_ns) == (START_NS, START_NS + 1_000_000)
    assert record.attributes == {"http.status_code": 200}
    assert record.status_code == StatusCode.OK
    assert record.scope.name == "manual"


@pytest.mark.parametrize(
    ("trace_id", "span_id", "parent"),
    [
        ("5b8efff798038103d269b633813fc6", ROOT_ID, ""),
        ("00000000000000000000000000000000", ROOT_ID, ""),
        (TRACE_ID, "0000000000000000", ""),
        (TRACE_ID, "eee19b7ec3c1", ""),
        (TRACE_ID, ROOT_ID, "abcdef"),
    ],
)
def test_spans_with_invalid_ids_are_rejected(decoder, trace_id, span_id, parent):
    message = request(
        span(trace_id, span_id, "bad", parent=parent), span(TRACE_ID, CHILD_ID, "good")
    )

    batch = decoder.from_protobuf(message.SerializeToString())

    assert [record.name for record in batch.spans] == ["good"]
    assert batch.rejected == 1


def test_all_zero_parent_id_means_no_parent(decoder):
    body = request(
        span(TRACE_ID, ROOT_ID, "root", parent="0000000000000000")
    ).SerializeToString()

    (record,) = decoder.from_protobuf(body).spans

    assert record.parent_span_id is None


def test_empty_protobuf_body_is_an_empty_batch(decoder):
    assert decoder.from_protobuf(b"") == ExportBatch(spans=[], rejected=0)


def test_invalid_protobuf_raises(decoder):
    with pytest.raises(OtlpDecodeError):
        decoder.from_protobuf(b"\xff\xff\xff")


def test_too_deeply_nested_json_value_raises(decoder):
    value: dict[str, object] = {"stringValue": "leaf"}
    for _ in range(200):
        value = {"arrayValue": {"values": [value]}}
    data = json.loads(as_json(request(span(TRACE_ID, ROOT_ID, "deep"))))
    data["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["attributes"] = [
        {"key": "deep", "value": value}
    ]

    with pytest.raises(OtlpDecodeError):
        decoder.from_json(json.dumps(data).encode())


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"{not json",
        b"[]",
        b"[" * 100_000,
        b'{"resourceSpans": 5}',
        b'{"resourceSpans": [{"scopeSpans": [{"spans": [{"traceId": "zz"}]}]}]}',
        b"\xff\xfe\x00",
    ],
    ids=[
        "empty",
        "not json",
        "array",
        "deep array",
        "spans not a list",
        "bad trace id",
        "not utf-8",
    ],
)
def test_invalid_json_raises(decoder, body):
    with pytest.raises(OtlpDecodeError):
        decoder.from_json(body)


@pytest.mark.parametrize(("start", "end"), [(2**63, 2**63 + 5), (START_NS, 2**64 - 1)])
def test_spans_with_timestamps_beyond_int64_are_rejected(decoder, start, end):
    message = request(
        span(TRACE_ID, ROOT_ID, "far future", start=start, end=end),
        span(TRACE_ID, CHILD_ID, "good"),
    )

    batch = decoder.from_protobuf(message.SerializeToString())

    assert [record.name for record in batch.spans] == ["good"]
    assert batch.rejected == 1
