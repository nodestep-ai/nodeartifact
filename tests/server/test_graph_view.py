import asyncio
import html
import json
import re
from importlib.resources import files

import pytest
from markupsafe import escape
from opentelemetry.proto.trace.v1.trace_pb2 import Status

from conftest import BASE_URL
from graphs import (
    ACT_ID,
    ANSWER_ID,
    MERMAID,
    MISSING_RUN_ID,
    THINK_ID,
    TOOL_ID,
    call_span,
    completed_run,
    node_span,
    open_run,
    paused_run,
    run_span,
)
from nodeartifact.server.app import create_app
from nodeartifact.server.live import LiveTrace
from payloads import (
    OTHER_TRACE_ID,
    ROOT_ID,
    START_NS,
    TRACE_ID,
    event,
    records,
    span,
)

MERMAID_SCRIPT = (
    '<script src="https://cdn.jsdelivr.net/npm/mermaid@12.0.0/dist/mermaid.min.js"'
    ' integrity="sha384-xzghz1GQ5u9HCpVskeDPqMsdogD1yvuMQbEK53+wi+G70+6J1AG0L2cfi9PHjDWI"'
    ' crossorigin="anonymous"></script>'
)
GRAPH_DATA = re.compile(
    r'<script type="application/json" id="trace-graph-data">(.*?)</script>', re.S
)


def graph_data(page: str) -> dict:
    match = GRAPH_DATA.search(page)
    assert match is not None
    return json.loads(match.group(1))


def policy(response) -> dict[str, str]:
    directives = response.headers["content-security-policy"].split(";")
    return dict(item.strip().split(" ", 1) for item in directives if item.strip())


def events(body: str) -> list[tuple[str | None, str, dict]]:
    parsed = []
    for block in body.split("\n\n"):
        fields = dict(
            line.split(": ", 1) for line in block.splitlines() if ": " in line
        )
        if "event" in fields:
            parsed.append(
                (fields.get("id"), fields["event"], json.loads(fields["data"]))
            )
    return parsed


@pytest.fixture
def quick_live(monkeypatch):
    monkeypatch.setattr(LiveTrace, "poll_seconds", 0.01)
    monkeypatch.setattr(LiveTrace, "idle_seconds", 0.05)


def test_trace_with_a_graph_opens_on_the_graph_view(client, store):
    store.add(records(*completed_run()))

    page = client.get(f"/traces/{TRACE_ID}").text

    assert (
        f'<pre class="mermaid nodestep-mermaid" id="trace-diagram">{escape(MERMAID)}</pre>'
        in page
    )
    assert (
        f'<a class="nodestep-tab" href="{BASE_URL}/traces/{TRACE_ID}" aria-current="page">'
        "Graph</a>" in page
    )
    assert (
        f'<a class="nodestep-tab" href="{BASE_URL}/traces/{TRACE_ID}/timeline">'
        "Timeline</a>" in page
    )
    assert 'class="nodestep-timeline"' not in page


def test_graph_view_loads_mermaid_pinned_and_its_own_script(client, store):
    store.add(records(*completed_run()))

    page = client.get(f"/traces/{TRACE_ID}").text

    assert MERMAID_SCRIPT in page
    assert f'<script src="{BASE_URL}/static/trace.js"></script>' in page
    assert page.index(MERMAID_SCRIPT) < page.index("static/trace.js")
    script_type = client.get("/static/trace.js").headers["content-type"]
    assert script_type.split(";")[0] in ("text/javascript", "application/javascript")


def test_graph_view_policy_allows_only_the_pinned_mermaid_file(client, store):
    store.add(records(*completed_run()))

    directives = policy(client.get(f"/traces/{TRACE_ID}"))

    assert directives["default-src"] == "'none'"
    assert directives["script-src"] == (
        "'self' https://cdn.jsdelivr.net/npm/mermaid@12.0.0/dist/mermaid.min.js"
    )
    assert directives["style-src"] == "'self' 'unsafe-inline'"
    assert directives["connect-src"] == "'self'"


