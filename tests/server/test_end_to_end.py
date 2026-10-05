import asyncio
import json
import re
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
import uvicorn
from nodestep import (
    END,
    START,
    BaseState,
    Graph,
    ScriptedChat,
    build_react_agent,
    node,
    run_agent,
    tool,
)
from nodestep.chat import ChatResponse, ToolCall
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from nodeartifact import configure, instrument
from nodeartifact.server import TraceStore, create_app

ROOT = Path(__file__).parents[2]
README = ROOT / "README.md"
DOCS_INDEX = ROOT / "docs" / "index.md"
TRACING_PAGE = ROOT / "docs" / "tracing.md"


@dataclass(frozen=True, slots=True)
class RunningServer:
    url: str
    port: int
    store: TraceStore


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def row_of(page: str, name: str) -> str:
    return next(row for row in page.split("<tr") if f">{name}</a>" in row)


@pytest.fixture
def server(store: TraceStore) -> Iterator[RunningServer]:
    port = free_port()
    config = uvicorn.Config(
        create_app(store, host="127.0.0.1"),
        host="127.0.0.1",
        port=port,
        log_level="warning",
    )
    uvicorn_server = uvicorn.Server(config)
    thread = threading.Thread(target=uvicorn_server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not uvicorn_server.started:
        if time.monotonic() > deadline or not thread.is_alive():
            raise RuntimeError("uvicorn did not start")
        time.sleep(0.02)
    try:
        yield RunningServer(url=f"http://127.0.0.1:{port}", port=port, store=store)
    finally:
        uvicorn_server.should_exit = True
        thread.join(timeout=10)


def test_spans_exported_by_the_sdk_appear_in_the_trace_list(server):
    provider = TracerProvider(resource=Resource({"service.name": "e2e"}))
    exporter = OTLPSpanExporter(endpoint=f"{server.url}/v1/traces", timeout=10)
    provider.add_span_processor(BatchSpanProcessor(exporter))
    tracer = provider.get_tracer("nodeartifact.tests")

    with tracer.start_as_current_span("nodestep.graph demo"):
        with tracer.start_as_current_span("nodestep.node plan"):
            pass
        with tracer.start_as_current_span("chat gpt-6-luna") as model:
            model.set_attribute("gen_ai.operation.name", "chat")
            model.set_attribute("gen_ai.usage.input_tokens", 12)
            model.set_attribute("gen_ai.usage.output_tokens", 5)
    assert provider.force_flush()
    provider.shutdown()

    row = row_of(httpx.get(f"{server.url}/").text, "nodestep.graph demo")
    assert "e2e" in row
    assert ">3<" in row
    assert ">12<" in row
    assert ">5<" in row
    (summary,) = server.store.list_traces()
    page = httpx.get(f"{server.url}/traces/{summary.trace_id}").text
    for name in ("nodestep.graph demo", "nodestep.node plan", "chat gpt-6-luna"):
        assert f">{name}</a>" in page


def python_examples(path: Path) -> list[str]:
    return re.findall(
        r"```python\n(.*?)```", path.read_text(encoding="utf-8"), re.DOTALL
    )


def run_example(example: str, port: int) -> subprocess.CompletedProcess[str]:
    assert "127.0.0.1:4318" in example
    return subprocess.run(
        [sys.executable, "-c", example.replace("127.0.0.1:4318", f"127.0.0.1:{port}")],
        capture_output=True,
        text=True,
        timeout=60,
        encoding="utf-8",
    )


def test_readme_has_the_quick_start_example():
    assert len(python_examples(README)) == 1


def test_docs_index_shows_the_readme_quick_start():
    assert python_examples(DOCS_INDEX) == python_examples(README)


def test_tracing_page_has_the_two_examples():
    assert len(python_examples(TRACING_PAGE)) == 2


def test_readme_quick_start_sends_a_trace(server):
    (example,) = python_examples(README)
    result = run_example(example, server.port)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "Order A-1001 has shipped."
    (summary,) = server.store.list_traces()
    assert (summary.name, summary.service_name) == ("nodestep.graph agent", "my-app")
    assert (summary.thread_id, summary.input_text) == (
        "order-1",
        "Where is order A-1001?",
    )


def test_tracing_page_agent_example_sends_a_trace(server):
    result = run_example(python_examples(TRACING_PAGE)[0], server.port)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "Order A-1001 has shipped."
    (summary,) = server.store.list_traces()
    assert (summary.name, summary.service_name) == ("nodestep.graph agent", "my-app")
    assert (summary.thread_id, summary.input_text) == (
        "order-1",
        "Where is order A-1001?",
    )
    assert (summary.input_tokens, summary.output_tokens) == (280, 27)
    page = httpx.get(f"{server.url}/traces/{summary.trace_id}/timeline").text
    assert ">chat gpt-6-luna</a>" in page
    assert ">execute_tool get_order_status</a>" in page
    assert ">chat scripted</a>" not in page


def test_tracing_page_sdk_example_sends_a_trace(server):
    result = run_example(python_examples(TRACING_PAGE)[1], server.port)

    assert result.returncode == 0, result.stderr
    (summary,) = server.store.list_traces()
    assert (summary.name, summary.service_name) == ("checkout", "my-app")
    assert (summary.input_tokens, summary.output_tokens) == (120, 40)


@tool
def get_order_status(order_id: str) -> str:
    """Look up the shipping status of an order."""
    return "shipped" if order_id == "A-1001" else "unknown"


def test_an_instrumented_agent_run_appears_in_the_trace_list(server):
    chat = ScriptedChat(
        [
            ChatResponse(
                tool_calls=[
                    ToolCall(
                        id="call_1",
                        name="get_order_status",
                        arguments={"order_id": "A-1001"},
                    )
                ],
                model="gpt-6-luna",
                usage={"input_tokens": 40, "output_tokens": 9},
            ),
            ChatResponse(
                content="Order A-1001 has shipped.",
                model="gpt-6-luna",
                usage={"input_tokens": 61, "output_tokens": 8},
            ),
        ]
    )
    provider = configure(server.url, service_name="orders")
    agent = instrument(
        build_react_agent(chat=chat, tools=[get_order_status]),
        tracer_provider=provider,
    )

    answer = asyncio.run(
        run_agent(agent, "Where is order A-1001?", thread_id="orders-7")
    )
    assert provider.force_flush()
    provider.shutdown()

    assert answer == "Order A-1001 has shipped."
    row = row_of(httpx.get(f"{server.url}/").text, "Where is order A-1001?")
    assert "nodestep.graph agent" in row
    assert '<code title="orders-7">orders-7</code>' in row
    assert "orders" in row
    assert ">101<" in row
    assert ">17<" in row
    (summary,) = server.store.list_traces()
    assert summary.complete
    assert summary.span_count == 7
    page = httpx.get(f"{server.url}/traces/{summary.trace_id}/timeline").text
    for name in (
        "nodestep.graph agent",
        "nodestep.node think",
        "nodestep.node act",
        "execute_tool get_order_status",
        "chat gpt-6-luna",
    ):
        assert f">{name}</a>" in page
    graph_page = httpx.get(f"{server.url}/traces/{summary.trace_id}").text
    data = graph_data(graph_page)
    assert [(step["node_name"], step["mermaid_id"]) for step in data["steps"]] == [
        ("think", "think"),
        ("act", "act"),
        ("think", "think"),
    ]
    assert data["finished"] is True
    assert data["status"] == "completed"
    assert (
        '<pre class="mermaid nodestep-mermaid" id="trace-diagram">graph TD'
        in graph_page
    )


def graph_data(page: str) -> dict:
    match = re.search(
        r'<script type="application/json" id="trace-graph-data">(.*?)</script>',
        page,
        re.DOTALL,
    )
    assert match is not None
    return json.loads(match.group(1))


class Gate(BaseState):
    count: int = 0


def gated_graph(release: threading.Event) -> Graph:
    @node
    def first(state: Gate) -> dict:
        return {"count": state.count + 1}

    @node
    async def second(state: Gate) -> dict:
        await asyncio.to_thread(release.wait, 10)
        return {"count": state.count + 1}

    return Graph(Gate, name="gated").flow(
        START >> first, first >> second, second >> END
    )


def sse_events(lines: Iterator[str]) -> Iterator[tuple[str, dict]]:
    fields: dict[str, str] = {}
    for line in lines:
        if line:
            name, _, value = line.partition(": ")
            fields[name] = value
        elif "event" in fields:
            yield fields["event"], json.loads(fields["data"])
            fields = {}


def test_live_stream_follows_a_run_while_it_is_open(server):
    release = threading.Event()
    provider = configure(server.url, service_name="live", schedule_delay_millis=20)
    graph = instrument(gated_graph(release), tracer_provider=provider)
    runner = threading.Thread(target=lambda: asyncio.run(graph.ainvoke({"count": 0})))
    runner.start()
    try:
        deadline = time.monotonic() + 10
        while not server.store.list_traces():
            assert time.monotonic() < deadline, "the first node span did not arrive"
            time.sleep(0.02)
        (summary,) = server.store.list_traces()
        page = httpx.get(f"{server.url}/traces/{summary.trace_id}").text
        assert 'data-live="true"' in page
        assert (
            '<span class="nodestep-badge nodestep-badge-running" id="trace-status">'
            in page
        )

        seen: list[tuple[str, list[str], bool]] = []
        with httpx.stream(
            "GET", f"{server.url}/traces/{summary.trace_id}/live", timeout=10
        ) as response:
            assert response.headers["content-type"].startswith("text/event-stream")
            for name, data in sse_events(response.iter_lines()):
                if name == "spans":
                    steps = [step["node_name"] for step in data["graph"]["steps"]]
                    seen.append((name, steps, data["graph"]["finished"]))
                    release.set()
                else:
                    seen.append((name, [], data["status"] == "completed"))
                    break
    finally:
        release.set()
        runner.join(timeout=10)
        provider.shutdown()

    assert seen[0] == ("spans", ["first"], False)
    assert seen[-2][1:] == (["first", "second"], True)
    assert seen[-1] == ("end", [], True)
