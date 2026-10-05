import hashlib
import json
import sqlite3
import threading
from collections.abc import Sequence
from pathlib import Path
from types import TracebackType
from typing import Any, Self

from nodeartifact.server.display import InputPreview, TraceSearch, format_value
from nodeartifact.server.lineage import RunLineage, RunStart
from nodeartifact.server.models import (
    NODESTEP_STATUSES,
    EventRecord,
    JsonValue,
    ResourceRecord,
    RunStatus,
    ScopeRecord,
    SpanRecord,
    SpanSummary,
    StatusCode,
    StoredSpan,
    TraceDetail,
    TraceMatches,
    TraceSummary,
)

APPLICATION_ID = int.from_bytes(b"ndtr")
SCHEMA_VERSION = 1
INPUT_TOKENS = "gen_ai.usage.input_tokens"
OUTPUT_TOKENS = "gen_ai.usage.output_tokens"
THREAD_ID = "nodestep.thread_id"
RUN_INPUTS = {
    "nodestep.input": InputPreview.line,
    "gen_ai.input.messages": InputPreview.line,
    "nodestep.state.before": InputPreview.user_line,
}
MAIN_BRANCH = "main"
SEARCH_SCAN = 5000
SEARCH_CHUNK = 200

SCHEMA = f"""
BEGIN;
CREATE TABLE resources (
    id INTEGER PRIMARY KEY,
    fingerprint TEXT NOT NULL UNIQUE,
    service_name TEXT,
    attributes TEXT NOT NULL,
    schema_url TEXT NOT NULL
);
CREATE TABLE scopes (
    id INTEGER PRIMARY KEY,
    fingerprint TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    version TEXT NOT NULL,
    attributes TEXT NOT NULL,
    schema_url TEXT NOT NULL
);
CREATE TABLE spans (
    trace_id TEXT NOT NULL,
    span_id TEXT NOT NULL,
    parent_span_id TEXT,
    resource_id INTEGER NOT NULL REFERENCES resources (id),
    scope_id INTEGER NOT NULL REFERENCES scopes (id),
    name TEXT NOT NULL,
    kind INTEGER NOT NULL,
    start_ns INTEGER NOT NULL,
    end_ns INTEGER NOT NULL,
    status_code INTEGER NOT NULL,
    status_message TEXT NOT NULL,
    attributes TEXT NOT NULL,
    events TEXT NOT NULL,
    input_tokens INTEGER,
    output_tokens INTEGER,
    PRIMARY KEY (trace_id, span_id)
);
CREATE INDEX spans_listing
    ON spans (trace_id, start_ns, end_ns, status_code, input_tokens, output_tokens);
PRAGMA application_id = {APPLICATION_ID};
PRAGMA user_version = {SCHEMA_VERSION};
COMMIT;
"""

TRACE_SUMMARIES = """
WITH page AS (
    SELECT
        trace_id,
        min(start_ns) AS trace_start,
        max(end_ns) AS trace_end,
        count(*) AS span_count,
        sum(status_code = {error}) AS error_count,
        CASE WHEN count(input_tokens) > 0 THEN total(input_tokens) END AS input_tokens,
        CASE WHEN count(output_tokens) > 0 THEN total(output_tokens) END AS output_tokens
    FROM spans
    {where}
    GROUP BY trace_id
    ORDER BY trace_start DESC, trace_id
    LIMIT ? OFFSET ?
)
SELECT
    page.trace_id,
    root.name,
    resources.service_name,
    root.status_code,
    root.parent_span_id IS NULL,
    page.trace_start,
    page.trace_end,
    page.span_count,
    page.error_count,
    page.input_tokens,
    page.output_tokens,
    {thread_id},
    {inputs},
    root.attributes -> '$."nodestep.status"',
    EXISTS (
        SELECT 1 FROM json_each(root.events)
        WHERE json_each.value ->> '$.name' = 'nodestep.interrupt'
    ),
    root.attributes -> '$."nodestep.resuming"',
    root.attributes -> '$."nodestep.resume"',
    root.attributes -> '$."nodestep.branch_id"',
    json_type(root.attributes, '$."nodestep.node.name"') = 'text',
    root.attributes ->> '$."nodestep.graph.name"'
FROM page
JOIN spans AS root ON root.rowid = (
    SELECT candidate.rowid
    FROM spans AS candidate
    WHERE candidate.trace_id = page.trace_id
    ORDER BY candidate.parent_span_id IS NOT NULL, candidate.start_ns, candidate.span_id
    LIMIT 1
)
JOIN resources ON resources.id = root.resource_id
ORDER BY page.trace_start DESC, page.trace_id
"""
FIRST_ATTRIBUTE = """(
        SELECT candidate.attributes -> '$."{key}"'
        FROM spans AS candidate
        WHERE candidate.trace_id = page.trace_id
            AND candidate.attributes -> '$."{key}"' IS NOT NULL
        ORDER BY candidate.parent_span_id IS NOT NULL, candidate.start_ns, candidate.span_id
        LIMIT 1
    )"""
