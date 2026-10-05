import gzip
import json

import pytest
from google.rpc.status_pb2 import Status as RpcStatus
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceResponse,
)
from starlette.testclient import TestClient

from conftest import BASE_URL
from nodeartifact.server.app import create_app
from payloads import CHILD_ID, ROOT_ID, TRACE_ID, as_json, request, span

PROTOBUF = {"content-type": "application/x-protobuf"}
JSON = {"content-type": "application/json"}


def sample_body() -> bytes:
    return request(
        span(TRACE_ID, ROOT_ID, "nodestep.graph demo"),
        span(TRACE_ID, CHILD_ID, "chat gpt-6-luna", parent=ROOT_ID),
    ).SerializeToString()


def limited_client(store, limit: int) -> TestClient:
    return TestClient(
        create_app(store, host="127.0.0.1", max_body_bytes=limit), base_url=BASE_URL
    )


def test_protobuf_export_is_stored(client, store):
    response = client.post("/v1/traces", content=sample_body(), headers=PROTOBUF)

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/x-protobuf"
    assert not ExportTraceServiceResponse.FromString(response.content).HasField(
        "partial_success"
    )
    detail = store.get_trace(TRACE_ID)
    assert detail is not None
    assert detail.summary.span_count == 2


@pytest.mark.parametrize(
    "content_type", ["application/json", "application/json; charset=utf-8"]
)
def test_json_export_is_stored(client, store, content_type):
    body = {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [
                        {"key": "service.name", "value": {"stringValue": "shop"}}
                    ]
                },
                "scopeSpans": [
                    {
                        "scope": {"name": "manual"},
                        "spans": [
                            {
                                "traceId": TRACE_ID,
                                "spanId": ROOT_ID,
                                "name": "GET /cart",
                                "kind": 2,
                                "startTimeUnixNano": "1700000000000000000",
                                "endTimeUnixNano": "1700000000002000000",
                            }
                        ],
                    }
                ],
            }
        ]
    }

    response = client.post(
        "/v1/traces", content=json.dumps(body), headers={"content-type": content_type}
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.json() == {}
    (summary,) = store.list_traces()
    assert (summary.name, summary.service_name) == ("GET /cart", "shop")


def test_rejected_spans_are_reported_as_partial_success(client, store):
    body = request(
        span("00000000000000000000000000000000", ROOT_ID, "bad"),
        span(TRACE_ID, CHILD_ID, "good"),
    ).SerializeToString()

    response = client.post("/v1/traces", content=body, headers=PROTOBUF)

    assert response.status_code == 200
    partial = ExportTraceServiceResponse.FromString(response.content).partial_success
    assert partial.rejected_spans == 1
    assert "rejected 1 span" in partial.error_message
    assert [summary.name for summary in store.list_traces()] == ["good"]


def test_partial_success_in_json(client):
    body = as_json(request(span("00000000000000000000000000000000", ROOT_ID, "bad")))

    response = client.post("/v1/traces", content=body, headers=JSON)

    assert response.status_code == 200
    partial = response.json()["partialSuccess"]
    assert partial["rejectedSpans"] == "1"
    assert "rejected 1 span" in partial["errorMessage"]


def test_empty_protobuf_export_succeeds(client):
    response = client.post("/v1/traces", content=b"", headers=PROTOBUF)

    assert response.status_code == 200
    assert response.content == b""


def test_empty_json_export_succeeds(client):
    response = client.post("/v1/traces", content=b'{"resourceSpans": []}', headers=JSON)

    assert response.status_code == 200
    assert response.json() == {}


def test_gzip_body_is_accepted(client, store):
    headers = {**PROTOBUF, "content-encoding": "gzip"}

    response = client.post(
        "/v1/traces", content=gzip.compress(sample_body()), headers=headers
    )

    assert response.status_code == 200
    assert len(store.list_traces()) == 1


def test_body_over_the_limit_is_refused(store):
    body = sample_body()

    response = limited_client(store, len(body) - 1).post(
        "/v1/traces", content=body, headers=PROTOBUF
    )

    assert response.status_code == 413
    assert (
        f"exceeds {len(body) - 1} bytes"
        in RpcStatus.FromString(response.content).message
    )
    assert store.list_traces() == []


def test_chunked_body_over_the_limit_is_refused(store):
    body = sample_body()

    def chunks():
        yield body[:50]
        yield body[50:]

    response = limited_client(store, len(body) - 1).post(
        "/v1/traces", content=chunks(), headers=PROTOBUF
    )

    assert response.status_code == 413
    assert store.list_traces() == []


def test_body_at_the_limit_is_accepted(store):
    body = sample_body()

    response = limited_client(store, len(body)).post(
        "/v1/traces", content=body, headers=PROTOBUF
    )

    assert response.status_code == 200


def test_gzip_body_that_inflates_past_the_limit_is_refused(store):
    body = gzip.compress(sample_body() + b"\x00" * 10_000)
    headers = {**PROTOBUF, "content-encoding": "gzip"}

    response = limited_client(store, 5_000).post(
        "/v1/traces", content=body, headers=headers
    )

    assert len(body) < 5_000
    assert response.status_code == 413
    assert store.list_traces() == []


@pytest.mark.parametrize(
    "body",
    [
        b"not gzip at all",
        gzip.compress(b"abc")[:-4],
        gzip.compress(b"abc") + b"trailing",
    ],
)
def test_invalid_gzip_is_refused(client, body):
    headers = {**PROTOBUF, "content-encoding": "gzip"}

    response = client.post("/v1/traces", content=body, headers=headers)

    assert response.status_code == 400
    assert "gzip" in RpcStatus.FromString(response.content).message


def test_default_limit_fits_a_full_batch_of_nodestep_spans(client, store):
    state = {
        "nodestep.state.before": "b" * 16_384,
        "nodestep.state.after": "a" * 16_384,
    }
    spans = [
        span(
            TRACE_ID, f"{number:016x}", f"nodestep.node step{number}", attributes=state
        )
        for number in range(1, 513)
    ]
    body = request(*spans).SerializeToString()

    response = client.post("/v1/traces", content=body, headers=PROTOBUF)

    assert len(body) > 16 * 1024 * 1024
    assert response.status_code == 200
    (summary,) = store.list_traces()
    assert summary.span_count == 512


def test_unsupported_content_type_is_refused(client):
    response = client.post(
        "/v1/traces", content=b"hello", headers={"content-type": "text/plain"}
    )

    assert response.status_code == 415
    assert "application/x-protobuf" in response.text


def test_unsupported_content_encoding_is_refused(client):
    headers = {**JSON, "content-encoding": "br"}

    response = client.post("/v1/traces", content=b"{}", headers=headers)

    assert response.status_code == 415
    assert "br" in response.json()["message"]


def test_invalid_protobuf_is_refused(client, store):
    response = client.post("/v1/traces", content=b"\xff\xff\xff", headers=PROTOBUF)

    assert response.status_code == 400
    assert response.headers["content-type"] == "application/x-protobuf"
    assert "invalid OTLP protobuf" in RpcStatus.FromString(response.content).message
    assert store.list_traces() == []


def test_invalid_json_is_refused(client):
    response = client.post("/v1/traces", content=b"[]", headers=JSON)

    assert response.status_code == 400
    assert response.headers["content-type"] == "application/json"
    assert "JSON object" in response.json()["message"]


def test_get_is_not_allowed(client):
    assert client.get("/v1/traces").status_code == 405
