import html
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from opentelemetry.proto.trace.v1.trace_pb2 import Status

from conftest import BASE_URL
from graphs import ACT_ID, MISSING_RUN_ID, THINK_ID, completed_run, node_span
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

SCRIPT = "<script>alert(1)</script>"
NODE = shutil.which("node")
TRACE_SCRIPT_TESTS = Path(__file__).parents[1] / "js" / "trace.test.mjs"


def root_span():
    return span(
        TRACE_ID,
        ROOT_ID,
        "nodestep.graph demo",
        attributes={"nodestep.graph.name": "demo"},
    )


def model_span():
    return span(
        TRACE_ID,
        CHILD_ID,
        "chat gpt-6-luna",
        parent=ROOT_ID,
        start=START_NS + 100,
        end=START_NS + 900_000,
        status=Status.STATUS_CODE_ERROR,
        message="rate limited",
        attributes={
            "gen_ai.request.model": "gpt-6-luna",
            "gen_ai.usage.input_tokens": 12,
            "gen_ai.usage.output_tokens": 5,
            "nodestep.node.name": "plan",
            "nodestep.state.before": '{"count": 1}',
            "nodestep.state.after": '{"count": 2}',
            "http.status_code": 429,
        },
        events=[event("retry", START_NS + 500, {"attempt": 2})],
    )


def tool_span():
    return span(
        TRACE_ID,
        GRANDCHILD_ID,
        "execute_tool search",
        parent=CHILD_ID,
        start=START_NS + 200,
        end=START_NS + 300,
    )


def add_sample(store):
    store.add(records(root_span(), model_span(), tool_span()))


def row_of(page: str, name: str) -> str:
    return next(row for row in page.split("<tr") if f">{name}</a>" in row)


def timeline_row_of(page: str, name: str) -> str:
    rows = page.split('<li class="nodestep-timeline-row')
    return next(row for row in rows if f">{name}</a>" in row)


def status_fact(page: str) -> str:
    return page.split("<dt>Status</dt><dd>")[1].split("</dd>")[0]


def script_tags(page: str) -> list[str]:
    return re.findall(r"<script\b[^>]*>", page)


def test_empty_trace_list_names_the_otlp_endpoint(client):
    response = client.get("/")

    assert response.status_code == 200
    assert "No traces yet" in response.text
    assert f"{BASE_URL}/v1/traces" in response.text


def test_empty_trace_list_shows_instrument_with_a_provider_for_this_server(client):
    response = client.get("/")

    assert (
        "<code>nodeartifact.instrument(graph, tracer_provider="
        f'nodeartifact.configure("{BASE_URL}/"))</code>'
    ) in response.text
    assert "nodeartifact.instrument(graph)</code>" not in response.text


def test_trace_list_shows_each_trace(client, store):
    add_sample(store)

    row = row_of(client.get("/").text, "nodestep.graph demo")

    assert f'href="{BASE_URL}/traces/{TRACE_ID}"' in row
    assert "checkout" in row
    assert '<span class="nodestep-badge nodestep-badge-failed">Failed</span>' in row
    assert ">3<" in row
    assert "1.0 ms" in row
    assert "2023-11-14 22:13:20.000 UTC" in row
    assert ">12<" in row
    assert ">5<" in row


def test_trace_without_its_root_is_marked(client, store):
    store.add(records(tool_span()))

    row = row_of(client.get("/").text, "execute_tool search")

    assert "root span missing" in row


def test_trace_view_indents_nested_spans(client, store):
    add_sample(store)

    page = client.get(f"/traces/{TRACE_ID}").text
    indent = '<span class="nodestep-timeline-indent"></span>'

    assert timeline_row_of(page, "nodestep.graph demo").count(indent) == 0
    assert timeline_row_of(page, "chat gpt-6-luna").count(indent) == 1
    assert timeline_row_of(page, "execute_tool search").count(indent) == 2
    assert page.count("<rect") == 3
    assert timeline_row_of(page, "chat gpt-6-luna").startswith(
        " nodestep-timeline-failed"
    )
    assert (
        '<span class="nodestep-badge nodestep-badge-failed">Failed</span>'
        in timeline_row_of(page, "chat gpt-6-luna")
    )
    assert f'href="{BASE_URL}/traces/{TRACE_ID}/spans/{CHILD_ID}"' in page
    assert "12 / 5" in timeline_row_of(page, "chat gpt-6-luna")


@pytest.mark.parametrize(
    "path",
    [
        f"/traces/{OTHER_TRACE_ID}",
        "/traces/not-a-trace-id",
        f"/traces/{TRACE_ID.upper()}",
    ],
)
def test_unknown_trace_is_not_found(client, store, path):
    add_sample(store)

    response = client.get(path)

    assert response.status_code == 404
    assert "No trace" in response.text


