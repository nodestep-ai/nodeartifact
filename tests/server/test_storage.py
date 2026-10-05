import json
import sqlite3
import threading

import pytest
from opentelemetry.proto.trace.v1.trace_pb2 import Span, Status

from nodeartifact.server.models import RunStatus, StatusCode
from nodeartifact.server.storage import TraceStore, TraceStoreError
from payloads import (
    CHILD_ID,
    GRANDCHILD_ID,
    OTHER_TRACE_ID,
    ROOT_ID,
    START_NS,
    TRACE_ID,
    event,
    records,
    span,
)

SECOND = 1_000_000_000
SIBLING_ID = "eee19b7ec3c1b171"


def root_span(trace_id=TRACE_ID, start=START_NS, status=Status.STATUS_CODE_UNSET):
    return span(
        trace_id,
        ROOT_ID,
        "nodestep.graph demo",
        start=start,
        end=start + 1_000_000,
        status=status,
    )


def model_span():
    return span(
        TRACE_ID,
        CHILD_ID,
        "chat gpt-6-luna",
        parent=ROOT_ID,
        start=START_NS + 100,
        end=START_NS + 900,
        status=Status.STATUS_CODE_ERROR,
        attributes={"gen_ai.usage.input_tokens": 12, "gen_ai.usage.output_tokens": 5},
    )


def tool_span():
    return span(
        TRACE_ID,
        GRANDCHILD_ID,
        "execute_tool search",
        parent=CHILD_ID,
        start=START_NS + 200,
        end=START_NS + 300,
        attributes={"gen_ai.usage.input_tokens": 3},
    )


def table_names(connection: sqlite3.Connection, kind: str) -> set[str]:
    rows = connection.execute("SELECT name FROM sqlite_master WHERE type = ?", (kind,))
    return {name for (name,) in rows}


def test_new_database_uses_wal_and_has_the_schema(database_path):
    TraceStore(database_path).close()

    connection = sqlite3.connect(database_path)
    assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert {"resources", "scopes", "spans"} <= table_names(connection, "table")
    assert "spans_listing" in table_names(connection, "index")
    connection.close()


def test_spans_already_stored_are_ignored(store):
    spans = records(root_span(), model_span())

    assert store.add(spans) == 2
    assert store.add(spans) == 0
    (summary,) = store.list_traces()
    assert summary.span_count == 2
    assert summary.input_tokens == 12


def test_identical_resources_and_scopes_are_stored_once(store, database_path):
    store.add(records(root_span()))
    store.add(records(model_span()))

    connection = sqlite3.connect(database_path)
    assert connection.execute("SELECT count(*) FROM resources").fetchone()[0] == 1
    assert connection.execute("SELECT count(*) FROM scopes").fetchone()[0] == 1
    connection.close()


def test_trace_summary_aggregates_all_spans(store):
    store.add(records(root_span(), model_span(), tool_span()))

    (summary,) = store.list_traces()

    assert summary.trace_id == TRACE_ID
    assert summary.name == "nodestep.graph demo"
    assert summary.service_name == "checkout"
    assert summary.status_code == StatusCode.ERROR
    assert (summary.span_count, summary.error_count) == (3, 1)
    assert (summary.start_ns, summary.end_ns) == (START_NS, START_NS + 1_000_000)
    assert summary.duration_ns == 1_000_000
    assert (summary.input_tokens, summary.output_tokens) == (15, 5)
    assert summary.complete


def test_trace_status_is_the_root_status_without_errors(store):
    store.add(records(root_span(status=Status.STATUS_CODE_OK)))

    (summary,) = store.list_traces()

    assert summary.status_code == StatusCode.OK
    assert summary.error_count == 0
    assert (summary.input_tokens, summary.output_tokens) == (None, None)


@pytest.mark.parametrize("value", [True, "12", 1.5])
def test_only_integer_token_counts_are_totalled(store, value):
    store.add(
        records(
            span(
                TRACE_ID,
                ROOT_ID,
                "root",
                attributes={"gen_ai.usage.input_tokens": value},
            )
        )
    )

    (summary,) = store.list_traces()

    assert summary.input_tokens is None


