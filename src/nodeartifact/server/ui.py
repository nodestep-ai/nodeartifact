import json
import re
from collections.abc import Mapping, MutableMapping
from functools import partial
from time import time_ns
from typing import Any, Self, cast

from jinja2 import Environment, PackageLoader, StrictUndefined
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import (
    HTMLResponse,
    PlainTextResponse,
    Response,
    StreamingResponse,
)
from starlette.templating import Jinja2Templates

from nodeartifact.server.display import (
    AttributeGroups,
    DataTree,
    DataView,
    InputPreview,
    Timeline,
    TraceSearch,
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
from nodeartifact.server.graph import GraphStep, StepStatus, TraceGraph
from nodeartifact.server.live import LiveTrace, LiveTraceList, TraceListResults
from nodeartifact.server.models import (
    NODESTEP_STATUSES,
    Record,
    TraceDetail,
    TraceSummary,
)
from nodeartifact.server.security import (
    GRAPH_SECURITY_HEADERS,
    LIST_SECURITY_HEADERS,
    MERMAID_INTEGRITY,
    MERMAID_URL,
    SECURITY_HEADERS,
)
from nodeartifact.server.storage import TraceStore

TRACE_LIST_LIMIT = 100
TRACE_ID = re.compile(r"[0-9a-f]{32}")
SPAN_ID = re.compile(r"[0-9a-f]{16}")
ROW = re.compile(r"[0-9]{1,18}")
STEP_NUMBER = re.compile(r"[1-9][0-9]{0,8}")
STEP_VIEWS = (
    ("update", "Update"),
    ("state_before", "State before"),
    ("state_after", "State after"),
)


class StepView(Record):
    """One value of a step that the step details can show.

    Attributes
    ----------
    name
        ``update``, ``state_before`` or ``state_after``.
    label
        The name of the view switch.
    text
        The value, pretty-printed when it is JSON.
    truncated
        Whether the value was shortened before it was sent.
    """

    name: str
    label: str
    text: str
    truncated: bool

    @property
    def valid_json(self) -> bool:
        try:
            json.loads(self.text)
        except (ValueError, RecursionError):
            return False
        return True


class StepDetail(Record):
    """The step the graph view shows below the diagram and the steps.

    Attributes
    ----------
    step
        The step.
    total
        How many steps the run has so far.
    views
        The values the step has, in the order of ``STEP_VIEWS``.
    view
        The name of the view shown first, or ``""`` when there is none.
    """

    step: GraphStep
    total: int
    views: list[StepView]
    view: str

    @classmethod
    def select(cls, graph: TraceGraph, step: str, view: str) -> Self | None:
        """Pick a step and a view from the query of the page.

        Parameters
        ----------
        graph
            The run.
        step
            The ``step`` query parameter: a step number from 1. Any other
            text means the last step.
        view
            The ``view`` query parameter: the name of a view the step has.
            Any other text means its first view.

        Returns
        -------
        StepDetail | None
            The details, or ``None`` when the run has no step yet.
        """
        if not graph.steps:
            return None
        total = len(graph.steps)
        number = int(step) if STEP_NUMBER.fullmatch(step) else total
        chosen = graph.steps[number - 1 if number <= total else total - 1]
        views = [
            StepView(
                name=name,
                label=label,
                text=text,
                truncated=name in chosen.truncated,
            )
            for name, label in STEP_VIEWS
            if (text := getattr(chosen, name)) is not None
        ]
        names = [item.name for item in views]
        return cls(
            step=chosen,
            total=total,
            views=views,
            view=view if view in names else next(iter(names), ""),
        )


class TraceListPage(Record):
    """What the trace list shows: the traces found and the search that found them.

    ``searched`` is how many traces the search read when it stopped before
    the oldest ones, as ``TraceMatches.searched``.
    """

    traces: list[TraceSummary]
    limit: int
    search: TraceSearch
    searched: int | None
    endpoint: str
    now_ns: int


class TracePage(Record):
    """What the trace pages show: the trace, its graph run and the newest row."""

    detail: TraceDetail
    graph: TraceGraph | None
    last_row: int

    @property
    def live(self) -> bool:
        return self.graph is not None and not self.graph.finished

    @property
    def paused_spans(self) -> set[str]:
        if self.graph is None:
            return set()
        return {
            step.span_id
            for step in self.graph.steps
            if step.status == StepStatus.PAUSED
        }


def template_environment() -> Environment:
    """Build the Jinja environment of the pages, with their filters and tests."""
    environment = Environment(
        loader=PackageLoader("nodeartifact.server", "templates"),
        autoescape=True,
        undefined=StrictUndefined,
    )
    environment.filters.update(
        duration=format_duration,
        offset=format_offset,
        timestamp=format_timestamp,
        value=format_value,
        attribute=format_attribute,
        kind=kind_name,
        status=status_name,
        count=format_count,
        trace_status=trace_status,
        trace_status_class=trace_status_class,
        shorten=InputPreview.shorten,
        data_view=DataView.from_text,
    )
    environment.tests.update(error=is_error)
    template_globals = cast(MutableMapping[str, object], environment.globals)
    template_globals["data_tree"] = DataTree
    template_globals["search_max_length"] = TraceSearch.max_length
    return environment


class TraceUi:
    def __init__(self, store: TraceStore) -> None:
        self._store = store
        self._templates = Jinja2Templates(env=template_environment())

    async def trace_list(self, request: Request) -> Response:
        search = TraceSearch.from_query(request.query_params.get("q", ""))
        last_row = await run_in_threadpool(self._store.last_row)
        page = await run_in_threadpool(self._list_page, request, search)
        return self._render(
            request,
            "traces.html",
            headers=LIST_SECURITY_HEADERS,
            last_row=last_row,
            **dict(page),
        )

    async def list_live(self, request: Request) -> Response:
        search = TraceSearch.from_query(request.query_params.get("q", ""))
        stream = LiveTraceList(
            self._store,
            self._after(request),
            search,
            partial(self._list_results, request, search),
            request.is_disconnected,
        )
        return StreamingResponse(
            stream.events(),
            media_type="text/event-stream",
            headers={**SECURITY_HEADERS, "Cache-Control": "no-store"},
        )

    async def list_results(self, request: Request) -> Response:
        search = TraceSearch.from_query(request.query_params.get("q", ""))
        results = await run_in_threadpool(self._list_results, request, search)
        return HTMLResponse(
            results.html, headers={**SECURITY_HEADERS, "Cache-Control": "no-store"}
        )

    def _list_page(self, request: Request, search: TraceSearch) -> TraceListPage:
        if search.words:
            matches = self._store.search_traces(search, TRACE_LIST_LIMIT)
            traces, searched = matches.traces, matches.searched
        else:
            traces, searched = self._store.list_traces(TRACE_LIST_LIMIT), None
        return TraceListPage(
            traces=traces,
            limit=TRACE_LIST_LIMIT,
            search=search,
            searched=searched,
            endpoint=str(request.url_for("otlp_traces")),
            now_ns=time_ns(),
        )

    def _list_results(self, request: Request, search: TraceSearch) -> TraceListResults:
        page = self._list_page(request, search)
        template = self._templates.get_template("trace_results.html")
        return TraceListResults(
            html=template.render(request=request, **dict(page)).strip(),
            trace_ids=[trace.trace_id for trace in page.traces],
        )

    async def trace_view(self, request: Request) -> Response:
        return await self._trace_page(request, timeline=False)

    async def timeline_view(self, request: Request) -> Response:
        return await self._trace_page(request, timeline=True)

    async def live(self, request: Request) -> Response:
        trace_id = request.path_params["trace_id"]
        known = TRACE_ID.fullmatch(trace_id) and await run_in_threadpool(
            self._store.trace_spans, trace_id
        )
        if not known:
            return PlainTextResponse(
                f"No trace with id {trace_id}.",
                status_code=404,
                headers=SECURITY_HEADERS,
            )
        stream = LiveTrace(
            self._store, trace_id, self._after(request), request.is_disconnected
        )
        return StreamingResponse(
            stream.events(),
            media_type="text/event-stream",
            headers={**SECURITY_HEADERS, "Cache-Control": "no-store"},
        )

    @staticmethod
    def _after(request: Request) -> int:
        position = request.headers.get("last-event-id") or request.query_params.get(
            "after", ""
        )
        return int(position) if ROW.fullmatch(position) else 0

    async def _trace_page(self, request: Request, *, timeline: bool) -> Response:
        trace_id = request.path_params["trace_id"]
        page = (
            await run_in_threadpool(self._load, trace_id)
            if TRACE_ID.fullmatch(trace_id)
            else None
        )
        if page is None:
            return self._not_found(request, f"No trace with id {trace_id}.")
        graph_view = page.graph is not None and not timeline
        detail = (
            StepDetail.select(
                page.graph,
                request.query_params.get("step", ""),
                request.query_params.get("view", ""),
            )
            if graph_view and page.graph is not None
            else None
        )
        return self._render(
            request,
            "trace.html",
            headers=GRAPH_SECURITY_HEADERS if graph_view else SECURITY_HEADERS,
            trace=page.detail.summary,
            page=page,
            graph=page.graph,
            view="graph" if graph_view else "timeline",
            timeline=None if graph_view else Timeline.from_trace(page.detail),
            detail=detail,
            mermaid_url=MERMAID_URL,
            mermaid_integrity=MERMAID_INTEGRITY,
            now_ns=time_ns(),
        )

    def _load(self, trace_id: str) -> TracePage | None:
        detail = self._store.get_trace(trace_id)
        if detail is None:
            return None
        stored = self._store.trace_spans(trace_id)
        return TracePage(
            detail=detail,
            graph=TraceGraph.from_store(self._store, [item.span for item in stored]),
            last_row=max((item.row for item in stored), default=0),
        )

    async def span_view(self, request: Request) -> Response:
        trace_id = request.path_params["trace_id"]
        span_id = request.path_params["span_id"]
        valid = TRACE_ID.fullmatch(trace_id) and SPAN_ID.fullmatch(span_id)
        span = (
            await run_in_threadpool(self._store.get_span, trace_id, span_id)
            if valid
            else None
        )
        if span is None:
            return self._not_found(
                request, f"No span with id {span_id} in trace {trace_id}."
            )
        status = span.attributes.get("nodestep.status")
        run_status = NODESTEP_STATUSES.get(status) if isinstance(status, str) else None
        return self._render(
            request,
            "span.html",
            span=span,
            run_status=run_status,
            groups=AttributeGroups.from_attributes(span.attributes),
        )

    async def unknown_page(self, request: Request, error: Exception) -> Response:
        return self._not_found(request, f"No page at {request.url.path}.")

    def _not_found(self, request: Request, message: str) -> Response:
        return self._render(
            request, "error.html", status_code=404, title="Not found", message=message
        )

    def _render(
        self,
        request: Request,
        name: str,
        *,
        status_code: int = 200,
        headers: Mapping[str, str] = SECURITY_HEADERS,
        **context: Any,
    ) -> Response:
        return self._templates.TemplateResponse(
            request, name, context, status_code=status_code, headers=headers
        )