def test_span_view_shows_attributes_events_and_state(client, store):
    add_sample(store)

    page = client.get(f"/traces/{TRACE_ID}/spans/{CHILD_ID}").text
    text = html.unescape(page)

    assert 'class="nodestep-table attributes gen-ai"' in page
    assert 'class="nodestep-table attributes nodestep"' in page
    assert "gen_ai.request.model" in text
    assert "nodestep.node.name" in text
    assert "http.status_code" in text
    assert '{\n  "count": 1\n}' in text
    assert '{\n  "count": 2\n}' in text
    assert text.index("nodestep.state.before") < text.index("nodestep.state.after")
    assert "rate limited" in text
    assert "retry" in text
    assert "+400 ns" in text
    assert "attempt" in text
    assert "service.name" in text
    assert "tests 1.0" in text
    assert f'href="{BASE_URL}/traces/{TRACE_ID}/spans/{ROOT_ID}"' in page


def test_span_view_notes_state_that_is_not_valid_json(client, store):
    truncated = '{"messages": ["hello", "wor'
    store.add(
        records(
            span(
                TRACE_ID,
                ROOT_ID,
                "root",
                attributes={"nodestep.state.before": truncated},
            )
        )
    )

    text = html.unescape(client.get(f"/traces/{TRACE_ID}/spans/{ROOT_ID}").text)

    assert "not valid JSON" in text
    assert truncated in text


@pytest.mark.parametrize(
    "path",
    [
        f"/traces/{TRACE_ID}/spans/0000000000000001",
        f"/traces/{TRACE_ID}/spans/not-a-span",
        f"/traces/{OTHER_TRACE_ID}/spans/{ROOT_ID}",
    ],
)
def test_unknown_span_is_not_found(client, store, path):
    add_sample(store)

    response = client.get(path)

    assert response.status_code == 404
    assert "No span" in response.text


def page_title(page: str) -> str:
    found = re.search(r"<title>([^<]*)</title>", page)
    assert found
    return found.group(1)


def test_list_span_and_error_pages_are_titled_page_first(client, store):
    add_sample(store)

    titles = {
        "/": "nodeartifact",
        f"/traces/{TRACE_ID}/spans/{CHILD_ID}": "chat gpt-6-luna · nodeartifact",
        f"/traces/{OTHER_TRACE_ID}": "Not found · nodeartifact",
    }

    assert {path: page_title(client.get(path).text) for path in titles} == titles


def test_untrusted_text_is_escaped_on_every_page(client, store):
    store.add(
        records(
            span(
                TRACE_ID,
                ROOT_ID,
                SCRIPT,
                attributes={SCRIPT: SCRIPT, "nodestep.state.before": SCRIPT},
                events=[event(SCRIPT, START_NS, {SCRIPT: SCRIPT})],
            ),
            service=SCRIPT,
        )
    )

    for path in ("/", f"/traces/{TRACE_ID}", f"/traces/{TRACE_ID}/spans/{ROOT_ID}"):
        page = client.get(path).text
        assert SCRIPT not in page
        assert all(f'src="{BASE_URL}/static/' in tag for tag in script_tags(page))
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page


def test_every_page_sends_security_headers(client, store):
    add_sample(store)

    for path in (
        "/",
        f"/traces/{TRACE_ID}",
        f"/traces/{TRACE_ID}/spans/{ROOT_ID}",
        f"/traces/{OTHER_TRACE_ID}",
    ):
        headers = client.get(path).headers
        assert headers["content-security-policy"].startswith(
            "default-src 'none'; script-src 'self'; style-src 'self';"
        )
        assert headers["x-content-type-options"] == "nosniff"
        assert headers["referrer-policy"] == "no-referrer"


def test_pages_load_nodestep_design_before_the_app_stylesheet(client, store):
    add_sample(store)

    for path in ("/", f"/traces/{TRACE_ID}", f"/traces/{TRACE_ID}/spans/{CHILD_ID}"):
        page = client.get(path).text
        nodestep = page.index(
            f'<link rel="stylesheet" href="{BASE_URL}/static/nodestep.css">'
        )
        app = page.index(f'<link rel="stylesheet" href="{BASE_URL}/static/style.css">')
        assert nodestep < app
    for name in ("nodestep.css", "style.css"):
        stylesheet = client.get(f"/static/{name}")
        assert stylesheet.status_code == 200
        assert stylesheet.headers["content-type"].startswith("text/css")


def test_pages_without_a_graph_load_only_the_theme_script(client, store):
    add_sample(store)

    for path in (f"/traces/{TRACE_ID}", f"/traces/{TRACE_ID}/timeline"):
        assert script_tags(client.get(path).text) == [
            f'<script src="{BASE_URL}/static/nodestep-theme.js">',
        ]