FIRST_ATTRIBUTE_START = """(
        SELECT candidate.start_ns
        FROM spans AS candidate
        WHERE candidate.trace_id = page.trace_id
            AND candidate.attributes -> '$."{key}"' IS NOT NULL
        ORDER BY candidate.parent_span_id IS NOT NULL, candidate.start_ns, candidate.span_id
        LIMIT 1
    )"""
SUMMARY_COLUMNS = {
    "error": StatusCode.ERROR.value,
    "thread_id": FIRST_ATTRIBUTE.format(key=THREAD_ID),
    "inputs": ",\n    ".join(
        [FIRST_ATTRIBUTE.format(key=key) for key in RUN_INPUTS]
        + [FIRST_ATTRIBUTE_START.format(key=key) for key in RUN_INPUTS]
    ),
}
LIST_TRACES = TRACE_SUMMARIES.format(where="", **SUMMARY_COLUMNS)
CHANGED_TRACES = TRACE_SUMMARIES.format(
    where="WHERE trace_id IN (SELECT trace_id FROM spans WHERE rowid > ?)",
    **SUMMARY_COLUMNS,
)
CHOSEN_TRACES = TRACE_SUMMARIES.format(
    where="WHERE trace_id IN (SELECT value FROM json_each(?))", **SUMMARY_COLUMNS
)
NEWEST_TRACES = """
SELECT trace_id
FROM spans
GROUP BY trace_id
ORDER BY min(start_ns) DESC, trace_id
LIMIT ?
"""
ONE_TRACE = TRACE_SUMMARIES.format(where="WHERE trace_id = ?", **SUMMARY_COLUMNS)

ROOT_RUNS = """
SELECT
    trace_id,
    start_ns,
    attributes ->> '$."nodestep.thread_id"',
    attributes ->> '$."nodestep.branch_id"',
    attributes ->> '$."nodestep.input"',
    attributes ->> '$."nodestep.state.before"',
    attributes ->> '$."nodestep.status"',
    attributes -> '$."nodestep.resuming"',
    attributes -> '$."nodestep.truncated"'
FROM spans
WHERE parent_span_id IS NULL
    AND attributes ->> '$."nodestep.thread_id"' IN (SELECT value FROM json_each(?))
"""

TRACE_SPANS = """
SELECT
    spans.span_id,
    spans.parent_span_id,
    spans.name,
    spans.kind,
    spans.start_ns,
    spans.end_ns,
    spans.status_code,
    resources.service_name,
    spans.input_tokens,
    spans.output_tokens,
    spans.attributes -> '$."nodestep.status"',
    spans.attributes -> '$."nodestep.input"'
FROM spans
JOIN resources ON resources.id = spans.resource_id
WHERE spans.trace_id = ?
ORDER BY spans.start_ns, spans.span_id
"""

SPAN_COLUMNS = """
    spans.trace_id,
    spans.span_id,
    spans.parent_span_id,
    spans.name,
    spans.kind,
    spans.start_ns,
    spans.end_ns,
    spans.status_code,
    spans.status_message,
    spans.attributes,
    spans.events,
    resources.attributes,
    resources.schema_url,
    scopes.name,
    scopes.version,
    scopes.attributes,
    scopes.schema_url
FROM spans
JOIN resources ON resources.id = spans.resource_id
JOIN scopes ON scopes.id = spans.scope_id"""

