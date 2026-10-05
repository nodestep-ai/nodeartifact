import asyncio
import json
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest
from opentelemetry.proto.trace.v1.trace_pb2 import Status

from conftest import BASE_URL
from graphs import MISSING_RUN_ID, THINK_ID, node_span, run_span
from nodeartifact.server import storage
from nodeartifact.server.app import create_app
from nodeartifact.server.display import TraceSearch
from nodeartifact.server.live import LiveTraceList, TraceListResults
from payloads import ROOT_ID, START_NS, records, span

REFUND_TRACE = "11111111111111111111111111111111"
REPORT_TRACE = "22222222222222222222222222222222"
CHECKOUT_TRACE = "33333333333333333333333333333333"
REFUND_THREAD = "th-refund-0001"
REPORT_THREAD = "th-report-0002"
REPORT = "Write a report on sales by store in 2025."
SECOND = 1_000_000_000
NODE = shutil.which("node")
LIST_SCRIPT_TESTS = Path(__file__).parents[1] / "js" / "traces.test.mjs"


def refund_run():
    return records(
        run_span(
            trace_id=REFUND_TRACE,
            start=START_NS,
            extra={"nodestep.thread_id": REFUND_THREAD},
        ),
        service="text-to-sql-demo",
    )


def report_run():
    return records(
        node_span(
            THINK_ID,
            "think",
            1000,
            parent=MISSING_RUN_ID,
            trace_id=REPORT_TRACE,
            extra={
                "nodestep.thread_id": REPORT_THREAD,
                "nodestep.state.before": json.dumps(
                    {"messages": [{"id": "m1", "type": "human", "content": REPORT}]}
                ),
            },
        ),
        service="text-to-sql-demo",
    )


def checkout_trace():
    return records(
        span(
            CHECKOUT_TRACE,
            ROOT_ID,
            "checkout",
            start=START_NS + 2 * SECOND,
            end=START_NS + 3 * SECOND,
            status=Status.STATUS_CODE_ERROR,
        ),
        service="shop",
    )


def add_three(store):
    store.add(refund_run())
    store.add(report_run())
    store.add(checkout_trace())


def listed(page: str) -> list[str]:
    return re.findall(r'<tr data-trace="([0-9a-f]{32})">', page)


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
    monkeypatch.setattr(LiveTraceList, "poll_seconds", 0.01)
    monkeypatch.setattr(LiveTraceList, "idle_seconds", 0.05)


def top_bar_search(page: str) -> str:
    header = page.split('<header class="nodestep-topbar">')[1].split("</header>")[0]
    end = header.split('<div class="nodestep-topbar-end">')[1]
    assert end.startswith("\n<form "), end[:80]
    assert end.index("</form>") < end.index('class="nodestep-theme-button"')
    return end.split("</form>")[0] + "</form>"


def test_the_search_field_sits_in_the_top_bar_and_works_without_a_script(client, store):
    add_three(store)

    page = client.get("/").text

    form = top_bar_search(page)
    assert form.startswith(
        f'\n<form class="nodestep-search" role="search" action="{BASE_URL}/"'
        ' method="get">\n'
    )
    assert re.search(
        r'<label class="nodestep-search-icon" for="trace-search"><svg viewBox="0 0 24 24"'
        r' aria-hidden="true" focusable="false"><path d="M9\.5 3A6\.5 [^"]+"></path>'
        r"</svg></label>",
        form,
    )
    assert (
        '<input class="nodestep-search-input" id="trace-search" type="search" name="q"'
        ' value="" maxlength="200" autocomplete="off" placeholder="Search traces"'
        ' aria-label="Search traces">'
    ) in form
    assert re.search(
        rf'<a class="nodestep-search-clear" href="{BASE_URL}/"'
        r' aria-label="Clear the search" data-clear-search><svg viewBox="0 0 24 24"'
        r' aria-hidden="true" focusable="false"><path d="M19 6\.41 [^"]+"></path>'
        r"</svg></a>",
        form,
    )
    main = page.split('<main class="nodestep-main nodestep-stack"')[1]
    assert "<form" not in main
    assert "trace-list-head" not in page
    assert "Search</button>" not in page
    assert "<h1>Traces</h1>" in main
    assert listed(page) == [CHECKOUT_TRACE, REPORT_TRACE, REFUND_TRACE]