def test_span_page_loads_the_data_viewer_script_in_its_head(client, store):
    add_sample(store)

    page = client.get(f"/traces/{TRACE_ID}/spans/{CHILD_ID}").text
    head = page.split("</head>")[0]

    assert script_tags(page) == [
        f'<script src="{BASE_URL}/static/nodestep-theme.js">',
        f'<script src="{BASE_URL}/static/nodestep-data.js">',
    ]
    assert head.index("static/nodestep-data.js") < head.index("static/nodestep.css")
    script = client.get("/static/nodestep-data.js")
    assert script.status_code == 200
    script_type = script.headers["content-type"].split(";")[0]
    assert script_type in ("text/javascript", "application/javascript")


def test_theme_is_set_in_the_head_before_the_stylesheets(client, store):
    add_sample(store)

    page = client.get("/").text
    head = page.split("</head>")[0]

    assert head.index("static/nodestep-theme.js") < head.index("static/nodestep.css")


def test_top_bar_has_one_theme_button_with_a_moon_and_a_sun(client, store):
    add_sample(store)

    page = client.get("/").text
    header = page.split('<header class="nodestep-topbar">')[1].split("</header>")[0]
    end = header.split('<div class="nodestep-topbar-end">')[1].split("</form>", 1)[1]

    button = re.fullmatch(
        r'\n(<button class="nodestep-theme-button" type="button" aria-label="Dark theme"'
        r' aria-pressed="false">(.*?)</button>)\n</div>\n',
        end,
    )
    assert button is not None
    icons = re.findall(
        r'<svg class="(nodestep-theme-\w+)" viewBox="0 0 16 16" aria-hidden="true"'
        r' focusable="false">',
        button.group(2),
    )
    assert icons == ["nodestep-theme-moon", "nodestep-theme-sun"]
    assert not re.search(r'\b(?:fill|stroke|style)="', button.group(2))
    assert "nodestep-theme-toggle" not in page
    assert 'type="radio"' not in header


def test_theme_script_remembers_the_choice_and_survives_blocked_storage(client):
    script = client.get("/static/nodestep-theme.js").text

    assert 'const key = "nodestep-theme";' in script
    assert "localStorage.getItem(key)" in script
    assert 'stored === "light" || stored === "dark"' in script
    assert 'document.querySelectorAll(".nodestep-theme-button")' in script
    assert 'matchMedia("(prefers-color-scheme: dark)")' in script
    assert 'const theme = isDark() ? "light" : "dark";' in script
    assert "localStorage.setItem(key, theme)" in script
    assert 'setAttribute("aria-pressed", String(isDark()))' in script
    assert "removeItem" not in script
    assert script.count("try {") == script.count("} catch {}") == 2
    for name in ("theme.js", "theme-toggle.js"):
        assert client.get(f"/static/{name}").status_code == 404


def test_trace_list_table_scrolls_inside_its_own_box(client, store):
    add_sample(store)

    page = client.get("/").text

    assert re.search(
        r'<div class="nodestep-table-wrap">\s*<table class="nodestep-table nodestep-table-stack traces">',
        page,
    )


def test_timeline_is_a_list_of_rows_without_style_attributes(client, store):
    add_sample(store)

    page = client.get(f"/traces/{TRACE_ID}").text

    assert '<ol class="nodestep-timeline" aria-label="Spans of the trace">' in page
    assert page.count('<li class="nodestep-timeline-row') == 3
    assert " style=" not in page


def test_event_attributes_are_a_description_list(client, store):
    add_sample(store)

    page = client.get(f"/traces/{TRACE_ID}/spans/{CHILD_ID}").text

    assert (
        '<dl class="nodestep-pairs"><dt><code>attempt</code></dt><dd><pre>2</pre></dd></dl>'
        in page
    )


def test_trace_list_cells_carry_labels_for_narrow_screens(client, store):
    add_sample(store)

    row = row_of(client.get("/").text, "nodestep.graph demo")

    for label in (
        "Thread",
        "Service",
        "Status",
        "Spans",
        "Duration",
        "Started",
        "Input tokens",
        "Output tokens",
    ):
        assert f'data-label="{label}"' in row


def paused_run():
    return span(
        TRACE_ID,
        ROOT_ID,
        "nodestep.graph text-to-sql-demo",
        attributes={
            "nodestep.thread_id": "th-c04e5519",
            "nodestep.input": "Which category brings in the most revenue?",
        },
        events=[event("nodestep.interrupt", START_NS, {"nodestep.interrupt.key": "k"})],
    )


def busy_model_span():
    return span(
        TRACE_ID,
        CHILD_ID,
        "chat gpt-6-luna",
        parent=ROOT_ID,
        status=Status.STATUS_CODE_ERROR,
        attributes={
            "gen_ai.usage.input_tokens": 1402,
            "gen_ai.usage.output_tokens": 12_345,
            "gen_ai.response.finish_reasons": ["tool_calls"],
        },
    )