def test_timeline_tab_keeps_the_strict_style_policy_and_loads_no_mermaid(client, store):
    store.add(records(*completed_run()))

    response = client.get(f"/traces/{TRACE_ID}/timeline")

    assert response.status_code == 200
    page = response.text
    assert 'class="nodestep-timeline"' in page
    assert "mermaid" not in page
    assert "trace.js" not in page
    assert (
        f'<a class="nodestep-tab" href="{BASE_URL}/traces/{TRACE_ID}/timeline"'
        ' aria-current="page">Timeline</a>'
    ) in page
    directives = policy(response)
    assert directives["style-src"] == "'self'"
    assert directives["script-src"] == "'self'"


def test_trace_without_a_graph_shows_the_timeline_without_tabs(client, store):
    store.add(records(span(TRACE_ID, ROOT_ID, "checkout")))

    for path in (f"/traces/{TRACE_ID}", f"/traces/{TRACE_ID}/timeline"):
        page = client.get(path).text
        assert 'class="nodestep-timeline"' in page
        assert ">Graph</a>" not in page
        assert "mermaid" not in page


def test_unknown_trace_has_no_timeline(client, store):
    store.add(records(*completed_run()))

    assert client.get(f"/traces/{OTHER_TRACE_ID}/timeline").status_code == 404
    assert client.get("/traces/not-a-trace/timeline").status_code == 404


def test_graph_data_lists_the_steps_with_their_edges_but_not_their_values(
    client, store
):
    store.add(records(*completed_run()))

    data = graph_data(client.get(f"/traces/{TRACE_ID}").text)

    assert [
        (step["number"], step["mermaid_id"], step["entered_from"])
        for step in data["steps"]
    ] == [(1, "think", ["START"]), (2, "act", ["think"]), (3, "think", ["act"])]
    assert data["ended_from"] == ["think"]
    assert data["finished"] is True
    assert data["steps"][0]["duration"] == "1.0 ms"
    assert "update" not in data["steps"][0]
    assert "state_before" not in data["steps"][0]


def test_step_values_cannot_close_or_open_a_script_element(client, store):
    store.add(
        records(
            run_span(),
            node_span(
                THINK_ID,
                "think",
                1,
                extra={"nodestep.update": '{"text": "</script><script>alert(1)"}'},
            ),
        )
    )

    page = client.get(f"/traces/{TRACE_ID}").text

    assert "</script><script>alert(1)" not in page
    assert "&lt;/script&gt;&lt;script&gt;alert(1)" in detail_of(page)


def test_steps_are_numbered_and_paused_and_failed_steps_are_marked(client, store):
    store.add(
        records(
            *paused_run(),
            node_span(ANSWER_ID, "think", 3, status=Status.STATUS_CODE_ERROR),
        )
    )

    page = client.get(f"/traces/{TRACE_ID}").text
    items = re.findall(r'<li><a class="nodestep-panel-item".*?</a></li>', page, re.S)

    assert len(items) == 3
    assert [re.findall(r'data-step="(\d+)"', item) for item in items] == [
        ["1"],
        ["2"],
        ["3"],
    ]
    assert f'href="{BASE_URL}/traces/{TRACE_ID}?step=1#trace-step-detail"' in items[0]
    assert "nodestep-badge" not in items[0]
    assert (
        '<span class="nodestep-badge nodestep-badge-paused">Paused</span>' in items[1]
    )
    assert (
        '<span class="nodestep-badge nodestep-badge-failed">Failed</span>' in items[2]
    )


def test_step_detail_shows_the_last_step_without_a_script(client, store):
    store.add(records(*completed_run()))

    page = html.unescape(client.get(f"/traces/{TRACE_ID}").text)

    detail = page.split('id="trace-step-detail"')[1]
    assert "Step 3 of 3" in detail
    assert '"from think"' in detail
    assert "State before" in detail
    assert f"/traces/{TRACE_ID}/spans/{ANSWER_ID}" in detail


def test_finished_run_is_not_live(client, store):
    store.add(records(*completed_run()))

    page = client.get(f"/traces/{TRACE_ID}").text

    assert 'data-live="false"' in page
    assert (
        '<span class="nodestep-badge nodestep-badge-completed">Completed</span>' in page
    )