@pytest.mark.parametrize(
    "path",
    [
        f"/traces/{CHECKOUT_TRACE}",
        f"/traces/{CHECKOUT_TRACE}/timeline",
        f"/traces/{CHECKOUT_TRACE}/spans/{ROOT_ID}",
        f"/traces/{REFUND_TRACE}",
        "/nope",
    ],
)
def test_every_page_has_the_search_field_and_sends_a_query_to_the_trace_list(
    client, store, path
):
    add_three(store)

    form = top_bar_search(client.get(path).text)

    assert form.startswith(
        f'\n<form class="nodestep-search" role="search" action="{BASE_URL}/"'
        ' method="get">\n'
    )
    assert (
        '<input class="nodestep-search-input" id="trace-search" type="search" name="q"'
        ' value="" maxlength="200" autocomplete="off" placeholder="Search traces"'
        ' aria-label="Search traces">'
    ) in form
    assert re.search(
        r'<button class="nodestep-search-clear" type="reset"'
        r' aria-label="Clear the search"><svg [^>]+><path d="M19 6\.41 [^"]+"></path>'
        r"</svg></button>",
        form,
    )
    assert "data-clear-search" not in form


@pytest.mark.parametrize(
    ("query", "found"),
    [
        ("refund", [REFUND_TRACE]),
        ("REFUND order", [REFUND_TRACE]),
        ("sales by store", [REPORT_TRACE]),
        ("nodestep.graph", [REPORT_TRACE, REFUND_TRACE]),
        ("th-rep", [REPORT_TRACE]),
        ("th-", [REPORT_TRACE, REFUND_TRACE]),
        ("3333", [CHECKOUT_TRACE]),
        ("shop", [CHECKOUT_TRACE]),
        ("text-to-sql-demo", [REPORT_TRACE, REFUND_TRACE]),
        ("running", [REPORT_TRACE]),
        ("Failed", [CHECKOUT_TRACE]),
        ("completed", [REFUND_TRACE]),
        ("refund completed", [REFUND_TRACE]),
        ("  refund   ", [REFUND_TRACE]),
        ('"sales by store"', [REPORT_TRACE]),
        ('"store by sales"', []),
        ('"sales by store', [REPORT_TRACE]),
        ('text-to-sql-demo "order A-1001"', [REFUND_TRACE]),
    ],
)
def test_search_finds_title_trace_id_thread_service_and_status(
    client, store, query, found
):
    add_three(store)

    page = client.get("/", params={"q": query}).text

    assert listed(page) == found


def test_a_search_keeps_its_words_in_the_field_and_offers_to_clear_them(client, store):
    add_three(store)

    page = client.get("/", params={"q": "  REFUND  "}).text

    form = top_bar_search(page)
    assert 'name="q" value="REFUND"' in form
    assert (
        f'<a class="nodestep-search-clear" href="{BASE_URL}/"'
        ' aria-label="Clear the search" data-clear-search>'
    ) in form


def test_a_search_without_matches_shows_the_empty_state_with_a_way_back(client, store):
    add_three(store)

    page = client.get("/", params={"q": "refund failed"}).text

    assert listed(page) == []
    assert "<table" not in page
    empty = page.split('<div class="nodestep-empty">')[1].split("</div>")[0]
    assert '<h2 class="nodestep-empty-title">No traces match this search</h2>' in empty
    assert (
        "<p>Search looks at the title, root span, trace id, thread, service and status.</p>"
        in (empty)
    )
    assert (
        f'<a class="nodestep-button nodestep-button-secondary" href="{BASE_URL}/"'
        " data-clear-search>Clear the search</a>"
    ) in empty
    assert "No traces yet" not in page


def test_an_empty_list_without_a_search_still_explains_how_to_send_traces(client):
    page = client.get("/").text

    assert "No traces yet" in page
    assert "No traces match" not in page


def test_the_query_is_escaped_in_the_field(client, store):
    add_three(store)

    page = client.get("/", params={"q": '"><script>alert(1)</script>'}).text

    assert "<script>alert(1)" not in page
    assert 'value="&#34;&gt;&lt;script&gt;alert(1)&lt;/script&gt;"' in page


def test_a_long_query_is_cut_to_the_length_of_the_field(client, store):
    add_three(store)

    page = client.get("/", params={"q": "r" * 1000}).text

    assert f'value="{"r" * 200}"' in page


def test_a_search_that_fills_the_page_says_the_newest_matches_are_shown(
    client, store, monkeypatch
):
    monkeypatch.setattr("nodeartifact.server.ui.TRACE_LIST_LIMIT", 2)
    add_three(store)

    page = client.get("/", params={"q": "e"}).text

    assert listed(page) == [CHECKOUT_TRACE, REPORT_TRACE]
    assert "Showing the 2 matching traces that started last." in page