ONE_SPAN = f"""
SELECT
{SPAN_COLUMNS}
WHERE spans.trace_id = ? AND spans.span_id = ?
"""

SPANS_AFTER = f"""
SELECT
    spans.rowid,
{SPAN_COLUMNS}
WHERE spans.trace_id = ? AND spans.rowid > ?
ORDER BY spans.rowid
"""

LAST_ROW = "SELECT coalesce(max(rowid), 0) FROM spans"

LATEST_GRAPH_SOURCE = """
SELECT spans.attributes ->> '$."nodestep.graph.mermaid"'
FROM spans
JOIN resources ON resources.id = spans.resource_id
WHERE spans.attributes ->> '$."nodestep.graph.name"' = ?
    AND resources.service_name IS ?
    AND json_type(spans.attributes, '$."nodestep.graph.mermaid"') = 'text'
ORDER BY spans.start_ns DESC, spans.rowid DESC
LIMIT 1
"""

INSERT_SPAN = """
INSERT OR IGNORE INTO spans (
    trace_id, span_id, parent_span_id, resource_id, scope_id, name, kind, start_ns, end_ns,
    status_code, status_message, attributes, events, input_tokens, output_tokens
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


class TraceStoreError(Exception):
    """Raised when a trace database cannot be opened or is not nodeartifact's."""