def test_open_run_is_live_and_borrows_the_diagram_of_an_earlier_run(client, store):
    store.add(records(*completed_run(trace_id=OTHER_TRACE_ID)))
    store.add(records(*open_run()))

    page = client.get(f"/traces/{TRACE_ID}").text

    assert 'data-live="true"' in page
    assert f'data-live-url="{BASE_URL}/traces/{TRACE_ID}/live?after=' in page
    assert (
        '<span class="nodestep-badge nodestep-badge-running" id="trace-status">Running</span>'
        in page
    )
    assert "earlier run" in page
    assert graph_data(page)["borrowed"] is True
    assert graph_data(page)["run_span_id"] == MISSING_RUN_ID


def test_open_run_names_the_node_it_is_in_on_the_page_and_in_the_stream(
    client, store, quick_live
):
    store.add(records(*completed_run(trace_id=OTHER_TRACE_ID)))
    store.add(records(*open_run()))

    page = graph_data(client.get(f"/traces/{TRACE_ID}").text)
    sent = events(client.get(f"/traces/{TRACE_ID}/live").text)

    assert page["current"] == "think"
    assert sent[0][2]["graph"]["current"] == "think"
    assert sent[0][2]["graph"]["borrowed"] is True


def test_open_run_without_any_diagram_still_lists_its_steps(client, store):
    store.add(records(*open_run()))

    page = client.get(f"/traces/{TRACE_ID}").text

    assert '<pre class="mermaid' not in page
    assert "mermaid.min.js" not in page
    assert "No diagram yet" in page
    assert len(re.findall(r'class="nodestep-panel-item"', page)) == 2


def test_live_stream_of_a_finished_run_sends_its_spans_and_ends(client, store):
    store.add(records(*completed_run()))

    response = client.get(f"/traces/{TRACE_ID}/live")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-store"
    sent = events(response.text)
    assert [name for _, name, _ in sent] == ["spans", "end"]
    row, _, data = sent[0]
    assert row is not None
    assert int(row) > 0
    assert {item["span_id"] for item in data["spans"]} == {
        ROOT_ID,
        THINK_ID,
        ACT_ID,
        ANSWER_ID,
    }
    assert [step["number"] for step in data["graph"]["steps"]] == [1, 2, 3]
    assert sent[1][2] == {"status": "completed"}


def test_live_stream_starts_after_the_last_event_id(client, store, quick_live):
    store.add(records(*open_run()))
    first = events(client.get(f"/traces/{TRACE_ID}/live?after=0").text)
    seen = first[0][0]
    store.add(records(run_span(status="failed")))

    for request in (
        {"headers": {"Last-Event-ID": seen}},
        {"params": {"after": seen}},
    ):
        sent = events(client.get(f"/traces/{TRACE_ID}/live", **request).text)
        assert [name for _, name, _ in sent] == ["spans", "end"]
        assert [item["span_id"] for item in sent[0][2]["spans"]] == [ROOT_ID]
        assert sent[1][2] == {"status": "failed"}


def test_live_stream_of_a_quiet_open_run_stops_with_an_idle_event(
    client, store, quick_live
):
    store.add(records(*open_run()))

    sent = events(client.get(f"/traces/{TRACE_ID}/live").text)

    assert [name for _, name, _ in sent] == ["spans", "idle"]
    assert sent[0][2]["graph"]["finished"] is False
    assert [item["name"] for item in sent[0][2]["spans"]] == [
        "nodestep.node think",
        "nodestep.node act",
    ]


def test_live_stream_without_new_spans_sends_none(client, store, quick_live):
    store.add(records(*open_run()))
    (seen, _, _), _ = events(client.get(f"/traces/{TRACE_ID}/live").text)

    sent = events(client.get(f"/traces/{TRACE_ID}/live?after={seen}").text)

    assert [name for _, name, _ in sent] == ["idle"]


def test_live_stream_span_summaries_carry_status_and_duration(client, store):
    store.add(
        records(
            run_span(status="failed"),
            node_span(THINK_ID, "think", 1, status=Status.STATUS_CODE_ERROR),
        )
    )

    (_, _, data), _ = events(client.get(f"/traces/{TRACE_ID}/live").text)

    think = next(item for item in data["spans"] if item["span_id"] == THINK_ID)
    assert think == {
        "span_id": THINK_ID,
        "parent_span_id": ROOT_ID,
        "name": "nodestep.node think",
        "status": "error",
        "duration": "1.0 ms",
    }