@pytest.mark.parametrize(
    ("words", "matches"),
    [
        ("", True),
        ("refund", True),
        ("REFUND", True),
        ("th-refund", True),
        ("1111", True),
        ("text-to-sql-demo completed", True),
        ("refund running", False),
        ("unknown", False),
    ],
)
def test_a_trace_search_matches_every_word(store, words, matches):
    store.add(refund_run())
    (summary,) = store.list_traces()

    assert TraceSearch.from_query(words).matches(summary) is matches


@pytest.mark.parametrize(
    ("query", "words"),
    [
        ('"sales by store" 2025', ["sales by store", "2025"]),
        ('"Sales  by\tStore"', ["sales by store"]),
        ('say "hi', ["say", "hi"]),
        ('"" refund', ["refund"]),
        ('run_sql("x")', ['run_sql("x")']),
    ],
)
def test_a_quoted_phrase_is_one_word_and_stray_quotes_are_left_out(query, words):
    assert TraceSearch.from_query(query).words == words


def test_a_trace_search_keeps_its_text_on_one_line_and_short():
    assert TraceSearch.from_query("  a \n b\t").text == "a b"
    assert len(TraceSearch.from_query("x" * 500).text) == TraceSearch.max_length
    assert TraceSearch.from_query(" ").words == []


def seven_traces(store) -> list[str]:
    trace_ids = [f"{index:032x}" for index in range(1, 8)]
    for index, trace_id in enumerate(trace_ids):
        name = "match" if index % 2 == 0 else "other"
        store.add(records(span(trace_id, ROOT_ID, name, start=START_NS + index)))
    return trace_ids


def test_the_store_searches_the_newest_traces_first_and_stops_at_the_limit(store):
    trace_ids = seven_traces(store)

    found = store.search_traces(TraceSearch.from_query("match"), 3)
    every = store.search_traces(TraceSearch.from_query("match"), 10)

    assert [summary.trace_id for summary in found.traces] == [
        trace_ids[6],
        trace_ids[4],
        trace_ids[2],
    ]
    assert found.searched is None
    assert [summary.trace_id for summary in every.traces] == trace_ids[::-2]
    assert every.searched is None


def test_a_search_reads_at_most_the_newest_traces_and_says_how_many(store, monkeypatch):
    monkeypatch.setattr(storage, "SEARCH_SCAN", 4)
    trace_ids = seven_traces(store)

    found = store.search_traces(TraceSearch.from_query("match"), 10)
    filled = store.search_traces(TraceSearch.from_query("match"), 2)

    assert [summary.trace_id for summary in found.traces] == [
        trace_ids[6],
        trace_ids[4],
    ]
    assert found.searched == 4
    assert [summary.trace_id for summary in filled.traces] == [
        trace_ids[6],
        trace_ids[4],
    ]
    assert filled.searched is None


def test_a_search_of_exactly_the_most_traces_it_reads_reads_them_all(
    store, monkeypatch
):
    monkeypatch.setattr(storage, "SEARCH_SCAN", 7)
    seven_traces(store)

    assert store.search_traces(TraceSearch.from_query("zzz"), 10).searched is None


def test_a_search_orders_the_traces_once_and_reads_them_a_chunk_at_a_time(
    store, monkeypatch
):
    monkeypatch.setattr(storage, "SEARCH_SCAN", 1000)
    for index in range(450):
        store.add(records(span(f"{index + 1:032x}", ROOT_ID, "other")))
    statements: list[str] = []
    store._connection.set_trace_callback(statements.append)

    found = store.search_traces(TraceSearch.from_query("zzz"), 100)

    assert found.traces == []
    over_every_span = [
        sql
        for sql in statements
        if "GROUP BY trace_id" in sql and "json_each" not in sql
    ]
    assert len(over_every_span) == 1
    assert len(statements) - len(over_every_span) == 3


def test_the_store_summarizes_the_traces_that_got_spans_after_a_row(store):
    store.add(refund_run())
    seen = store.last_row()
    store.add(checkout_trace())
    store.add(records(span(REFUND_TRACE, "eee19b7ec3c1b179", "late", parent=ROOT_ID)))

    changed = store.changed_traces(seen)

    assert sorted(summary.trace_id for summary in changed) == [
        REFUND_TRACE,
        CHECKOUT_TRACE,
    ]
    refund = next(item for item in changed if item.trace_id == REFUND_TRACE)
    assert refund.span_count == 2
    assert store.changed_traces(store.last_row()) == []


