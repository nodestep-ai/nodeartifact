from enum import IntEnum, StrEnum

from pydantic import BaseModel, ConfigDict, Field

type JsonValue = (
    str | bool | int | float | list[JsonValue] | dict[str, JsonValue] | None
)


class SpanKind(IntEnum):
    UNSPECIFIED = 0
    INTERNAL = 1
    SERVER = 2
    CLIENT = 3
    PRODUCER = 4
    CONSUMER = 5


class StatusCode(IntEnum):
    UNSET = 0
    OK = 1
    ERROR = 2


class RunStatus(StrEnum):
    """How a nodestep run ended, read from the ``nodestep.status`` attribute."""

    COMPLETED = "completed"
    PAUSED = "paused"
    STOPPED = "stopped"
    FAILED = "failed"


NODESTEP_STATUSES: dict[str, RunStatus] = {
    "completed": RunStatus.COMPLETED,
    "paused": RunStatus.PAUSED,
    "interrupted": RunStatus.PAUSED,
    "cancelled": RunStatus.STOPPED,
    "failed": RunStatus.FAILED,
    "error": RunStatus.FAILED,
}


class Record(BaseModel):
    model_config = ConfigDict(frozen=True)


class ResourceRecord(Record):
    """The entity that produced a span, such as a service."""

    attributes: dict[str, JsonValue]
    schema_url: str = ""

    @property
    def service_name(self) -> str | None:
        value = self.attributes.get("service.name")
        return value if isinstance(value, str) else None


class ScopeRecord(Record):
    """The instrumentation library that created a span."""

    name: str
    version: str = ""
    attributes: dict[str, JsonValue] = Field(default_factory=dict)
    schema_url: str = ""


class EventRecord(Record):
    """A timestamped event inside a span."""

    name: str
    time_ns: int
    attributes: dict[str, JsonValue]


class SpanRecord(Record):
    """One span with its attributes, events, resource and scope."""

    trace_id: str
    span_id: str
    parent_span_id: str | None
    name: str
    kind: int
    start_ns: int
    end_ns: int
    status_code: int
    status_message: str
    attributes: dict[str, JsonValue]
    events: list[EventRecord]
    resource: ResourceRecord
    scope: ScopeRecord

    @property
    def duration_ns(self) -> int:
        return self.end_ns - self.start_ns


class StoredSpan(Record):
    """A span with the row number it was stored under.

    Row numbers grow with every stored span, so they tell which spans of a
    trace arrived after another one.
    """

    row: int
    span: SpanRecord


class ExportBatch(Record):
    """The spans of one OTLP export request, and how many were rejected."""

    spans: list[SpanRecord]
    rejected: int


class TraceSummary(Record):
    """One row of the trace list, aggregated over all spans of a trace.

    ``thread_id`` comes from the root span when it carries one, otherwise
    from the earliest span that does. ``input_text`` is the run input on one
    line and ``input_preview`` is that line cut to 80 characters. The input
    is ``nodestep.input`` when any span carries it, taken from the root span
    or else the earliest span, so a nested graph's ``nodestep.input`` wins
    over ``gen_ai.input.messages`` on the root. Only without it does
    ``gen_ai.input.messages`` count, in the same span order, and only
    without both the last user message in ``nodestep.state.before``.
    While the root span is missing, a ``nodestep.input`` that starts after
    the earliest other input belongs to a nested run, such as a sub-agent,
    and is skipped.

    ``running`` tells that the trace has no root span and that its earliest
    span is a node span: the run span of a graph run in progress has not
    arrived. ``name`` is then ``nodestep.graph {graph}``, the name that run
    span will have, from the ``nodestep.graph.name`` of that node span.

    The run fields come from the root span only, so they are empty until it
    has arrived. ``run_status`` is its ``nodestep.status``, or paused when it
    has no status but a ``nodestep.interrupt`` event. ``resumed`` is its
    ``nodestep.resuming`` and ``resume_text`` its ``nodestep.resume`` answers
    on one line. ``branch_id`` is its ``nodestep.branch_id`` unless that is
    ``main``. ``edit_of`` is the question of the run whose input this run
    replaced, and ``after_stop`` tells that this run is a new message after a
    stopped run; ``RunLineage`` sets both from the root spans of the thread.
    """

    trace_id: str
    name: str
    service_name: str | None
    status_code: int
    start_ns: int
    end_ns: int
    span_count: int
    error_count: int
    input_tokens: int | None
    output_tokens: int | None
    complete: bool
    thread_id: str | None
    input_text: str | None
    input_preview: str | None
    run_status: RunStatus | None
    resumed: bool
    resume_text: str | None
    branch_id: str | None
    edit_of: str | None = None
    after_stop: bool = False
    running: bool = False

    @property
    def duration_ns(self) -> int:
        return self.end_ns - self.start_ns


class TraceMatches(Record):
    """The traces that match a search, newest first.

    Attributes
    ----------
    traces
        The matching traces.
    searched
        How many traces the search read when it stopped at the most traces
        one search reads, with older traces left and fewer matches than it
        looked for; ``None`` when it read every trace or found them all.
    """

    traces: list[TraceSummary]
    searched: int | None


class SpanSummary(Record):
    """The fields of a span that the trace timeline shows.

    ``run_status`` is the ``nodestep.status`` of a run span, and
    ``input_text`` its ``nodestep.input`` on one line.
    """

    span_id: str
    parent_span_id: str | None
    name: str
    kind: int
    start_ns: int
    end_ns: int
    status_code: int
    service_name: str | None
    input_tokens: int | None
    output_tokens: int | None
    run_status: RunStatus | None = None
    input_text: str | None = None

    @property
    def duration_ns(self) -> int:
        return self.end_ns - self.start_ns


class TraceDetail(Record):
    """A trace summary with the spans of that trace, ordered by start time."""

    summary: TraceSummary
    spans: list[SpanSummary]