def test_trace_list_shows_thread_and_input_preview(client, store):
    store.add(records(paused_run(), busy_model_span()))

    row = row_of(client.get("/").text, "Which category brings in the most revenue?")

    assert f'href="{BASE_URL}/traces/{TRACE_ID}"' in row
    assert (
        '<td class="thread" data-label="Thread">'
        '<code title="th-c04e5519">th-c04e5519</code></td>'
    ) in row
    assert "nodestep.graph text-to-sql-demo" in row


def test_trace_list_without_input_links_the_root_name(client, store):
    add_sample(store)

    row = row_of(client.get("/").text, "nodestep.graph demo")

    assert '<td class="thread" data-label="Thread">-</td>' in row


def test_paused_run_is_labelled_paused(client, store):
    store.add(records(paused_run(), busy_model_span()))

    row = row_of(client.get("/").text, "Which category brings in the most revenue?")
    facts = client.get(f"/traces/{TRACE_ID}").text

    for page in (row, status_fact(facts)):
        assert ">Paused<" in page
        assert "nodestep-badge-failed" not in page
        assert 'class="nodestep-badge nodestep-badge-paused"' in page
    assert "th-c04e5519" in facts


def test_token_counts_use_thousands_separators(client, store):
    store.add(records(paused_run(), busy_model_span()))

    row = row_of(client.get("/").text, "Which category brings in the most revenue?")
    trace = client.get(f"/traces/{TRACE_ID}").text
    model = client.get(f"/traces/{TRACE_ID}/spans/{CHILD_ID}").text

    assert ">1,402<" in row
    assert ">12,345<" in row
    assert "1,402 / 12,345" in trace
    assert "<pre>1,402</pre>" in model


def test_one_item_finish_reasons_render_inline(client, store):
    store.add(records(paused_run(), busy_model_span()))

    page = html.unescape(client.get(f"/traces/{TRACE_ID}/spans/{CHILD_ID}").text)

    assert '<pre>["tool_calls"]</pre>' in page


def test_pages_link_the_favicon_file_that_the_policy_allows(client, store):
    add_sample(store)
    store.add(records(*completed_run(trace_id=OTHER_TRACE_ID)))

    for path in (
        "/",
        f"/traces/{TRACE_ID}",
        f"/traces/{TRACE_ID}/spans/{ROOT_ID}",
        f"/traces/{OTHER_TRACE_ID}",
    ):
        response = client.get(path)
        assert (
            f'<link rel="icon" href="{BASE_URL}/static/favicon.svg" type="image/svg+xml">'
            in response.text
        )
        assert "data:" not in response.text
        assert "img-src 'self';" in response.headers["content-security-policy"]
    favicon = client.get("/static/favicon.svg")
    assert favicon.status_code == 200
    assert favicon.headers["content-type"].startswith("image/svg+xml")


def test_top_bar_brand_has_the_inline_mark_in_the_shared_classes(client, store):
    add_sample(store)

    page = client.get("/").text
    header = page.split('<header class="nodestep-topbar">')[1].split("</header>")[0]
    brand = re.search(
        r'<a class="nodestep-brand" href="([^"]+)">(.*?)</a>', header, re.S
    )

    assert brand is not None
    assert brand.group(1) == f"{BASE_URL}/"
    mark, label = brand.group(2).split("</svg>")
    assert mark.startswith(
        '<svg class="nodestep-brand-mark" viewBox="0 0 64 64" aria-hidden="true"'
        ' focusable="false">'
    )
    assert label == "nodeartifact"
    assert mark.count('stroke="currentColor"') == 1
    assert mark.count('class="nodestep-mark-accent-fill"') == 1
    assert mark.count('fill="currentColor"') == 3
    assert "#" not in mark


def test_trace_view_is_named_after_the_run_input(client, store):
    store.add(records(paused_run(), busy_model_span()))

    page = client.get(f"/traces/{TRACE_ID}").text

    assert (
        "<title>Which category brings in the most revenue? · nodeartifact</title>"
        in page
    )
    assert (
        '<h1>Which category brings in the most revenue? <span class="root">nodestep.graph text-to-sql-demo</span></h1>'
        in page
    )


def test_trace_view_without_input_is_named_after_the_root_span(client, store):
    add_sample(store)

    page = client.get(f"/traces/{TRACE_ID}").text

    assert "<title>nodestep.graph demo · nodeartifact</title>" in page
    assert "<h1>nodestep.graph demo</h1>" in page