def test_a_search_that_stops_before_the_oldest_traces_says_how_many_it_read(
    client, store, monkeypatch
):
    monkeypatch.setattr(storage, "SEARCH_SCAN", 2)
    add_three(store)

    missed = client.get("/", params={"q": "refund"}).text
    found = client.get("/", params={"q": "shop"}).text

    assert listed(missed) == []
    assert (
        "<p>Search looks at the title, root span, trace id, thread, service and status"
        " of the 2 traces that started last.</p>"
    ) in missed
    assert listed(found) == [CHECKOUT_TRACE]
    assert '<p class="nodestep-hint">Searched the 2 traces that started last.</p>' in (
        found
    )


def test_the_store_tells_the_row_of_the_newest_span(store):
    assert store.last_row() == 0

    store.add(refund_run())
    first = store.last_row()
    store.add(checkout_trace())

    assert first > 0
    assert store.last_row() > first


def test_the_list_page_follows_new_traces_under_its_own_policy(client, store):
    add_three(store)

    response = client.get("/", params={"q": "refund"})

    directives = policy(response)
    assert response.headers["content-security-policy"].startswith(
        "default-src 'none'; script-src 'self'; style-src 'self';"
    )
    assert directives["connect-src"] == "'self'"
    assert directives["form-action"] == "'self'"
    assert directives["script-src"] == "'self'"
    page = response.text
    results = page.split('<div class="trace-results" id="trace-results"')[1]
    assert results.startswith(
        f' data-live-url="{BASE_URL}/live?q=refund&amp;after={store.last_row()}"'
        f' data-results-url="{BASE_URL}/results">'
    )
    assert re.findall(r"<script\b[^>]*>", page) == [
        f'<script src="{BASE_URL}/static/nodestep-theme.js">',
        f'<script src="{BASE_URL}/static/traces.js">',
    ]
    assert page.index("static/traces.js") > page.index("</main>")


def test_other_pages_let_the_search_go_to_the_list_and_keep_connections_closed(
    client, store
):
    add_three(store)

    for path in (f"/traces/{CHECKOUT_TRACE}", f"/traces/{CHECKOUT_TRACE}/timeline"):
        directives = policy(client.get(path))
        assert directives["form-action"] == "'self'"
        assert "connect-src" not in directives
        assert directives["script-src"] == "'self'"
        assert directives["style-src"] == "'self'"
    graph = policy(client.get(f"/traces/{REFUND_TRACE}"))
    assert graph["form-action"] == "'self'"


@pytest.mark.parametrize(
    ("query", "found"),
    [
        ("", [CHECKOUT_TRACE, REPORT_TRACE, REFUND_TRACE]),
        ("refund", [REFUND_TRACE]),
        ("  TEXT-TO-SQL-DEMO  ", [REPORT_TRACE, REFUND_TRACE]),
        ('"sales by store"', [REPORT_TRACE]),
    ],
)
def test_the_results_route_sends_only_the_results_for_a_query(
    client, store, query, found
):
    add_three(store)

    response = client.get("/results", params={"q": query})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-security-policy"].startswith("default-src 'none'")
    assert response.text.startswith('<div class="nodestep-table-wrap">')
    for page_part in ("<html", "<head", "<header", "<main", "<script"):
        assert page_part not in response.text
    assert listed(response.text) == found


def test_the_results_route_sends_what_the_list_page_shows(client, store, monkeypatch):
    monkeypatch.setattr("nodeartifact.server.ui.time_ns", lambda: START_NS + 5 * SECOND)
    add_three(store)

    page = client.get("/", params={"q": "text-to-sql-demo"}).text
    fragment = client.get("/results", params={"q": "text-to-sql-demo"}).text

    shown = re.search(
        r'<div class="trace-results" id="trace-results"[^>]*>(.*)</div>\s*</main>',
        page,
        re.DOTALL,
    )
    assert shown is not None
    assert " ".join(shown.group(1).split()) == " ".join(fragment.split())


def test_the_results_route_offers_to_clear_a_search_without_matches(client, store):
    add_three(store)

    fragment = client.get("/results", params={"q": "nothing like this"}).text

    assert listed(fragment) == []
    assert '<h2 class="nodestep-empty-title">No traces match this search</h2>' in (
        fragment
    )
    assert (
        f'<a class="nodestep-button nodestep-button-secondary" href="{BASE_URL}/"'
        " data-clear-search>Clear the search</a>"
    ) in fragment


