import json

from opentelemetry.proto.trace.v1.trace_pb2 import Status

from graphs import (
    ACT_ID,
    ANSWER_ID,
    CHILD_NODE_ID,
    CHILD_RUN_ID,
    MERMAID,
    MISSING_NODE_ID,
    MISSING_RUN_ID,
    MODEL_ID,
    THINK_ID,
    TOOL_ID,
    call_span,
    completed_run,
    interrupt_event,
    node_span,
    open_run,
    paused_run,
    run_span,
)
from nodeartifact.server.graph import StepStatus, TraceGraph
from nodeartifact.server.models import RunStatus
from payloads import ROOT_ID, START_NS, TRACE_ID, event, records, span


def steps_of(graph):
    return [
        (step.number, step.span_id, step.node_name, step.mermaid_id, step.status)
        for step in graph.steps
    ]


def test_finished_run_has_its_diagram_and_numbered_steps():
    graph = TraceGraph.from_spans(records(*reversed(completed_run())))

    assert graph is not None
    assert graph.source == MERMAID
    assert not graph.borrowed
    assert (graph.graph_name, graph.service_name) == ("agent", "checkout")
    assert steps_of(graph) == [
        (1, THINK_ID, "think", "think", StepStatus.COMPLETED),
        (2, ACT_ID, "act", "act", StepStatus.COMPLETED),
        (3, ANSWER_ID, "think", "think", StepStatus.COMPLETED),
    ]


def test_steps_carry_their_update_and_input_state_as_pretty_json():
    graph = TraceGraph.from_spans(records(*completed_run()))

    assert graph is not None
    first = graph.steps[0]
    assert first.update == '{\n  "messages": [\n    "from think"\n  ]\n}'
    assert first.state_before == '{\n  "messages": []\n}'
    assert first.duration_ns == 1_000_000


def pretty_state(*messages: str) -> str:
    return json.dumps({"messages": list(messages)}, indent=2)


def state(*messages: str) -> str:
    return json.dumps({"messages": list(messages)})


def test_a_completed_step_has_the_state_after_its_superstep():
    graph = TraceGraph.from_spans(
        records(
            run_span(extra={"nodestep.state.after": state("final")}),
            node_span(THINK_ID, "think", 1, extra={"nodestep.state.before": state()}),
            node_span(ACT_ID, "act", 2, extra={"nodestep.state.before": state("a")}),
            node_span(
                ANSWER_ID, "think", 3, extra={"nodestep.state.before": state("b")}
            ),
        )
    )

    assert graph is not None
    assert [step.state_after for step in graph.steps] == [
        pretty_state("a"),
        pretty_state("b"),
        pretty_state("final"),
    ]


def test_nodes_of_one_superstep_share_the_state_after_it():
    graph = TraceGraph.from_spans(
        records(
            run_span(),
            node_span(THINK_ID, "think", 1),
            node_span(ACT_ID, "act", 1),
            node_span(
                ANSWER_ID, "think", 2, extra={"nodestep.state.before": state("both")}
            ),
        )
    )

    assert graph is not None
    assert [step.state_after for step in graph.steps] == [
        pretty_state("both"),
        pretty_state("both"),
        None,
    ]


def test_a_step_that_did_not_complete_or_that_an_open_run_has_not_left_has_no_state_after():
    paused = TraceGraph.from_spans(records(*paused_run()))
    running = TraceGraph.from_spans(records(*open_run()))

    assert paused is not None
    assert running is not None
    assert [step.state_after is None for step in paused.steps] == [False, True]
    assert [step.state_after is None for step in running.steps] == [False, True]


def test_shortened_step_values_are_marked_by_view():
    graph = TraceGraph.from_spans(
        records(
            run_span(
                extra={
                    "nodestep.state.after": "{}",
                    "nodestep.truncated": ["nodestep.state.after"],
                }
            ),
            node_span(
                THINK_ID, "think", 1, extra={"nodestep.truncated": ["nodestep.update"]}
            ),
            node_span(
                ACT_ID,
                "act",
                2,
                extra={"nodestep.truncated": ["nodestep.state.before"]},
            ),
        )
    )

    assert graph is not None
    assert [step.truncated for step in graph.steps] == [
        ["update", "state_after"],
        ["state_before", "state_after"],
    ]


