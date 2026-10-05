import pytest

from nodeartifact.server.display import (
    AttributeGroups,
    AttributeRow,
    InputPreview,
    StateView,
    Timeline,
    format_attribute,
    format_count,
    format_duration,
    format_offset,
    format_timestamp,
    format_value,
    is_error,
    kind_name,
    status_name,
    trace_status,
    trace_status_class,
)
from nodeartifact.server.models import (
    JsonValue,
    RunStatus,
    SpanSummary,
    TraceDetail,
    TraceSummary,
)


def summary_span(span_id: str, parent: str | None, start: int, end: int) -> SpanSummary:
    return SpanSummary(
        span_id=span_id,
        parent_span_id=parent,
        name=f"span {span_id}",
        kind=1,
        start_ns=start,
        end_ns=end,
        status_code=0,
        service_name="checkout",
        input_tokens=None,
        output_tokens=None,
    )


def trace_summary(
    start_ns: int = 0,
    end_ns: int = 1,
    status_code: int = 0,
    run_status: RunStatus | None = None,
) -> TraceSummary:
    return TraceSummary(
        trace_id="5b8efff798038103d269b633813fc60c",
        name="root",
        service_name="checkout",
        status_code=status_code,
        start_ns=start_ns,
        end_ns=end_ns,
        span_count=1,
        error_count=0,
        input_tokens=None,
        output_tokens=None,
        complete=True,
        thread_id=None,
        input_text=None,
        input_preview=None,
        run_status=run_status,
        resumed=False,
        resume_text=None,
        branch_id=None,
    )


def detail(*spans: SpanSummary) -> TraceDetail:
    ordered = sorted(spans, key=lambda item: (item.start_ns, item.span_id))
    return TraceDetail(
        summary=trace_summary(
            start_ns=min(item.start_ns for item in spans),
            end_ns=max(item.end_ns for item in spans),
        ),
        spans=ordered,
    )


def layout(timeline: Timeline) -> list[tuple[str, int]]:
    return [(row.span.span_id, row.depth) for row in timeline.rows]


@pytest.mark.parametrize(
    ("ns", "text"),
    [
        (0, "0 ns"),
        (999, "999 ns"),
        (1_234, "1.2 µs"),
        (12_345_678, "12.3 ms"),
        (2_500_000_000, "2.50 s"),
        (-5, "-"),
    ],
)
def test_format_duration(ns, text):
    assert format_duration(ns) == text


@pytest.mark.parametrize(
    ("ns", "text"), [(0, "+0 ns"), (1_500, "+1.5 µs"), (-2_000_000, "-2.0 ms")]
)
def test_format_offset(ns, text):
    assert format_offset(ns) == text


def test_format_timestamp_is_utc_with_milliseconds():
    assert format_timestamp(1_700_000_000_123_456_789) == "2023-11-14 22:13:20.123 UTC"


@pytest.mark.parametrize(
    ("value", "text"),
    [
        ("plain", "plain"),
        (True, "true"),
        (False, "false"),
        (None, "null"),
        (3, "3"),
        (0.25, "0.25"),
        (["a", 1], '[\n  "a",\n  1\n]'),
        (["stop"], '["stop"]'),
        ([{"a": 1}], '[\n  {\n    "a": 1\n  }\n]'),
        ({"key": "välue"}, '{\n  "key": "välue"\n}'),
    ],
)
def test_format_value(value, text):
    assert format_value(value) == text


@pytest.mark.parametrize(
    ("value", "text"),
    [
        ('{"order_id": "A-1001"}', '{\n  "order_id": "A-1001"\n}'),
        ('[{"role": "user"}]', '[\n  {\n    "role": "user"\n  }\n]'),
        ('["stop"]', '["stop"]'),
        ('"quoted"', '"quoted"'),
        ("12", "12"),
        ("{not json", "{not json"),
        ("plain", "plain"),
        (["a", 1], '[\n  "a",\n  1\n]'),
        (3, "3"),
    ],
)
def test_format_attribute_pretty_prints_json_objects_and_arrays(value, text):
    assert format_attribute(value) == text


@pytest.mark.parametrize(
    ("value", "text"),
    [(None, "-"), (0, "0"), (999, "999"), (1402, "1,402"), (6_092_000, "6,092,000")],
)
def test_format_count(value, text):
    assert format_count(value) == text


@pytest.mark.parametrize(
    ("run_status", "label", "css_class"),
    [
        (RunStatus.COMPLETED, "Completed", "completed"),
        (RunStatus.PAUSED, "Paused", "paused"),
        (RunStatus.STOPPED, "Stopped", "stopped"),
        (RunStatus.FAILED, "Failed", "failed"),
    ],
)
def test_trace_status_follows_the_run_status(run_status, label, css_class):
    summary = trace_summary(status_code=2, run_status=run_status)

    assert (trace_status(summary), trace_status_class(summary)) == (label, css_class)