def test_the_results_route_escapes_what_the_traces_hold(client, store):
    store.add(
        records(
            span(REFUND_TRACE, ROOT_ID, "<script>alert(1)</script>"),
            service="<b>shop</b>",
        )
    )

    fragment = client.get("/results", params={"q": "shop"}).text

    assert "<script>" not in fragment
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in fragment


def test_a_running_trace_from_a_clock_ahead_of_the_server_has_no_time_to_count(
    client, store, monkeypatch
):
    monkeypatch.setattr(
        "nodeartifact.server.ui.time_ns", lambda: START_NS + 500_000_000
    )
    store.add(report_run())

    page = client.get("/").text

    assert '<td class="nodestep-num" data-label="Duration">-</td>' in page
    assert "data-elapsed-ms" not in page


def test_a_running_trace_counts_its_duration_from_the_page_time(
    client, store, monkeypatch
):
    monkeypatch.setattr(
        "nodeartifact.server.ui.time_ns", lambda: START_NS + 7_500_000_000
    )
    store.add(report_run())

    page = client.get("/").text

    assert (
        '<td class="nodestep-num" data-label="Duration" data-elapsed-ms="6500">'
        "6.50 s</td>"
    ) in page


def test_the_live_list_sends_the_results_when_spans_arrive(client, store, quick_live):
    store.add(refund_run())
    seen = store.last_row()
    store.add(report_run())

    response = client.get("/live", params={"after": seen})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-security-policy"].startswith("default-src 'none'")
    sent = events(response.text)
    assert [(row, name) for row, name, _ in sent] == [(str(store.last_row()), "traces")]
    results = sent[0][2]["html"]
    assert re.findall(r'<tr data-trace="([0-9a-f]{32})">', results) == [
        REPORT_TRACE,
        REFUND_TRACE,
    ]
    assert results.startswith('<div class="nodestep-table-wrap">')


def test_the_live_list_respects_the_search(client, store, quick_live):
    add_three(store)

    sent = events(client.get("/live", params={"q": "refund"}).text)

    (_, _, data) = sent[0]
    assert re.findall(r'<tr data-trace="([0-9a-f]{32})">', data["html"]) == [
        REFUND_TRACE
    ]


def test_the_live_list_sends_nothing_while_no_span_arrives(client, store, quick_live):
    add_three(store)
    newest = str(store.last_row())

    for request in (
        {"params": {"after": newest}},
        {"headers": {"Last-Event-ID": newest}},
    ):
        assert events(client.get("/live", **request).text) == []


@pytest.mark.parametrize("after", ["-1", "many", ""])
def test_the_live_list_reads_a_bad_position_as_the_start(
    client, store, quick_live, after
):
    add_three(store)

    sent = events(client.get("/live", params={"after": after}).text)

    assert [name for _, name, _ in sent] == ["traces"]


def test_the_live_list_escapes_what_the_traces_hold(client, store, quick_live):
    store.add(
        records(
            span(REFUND_TRACE, ROOT_ID, "<script>alert(1)</script>"),
            service="<b>shop</b>",
        )
    )

    sent = events(client.get("/live").text)

    results = sent[0][2]["html"]
    assert "<script>" not in results
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in results
    assert "&lt;b&gt;shop&lt;/b&gt;" in results


def results(html: str, *trace_ids: str) -> TraceListResults:
    return TraceListResults(html=html, trace_ids=list(trace_ids))


async def test_the_live_list_sends_new_results_once_and_skips_repeats(store):
    store.add(refund_run())
    pages = ["first", "first", "second"]
    rendered: list[str] = []
    repeated = threading.Event()

    def render() -> TraceListResults:
        rendered.append(pages[len(rendered)])
        if len(rendered) == len(pages) - 1:
            repeated.set()
        return results(rendered[-1])

    stream = LiveTraceList(store, store.last_row(), TraceSearch(text=""), render, never)
    stream.poll_seconds = 0.01
    stream.idle_seconds = 0.5
    sent = stream.events()

    assert (await anext(sent)).startswith("retry: ")
    store.add(report_run())
    first = await asyncio.wait_for(anext(sent), 1)
    following = asyncio.ensure_future(anext(sent))
    store.add(checkout_trace())
    assert await asyncio.to_thread(repeated.wait, 1)
    store.add(records(span(REFUND_TRACE, "eee19b7ec3c1b179", "late", parent=ROOT_ID)))
    second = await asyncio.wait_for(following, 1)

    assert first.startswith("id: ")
    assert "\nevent: traces\n" in first
    assert '"html":"first"' in first
    assert second.startswith(f"id: {store.last_row()}\n")
    assert '"html":"second"' in second
    assert len(rendered) == 3