def test_cut_input_preview_shows_the_full_input_on_hover(client, store):
    question = (
        "Which store has the most returns this year,"
        ' and how does that compare with "last year"?'
    )
    store.add(
        records(
            span(
                TRACE_ID,
                ROOT_ID,
                "nodestep.graph text-to-sql-demo",
                attributes={"nodestep.input": question},
            )
        )
    )
    title = 'title="Which store has the most returns this year, and how does that compare with &#34;last year&#34;?"'

    list_page = client.get("/").text

    assert f'<a {title} href="{BASE_URL}/traces/{TRACE_ID}">' in list_page


def test_trace_view_heading_shows_the_full_run_input(client, store):
    question = (
        "Which store has the most returns this year,"
        ' and how does that compare with "last year"?'
    )
    store.add(
        records(
            span(
                TRACE_ID,
                ROOT_ID,
                "nodestep.graph text-to-sql-demo",
                attributes={"nodestep.input": question},
            )
        )
    )

    page = client.get(f"/traces/{TRACE_ID}").text

    assert (
        "<h1>Which store has the most returns this year, and how does that compare"
        ' with &#34;last year&#34;? <span class="root">nodestep.graph text-to-sql-demo</span></h1>'
        in page
    )
    assert (
        "<title>Which store has the most returns this year, and how does that compare"
        " with &#34;las… · nodeartifact</title>" in page
    )


def test_short_input_preview_has_no_hover_text(client, store):
    store.add(records(paused_run()))

    for path in ("/", f"/traces/{TRACE_ID}"):
        assert "<a title=" not in client.get(path).text


def test_json_attribute_and_event_values_are_pretty_printed(client, store):
    store.add(
        records(
            span(
                TRACE_ID,
                ROOT_ID,
                "nodestep.graph refund",
                attributes={"gen_ai.input.messages": '[{"role": "user"}]'},
                events=[
                    event(
                        "nodestep.interrupt",
                        START_NS,
                        {"nodestep.interrupt.payload": '{"question": "Approve?"}'},
                    )
                ],
            )
        )
    )

    page = html.unescape(client.get(f"/traces/{TRACE_ID}/spans/{ROOT_ID}").text)

    assert (
        '<pre class="nodestep-data-source">[\n  {\n    "role": "user"\n  }\n]</pre>'
        in page
    )
    assert (
        '<pre class="nodestep-data-source">{\n  "question": "Approve?"\n}</pre>' in page
    )


def test_span_page_shows_state_and_json_values_in_the_data_viewer(client, store):
    store.add(
        records(
            span(
                TRACE_ID,
                ROOT_ID,
                "nodestep.graph demo",
                attributes={
                    "gen_ai.input.messages": '[{"role": "user", "parts": []}]',
                    "gen_ai.response.finish_reasons": ["stop"],
                    "nodestep.state.after": json.dumps(
                        {"messages": [{"type": "human", "content": "Hello"}]}
                    ),
                },
                events=[event("retry", START_NS, {"payload": '{"attempt": 2}'})],
            )
        )
    )

    page = client.get(f"/traces/{TRACE_ID}/spans/{ROOT_ID}").text
    state = page.split('<section class="nodestep-section state">')[1].split(
        "</section>"
    )[0]

    assert (
        '<div class="nodestep-data" role="group" aria-label="nodestep.state.after">'
        in state
    )
    assert 'aria-label="Search nodestep.state.after"' in state
    assert (
        '<span class="nodestep-data-role nodestep-data-role-human">human</span>'
        in state
    )
    assert '<p class="nodestep-data-text">Hello</p>' in state
    assert 'aria-label="Search gen_ai.input.messages"' in page
    assert 'aria-label="Search payload"' in page
    assert "<pre>[&#34;stop&#34;]</pre>" in page
    assert page.count('<div class="nodestep-data" role="group"') == 3


def finished_run(attributes):
    return span(
        TRACE_ID,
        ROOT_ID,
        "nodestep.graph text-to-sql-demo",
        attributes={
            "nodestep.input": "Which category brings in the most revenue?",
            **attributes,
        },
    )


@pytest.mark.parametrize(
    ("status", "label", "badge"),
    [
        ("completed", "Completed", "completed"),
        ("cancelled", "Stopped", "stopped"),
        ("failed", "Failed", "failed"),
        ("paused", "Paused", "paused"),
    ],
)
def test_trace_status_is_the_run_status(client, store, status, label, badge):
    store.add(records(finished_run({"nodestep.status": status}), busy_model_span()))

    row = row_of(client.get("/").text, "Which category brings in the most revenue?")
    facts = status_fact(client.get(f"/traces/{TRACE_ID}").text)

    for page in (row, facts):
        assert (
            f'<span class="nodestep-badge nodestep-badge-{badge}">{label}</span>'
            in page
        )
        assert re.findall(r"nodestep-badge-(\w+)", page) == [badge]