@pytest.mark.parametrize(
    ("status_code", "label", "css_class"),
    [(2, "Failed", "failed"), (1, "Completed", "completed"), (0, "unset", "")],
)
def test_trace_status_without_a_run_status_is_the_span_status(
    status_code, label, css_class
):
    summary = trace_summary(status_code=status_code)

    assert (trace_status(summary), trace_status_class(summary)) == (label, css_class)


@pytest.mark.parametrize(
    ("value", "text"),
    [
        ("Which store has the most returns?", "Which store has the most returns?"),
        ("  two\n  lines ", "two lines"),
        ('"quoted"', "quoted"),
        ('{"messages": [{"role": "user", "content": "hi"}]}', "hi"),
        (
            [
                {"role": "user", "content": "first"},
                {"role": "assistant", "content": "answer"},
                {"role": "user", "content": "second"},
            ],
            "second",
        ),
        ([{"role": "assistant", "content": "only"}], "only"),
        (
            [
                {
                    "role": "user",
                    "parts": [
                        {"type": "text", "content": "a"},
                        {"type": "text", "content": "b"},
                    ],
                }
            ],
            "a b",
        ),
        ([{"role": "user", "content": [{"type": "text", "text": "open"}]}], "open"),
        ({"question": "why"}, '{"question": "why"}'),
        (["x", 1], '["x", 1]'),
        (7, "7"),
        ("x" * 100, "x" * 79 + "…"),
        ("", None),
        (None, None),
        ("{not json", "{not json"),
        (
            {
                "messages": [
                    {"id": "m1", "type": "human", "content": "first"},
                    {"id": "m2", "type": "ai", "content": "answer"},
                    {"id": "m3", "type": "human", "content": "second"},
                ]
            },
            "second",
        ),
        ([{"type": "ai", "content": "only", "tool_calls": []}], "only"),
    ],
)
def test_input_preview(value, text):
    line = InputPreview.line(value)

    assert (line and InputPreview.shorten(line)) == text


def test_kind_and_status_names():
    assert (kind_name(3), kind_name(9)) == ("client", "kind 9")
    assert (status_name(0), status_name(2), status_name(7)) == (
        "unset",
        "error",
        "status 7",
    )
    assert (is_error(2), is_error(1), is_error(0)) == (True, False, False)


def test_timeline_nests_children_under_their_parents():
    trace = detail(
        summary_span("a", None, 0, 1000),
        summary_span("b", "a", 100, 600),
        summary_span("c", "b", 200, 300),
        summary_span("d", "a", 500, 1000),
    )

    assert layout(Timeline.from_trace(trace)) == [
        ("a", 0),
        ("b", 1),
        ("c", 2),
        ("d", 1),
    ]


def test_timeline_puts_spans_with_a_missing_parent_at_the_top_level():
    trace = detail(
        summary_span("b", "missing", 100, 600),
        summary_span("c", "b", 200, 300),
        summary_span("x", None, 300, 900),
    )

    assert layout(Timeline.from_trace(trace)) == [("b", 0), ("c", 1), ("x", 0)]


def test_timeline_shows_cycles_and_self_parents_once():
    trace = detail(
        summary_span("a", "b", 0, 1000),
        summary_span("b", "a", 100, 600),
        summary_span("c", "c", 200, 300),
    )

    rows = layout(Timeline.from_trace(trace))

    assert sorted(span_id for span_id, _ in rows) == ["a", "b", "c"]
    assert ("c", 0) in rows


def test_timeline_bars_are_proportional_to_duration():
    trace = detail(summary_span("a", None, 0, 1000), summary_span("b", "a", 500, 1000))

    root, child = Timeline.from_trace(trace).rows

    assert (root.offset, root.width) == (0, 1000)
    assert (child.offset, child.width) == (500, 500)


def test_short_spans_keep_a_visible_bar_inside_the_timeline():
    trace = detail(
        summary_span("a", None, 0, 1_000_000),
        summary_span("b", "a", 999_999, 1_000_000),
    )

    child = Timeline.from_trace(trace).rows[1]

    assert child.width == 2
    assert child.offset + child.width <= 1000


def test_zero_length_trace_has_visible_bars():
    (row,) = Timeline.from_trace(detail(summary_span("a", None, 5, 5))).rows

    assert (row.offset, row.width) == (0, 2)