def test_live_stream_of_spans_outside_a_graph_ends_at_once(client, store):
    store.add(records(span(TRACE_ID, ROOT_ID, "checkout", start=START_NS)))

    sent = events(client.get(f"/traces/{TRACE_ID}/live").text)

    assert [name for _, name, _ in sent] == ["spans", "end"]
    assert sent[0][2]["graph"] is None
    assert sent[1][2] == {"status": None}


@pytest.mark.parametrize("spec_version", ["2.3", "2.4"])
async def test_live_stream_stops_when_the_client_disconnects(
    store, monkeypatch, spec_version
):
    monkeypatch.setattr(LiveTrace, "poll_seconds", 0.01)
    store.add(records(*open_run()))
    app = create_app(store, host="127.0.0.1")
    streaming = asyncio.Event()
    requests = [{"type": "http.request", "body": b"", "more_body": False}]
    sent = []

    async def receive():
        if requests:
            return requests.pop()
        await streaming.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)
        if b"event: spans" in message.get("body", b""):
            streaming.set()

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": spec_version},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": f"/traces/{TRACE_ID}/live",
        "raw_path": f"/traces/{TRACE_ID}/live".encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"127.0.0.1:4318")],
        "client": ("127.0.0.1", 50000),
        "server": ("127.0.0.1", 4318),
    }

    await asyncio.wait_for(app(scope, receive, send), timeout=2)

    assert streaming.is_set()
    assert not any(b"event: idle" in item.get("body", b"") for item in sent)


@pytest.mark.parametrize(
    "path",
    [f"/traces/{OTHER_TRACE_ID}/live", "/traces/not-a-trace/live"],
)
def test_live_stream_of_an_unknown_trace_is_not_found(client, store, path):
    store.add(records(*completed_run()))

    response = client.get(path)

    assert response.status_code == 404
    assert not response.headers["content-type"].startswith("text/event-stream")


@pytest.mark.parametrize("after", ["-1", "many", ""])
def test_live_stream_reads_a_bad_position_as_the_start(client, store, after):
    store.add(records(*completed_run()))

    sent = events(client.get(f"/traces/{TRACE_ID}/live?after={after}").text)

    assert len(sent[0][2]["spans"]) == 4


def test_graph_script_writes_text_only_and_follows_the_live_stream(client):
    script = client.get("/static/trace.js").text

    for unsafe in (
        "innerHTML",
        "outerHTML",
        "insertAdjacentHTML",
        "eval(",
        "Function(",
    ):
        assert unsafe not in script
    assert "new EventSource(root.dataset.liveUrl)" in script
    for event_name in ('"spans"', '"end"', '"idle"'):
        assert f"addEventListener({event_name}" in script
    assert 'startOnLoad: false, securityLevel: "strict"' in script
    assert (
        "mark.textContent = step.status[0].toUpperCase() + step.status.slice(1);"
        in script
    )
    assert 'new DOMParser().parseFromString(text, "text/html")' in script
    assert '.getElementById("trace-step-detail")' in script
    assert "fetch(`${root.dataset.pageUrl}?step=${number}`" in script


def test_diagram_failure_note_waits_for_the_script(client, store):
    store.add(records(*completed_run()))

    page = client.get(f"/traces/{TRACE_ID}").text
    script = client.get("/static/trace.js").text

    assert (
        '<p class="nodestep-message nodestep-message-warning" id="trace-diagram-note" hidden>'
        "The diagram could not be drawn. The Mermaid script may be blocked or offline,"
        in page
    )
    assert ".catch(() => {})" not in script
    assert 'getElementById("trace-diagram-note")' in script


def test_graph_page_has_no_inline_script_but_the_graph_data(client, store):
    store.add(records(*completed_run()))

    page = client.get(f"/traces/{TRACE_ID}").text
    tags = re.findall(r"<script\b[^>]*>", page)

    assert tags == [
        f'<script src="{BASE_URL}/static/nodestep-theme.js">',
        f'<script src="{BASE_URL}/static/nodestep-data.js">',
        '<script type="application/json" id="trace-graph-data">',
        MERMAID_SCRIPT.removesuffix("</script>"),
        f'<script src="{BASE_URL}/static/trace.js">',
    ]
    assert page.index("static/nodestep-data.js") < page.index("</head>")
    assert " style=" not in page
    assert re.search(r"\son[a-z]+=", page) is None