def test_completed_run_with_a_failed_tool_call_is_completed(client, store):
    store.add(
        records(finished_run({"nodestep.status": "completed"}), busy_model_span())
    )

    row = row_of(client.get("/").text, "Which category brings in the most revenue?")
    facts = client.get(f"/traces/{TRACE_ID}").text

    assert ">Completed<" in row
    assert "nodestep-badge-failed" not in row
    assert "1 with errors" in facts


def test_resumed_run_is_labelled_with_its_answer(client, store):
    store.add(
        records(
            finished_run(
                {"nodestep.resuming": True, "nodestep.resume": '{"clarify": "net"}'}
            )
        )
    )

    row = row_of(client.get("/").text, "Which category brings in the most revenue?")
    page = client.get(f"/traces/{TRACE_ID}").text

    assert '<span class="nodestep-tag nodestep-tag-wrap">resumed: net</span>' in row
    assert (
        "<h1>Which category brings in the most revenue?"
        ' <span class="nodestep-tag nodestep-tag-wrap">resumed: net</span>'
    ) in page
    assert (
        "<title>Which category brings in the most revenue? (resumed: net)"
        " · nodeartifact</title>"
    ) in page
    assert "<dt>Resumed with</dt><dd>net</dd>" in page


def test_long_resume_answer_is_cut_in_the_list_only(client, store):
    answer = "Net revenue after returns and discounts, " * 4
    store.add(
        records(
            finished_run({"nodestep.resuming": True, "nodestep.resume": f'"{answer}"'})
        )
    )

    row = row_of(client.get("/").text, "Which category brings in the most revenue?")
    page = client.get(f"/traces/{TRACE_ID}").text

    assert "resumed: Net revenue after returns and discounts, Net revenue" in row
    assert "…</span>" in row
    assert f"<dt>Resumed with</dt><dd>{answer.strip()}</dd>" in page


def test_run_on_a_branch_shows_the_branch_only_in_the_facts(client, store):
    store.add(records(finished_run({"nodestep.branch_id": "b-7f3a"})))

    row = row_of(client.get("/").text, "Which category brings in the most revenue?")
    page = client.get(f"/traces/{TRACE_ID}").text

    assert "b-7f3a" not in row
    assert "<dt>Branch</dt><dd><code>b-7f3a</code></dd>" in page
    assert page.count("b-7f3a") == 1


def thread_run(trace_id, start, *, question, branch, status="completed", before=()):
    message = {"id": f"m-{trace_id[:4]}", "content": question, "type": "human"}
    return span(
        trace_id,
        ROOT_ID,
        "nodestep.graph text-to-sql-demo",
        start=start,
        end=start + 1_000,
        attributes={
            "nodestep.thread_id": "th-c04e5519",
            "nodestep.branch_id": branch,
            "nodestep.status": status,
            "nodestep.input": json.dumps({"messages": [message]}),
            "nodestep.state.before": json.dumps({"messages": [*before, message]}),
        },
    )


def test_edited_run_is_labelled_with_the_question_it_replaced(client, store):
    store.add(
        records(
            thread_run(
                OTHER_TRACE_ID,
                START_NS,
                question="Which category brings in the most revenue?",
                branch="main",
            ),
            thread_run(
                TRACE_ID,
                START_NS + 10_000,
                question="What is the revenue per month?",
                branch="b-7f3a",
            ),
        )
    )

    row = row_of(client.get("/").text, "What is the revenue per month?")
    page = client.get(f"/traces/{TRACE_ID}").text

    tag = (
        '<span class="nodestep-tag nodestep-tag-wrap">edit of Which category brings in the most'
        " revenue?</span>"
    )
    assert tag in row
    assert f"<h1>What is the revenue per month? {tag}" in page
    assert "<dt>Edit of</dt><dd>Which category brings in the most revenue?</dd>" in page
    assert "b-7f3a" not in row


def test_new_message_after_a_stop_is_labelled_after_a_stop(client, store):
    store.add(
        records(
            thread_run(
                OTHER_TRACE_ID,
                START_NS,
                question="Write a report on sales by store in 2025.",
                branch="main",
                status="cancelled",
            ),
            thread_run(
                TRACE_ID,
                START_NS + 10_000,
                question="Which store has the most returns?",
                branch="b-7f3a",
                before=[{"id": "m-0af7", "content": "Write a report", "type": "human"}],
            ),
        )
    )

    row = row_of(client.get("/").text, "Which store has the most returns?")
    page = client.get(f"/traces/{TRACE_ID}").text

    assert '<span class="nodestep-tag">after a stop</span>' in row
    assert (
        '<h1>Which store has the most returns? <span class="nodestep-tag">after a stop</span>'
        in page
    )


