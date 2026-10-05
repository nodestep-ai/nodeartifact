import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from opentelemetry import trace
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
)
from opentelemetry.sdk.trace import TracerProvider

from nodeartifact import configure


@dataclass
class CapturedRequest:
    path: str
    headers: dict[str, str]
    body: bytes


@dataclass
class CaptureServer:
    url: str
    requests: list[CapturedRequest] = field(default_factory=list)


@pytest.fixture
def capture_server() -> Iterator[CaptureServer]:
    requests: list[CapturedRequest] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers["Content-Length"])
            requests.append(
                CapturedRequest(
                    path=self.path,
                    headers={key.lower(): value for key, value in self.headers.items()},
                    body=self.rfile.read(length),
                )
            )
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield CaptureServer(
            url=f"http://127.0.0.1:{server.server_address[1]}", requests=requests
        )
    finally:
        server.shutdown()
        thread.join(timeout=10)
        server.server_close()


def export_one_span(provider: TracerProvider) -> None:
    with provider.get_tracer("tests").start_as_current_span("nodestep.graph demo"):
        pass
    assert provider.force_flush()
    provider.shutdown()


def test_configure_exports_to_the_traces_path_with_the_given_headers(
    capture_server: CaptureServer,
):
    provider = configure(
        capture_server.url, service_name="shop", headers={"x-team": "search"}
    )

    export_one_span(provider)

    (request,) = capture_server.requests
    assert request.path == "/v1/traces"
    assert request.headers["x-team"] == "search"
    assert request.headers["content-type"] == "application/x-protobuf"
    decoded = ExportTraceServiceRequest.FromString(request.body)
    (resource_spans,) = decoded.resource_spans
    attributes = {
        item.key: item.value.string_value for item in resource_spans.resource.attributes
    }
    assert attributes["service.name"] == "shop"
    assert resource_spans.scope_spans[0].spans[0].name == "nodestep.graph demo"


def test_configure_accepts_an_endpoint_with_a_trailing_slash(
    capture_server: CaptureServer,
):
    export_one_span(configure(f"{capture_server.url}/"))

    (request,) = capture_server.requests
    assert request.path == "/v1/traces"


def test_configure_names_the_service_nodestep_by_default():
    provider = configure()

    assert provider.resource.attributes["service.name"] == "nodestep"
    provider.shutdown()


def test_configure_leaves_the_global_provider_alone():
    before = trace.get_tracer_provider()

    provider = configure()

    assert trace.get_tracer_provider() is before
    assert provider is not before
    provider.shutdown()


def wait_for_requests(server: CaptureServer, seconds: float = 3.0) -> int:
    deadline = time.monotonic() + seconds
    while not server.requests and time.monotonic() < deadline:
        time.sleep(0.01)
    return len(server.requests)


def end_one_span(provider: TracerProvider) -> None:
    with provider.get_tracer("tests").start_as_current_span("nodestep.graph demo"):
        pass


def test_configure_sends_batches_after_the_given_delay(capture_server: CaptureServer):
    provider = configure(capture_server.url, schedule_delay_millis=50)

    end_one_span(provider)

    assert wait_for_requests(capture_server) == 1
    provider.shutdown()


def test_configure_without_a_delay_keeps_the_sdk_default(
    capture_server: CaptureServer, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("OTEL_BSP_SCHEDULE_DELAY", "50")
    provider = configure(capture_server.url)

    end_one_span(provider)

    assert wait_for_requests(capture_server) == 1
    provider.shutdown()