def test_replay_controls_wait_for_the_script(client, store):
    store.add(records(*completed_run()))

    page = client.get(f"/traces/{TRACE_ID}").text
    controls = page.split('id="trace-replay"')[1].split("</div>")[0]

    assert controls.startswith(" hidden>")
    assert re.findall(r'data-replay="(\w+)">(\w+)<', controls) == [
        ("first", "First"),
        ("previous", "Previous"),
        ("play", "Replay"),
        ("next", "Next"),
        ("last", "Last"),
    ]


def test_live_note_says_where_the_run_is_followed(client, store):
    store.add(records(*open_run()))

    graph_page = client.get(f"/traces/{TRACE_ID}").text
    timeline_page = client.get(f"/traces/{TRACE_ID}/timeline").text

    assert "New steps appear here as their spans arrive" in graph_page
    assert "New steps appear here" not in timeline_page
    assert "open the Graph view to follow the run" in timeline_page


def step_items(page: str) -> list[str]:
    return re.findall(r'<li><a class="nodestep-panel-item".*?</a></li>', page, re.S)


def test_a_step_with_a_failed_tool_call_is_marked_and_names_the_tool(client, store):
    store.add(
        records(
            *completed_run(),
            call_span(
                TOOL_ID, "run_sql", 2, parent=ACT_ID, status=Status.STATUS_CODE_ERROR
            ),
        )
    )

    page = client.get(f"/traces/{TRACE_ID}").text
    items = step_items(page)

    assert [
        '<span class="nodestep-badge nodestep-badge-failed">Tool error</span>' in item
        for item in items
    ] == [False, True, False]
    assert "nodestep-badge-completed" not in "".join(items)
    assert [step["failed_tools"] for step in graph_data(page)["steps"]] == [
        [],
        ["run_sql"],
        [],
    ]


def test_the_node_where_a_stopped_run_ended_is_marked_stopped(client, store):
    store.add(
        records(
            run_span(status="cancelled"),
            node_span(THINK_ID, "think", 1),
            node_span(
                ACT_ID,
                "act",
                2,
                events=[event("nodestep.cancelled", START_NS + 2_500_000)],
            ),
        )
    )

    items = step_items(client.get(f"/traces/{TRACE_ID}").text)

    assert "nodestep-badge" not in items[0]
    assert (
        '<span class="nodestep-badge nodestep-badge-stopped">Stopped</span>' in items[1]
    )


def test_the_failed_tool_calls_of_the_last_step_are_in_its_facts(client, store):
    store.add(
        records(
            run_span(),
            node_span(THINK_ID, "think", 1),
            node_span(ACT_ID, "act", 2),
            call_span(
                TOOL_ID, "run_sql", 2, parent=ACT_ID, status=Status.STATUS_CODE_ERROR
            ),
        )
    )

    page = client.get(f"/traces/{TRACE_ID}").text

    assert (
        '<p class="trace-step-facts" id="trace-step-facts">'
        '<span class="nodestep-badge nodestep-badge-completed">Completed</span>'
        "<span>1.0 ms</span><span>Failed tool call: run_sql</span>"
        f'<a href="{BASE_URL}/traces/{TRACE_ID}/spans/{ACT_ID}">Open the span</a></p>'
    ) in page


def test_open_run_says_which_node_is_highlighted_before_the_script_runs(client, store):
    store.add(records(*completed_run(trace_id=OTHER_TRACE_ID)))
    store.add(records(*open_run()))

    page = client.get(f"/traces/{TRACE_ID}").text

    assert (
        '<p class="nodestep-hint" id="trace-position" aria-live="polite">'
        "2 steps have finished so far. The highlighted node is running.</p>"
    ) in page


def test_open_run_without_a_known_node_says_it_is_in_progress(client, store):
    store.add(records(*open_run()))

    page = client.get(f"/traces/{TRACE_ID}").text

    assert "2 steps have finished so far. The run is in progress." in page