def test_the_graph_data_for_the_page_leaves_the_step_values_out():
    graph = TraceGraph.from_spans(records(*completed_run()))

    assert graph is not None
    data = graph.model_dump(mode="json")
    assert graph.steps[0].update is not None
    for step in data["steps"]:
        assert not {"update", "state_before", "state_after", "truncated"} & set(step)
    assert data["steps"][0]["duration"] == "1.0 ms"


def test_node_with_an_interrupt_is_paused_and_a_failed_node_is_failed():
    spans = [
        *paused_run(),
        node_span(ANSWER_ID, "think", 3, status=Status.STATUS_CODE_ERROR),
    ]

    graph = TraceGraph.from_spans(records(*spans))

    assert graph is not None
    assert [step.status for step in graph.steps] == [
        StepStatus.COMPLETED,
        StepStatus.PAUSED,
        StepStatus.FAILED,
    ]


def test_node_cancelled_with_the_run_is_stopped():
    spans = [
        run_span(status="cancelled"),
        node_span(
            THINK_ID,
            "think",
            1,
            events=[event("nodestep.cancelled", START_NS + 2_000_000)],
        ),
    ]

    graph = TraceGraph.from_spans(records(*spans))

    assert graph is not None
    assert [step.status for step in graph.steps] == [StepStatus.STOPPED]


def test_nodes_of_a_child_graph_are_not_steps_of_the_run():
    spans = [
        *completed_run(),
        run_span(
            graph="lookup",
            span_id=CHILD_RUN_ID,
            parent=ACT_ID,
            start=START_NS + 2_000_100,
            source='graph TD\n  START([START])\n  find["find"]',
        ),
        node_span(CHILD_NODE_ID, "find", 2, parent=CHILD_RUN_ID, graph="lookup"),
    ]

    graph = TraceGraph.from_spans(records(*spans))

    assert graph is not None
    assert graph.graph_name == "agent"
    assert CHILD_NODE_ID not in [step.span_id for step in graph.steps]


def test_open_run_has_the_steps_of_its_missing_run_span_and_no_diagram():
    spans = [
        *open_run(),
        node_span(CHILD_NODE_ID, "find", 3, parent=CHILD_RUN_ID, graph="lookup"),
        run_span(
            graph="lookup",
            span_id=CHILD_RUN_ID,
            parent=ACT_ID,
            start=START_NS + 2_500_000,
            source=None,
        ),
    ]

    graph = TraceGraph.from_spans(records(*spans))

    assert graph is not None
    assert graph.source is None
    assert graph.run_span_id == MISSING_RUN_ID
    assert graph.graph_name == "agent"
    assert [step.span_id for step in graph.steps] == [THINK_ID, ACT_ID]


def test_open_run_is_not_replaced_by_a_finished_child_run_with_a_diagram():
    spans = [
        *open_run(),
        node_span(CHILD_NODE_ID, "find", 3, parent=CHILD_RUN_ID, graph="lookup"),
        run_span(
            graph="lookup",
            span_id=CHILD_RUN_ID,
            parent=ACT_ID,
            start=START_NS + 2_500_000,
            source='graph TD\n  START([START])\n  find["find"]',
        ),
    ]

    graph = TraceGraph.from_spans(records(*spans))

    assert graph is not None
    assert graph.graph_name == "agent"
    assert graph.run_span_id == MISSING_RUN_ID
    assert graph.finished is False
    assert graph.status is None
    assert [step.span_id for step in graph.steps] == [THINK_ID, ACT_ID]


def test_run_span_under_an_open_outer_span_is_the_finished_run():
    spans = [
        run_span(parent=MISSING_RUN_ID),
        node_span(THINK_ID, "think", 1),
        node_span(ACT_ID, "act", 2),
    ]

    graph = TraceGraph.from_spans(records(*spans))

    assert graph is not None
    assert graph.run_span_id == ROOT_ID
    assert graph.finished is True


def test_borrowed_diagram_is_marked():
    graph = TraceGraph.from_spans(records(*open_run()))

    assert graph is not None
    borrowed = graph.borrow(MERMAID)
    assert (borrowed.source, borrowed.borrowed) == (MERMAID, True)
    assert graph.borrow(None) == graph


def test_trace_without_node_spans_has_no_graph():
    assert TraceGraph.from_spans(records(span(TRACE_ID, ROOT_ID, "checkout"))) is None