def test_run_on_the_main_branch_has_no_branch_label(client, store):
    store.add(records(finished_run({"nodestep.branch_id": "main"})))

    page = client.get("/").text + client.get(f"/traces/{TRACE_ID}").text

    assert "branch <code>" not in page
    assert "<dt>Branch</dt>" not in page


def span_page(client, store, attributes):
    store.add(records(span(TRACE_ID, ROOT_ID, "root", attributes=attributes)))
    return html.unescape(client.get(f"/traces/{TRACE_ID}/spans/{ROOT_ID}").text)


def test_span_with_only_grouped_attributes_has_no_empty_attribute_section(
    client, store
):
    page = span_page(client, store, {"nodestep.node.name": "plan"})

    assert "<h2>nodestep attributes</h2>" in page
    assert "<h2>Attributes</h2>" not in page
    assert "<h2>Other attributes</h2>" not in page


def test_other_attributes_next_to_grouped_ones_are_named_other(client, store):
    page = span_page(
        client, store, {"nodestep.node.name": "plan", "http.method": "GET"}
    )

    assert "<h2>Other attributes</h2>" in page
    assert "<h2>Attributes</h2>" not in page


def test_span_with_only_other_attributes_names_them_attributes(client, store):
    page = span_page(client, store, {"http.method": "GET"})

    assert "<h2>Attributes</h2>" in page


def test_span_without_attributes_says_so(client, store):
    page = span_page(client, store, {})

    assert '<h2>Attributes</h2>\n<p class="nodestep-hint">None.</p>' in page


def test_truncated_state_and_attributes_have_a_visible_note(client, store):
    page = span_page(
        client,
        store,
        {
            "nodestep.state.before": '{"count": 1}',
            "nodestep.state.after": '{"messages": ["latest"]}',
            "gen_ai.input.messages": "[]",
            "nodestep.truncated": ["nodestep.state.after", "gen_ai.input.messages"],
        },
    )

    sections = {
        section.split("</code>")[0]: section
        for section in page.split(
            '<section class="nodestep-section state">\n<h2><code>'
        )[1:]
    }
    assert "Shortened to fit" in sections["nodestep.state.after"].split("</section>")[0]
    assert (
        "Shortened to fit"
        not in sections["nodestep.state.before"].split("</section>")[0]
    )
    assert (
        '<p class="nodestep-message nodestep-message-warning">Some values were shortened'
        " before they were sent:"
        " <code>nodestep.state.after</code>, <code>gen_ai.input.messages</code>."
    ) in page
    assert (
        '<code>gen_ai.input.messages</code> <span class="nodestep-tag">shortened</span>'
        in page
    )


REPORT = "Write a report on sales by store in 2025."
THREAD = "ec4be1907b504d4e8beef63f282c1814"


def running_spans():
    extra = {
        "nodestep.thread_id": THREAD,
        "nodestep.state.before": json.dumps(
            {"messages": [{"id": "m1", "content": REPORT, "type": "human"}]}
        ),
    }
    return [
        node_span(THINK_ID, "think", 1, parent=MISSING_RUN_ID, extra=extra),
        node_span(ACT_ID, "act", 2, parent=MISSING_RUN_ID, extra=extra),
    ]


@pytest.fixture
def later(monkeypatch):
    monkeypatch.setattr(
        "nodeartifact.server.ui.time_ns", lambda: START_NS + 7_500_000_000
    )


def test_running_trace_has_the_running_badge_its_thread_and_question(
    client, store, later
):
    store.add(records(*running_spans()))

    row = row_of(client.get("/").text, REPORT)

    assert '<span class="nodestep-badge nodestep-badge-running">Running</span>' in row
    assert f'<code title="{THREAD}">{THREAD}</code>' in row
    assert '<span class="root">nodestep.graph agent</span>' in row
    assert "root span missing" not in row
    assert "unset" not in row
    assert (
        '<td class="nodestep-num" data-label="Duration" data-elapsed-ms="7499">'
        "7.50 s</td>"
    ) in row


def test_running_trace_page_counts_the_time_since_its_earliest_span(
    client, store, later
):
    store.add(records(*running_spans()))

    for path in (f"/traces/{TRACE_ID}", f"/traces/{TRACE_ID}/timeline"):
        page = client.get(path).text
        assert (
            f'<h1>{REPORT} <span class="root">nodestep.graph agent</span></h1>' in page
        )
        assert (
            '<div><dt>Duration</dt><dd id="trace-duration" data-elapsed-ms="7499">'
            "7.50 s</dd></div>"
        ) in page
        assert "The root span of this trace has not been received" not in page