def test_graph_script_matches_edges_by_their_ids_and_marks_the_current_node(client):
    script = client.get("/static/trace.js").text

    assert (
        'const marks = ["visited", "current", "paused", "failed", "stopped", "tool-error"];'
        in script
    )
    assert r"new RegExp(`^${pattern(from)}-${pattern(to)}(-\\d+)?$`)" in script
    assert (
        r"new RegExp(`(^|-)L[_-]${pattern(from)}[_-]${pattern(to)}[_-]\\d+$`)" in script
    )
    assert "graph.current" in script
    assert 'node.classList.add("tool-error")' in script
    assert "The highlighted node is running." in script
    assert "The run is in progress." in script


def detail_of(page: str) -> str:
    return page.split('<section class="nodestep-card trace-step-detail"')[1].split(
        "</section>\n</div>"
    )[0]


def test_diagram_steps_and_details_are_the_three_parts_of_the_graph_view(client, store):
    store.add(records(*completed_run()))

    page = client.get(f"/traces/{TRACE_ID}").text
    graph = page.split('<div class="trace-graph" id="trace-graph"')[1].split(
        '<script type="application/json" id="trace-graph-data">'
    )[0]

    parts = [
        graph.index(part)
        for part in (
            '\n<div class="trace-graph-diagram">\n',
            '\n<section class="nodestep-card trace-steps" ',
            '\n<section class="nodestep-card trace-step-detail" ',
        )
    ]
    assert parts == sorted(parts)
    assert graph.rstrip().endswith("</section>\n</div>")


def test_each_step_is_one_row_with_number_node_badges_and_duration(client, store):
    store.add(
        records(
            *paused_run(),
            call_span(
                TOOL_ID, "run_sql", 2, parent=ACT_ID, status=Status.STATUS_CODE_ERROR
            ),
        )
    )

    items = step_items(client.get(f"/traces/{TRACE_ID}").text)

    assert items[0] == (
        f'<li><a class="nodestep-panel-item" href="{BASE_URL}/traces/{TRACE_ID}'
        '?step=1#trace-step-detail" data-step="1">'
        '<span class="nodestep-panel-item-line">'
        '<span class="trace-step-number">1</span>'
        '<span class="nodestep-panel-item-title">think</span>'
        '<span class="nodestep-panel-item-time">1.0 ms</span></span></a></li>'
    )
    assert (
        '<span class="nodestep-panel-item-title">act</span>'
        '<span class="nodestep-badge nodestep-badge-paused">Paused</span>'
        '<span class="nodestep-badge nodestep-badge-failed">Tool error</span>'
        '<span class="nodestep-panel-item-time">1.0 ms</span>'
    ) in items[1]
    assert 'aria-current="true"' in items[1]


def test_the_details_name_the_step_and_switch_between_its_values(client, store):
    store.add(records(*completed_run()))

    detail = detail_of(client.get(f"/traces/{TRACE_ID}?step=2").text)

    assert detail.startswith(
        ' id="trace-step-detail" aria-labelledby="trace-step-title">\n'
        '<h2 class="nodestep-card-title" id="trace-step-title">Step 2 of 3: act</h2>\n'
        '<p class="trace-step-facts" id="trace-step-facts">'
        '<span class="nodestep-badge nodestep-badge-completed">Completed</span>'
        "<span>1.0 ms</span>"
        f'<a href="{BASE_URL}/traces/{TRACE_ID}/spans/{ACT_ID}">Open the span</a></p>'
    )
    tabs = re.findall(
        r'<a class="nodestep-tab" href="([^"]+)" data-view="(\w+)"'
        r'( aria-current="page")?>([^<]+)</a>',
        detail,
    )
    assert tabs == [
        (
            f"{BASE_URL}/traces/{TRACE_ID}?step=2&amp;view={name}#trace-step-detail",
            name,
            current,
            label,
        )
        for name, current, label in [
            ("update", ' aria-current="page"', "Update"),
            ("state_before", "", "State before"),
            ("state_after", "", "State after"),
        ]
    ]
    panels = re.findall(
        r'<div class="trace-step-view" data-view-panel="(\w+)"( hidden)?>', detail
    )
    assert panels == [
        ("update", ""),
        ("state_before", " hidden"),
        ("state_after", " hidden"),
    ]
    assert detail.count('<div class="nodestep-data" role="group"') == 3
    assert 'aria-label="the state before"' in detail
    assert 'aria-label="Search the update"' in detail
    assert 'aria-label="Search the state before"' in detail
    assert 'aria-label="Search the state after"' in detail


