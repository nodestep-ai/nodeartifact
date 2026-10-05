import asyncio
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence

from starlette.concurrency import run_in_threadpool

from nodeartifact.server.display import TraceSearch, format_duration, status_name
from nodeartifact.server.graph import TraceGraph
from nodeartifact.server.models import JsonValue, Record, StoredSpan
from nodeartifact.server.storage import TraceStore

RETRY_MILLISECONDS = 2000


class LiveSpan(Record):
    """What the live stream sends about one new span."""

    span_id: str
    parent_span_id: str | None
    name: str
    status: str
    duration: str


class TraceListUpdate(Record):
    """What the live trace list sends: the results of the list as the page shows them."""

    html: str


class TraceListResults(Record):
    """The results of the trace list as the page shows them, and the traces they list."""

    html: str
    trace_ids: list[str]


def server_sent_event(
    name: str, data: dict[str, JsonValue], *, event_id: int | None = None
) -> str:
    lines: Sequence[str] = [
        *([f"id: {event_id}"] if event_id is not None else []),
        f"event: {name}",
        f"data: {json.dumps(data, ensure_ascii=False, separators=(',', ':'))}",
    ]
    return "\n".join(lines) + "\n\n"


class LiveTrace:
    """The server-sent events that follow one trace while its run is open.

    The stream sends a ``spans`` event whenever new spans of the trace are
    stored: the new spans, and the graph of the run worked out from every
    span of the trace. Its id is the row of the newest span, so a client
    that reconnects with ``Last-Event-ID`` gets only what it has not seen.
    An ``end`` event with the run status follows as soon as the run span has
    arrived, or at once when the trace holds no traced graph. When no span
    arrives for ``idle_seconds``, an ``idle`` event ends the stream. The
    stream also stops as soon as ``disconnected`` reports that the client
    has gone.
    """

    poll_seconds = 0.5
    idle_seconds = 600.0

    def __init__(
        self,
        store: TraceStore,
        trace_id: str,
        after: int,
        disconnected: Callable[[], Awaitable[bool]],
    ) -> None:
        self._store = store
        self._trace_id = trace_id
        self._after = after
        self._disconnected = disconnected

    async def events(self) -> AsyncIterator[str]:
        yield f"retry: {RETRY_MILLISECONDS}\n\n"
        graph = await run_in_threadpool(self._graph)
        quiet = 0.0
        while True:
            new = await run_in_threadpool(
                self._store.trace_spans, self._trace_id, self._after
            )
            if new:
                self._after = new[-1].row
                quiet = 0.0
                graph = await run_in_threadpool(self._graph)
                yield server_sent_event(
                    "spans",
                    {
                        "spans": [self._summary(item) for item in new],
                        "graph": None
                        if graph is None
                        else graph.model_dump(mode="json"),
                    },
                    event_id=self._after,
                )
            if graph is None or graph.finished:
                status = None if graph is None else graph.status
                yield server_sent_event("end", {"status": status})
                return
            if quiet >= self.idle_seconds:
                yield server_sent_event("idle", {"seconds": self.idle_seconds})
                return
            if await self._disconnected():
                return
            await asyncio.sleep(self.poll_seconds)
            quiet += self.poll_seconds

    def _graph(self) -> TraceGraph | None:
        stored = self._store.trace_spans(self._trace_id)
        return TraceGraph.from_store(self._store, [item.span for item in stored])

    @staticmethod
    def _summary(item: StoredSpan) -> dict[str, JsonValue]:
        span = item.span
        return LiveSpan(
            span_id=span.span_id,
            parent_span_id=span.parent_span_id,
            name=span.name,
            status=status_name(span.status_code),
            duration=format_duration(span.duration_ns),
        ).model_dump()


class LiveTraceList:
    """The server-sent events that keep the trace list up to date.

    Whenever new spans are stored, ``render`` is called for the results of
    the list, and a ``traces`` event sends them when they differ from the
    results sent last. With a search, the results are rendered again only
    when a trace with new spans is listed or matches the search. The event
    id is the row of the newest span, so a client that reconnects with
    ``Last-Event-ID`` gets an event only when spans arrived since. After a
    render it waits at least as long as the render took, so a slow search
    over many traces does not keep the server busy.
    When no span arrives for ``idle_seconds``, the stream ends and the
    browser opens it again. It also stops as soon as ``disconnected``
    reports that the client has gone.
    """

    poll_seconds = 0.5
    idle_seconds = 600.0

    def __init__(
        self,
        store: TraceStore,
        after: int,
        search: TraceSearch,
        render: Callable[[], TraceListResults],
        disconnected: Callable[[], Awaitable[bool]],
    ) -> None:
        self._store = store
        self._after = after
        self._search = search
        self._render = render
        self._disconnected = disconnected

    async def events(self) -> AsyncIterator[str]:
        yield f"retry: {RETRY_MILLISECONDS}\n\n"
        sent: TraceListResults | None = None
        quiet = 0.0
        while True:
            wait = self.poll_seconds
            last = await run_in_threadpool(self._store.last_row)
            if last != self._after:
                concerned = sent is None or await run_in_threadpool(
                    self._concerns, sent
                )
                self._after = last
                quiet = 0.0
                if concerned:
                    began = time.monotonic()
                    results = await run_in_threadpool(self._render)
                    wait = max(wait, time.monotonic() - began)
                    if sent is None or results.html != sent.html:
                        yield server_sent_event(
                            "traces",
                            TraceListUpdate(html=results.html).model_dump(),
                            event_id=last,
                        )
                    sent = results
            elif quiet >= self.idle_seconds:
                return
            if await self._disconnected():
                return
            await asyncio.sleep(wait)
            quiet += wait

    def _concerns(self, sent: TraceListResults) -> bool:
        if not self._search.words:
            return True
        listed = set(sent.trace_ids)
        return any(
            summary.trace_id in listed or self._search.matches(summary)
            for summary in self._store.changed_traces(self._after)
        )