class TraceStore:
    """Spans stored in a SQLite database in WAL mode.

    A new file gets the nodeartifact schema. An existing file is opened only when
    nodeartifact created it with the same schema version. One connection is
    shared by all threads and guarded by a lock.

    Parameters
    ----------
    path
        Path of the SQLite database file. Its directory must exist.

    Raises
    ------
    TraceStoreError
        If the file cannot be opened or used, is not a SQLite database, or
        was not created by this version of nodeartifact.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        try:
            self._connection = sqlite3.connect(self.path, check_same_thread=False)
        except sqlite3.Error as error:
            raise TraceStoreError(f"cannot open {self.path}: {error}") from error
        try:
            self._prepare()
        except sqlite3.Error as error:
            self._connection.close()
            raise TraceStoreError(f"cannot use {self.path}: {error}") from error
        except TraceStoreError:
            self._connection.close()
            raise

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def add(self, spans: Sequence[SpanRecord]) -> int:
        """Store spans, skipping any already stored.

        Parameters
        ----------
        spans
            Spans to store. A span is identified by its trace id and span id.

        Returns
        -------
        int
            The number of spans that were new.
        """
        with self._lock, self._connection:
            resources: dict[str, int] = {}
            scopes: dict[str, int] = {}
            stored = 0
            for span in spans:
                cursor = self._connection.execute(
                    INSERT_SPAN,
                    (
                        span.trace_id,
                        span.span_id,
                        span.parent_span_id,
                        self._resource_id(span.resource, resources),
                        self._scope_id(span.scope, scopes),
                        span.name,
                        span.kind,
                        span.start_ns,
                        span.end_ns,
                        span.status_code,
                        span.status_message,
                        self._dump(span.attributes),
                        self._dump([self._event_data(event) for event in span.events]),
                        self._token_count(span.attributes, INPUT_TOKENS),
                        self._token_count(span.attributes, OUTPUT_TOKENS),
                    ),
                )
                stored += cursor.rowcount
            return stored

    def list_traces(self, limit: int = 100) -> list[TraceSummary]:
        """Summarize the traces that started last.

        Parameters
        ----------
        limit
            Maximum number of traces to return.

        Returns
        -------
        list[TraceSummary]
            Traces ordered by start time, newest first.
        """
        return self._labelled(self._summaries(limit, 0))

    def search_traces(self, search: TraceSearch, limit: int = 100) -> TraceMatches:
        """Find the traces that started last and match a search.

        One query orders the traces, newest first. Their summaries are then
        read ``SEARCH_CHUNK`` traces at a time, until ``limit`` of them match,
        every trace is read, or ``SEARCH_SCAN`` traces are read, so a search
        over a large database stays quick.

        Parameters
        ----------
        search
            Words the traces must match.
        limit
            Maximum number of traces to return.

        Returns
        -------
        TraceMatches
            The matching traces, newest first, and how many traces were read
            when the search stopped at ``SEARCH_SCAN``.
        """
        with self._lock:
            newest = [
                trace_id
                for (trace_id,) in self._connection.execute(
                    NEWEST_TRACES, (SEARCH_SCAN + 1,)
                )
            ]
        chosen = newest[:SEARCH_SCAN]
        found: list[TraceSummary] = []
        for start in range(0, len(chosen), SEARCH_CHUNK):
            with self._lock:
                rows = self._connection.execute(
                    CHOSEN_TRACES,
                    (json.dumps(chosen[start : start + SEARCH_CHUNK]), -1, 0),
                ).fetchall()
            found.extend(
                summary
                for summary in map(self._summary, rows)
                if search.matches(summary)
            )
            if len(found) >= limit:
                return TraceMatches(traces=self._labelled(found[:limit]), searched=None)
        return TraceMatches(
            traces=self._labelled(found),
            searched=len(chosen) if len(newest) > SEARCH_SCAN else None,
        )

    def changed_traces(self, after: int) -> list[TraceSummary]:
        """Summarize the traces that got spans after the span with row number ``after``."""
        with self._lock:
            rows = self._connection.execute(CHANGED_TRACES, (after, -1, 0)).fetchall()
        return [self._summary(row) for row in rows]

    def last_row(self) -> int:
        """Return the row number of the newest stored span, or 0 when there is none."""
        with self._lock:
            return self._connection.execute(LAST_ROW).fetchone()[0]

    def get_trace(self, trace_id: str) -> TraceDetail | None:
        """Return one trace with its spans, or ``None`` when it is unknown."""
        with self._lock:
            row = self._connection.execute(ONE_TRACE, (trace_id, 1, 0)).fetchone()
            spans = self._connection.execute(TRACE_SPANS, (trace_id,)).fetchall()
        if row is None:
            return None
        (summary,) = self._labelled([self._summary(row)])
        return TraceDetail(
            summary=summary,
            spans=[
                SpanSummary(
                    span_id=span_id,
                    parent_span_id=parent_span_id,
                    name=name,
                    kind=kind,
                    start_ns=start_ns,
                    end_ns=end_ns,
                    status_code=status_code,
                    service_name=service_name,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    run_status=self._run_status(status),
                    input_text=InputPreview.line(json.loads(run_input))
                    if run_input is not None
                    else None,
                )
                for (
                    span_id,
                    parent_span_id,
                    name,
                    kind,
                    start_ns,
                    end_ns,
                    status_code,
                    service_name,
                    input_tokens,
                    output_tokens,
                    status,
                    run_input,
                ) in spans
            ],
        )

    def get_span(self, trace_id: str, span_id: str) -> SpanRecord | None:
        """Return one span with its resource and scope, or ``None`` when it is unknown."""
        with self._lock:
            row = self._connection.execute(ONE_SPAN, (trace_id, span_id)).fetchone()
        return None if row is None else self._span_record(row)

    def trace_spans(self, trace_id: str, after: int = 0) -> list[StoredSpan]:
        """Return the spans of a trace in the order they were stored.

        Parameters
        ----------
        trace_id
            The trace to read.
        after
            Only spans stored after the span with this row number are
            returned, so a caller can ask for the spans that are new since
            its last call.

        Returns
        -------
        list[StoredSpan]
            The spans with their row numbers, oldest row first.
        """
        with self._lock:
            rows = self._connection.execute(SPANS_AFTER, (trace_id, after)).fetchall()
        return [
            StoredSpan(row=row, span=self._span_record(columns))
            for row, *columns in rows
        ]

    def latest_graph_source(
        self, graph_name: str, service_name: str | None
    ) -> str | None:
        """Return the Mermaid source of the newest stored run of a graph.

        Parameters
        ----------
        graph_name
            The ``nodestep.graph.name`` of the run.
        service_name
            The service that sent the run.

        Returns
        -------
        str | None
            The ``nodestep.graph.mermaid`` value of the run that started
            last, or ``None`` when no run of that graph and service carries
            one.
        """
        with self._lock:
            row = self._connection.execute(
                LATEST_GRAPH_SOURCE, (graph_name, service_name)
            ).fetchone()
        return None if row is None else row[0]

    def _summaries(self, limit: int, offset: int) -> list[TraceSummary]:
        with self._lock:
            rows = self._connection.execute(LIST_TRACES, (limit, offset)).fetchall()
        return [self._summary(row) for row in rows]

    def _labelled(self, summaries: list[TraceSummary]) -> list[TraceSummary]:
        threads = sorted(
            {summary.thread_id for summary in summaries if summary.thread_id}
        )
        if not threads:
            return summaries
        with self._lock:
            rows = self._connection.execute(
                ROOT_RUNS, (json.dumps(threads),)
            ).fetchall()
        lineage = RunLineage(
            RunStart.from_row(row) for row in rows if isinstance(row[2], str)
        )
        return [lineage.label(summary) for summary in summaries]

    def close(self) -> None:
        """Close the database connection."""
        with self._lock:
            self._connection.close()

    def _prepare(self) -> None:
        connection = self._connection
        application_id = connection.execute("PRAGMA application_id").fetchone()[0]
        if application_id == 0:
            if connection.execute("SELECT count(*) FROM sqlite_master").fetchone()[0]:
                raise TraceStoreError(
                    f"{self.path} is a SQLite database not created by nodeartifact"
                )
            connection.executescript(SCHEMA)
        elif application_id != APPLICATION_ID:
            raise TraceStoreError(
                f"{self.path} is a SQLite database not created by nodeartifact"
            )
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version != SCHEMA_VERSION:
            raise TraceStoreError(
                f"{self.path} has schema version {version}; this nodeartifact reads version {SCHEMA_VERSION}"
            )
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
        connection.execute("PRAGMA foreign_keys = ON")

    def _resource_id(self, resource: ResourceRecord, known: dict[str, int]) -> int:
        attributes = self._dump(resource.attributes)
        fingerprint = self._fingerprint(resource.attributes, resource.schema_url)
        if fingerprint not in known:
            self._connection.execute(
                "INSERT OR IGNORE INTO resources (fingerprint, service_name, attributes, schema_url) VALUES (?, ?, ?, ?)",
                (fingerprint, resource.service_name, attributes, resource.schema_url),
            )
            known[fingerprint] = self._id("resources", fingerprint)
        return known[fingerprint]

    def _scope_id(self, scope: ScopeRecord, known: dict[str, int]) -> int:
        fingerprint = self._fingerprint(
            scope.name, scope.version, scope.attributes, scope.schema_url
        )
        if fingerprint not in known:
            self._connection.execute(
                "INSERT OR IGNORE INTO scopes (fingerprint, name, version, attributes, schema_url) VALUES (?, ?, ?, ?, ?)",
                (
                    fingerprint,
                    scope.name,
                    scope.version,
                    self._dump(scope.attributes),
                    scope.schema_url,
                ),
            )
            known[fingerprint] = self._id("scopes", fingerprint)
        return known[fingerprint]

    def _id(self, table: str, fingerprint: str) -> int:
        return self._connection.execute(
            f"SELECT id FROM {table} WHERE fingerprint = ?", (fingerprint,)
        ).fetchone()[0]

    @staticmethod
    def _span_record(row: Sequence[Any]) -> SpanRecord:
        (
            trace_id,
            span_id,
            parent_span_id,
            name,
            kind,
            start_ns,
            end_ns,
            status_code,
            status_message,
            attributes,
            events,
            resource_attributes,
            resource_schema_url,
            scope_name,
            scope_version,
            scope_attributes,
            scope_schema_url,
        ) = row
        return SpanRecord(
            trace_id=trace_id,
            span_id=span_id,
            parent_span_id=parent_span_id,
            name=name,
            kind=kind,
            start_ns=start_ns,
            end_ns=end_ns,
            status_code=status_code,
            status_message=status_message,
            attributes=json.loads(attributes),
            events=[
                EventRecord(
                    name=item["name"],
                    time_ns=item["time_ns"],
                    attributes=item["attributes"],
                )
                for item in json.loads(events)
            ],
            resource=ResourceRecord(
                attributes=json.loads(resource_attributes),
                schema_url=resource_schema_url,
            ),
            scope=ScopeRecord(
                name=scope_name,
                version=scope_version,
                attributes=json.loads(scope_attributes),
                schema_url=scope_schema_url,
            ),
        )

    @staticmethod
    def _summary(row: tuple) -> TraceSummary:
        (
            trace_id,
            name,
            service_name,
            root_status,
            complete,
            start_ns,
            end_ns,
            span_count,
            error_count,
            input_tokens,
            output_tokens,
            thread_id,
            *inputs,
            status,
            interrupted,
            resuming,
            resume,
            branch,
            node_root,
            graph_name,
        ) = row
        thread = None if thread_id is None else json.loads(thread_id)
        values, starts = inputs[: len(RUN_INPUTS)], inputs[len(RUN_INPUTS) :]
        candidates = [
            (key, start, read(json.loads(value)))
            for key, read, value, start in zip(
                RUN_INPUTS, RUN_INPUTS.values(), values, starts, strict=True
            )
            if value
        ]
        if not complete:
            others = [start for key, start, _ in candidates if key != "nodestep.input"]
            if others:
                candidates = [
                    candidate
                    for candidate in candidates
                    if candidate[0] != "nodestep.input" or candidate[1] <= min(others)
                ]
        input_text = next((line for _, _, line in candidates if line), None)
        running = not complete and bool(node_root)
        if running and isinstance(graph_name, str):
            name = f"nodestep.graph {graph_name}"
        run = TraceStore._root_values(
            complete, status=status, resuming=resuming, resume=resume, branch=branch
        )
        run_status = (
            NODESTEP_STATUSES.get(run["status"])
            if isinstance(run["status"], str)
            else None
        )
        if run["status"] is None and complete and interrupted:
            run_status = RunStatus.PAUSED
        resumed = run["resuming"] is True
        return TraceSummary(
            trace_id=trace_id,
            name=name,
            service_name=service_name,
            status_code=StatusCode.ERROR if error_count else root_status,
            start_ns=start_ns,
            end_ns=end_ns,
            span_count=span_count,
            error_count=error_count,
            input_tokens=None if input_tokens is None else int(input_tokens),
            output_tokens=None if output_tokens is None else int(output_tokens),
            complete=bool(complete),
            running=running,
            thread_id=None if thread is None else format_value(thread),
            input_text=input_text,
            input_preview=input_text and InputPreview.shorten(input_text),
            run_status=run_status,
            resumed=resumed,
            resume_text=(
                InputPreview.answers(run["resume"])
                if resumed and run["resume"] is not None
                else None
            ),
            branch_id=(
                run["branch"]
                if isinstance(run["branch"], str) and run["branch"] != MAIN_BRANCH
                else None
            ),
        )

    @staticmethod
    def _run_status(value: str | None) -> RunStatus | None:
        status = None if value is None else json.loads(value)
        return NODESTEP_STATUSES.get(status) if isinstance(status, str) else None

    @staticmethod
    def _root_values(complete: int, **values: str | None) -> dict[str, JsonValue]:
        return {
            key: json.loads(value) if complete and value is not None else None
            for key, value in values.items()
        }

    @staticmethod
    def _event_data(event: EventRecord) -> dict[str, JsonValue]:
        return {
            "name": event.name,
            "time_ns": event.time_ns,
            "attributes": event.attributes,
        }

    @staticmethod
    def _token_count(attributes: dict[str, JsonValue], key: str) -> int | None:
        value = attributes.get(key)
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    @staticmethod
    def _dump(value: JsonValue) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _fingerprint(*parts: JsonValue) -> str:
        canonical = json.dumps(
            list(parts), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(canonical.encode()).hexdigest()