def test_attributes_are_grouped_by_prefix_and_sorted():
    groups = AttributeGroups.from_attributes(
        {
            "z.other": 1,
            "gen_ai.usage.input_tokens": 3,
            "nodestep.node.name": "plan",
            "a.other": "x",
            "gen_ai.request.model": "gpt-6-luna",
        }
    )

    assert groups.gen_ai == [
        AttributeRow(key="gen_ai.request.model", text="gpt-6-luna"),
        AttributeRow(key="gen_ai.usage.input_tokens", text="3"),
    ]
    assert groups.nodestep == [AttributeRow(key="nodestep.node.name", text="plan")]
    assert groups.other == [
        AttributeRow(key="a.other", text="x"),
        AttributeRow(key="z.other", text="1"),
    ]
    assert groups.state == []


def test_json_attribute_values_are_pretty_printed():
    groups = AttributeGroups.from_attributes(
        {
            "gen_ai.tool.call.arguments": '{"order_id": "A-1001"}',
            "nodestep.update": '{"count": 2}',
            "http.route": "/v1/traces",
        }
    )

    assert groups.gen_ai == [
        AttributeRow(
            key="gen_ai.tool.call.arguments", text='{\n  "order_id": "A-1001"\n}'
        )
    ]
    assert groups.nodestep == [
        AttributeRow(key="nodestep.update", text='{\n  "count": 2\n}')
    ]
    assert groups.other == [AttributeRow(key="http.route", text="/v1/traces")]


def test_token_count_attributes_use_thousands_separators():
    groups = AttributeGroups.from_attributes(
        {
            "gen_ai.usage.input_tokens": 1480,
            "gen_ai.usage.output_tokens": "12000",
            "nodestep.superstep": 12000,
        }
    )

    assert groups.gen_ai == [
        AttributeRow(key="gen_ai.usage.input_tokens", text="1,480"),
        AttributeRow(key="gen_ai.usage.output_tokens", text="12000"),
    ]
    assert groups.nodestep == [AttributeRow(key="nodestep.superstep", text="12000")]


def test_state_attributes_are_pretty_printed_before_then_after():
    groups = AttributeGroups.from_attributes(
        {
            "nodestep.state.after": '{"count": 2}',
            "nodestep.state.before": '{"count": 1, "items": ["x"]}',
        }
    )

    assert groups.state == [
        StateView(
            key="nodestep.state.before",
            text='{\n  "count": 1,\n  "items": [\n    "x"\n  ]\n}',
            valid_json=True,
        ),
        StateView(
            key="nodestep.state.after", text='{\n  "count": 2\n}', valid_json=True
        ),
    ]
    assert groups.nodestep == []


def test_state_that_is_not_valid_json_is_kept_as_received():
    truncated = '{"messages": ["hello", "wor'

    groups = AttributeGroups.from_attributes({"nodestep.state.before": truncated})

    assert groups.state == [
        StateView(
            key="nodestep.state.before",
            text=truncated,
            valid_json=False,
            truncated=False,
        )
    ]


def test_attributes_listed_in_nodestep_truncated_are_marked():
    groups = AttributeGroups.from_attributes(
        {
            "nodestep.state.before": '{"count": 1}',
            "nodestep.state.after": '{"count": 2}',
            "gen_ai.input.messages": "[]",
            "nodestep.truncated": ["nodestep.state.after", "gen_ai.input.messages"],
        }
    )

    assert [view.truncated for view in groups.state] == [False, True]
    assert groups.gen_ai == [
        AttributeRow(key="gen_ai.input.messages", text="[]", truncated=True)
    ]
    assert groups.truncated == ["nodestep.state.after", "gen_ai.input.messages"]


@pytest.mark.parametrize("value", ["nodestep.state.after", 3, None])
def test_nodestep_truncated_that_is_not_a_list_of_names_marks_nothing(value):
    groups = AttributeGroups.from_attributes(
        {"nodestep.state.after": "{}", "nodestep.truncated": value}
    )

    assert groups.truncated == []
    assert not groups.state[0].truncated


def test_empty_when_the_span_has_no_attributes():
    assert AttributeGroups.from_attributes({}).empty
    assert not AttributeGroups.from_attributes({"nodestep.state.after": "{}"}).empty
    assert not AttributeGroups.from_attributes({"http.method": "GET"}).empty


def test_state_that_is_not_a_string_is_shown_as_json():
    value: JsonValue = {"count": 2}

    groups = AttributeGroups.from_attributes({"nodestep.state.after": value})

    assert groups.state == [
        StateView(
            key="nodestep.state.after", text='{\n  "count": 2\n}', valid_json=True
        )
    ]
