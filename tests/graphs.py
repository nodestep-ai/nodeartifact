from collections.abc import Mapping, Sequence

from opentelemetry.proto.trace.v1.trace_pb2 import Span, Status

from payloads import ROOT_ID, START_NS, TRACE_ID, Value, event, span

MERMAID = "\n".join(
    [
        "graph TD",
        "  START([START])",
        '  think["think"]',
        "  START --> think",
        '  act["act"]',
        '  think -->|"tools"| act',
        '  think -->|"done"| END([END])',
        "  act --> think",
    ]
)
THINK_ID = "aaaaaaaaaaaa0001"
ACT_ID = "aaaaaaaaaaaa0002"
ANSWER_ID = "aaaaaaaaaaaa0003"
MODEL_ID = "aaaaaaaaaaaa0004"
CHILD_RUN_ID = "aaaaaaaaaaaa0005"
CHILD_NODE_ID = "aaaaaaaaaaaa0006"
MISSING_RUN_ID = "bbbbbbbbbbbb0001"
MISSING_NODE_ID = "bbbbbbbbbbbb0002"
TOOL_ID = "aaaaaaaaaaaa0007"
MILLISECOND = 1_000_000


def run_span(
    *,
    status: str | None = "completed",
    source: str | None = MERMAID,
    graph: str = "agent",
    span_id: str = ROOT_ID,
    parent: str = "",
    start: int = START_NS,
    events: Sequence[Span.Event] = (),
    trace_id: str = TRACE_ID,
    resuming: bool = False,
    extra: Mapping[str, Value] | None = None,
) -> Span:
    attributes: dict[str, Value] = {
        "nodestep.graph.name": graph,
        "nodestep.input": "Please refund order A-1001",
        "nodestep.resuming": resuming,
        **(extra or {}),
    }
    if source is not None:
        attributes["nodestep.graph.mermaid"] = source
    if status is not None:
        attributes["nodestep.status"] = status
    return span(
        trace_id,
        span_id,
        f"nodestep.graph {graph}",
        parent=parent,
        start=start,
        end=start + 10 * MILLISECOND,
        attributes=attributes,
        events=events,
    )


def node_span(
    span_id: str,
    name: str,
    offset_ms: int,
    *,
    parent: str = ROOT_ID,
    graph: str = "agent",
    mermaid_id: str | None = None,
    status: Status.StatusCode.ValueType = Status.STATUS_CODE_UNSET,
    events: Sequence[Span.Event] = (),
    extra: Mapping[str, Value] | None = None,
    trace_id: str = TRACE_ID,
) -> Span:
    attributes: dict[str, Value] = {
        "nodestep.graph.name": graph,
        "nodestep.node.name": name,
        "nodestep.step": offset_ms,
        "nodestep.task_id": f"{name}-{offset_ms}",
        "nodestep.node.mermaid_id": mermaid_id or name,
        "nodestep.state.before": '{"messages": []}',
        "nodestep.update": f'{{"messages": ["from {name}"]}}',
        **(extra or {}),
    }
    start = START_NS + offset_ms * MILLISECOND
    return span(
        trace_id,
        span_id,
        f"nodestep.node {name}",
        parent=parent,
        start=start,
        end=start + MILLISECOND,
        status=status,
        attributes=attributes,
        events=events,
    )


def call_span(
    span_id: str,
    name: str,
    offset_ms: int,
    *,
    parent: str,
    operation: str = "execute_tool",
    graph: str = "agent",
    mermaid_id: str | None = None,
    status: Status.StatusCode.ValueType = Status.STATUS_CODE_UNSET,
    trace_id: str = TRACE_ID,
) -> Span:
    attributes: dict[str, Value] = {
        "gen_ai.operation.name": operation,
        "nodestep.graph.name": graph,
    }
    if operation == "execute_tool":
        attributes["gen_ai.tool.name"] = name
    if mermaid_id is not None:
        attributes["nodestep.node.mermaid_id"] = mermaid_id
    start = START_NS + offset_ms * MILLISECOND + 10
    prefix = "execute_tool" if operation == "execute_tool" else "chat"
    return span(
        trace_id,
        span_id,
        f"{prefix} {name}",
        parent=parent,
        start=start,
        end=start + 100,
        status=status,
        attributes=attributes,
    )


def interrupt_event(offset_ms: int = 2, task_id: str = "act-2") -> Span.Event:
    return event(
        "nodestep.interrupt",
        START_NS + offset_ms * MILLISECOND,
        {"nodestep.interrupt.key": "approve", "nodestep.interrupt.task_id": task_id},
    )


def paused_run() -> list[Span]:
    return [
        run_span(status="paused", events=[interrupt_event()]),
        node_span(THINK_ID, "think", 1),
        node_span(ACT_ID, "act", 2, events=[interrupt_event()]),
        span(
            TRACE_ID,
            MODEL_ID,
            "chat gpt-6-luna",
            parent=THINK_ID,
            start=START_NS + MILLISECOND + 10,
            end=START_NS + MILLISECOND + 20,
        ),
    ]


def completed_run(trace_id: str = TRACE_ID) -> list[Span]:
    return [
        run_span(trace_id=trace_id),
        node_span(THINK_ID, "think", 1, trace_id=trace_id),
        node_span(ACT_ID, "act", 2, trace_id=trace_id),
        node_span(ANSWER_ID, "think", 3, trace_id=trace_id),
    ]


def open_run() -> list[Span]:
    return [
        node_span(THINK_ID, "think", 1, parent=MISSING_RUN_ID),
        node_span(ACT_ID, "act", 2, parent=MISSING_RUN_ID),
    ]