def test_newest_trace_comes_first_and_limit_applies(store):
    store.add(records(root_span()))
    store.add(records(root_span(trace_id=OTHER_TRACE_ID, start=START_NS + 10 * SECOND)))

    assert [summary.trace_id for summary in store.list_traces()] == [
        OTHER_TRACE_ID,
        TRACE_ID,
    ]
    assert [summary.trace_id for summary in store.list_traces(limit=1)] == [
        OTHER_TRACE_ID
    ]


def test_trace_without_its_root_is_listed_as_incomplete(store):
    store.add(records(tool_span(), model_span()))

    (summary,) = store.list_traces()
    assert (summary.name, summary.complete) == ("chat gpt-6-luna", False)
    assert not summary.running

    store.add(records(root_span()))

    (summary,) = store.list_traces()
    assert (summary.name, summary.complete) == ("nodestep.graph demo", True)
    assert summary.span_count == 3


def test_get_trace_lists_spans_by_start_time(store):
    store.add(records(tool_span(), model_span(), root_span()))

    detail = store.get_trace(TRACE_ID)

    assert detail is not None
    assert detail.summary.name == "nodestep.graph demo"
    assert [item.span_id for item in detail.spans] == [ROOT_ID, CHILD_ID, GRANDCHILD_ID]
    model = detail.spans[1]
    assert model.parent_span_id == ROOT_ID
    assert model.status_code == StatusCode.ERROR
    assert model.service_name == "checkout"
    assert (model.input_tokens, model.output_tokens) == (12, 5)
    assert model.duration_ns == 800


def test_get_trace_returns_none_for_unknown_trace(store):
    assert store.get_trace(TRACE_ID) is None


def test_get_span_round_trips_the_record(store):
    (record,) = records(
        span(
            TRACE_ID,
            CHILD_ID,
            "chat gpt-6-luna",
            parent=ROOT_ID,
            kind=Span.SPAN_KIND_CLIENT,
            status=Status.STATUS_CODE_ERROR,
            message="rate limited",
            attributes={
                "gen_ai.request.model": "gpt-6-luna",
                "nested": {"list": [1, 2.5, True, None]},
                "raw": b"\x01",
            },
            events=[
                event("exception", START_NS + 10, {"exception.type": "ValueError"})
            ],
        )
    )
    store.add([record])

    assert store.get_span(TRACE_ID, CHILD_ID) == record


def test_get_span_returns_none_for_unknown_span(store):
    store.add(records(root_span()))

    assert store.get_span(TRACE_ID, CHILD_ID) is None
    assert store.get_span(OTHER_TRACE_ID, ROOT_ID) is None


def test_reopening_a_database_keeps_its_traces(database_path):
    with TraceStore(database_path) as store:
        store.add(records(root_span()))

    with TraceStore(database_path) as store:
        assert [summary.trace_id for summary in store.list_traces()] == [TRACE_ID]


def test_database_created_by_another_program_is_refused(database_path):
    connection = sqlite3.connect(database_path)
    connection.execute("CREATE TABLE invoices (id INTEGER)")
    connection.commit()
    connection.close()

    with pytest.raises(TraceStoreError, match="not created by nodeartifact"):
        TraceStore(database_path)


def test_file_that_is_not_a_database_is_refused(database_path):
    database_path.write_text("hello\n" * 100, encoding="utf-8")

    with pytest.raises(TraceStoreError, match="file is not a database"):
        TraceStore(database_path)


def test_database_with_another_schema_version_is_refused(database_path):
    TraceStore(database_path).close()
    connection = sqlite3.connect(database_path)
    connection.execute("PRAGMA user_version = 99")
    connection.close()

    with pytest.raises(TraceStoreError, match="schema version 99"):
        TraceStore(database_path)


def test_missing_parent_directory_is_reported(tmp_path):
    with pytest.raises(TraceStoreError, match="cannot open"):
        TraceStore(tmp_path / "missing" / "traces.db")