def test_run_span_without_nodes_still_has_its_diagram():
    graph = TraceGraph.from_spans(records(run_span()))

    assert graph is not None
    assert graph.source == MERMAID
    assert graph.steps == []


def test_node_without_a_mermaid_id_has_none():
    spans = [
        run_span(),
        node_span(THINK_ID, "think", 1, extra={"nodestep.node.mermaid_id": 7}),
    ]

    graph = TraceGraph.from_spans(records(*spans))

    assert graph is not None
    assert graph.steps[0].mermaid_id is None


def test_step_lookup_by_span_id():
    graph = TraceGraph.from_spans(records(*completed_run()))

    assert graph is not None
    assert graph.step(ACT_ID) == graph.steps[1]
    assert graph.step("0000000000000000") is None


def entries_of(graph):
    return [(step.node_name, step.entered_from) for step in graph.steps]


def test_each_step_names_the_nodes_it_was_entered_from():
    graph = TraceGraph.from_spans(records(*completed_run()))

    assert graph is not None
    assert entries_of(graph) == [
        ("think", ["START"]),
        ("act", ["think"]),
        ("think", ["act"]),
    ]


def test_completed_run_ends_from_the_nodes_of_its_last_superstep():
    graph = TraceGraph.from_spans(records(*completed_run()))

    assert graph is not None
    assert graph.finished
    assert graph.status == RunStatus.COMPLETED
    assert graph.ended_from == ["think"]


def test_nodes_of_one_superstep_share_their_entries():
    spans = [
        run_span(),
        node_span(THINK_ID, "think", 1),
        node_span(ACT_ID, "act", 2),
        node_span(ANSWER_ID, "answer", 2, extra={"nodestep.step": 2}),
        node_span(MODEL_ID, "reply", 3),
    ]

    graph = TraceGraph.from_spans(records(*spans))

    assert graph is not None
    assert [step.superstep for step in graph.steps] == [1, 2, 2, 3]
    assert entries_of(graph) == [
        ("think", ["START"]),
        ("act", ["think"]),
        ("answer", ["think"]),
        ("reply", ["act", "answer"]),
    ]
    assert graph.ended_from == ["reply"]


def test_resumed_run_is_not_entered_from_start():
    spans = [run_span(resuming=True), node_span(ACT_ID, "act", 1)]

    graph = TraceGraph.from_spans(records(*spans))

    assert graph is not None
    assert entries_of(graph) == [("act", [])]


def test_paused_run_has_not_ended():
    graph = TraceGraph.from_spans(records(*paused_run()))

    assert graph is not None
    assert graph.finished
    assert graph.status == RunStatus.PAUSED
    assert graph.ended_from == []


def test_open_run_is_not_finished_and_has_no_status():
    graph = TraceGraph.from_spans(records(*open_run()))

    assert graph is not None
    assert not graph.finished
    assert graph.status is None
    assert graph.ended_from == []
    assert entries_of(graph) == [("think", ["START"]), ("act", ["think"])]


def test_steps_without_a_superstep_follow_each_other():
    spans = [
        run_span(),
        node_span(THINK_ID, "think", 1, extra={"nodestep.step": "one"}),
        node_span(ACT_ID, "act", 2, extra={"nodestep.step": None}),
    ]

    graph = TraceGraph.from_spans(records(*spans))

    assert graph is not None
    assert [step.superstep for step in graph.steps] == [None, None]
    assert entries_of(graph) == [("think", ["START"]), ("act", ["think"])]


def test_step_without_a_diagram_id_is_skipped_in_entries():
    spans = [
        run_span(),
        node_span(THINK_ID, "think", 1, extra={"nodestep.node.mermaid_id": 7}),
        node_span(ACT_ID, "act", 2),
    ]

    graph = TraceGraph.from_spans(records(*spans))

    assert graph is not None
    assert entries_of(graph) == [("think", ["START"]), ("act", [])]


def test_graph_data_for_the_page_has_readable_durations():
    graph = TraceGraph.from_spans(records(*completed_run()))

    assert graph is not None
    data = graph.model_dump(mode="json")
    assert data["steps"][0]["duration"] == "1.0 ms"
    assert data["steps"][0]["status"] == "completed"
    assert data["ended_from"] == ["think"]


def same_task(name: str) -> dict:
    return {"nodestep.task_id": name}