def test_running_trace_page_from_a_clock_ahead_of_the_server_has_no_time_to_count(
    client, store, monkeypatch
):
    monkeypatch.setattr(
        "nodeartifact.server.ui.time_ns", lambda: START_NS - 120_000_000_000
    )
    store.add(records(*running_spans()))

    for path in (f"/traces/{TRACE_ID}", f"/traces/{TRACE_ID}/timeline"):
        page = client.get(path).text
        assert '<div><dt>Duration</dt><dd id="trace-duration">-</dd></div>' in page
        assert "data-elapsed-ms" not in page


@pytest.mark.skipif(NODE is None, reason="Node.js is not installed")
def test_the_trace_page_script_counts_only_a_time_that_is_not_negative():
    result = subprocess.run(
        [
            str(NODE),
            "--max-old-space-size=256",
            "--test-reporter=tap",
            str(TRACE_SCRIPT_TESTS),
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert result.returncode == 0, result.stdout[-6000:] + result.stderr[-2000:]
    assert "# fail 0" in result.stdout


def test_finished_trace_page_shows_the_span_of_its_spans(client, store, later):
    add_sample(store)

    page = client.get(f"/traces/{TRACE_ID}").text

    assert "<div><dt>Duration</dt><dd>1.0 ms</dd></div>" in page


@pytest.mark.parametrize("path", ["/nope", "/traces/", "/static/nope.css"])
def test_an_unknown_path_gives_the_not_found_page(client, path):
    response = client.get(path)

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("text/html")
    assert page_title(response.text) == "Not found · nodeartifact"
    assert f"No page at {path}." in response.text


STOPPED_TOPIC = "How many items were returned per store in 2025?"
DONE_TOPIC = "Which five products sold the most units in 2025?"
STOPPED_RUN_ID = "eee19b7ec3c1b1a1"
DONE_RUN_ID = "eee19b7ec3c1b1a2"


def sub_agent_run(span_id, question, status, start):
    return span(
        TRACE_ID,
        span_id,
        "nodestep.graph text-to-sql-demo-analyst",
        parent=CHILD_ID,
        start=START_NS + start,
        end=START_NS + start + 1_000,
        attributes={
            "nodestep.graph.name": "text-to-sql-demo-analyst",
            "nodestep.input": json.dumps(
                {"messages": [{"type": "human", "content": question}]}
            ),
            "nodestep.status": status,
        },
    )


def add_stopped_sub_agents(store):
    store.add(
        records(
            finished_run({"nodestep.status": "cancelled"}),
            span(
                TRACE_ID,
                CHILD_ID,
                "nodestep.node delegate",
                parent=ROOT_ID,
                start=START_NS + 100,
                attributes={"nodestep.node.name": "delegate"},
            ),
            sub_agent_run(STOPPED_RUN_ID, STOPPED_TOPIC, "cancelled", 200),
            sub_agent_run(DONE_RUN_ID, DONE_TOPIC, "completed", 300),
        )
    )


def test_timeline_names_each_nested_run_by_its_input(client, store):
    add_stopped_sub_agents(store)

    page = client.get(f"/traces/{TRACE_ID}/timeline").text
    rows = page.split('<li class="nodestep-timeline-row')[1:]
    nested = [
        row for row in rows if ">nodestep.graph text-to-sql-demo-analyst</a>" in row
    ]

    assert len(nested) == 2
    assert f'<span class="run-input">{STOPPED_TOPIC}</span>' in nested[0]
    assert f'<span class="run-input">{DONE_TOPIC}</span>' in nested[1]
    assert 'class="run-input"' not in timeline_row_of(
        page, "nodestep.graph text-to-sql-demo"
    )


def test_timeline_marks_stopped_runs(client, store):
    add_stopped_sub_agents(store)

    page = client.get(f"/traces/{TRACE_ID}/timeline").text
    stopped = '<span class="nodestep-badge nodestep-badge-stopped">Stopped</span>'
    rows = page.split('<li class="nodestep-timeline-row')[1:]
    marked = [row for row in rows if stopped in row]

    assert len(marked) == 2
    assert ">nodestep.graph text-to-sql-demo</a>" in marked[0]
    assert STOPPED_TOPIC in marked[1]


@pytest.mark.parametrize(
    ("span_id", "label", "badge"),
    [(STOPPED_RUN_ID, "Stopped", "stopped"), (DONE_RUN_ID, "Completed", "completed")],
)
def test_span_page_shows_the_status_of_a_nested_run(
    client, store, span_id, label, badge
):
    add_stopped_sub_agents(store)

    page = client.get(f"/traces/{TRACE_ID}/spans/{span_id}").text

    assert status_fact(page) == (
        f'<span class="nodestep-badge nodestep-badge-{badge}">{label}</span>'
    )


def test_span_page_of_a_span_without_a_run_status_shows_the_span_status(client, store):
    add_stopped_sub_agents(store)

    page = client.get(f"/traces/{TRACE_ID}/spans/{CHILD_ID}").text

    assert status_fact(page) == '<span class="status">unset</span>'