def test_concurrent_writers_store_every_span(store):
    trace_ids = [f"{index + 1:032x}" for index in range(8)]

    def write(trace_id: str) -> None:
        for number in range(1, 26):
            store.add(records(span(trace_id, f"{number:016x}", f"span {number}")))

    threads = [
        threading.Thread(target=write, args=(trace_id,)) for trace_id in trace_ids
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    summaries = store.list_traces()
    assert sorted(summary.trace_id for summary in summaries) == trace_ids
    assert {summary.span_count for summary in summaries} == {25}


def test_huge_token_counts_do_not_break_the_trace_list(store):
    usage = {"gen_ai.usage.input_tokens": 2**62}
    store.add(
        records(
            span(TRACE_ID, ROOT_ID, "root", attributes=usage),
            span(TRACE_ID, CHILD_ID, "child", parent=ROOT_ID, attributes=usage),
        )
    )

    (summary,) = store.list_traces()

    assert summary.input_tokens == 2**63


def run_span(attributes=None, events=(), status=Status.STATUS_CODE_UNSET):
    return span(
        TRACE_ID,
        ROOT_ID,
        "nodestep.graph demo",
        attributes=attributes,
        events=events,
        status=status,
    )


def test_trace_summary_has_the_thread_and_input_of_the_run(store):
    store.add(
        records(
            run_span(
                {
                    "nodestep.thread_id": "th-7f3a9c21",
                    "nodestep.input": "Which ten products sold the most units?",
                }
            ),
            span(
                TRACE_ID,
                CHILD_ID,
                "nodestep.node agent",
                parent=ROOT_ID,
                start=START_NS + 100,
                attributes={
                    "nodestep.thread_id": "th-other",
                    "nodestep.input": "not the run input",
                },
            ),
        )
    )

    (summary,) = store.list_traces()

    assert summary.thread_id == "th-7f3a9c21"
    assert summary.input_preview == "Which ten products sold the most units?"


def test_input_preview_comes_from_the_first_span_that_carries_one(store):
    messages = [
        {"role": "system", "parts": [{"type": "text", "content": "Answer in SQL."}]},
        {"role": "user", "parts": [{"type": "text", "content": "Top stores?"}]},
    ]
    store.add(
        records(
            run_span({"nodestep.thread_id": 42}),
            span(
                TRACE_ID,
                CHILD_ID,
                "chat gpt-6-luna",
                parent=ROOT_ID,
                start=START_NS + 100,
                attributes={"gen_ai.input.messages": json.dumps(messages)},
            ),
        )
    )

    (summary,) = store.list_traces()

    assert summary.thread_id == "42"
    assert summary.input_preview == "Top stores?"


def test_nodestep_input_is_preferred_over_model_messages(store):
    store.add(
        records(
            run_span(
                {
                    "gen_ai.input.messages": json.dumps(
                        [{"role": "user", "content": "from the model"}]
                    )
                }
            ),
            span(
                TRACE_ID,
                CHILD_ID,
                "nodestep.node agent",
                parent=ROOT_ID,
                start=START_NS + 100,
                attributes={
                    "nodestep.input": json.dumps(
                        {"messages": [{"role": "user", "content": "from the run"}]}
                    )
                },
            ),
        )
    )

    (summary,) = store.list_traces()

    assert summary.input_preview == "from the run"


def test_trace_without_thread_or_input_has_none(store):
    store.add(records(run_span()))

    (summary,) = store.list_traces()

    assert (
        summary.thread_id,
        summary.input_preview,
        summary.run_status,
        summary.resumed,
        summary.resume_text,
        summary.branch_id,
    ) == (None, None, None, False, None, None)


@pytest.mark.parametrize(
    ("value", "status"),
    [
        ("completed", RunStatus.COMPLETED),
        ("paused", RunStatus.PAUSED),
        ("interrupted", RunStatus.PAUSED),
        ("cancelled", RunStatus.STOPPED),
        ("failed", RunStatus.FAILED),
        ("error", RunStatus.FAILED),
        ("something new", None),
    ],
)
def test_run_status_comes_from_the_root_span(store, value, status):
    store.add(records(run_span({"nodestep.status": value}), model_span()))

    (summary,) = store.list_traces()
    detail = store.get_trace(TRACE_ID)

    assert summary.run_status == status
    assert detail is not None
    assert detail.summary.run_status == status


def test_root_interrupt_event_without_a_status_means_paused(store):
    interrupt = event("nodestep.interrupt", START_NS, {"nodestep.interrupt.key": "k"})
    store.add(records(run_span({}, [interrupt]), model_span()))

    (summary,) = store.list_traces()

    assert summary.run_status == RunStatus.PAUSED


def test_status_attribute_wins_over_an_interrupt_event(store):
    interrupt = event("nodestep.interrupt", START_NS)
    store.add(records(run_span({"nodestep.status": "completed"}, [interrupt])))

    (summary,) = store.list_traces()

    assert summary.run_status == RunStatus.COMPLETED


@pytest.mark.parametrize(
    ("answers", "text"),
    [
        ('{"clarify": "net"}', "net"),
        ('{"clarify": "net", "approve": true}', "clarify: net, approve: true"),
        ('"net"', "net"),
        ('{"clarify": {"id": "net"}}', '{"id": "net"}'),
        ("not json", "not json"),
    ],
)
def test_resumed_run_has_its_answer(store, answers, text):
    store.add(
        records(run_span({"nodestep.resuming": True, "nodestep.resume": answers}))
    )

    (summary,) = store.list_traces()

    assert (summary.resumed, summary.resume_text) == (True, text)


def test_resumed_run_without_answers_is_still_resumed(store):
    store.add(records(run_span({"nodestep.resuming": True})))

    (summary,) = store.list_traces()

    assert (summary.resumed, summary.resume_text) == (True, None)


def test_run_that_is_not_resuming_is_not_resumed(store):
    store.add(records(run_span({"nodestep.resuming": False})))

    (summary,) = store.list_traces()

    assert not summary.resumed


@pytest.mark.parametrize(("branch", "expected"), [("b-7f3a", "b-7f3a"), ("main", None)])
def test_run_on_a_branch_names_it(store, branch, expected):
    store.add(records(run_span({"nodestep.branch_id": branch})))

    (summary,) = store.list_traces()

    assert summary.branch_id == expected


def test_interrupt_on_a_child_span_alone_does_not_pause_the_run(store):
    store.add(
        records(
            run_span({"nodestep.status": "completed"}),
            span(
                TRACE_ID,
                CHILD_ID,
                "execute_tool ask_user",
                parent=ROOT_ID,
                events=[event("nodestep.interrupt", START_NS)],
            ),
        )
    )

    (summary,) = store.list_traces()

    assert summary.run_status == RunStatus.COMPLETED


def test_root_span_wins_over_a_span_that_started_before_it(store):
    store.add(
        records(
            span(
                TRACE_ID,
                CHILD_ID,
                "nodestep.node agent",
                parent=ROOT_ID,
                start=START_NS - 100,
                attributes={
                    "nodestep.thread_id": "th-early",
                    "nodestep.input": "from the early child",
                },
            ),
            run_span(
                {
                    "nodestep.thread_id": "th-root",
                    "nodestep.input": "from the root",
                    "nodestep.status": "completed",
                }
            ),
        )
    )

    (summary,) = store.list_traces()

    assert (summary.name, summary.thread_id, summary.input_preview) == (
        "nodestep.graph demo",
        "th-root",
        "from the root",
    )
    assert summary.start_ns == START_NS - 100


def test_without_the_root_the_earliest_span_that_carries_a_value_wins(store):
    store.add(
        records(
            span(
                TRACE_ID,
                GRANDCHILD_ID,
                "nodestep.node later",
                parent=ROOT_ID,
                start=START_NS + 200,
                attributes={
                    "nodestep.thread_id": "th-later",
                    "nodestep.input": "later",
                },
            ),
            span(
                TRACE_ID,
                SIBLING_ID,
                "nodestep.node plain",
                parent=ROOT_ID,
                start=START_NS,
            ),
            span(
                TRACE_ID,
                CHILD_ID,
                "nodestep.node earlier",
                parent=ROOT_ID,
                start=START_NS + 100,
                attributes={
                    "nodestep.thread_id": "th-earlier",
                    "nodestep.input": "earlier",
                },
            ),
        )
    )

    (summary,) = store.list_traces()

    assert (summary.name, summary.thread_id, summary.input_preview) == (
        "nodestep.node plain",
        "th-earlier",
        "earlier",
    )


def test_a_running_trace_takes_its_input_from_its_first_step_not_a_sub_agent(store):
    store.add(
        records(
            span(
                TRACE_ID,
                SIBLING_ID,
                "nodestep.node think",
                parent=ROOT_ID,
                start=START_NS,
                attributes={
                    "nodestep.node.name": "think",
                    "nodestep.state.before": json.dumps(
                        {"messages": [{"type": "human", "content": "Write a report"}]}
                    ),
                },
            ),
            span(
                TRACE_ID,
                CHILD_ID,
                "nodestep.graph analyst",
                parent=GRANDCHILD_ID,
                start=START_NS + 500,
                attributes={"nodestep.input": "How many returns per store?"},
            ),
        )
    )

    (summary,) = store.list_traces()

    assert summary.input_preview == "Write a report"


def test_interrupt_on_the_earliest_span_of_a_trace_without_root_does_not_pause_it(
    store,
):
    store.add(
        records(
            span(
                TRACE_ID,
                CHILD_ID,
                "nodestep.graph sub-agent",
                parent=ROOT_ID,
                attributes={
                    "nodestep.status": "interrupted",
                    "nodestep.resuming": True,
                    "nodestep.resume": '{"clarify": "net"}',
                    "nodestep.branch_id": "b-7f3a",
                },
                events=[event("nodestep.interrupt", START_NS)],
            )
        )
    )

    (summary,) = store.list_traces()
    detail = store.get_trace(TRACE_ID)

    assert not summary.complete
    assert summary.run_status is None
    assert (summary.resumed, summary.resume_text, summary.branch_id) == (
        False,
        None,
        None,
    )
    assert detail is not None
    assert detail.summary.run_status is None


def test_full_input_text_is_kept_next_to_the_preview(store):
    question = (
        "Which store has the most returns this year,"
        " and how does that compare with last year?"
    )
    store.add(records(run_span({"nodestep.input": f"  {question}\n"})))

    (summary,) = store.list_traces()

    assert summary.input_text == question
    assert summary.input_preview == question[:79].rstrip() + "…"


def test_trace_spans_come_in_arrival_order_with_their_row(store):
    store.add(records(model_span()))
    store.add(records(root_span(), tool_span()))
    store.add(records(root_span(trace_id=OTHER_TRACE_ID)))

    stored = store.trace_spans(TRACE_ID)

    assert [item.span.span_id for item in stored] == [CHILD_ID, ROOT_ID, GRANDCHILD_ID]
    rows = [item.row for item in stored]
    assert rows == sorted(rows)
    assert stored[0].span == store.get_span(TRACE_ID, CHILD_ID)


def test_trace_spans_after_a_row_skip_the_earlier_ones(store):
    store.add(records(model_span()))
    (first,) = store.trace_spans(TRACE_ID)
    store.add(records(root_span()))

    later = store.trace_spans(TRACE_ID, after=first.row)

    assert [item.span.span_id for item in later] == [ROOT_ID]
    assert store.trace_spans(TRACE_ID, after=later[0].row) == []
    assert store.trace_spans(OTHER_TRACE_ID) == []


def graph_run(trace_id, start, source, *, graph="agent"):
    return span(
        trace_id,
        ROOT_ID,
        f"nodestep.graph {graph}",
        start=start,
        attributes={"nodestep.graph.name": graph, "nodestep.graph.mermaid": source},
    )


def test_latest_graph_source_is_the_newest_run_of_that_graph_and_service(store):
    store.add(records(graph_run(OTHER_TRACE_ID, START_NS, "graph TD\n  old")))
    store.add(records(graph_run(TRACE_ID, START_NS + SECOND, "graph TD\n  new")))
    store.add(
        records(
            graph_run(
                "1af7651916cd43dd8448eb211c80319c",
                START_NS + 2 * SECOND,
                "graph TD\n  other",
                graph="lookup",
            )
        )
    )
    store.add(
        records(
            graph_run(
                "2af7651916cd43dd8448eb211c80319c",
                START_NS + 3 * SECOND,
                "graph TD\n  elsewhere",
            ),
            service="billing",
        )
    )

    assert store.latest_graph_source("agent", "checkout") == "graph TD\n  new"
    assert store.latest_graph_source("agent", "billing") == "graph TD\n  elsewhere"
    assert store.latest_graph_source("agent", None) is None
    assert store.latest_graph_source("missing", "checkout") is None


def open_node(span_id, name, offset, attributes=None):
    return span(
        TRACE_ID,
        span_id,
        f"nodestep.node {name}",
        parent=ROOT_ID,
        start=START_NS + offset,
        attributes={
            "nodestep.graph.name": "text-to-sql-demo",
            "nodestep.node.name": name,
            **(attributes or {}),
        },
    )


def state(*messages):
    return json.dumps({"messages": list(messages), "tool_calls": []})


def test_open_run_is_running_and_named_after_the_run_span_it_will_have(store):
    store.add(
        records(open_node(CHILD_ID, "think", 100), open_node(SIBLING_ID, "act", 200))
    )

    (summary,) = store.list_traces()
    detail = store.get_trace(TRACE_ID)

    assert (summary.running, summary.complete) == (True, False)
    assert summary.name == "nodestep.graph text-to-sql-demo"
    assert summary.run_status is None
    assert detail is not None
    assert detail.summary.running
    assert detail.summary.name == "nodestep.graph text-to-sql-demo"


def test_open_run_has_the_thread_and_last_user_message_of_its_node_spans(store):
    before = state(
        {"id": "m1", "content": "Top stores?", "type": "human"},
        {"id": "m2", "content": "Stores by revenue.", "type": "ai"},
    )
    store.add(
        records(
            open_node(
                CHILD_ID,
                "think",
                100,
                {"nodestep.thread_id": "th-c04e5519", "nodestep.state.before": before},
            )
        )
    )

    (summary,) = store.list_traces()

    assert summary.thread_id == "th-c04e5519"
    assert summary.input_preview == "Top stores?"


def test_state_before_gives_an_input_only_through_a_user_message(store):
    store.add(
        records(
            open_node(
                CHILD_ID,
                "count",
                100,
                {"nodestep.state.before": json.dumps({"count": 1})},
            ),
            open_node(
                SIBLING_ID,
                "reply",
                200,
                {"nodestep.state.before": state({"content": "hi", "type": "ai"})},
            ),
        )
    )

    (summary,) = store.list_traces()

    assert summary.input_preview is None


def test_model_messages_are_preferred_over_the_state_before(store):
    messages = [
        {"role": "user", "parts": [{"type": "text", "content": "From the model"}]}
    ]
    store.add(
        records(
            open_node(
                CHILD_ID,
                "think",
                100,
                {
                    "nodestep.state.before": state(
                        {"content": "From the state", "type": "human"}
                    )
                },
            ),
            span(
                TRACE_ID,
                GRANDCHILD_ID,
                "chat gpt-6-luna",
                parent=CHILD_ID,
                start=START_NS + 150,
                attributes={"gen_ai.input.messages": json.dumps(messages)},
            ),
        )
    )

    (summary,) = store.list_traces()

    assert summary.input_preview == "From the model"


def test_finished_run_is_not_running(store):
    store.add(
        records(
            run_span({"nodestep.status": "completed"}),
            open_node(CHILD_ID, "think", 100),
        )
    )

    (summary,) = store.list_traces()

    assert not summary.running
    assert summary.name == "nodestep.graph demo"