def clarification_run(*, event_on_node: bool = True) -> list:
    question = interrupt_event(4, task_id="act")
    return [
        run_span(status="paused", events=[question]),
        node_span(THINK_ID, "think", 1, extra=same_task("think")),
        node_span(ACT_ID, "act", 2, extra=same_task("act")),
        node_span(ANSWER_ID, "think", 3, extra=same_task("think")),
        node_span(
            MODEL_ID,
            "act",
            4,
            extra=same_task("act"),
            events=[question] if event_on_node else [],
        ),
    ]


def test_only_the_node_span_that_paused_is_paused():
    graph = TraceGraph.from_spans(records(*clarification_run()))

    assert graph is not None
    assert [step.status for step in graph.steps] == [
        StepStatus.COMPLETED,
        StepStatus.COMPLETED,
        StepStatus.COMPLETED,
        StepStatus.PAUSED,
    ]


def test_an_interrupt_on_the_run_only_pauses_the_last_span_of_its_task():
    graph = TraceGraph.from_spans(records(*clarification_run(event_on_node=False)))

    assert graph is not None
    assert [step.status for step in graph.steps] == [
        StepStatus.COMPLETED,
        StepStatus.COMPLETED,
        StepStatus.COMPLETED,
        StepStatus.PAUSED,
    ]


def test_a_step_lists_the_tool_calls_that_failed():
    spans = [
        *completed_run(),
        call_span(
            TOOL_ID, "run_sql", 2, parent=ACT_ID, status=Status.STATUS_CODE_ERROR
        ),
        call_span(CHILD_RUN_ID, "list_tables", 2, parent=ACT_ID),
        call_span(
            CHILD_NODE_ID,
            "gpt-6-luna",
            1,
            parent=THINK_ID,
            operation="chat",
            status=Status.STATUS_CODE_ERROR,
        ),
    ]

    graph = TraceGraph.from_spans(records(*spans))

    assert graph is not None
    assert [step.failed_tools for step in graph.steps] == [[], ["run_sql"], []]
    assert [step.status for step in graph.steps] == [StepStatus.COMPLETED] * 3


def current_of(spans, source=MERMAID):
    graph = TraceGraph.from_spans(records(*spans))
    assert graph is not None
    return graph.borrow(source).current


def test_an_open_run_is_in_the_only_node_after_the_last_finished_one():
    assert current_of(open_run()) == "think"
    assert current_of(open_run()[:1]) == "act"


def test_an_open_run_is_in_the_node_whose_calls_arrived_before_it():
    spans = [
        *open_run()[:1],
        call_span(TOOL_ID, "lookup", 3, parent=MISSING_NODE_ID, mermaid_id="act"),
    ]
    source = MERMAID + '\n  think -->|"again"| think'

    assert current_of(spans, source) == "act"


def test_calls_of_a_child_graph_or_of_several_nodes_name_no_node():
    child = call_span(
        TOOL_ID, "find", 3, parent=MISSING_NODE_ID, graph="lookup", mermaid_id="find"
    )
    other = call_span(MODEL_ID, "plan", 3, parent=CHILD_RUN_ID, mermaid_id="plan")
    act = call_span(
        CHILD_NODE_ID, "lookup", 3, parent=MISSING_NODE_ID, mermaid_id="act"
    )

    assert current_of([*open_run(), child]) == "think"
    assert current_of([*open_run(), act, other]) is None


def test_an_open_run_after_a_node_with_several_successors_names_no_node():
    source = MERMAID + '\n  think -->|"again"| think'

    assert current_of(open_run()[:1], source) is None
    assert current_of(open_run()[:1], None) is None


def test_successors_are_read_from_both_edge_formats():
    with_ids = "\n".join(
        [
            "graph TD",
            "  START([START])",
            '  think["think"]',
            "  START START-think@--> think",
            '  act["act"]',
            '  think think-act@-->|"tools"| act',
            '  think think-END@-->|"done"| END([END])',
            "  act act-think@-.-> think",
        ]
    )

    assert current_of(open_run(), with_ids) == "think"
    assert current_of(open_run()[:1], with_ids) == "act"


def test_a_finished_run_has_no_current_node():
    graph = TraceGraph.from_spans(records(*completed_run()))

    assert graph is not None
    assert graph.current is None
    assert graph.model_dump(mode="json")["current"] is None