def test_a_step_and_a_view_can_be_chosen_in_the_address(client, store):
    store.add(records(*completed_run()))

    page = client.get(f"/traces/{TRACE_ID}?step=1&view=state_before").text
    detail = detail_of(page)

    assert "Step 1 of 3: think" in detail
    assert '<div class="trace-step-view" data-view-panel="state_before">' in detail
    assert 'data-view="state_before" aria-current="page">' in detail
    current = [item for item in step_items(page) if 'aria-current="true"' in item]
    assert len(current) == 1
    assert 'data-step="1"' in current[0]


@pytest.mark.parametrize(
    "query", ["step=0", "step=9", "step=two", "step=-1", "view=other", "step=", ""]
)
def test_an_unknown_step_or_view_shows_the_last_step_and_its_update(
    client, store, query
):
    store.add(records(*completed_run()))

    response = client.get(f"/traces/{TRACE_ID}?{query}")

    assert response.status_code == 200
    detail = detail_of(response.text)
    assert "Step 3 of 3: think" in detail
    assert 'data-view="update" aria-current="page">' in detail


def test_the_last_step_of_an_open_run_has_no_state_after_yet(client, store):
    store.add(records(*open_run()))

    detail = detail_of(client.get(f"/traces/{TRACE_ID}").text)

    assert "Step 2 of 2: act" in detail
    assert 'data-view="state_after"' not in detail
    assert 'data-view="state_before"' in detail


def test_a_shortened_value_is_explained_in_its_panel(client, store):
    store.add(
        records(
            run_span(),
            node_span(
                THINK_ID,
                "think",
                1,
                extra={
                    "nodestep.state.before": '{"messages": ["hello", "wor',
                    "nodestep.truncated": ["nodestep.state.before"],
                },
            ),
        )
    )

    detail = detail_of(client.get(f"/traces/{TRACE_ID}?view=state_before").text)

    assert 'data-view="state_before" aria-current="page">State before</a>' in detail
    assert "nodestep-tag" not in detail
    panel = detail.split('data-view-panel="state_before">')[1]
    assert panel.startswith(
        '\n<p class="nodestep-message nodestep-message-warning">Shortened to fit the'
        " size limit and cut, so it is not valid JSON and is shown as received.</p>\n"
        '<div class="nodestep-code nodestep-code-wrap"><pre>'
    )


def test_a_step_without_values_says_so(client, store):
    store.add(
        records(
            run_span(),
            span(
                TRACE_ID,
                THINK_ID,
                "nodestep.node think",
                parent=ROOT_ID,
                attributes={"nodestep.node.name": "think"},
            ),
        )
    )

    detail = detail_of(client.get(f"/traces/{TRACE_ID}").text)

    assert "nodestep-tabs" not in detail
    assert "This step recorded no update and no state." in detail


def app_style() -> str:
    return (
        files("nodeartifact.server")
        .joinpath("static/style.css")
        .read_text(encoding="utf-8")
    )


def rule(style: str, selector: str) -> dict[str, str]:
    match = re.search(r"(?:^|\n)" + re.escape(selector) + r" \{([^}]*)\}", style)
    assert match is not None, selector
    pairs = (
        line.strip().rstrip(";").split(":", 1)
        for line in match.group(1).strip().splitlines()
    )
    return {name.strip(): value.strip() for name, value in pairs}


def test_the_graph_view_uses_the_shared_gap_and_stacks_its_parts_when_narrow():
    style = app_style()

    graph = rule(style, ".trace-graph")
    assert graph["display"] == "grid"
    assert graph["gap"] == "var(--nodestep-space-4)"
    assert graph["align-items"] == "stretch"
    assert rule(style, ".trace-steps")["contain"] == "size"
    assert rule(style, ".trace-step-list")["overflow-y"] == "auto"
    assert rule(style, ".trace-step-detail")["grid-column"] == "1 / -1"
    narrow = style.split("@media (max-width: 56rem) {")[1].split("\n}")[0]
    assert rule(narrow, "  .trace-graph")["grid-template-columns"] == "minmax(0, 1fr)"
    assert rule(narrow, "  .trace-steps")["contain"] == "none"