async def test_after_a_slow_list_the_live_list_waits_as_long_before_it_looks_again(
    store,
):
    store.add(refund_run())
    starts: list[float] = []

    def render() -> TraceListResults:
        starts.append(time.monotonic())
        time.sleep(0.1)
        return results(f"page {len(starts)}")

    stream = LiveTraceList(store, 0, TraceSearch(text=""), render, never)
    stream.poll_seconds = 0.01
    stream.idle_seconds = 0.5
    sent = stream.events()

    await anext(sent)
    await asyncio.wait_for(anext(sent), 1)
    following = asyncio.ensure_future(anext(sent))
    store.add(report_run())
    await asyncio.wait_for(following, 1)

    assert starts[1] - starts[0] >= 0.2


async def test_a_live_search_renders_again_only_when_a_listed_or_matching_trace_changes(
    store,
):
    store.add(refund_run())
    rendered: list[int] = []

    def render() -> TraceListResults:
        rendered.append(len(rendered) + 1)
        return results(f"page {len(rendered)}", REFUND_TRACE)

    stream = LiveTraceList(
        store, store.last_row(), TraceSearch.from_query("refund"), render, never
    )
    stream.poll_seconds = 0.01
    stream.idle_seconds = 1
    sent = stream.events()

    await anext(sent)
    store.add(checkout_trace())
    first = await asyncio.wait_for(anext(sent), 1)
    following = asyncio.ensure_future(anext(sent))
    store.add(report_run())
    await asyncio.sleep(0.2)
    unrelated = list(rendered)
    store.add(records(span(REFUND_TRACE, "eee19b7ec3c1b179", "late", parent=ROOT_ID)))
    listed_again = await asyncio.wait_for(following, 1)
    following = asyncio.ensure_future(anext(sent))
    store.add(
        records(
            span("44444444444444444444444444444444", ROOT_ID, "refund again"),
            service="shop",
        )
    )
    matching = await asyncio.wait_for(following, 1)

    assert '"html":"page 1"' in first
    assert unrelated == [1]
    assert '"html":"page 2"' in listed_again
    assert '"html":"page 3"' in matching


async def never() -> bool:
    return False


@pytest.mark.parametrize("spec_version", ["2.3", "2.4"])
async def test_the_live_list_stops_when_the_client_disconnects(
    store, monkeypatch, spec_version
):
    monkeypatch.setattr(LiveTraceList, "poll_seconds", 0.01)
    store.add(refund_run())
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
        if b"event: traces" in message.get("body", b""):
            streaming.set()

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": spec_version},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/live",
        "raw_path": b"/live",
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"127.0.0.1:4318")],
        "client": ("127.0.0.1", 50000),
        "server": ("127.0.0.1", 4318),
    }

    await asyncio.wait_for(app(scope, receive, send), timeout=2)

    assert streaming.is_set()


def test_the_list_script_follows_the_stream_only_while_the_tab_is_shown(client):
    script = client.get("/static/traces.js").text

    for unsafe in (
        "innerHTML",
        "outerHTML",
        "insertAdjacentHTML",
        "eval(",
        "Function(",
    ):
        assert unsafe not in script
    assert "new EventSource(" in script
    assert 'addEventListener("traces"' in script
    assert 'document.addEventListener("visibilitychange"' in script
    assert "document.hidden" in script
    assert ".close()" in script
    assert "new DOMParser().parseFromString(" in script
    assert "focus({ preventScroll: true })" in script
    assert "window.scrollBy(" in script
    assert "window.history.replaceState(" in script
    assert "fetch(" in script
    assert "pushState" not in script


@pytest.mark.skipif(NODE is None, reason="Node.js is not installed")
def test_the_list_script_keeps_the_page_still_and_follows_the_stream_in_a_page():
    result = subprocess.run(
        [
            str(NODE),
            "--max-old-space-size=256",
            "--test-reporter=tap",
            str(LIST_SCRIPT_TESTS),
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert result.returncode == 0, result.stdout[-6000:] + result.stderr[-2000:]
    assert "# fail 0" in result.stdout
