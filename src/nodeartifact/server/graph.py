import re
from collections.abc import Iterable, Sequence
from enum import StrEnum
from itertools import groupby
from typing import Self

from pydantic import Field, computed_field

from nodeartifact.server.display import (
    TRUNCATED_ATTRIBUTE,
    format_attribute,
    format_duration,
)
from nodeartifact.server.models import (
    NODESTEP_STATUSES,
    Record,
    RunStatus,
    SpanRecord,
    StatusCode,
)
from nodeartifact.server.storage import TraceStore

MERMAID_ATTRIBUTE = "nodestep.graph.mermaid"
GRAPH_NAME = "nodestep.graph.name"
NODE_NAME = "nodestep.node.name"
MERMAID_ID = "nodestep.node.mermaid_id"
INTERRUPT_EVENT = "nodestep.interrupt"
CANCELLED_EVENT = "nodestep.cancelled"
OPERATION = "gen_ai.operation.name"
TOOL_OPERATION = "execute_tool"
CALL_OPERATIONS = frozenset({"chat", TOOL_OPERATION})
UPDATE = "nodestep.update"
STATE_BEFORE = "nodestep.state.before"
STATE_AFTER = "nodestep.state.after"
TOOL_NAME = "gen_ai.tool.name"
START_ID = "START"
END_ID = "END"
EDGE_LINE = re.compile(
    r'^\s*(\w+) (?:\S+@)?(?:-->|-\.->)(?:\|"[^"]*"\|)? (\w+)', re.MULTILINE
)


class StepStatus(StrEnum):
    """How one node run of a traced graph ended."""

    COMPLETED = "completed"
    PAUSED = "paused"
    STOPPED = "stopped"
    FAILED = "failed"


class GraphStep(Record):
    """One node run of a traced graph, in the order the nodes started.

    Attributes
    ----------
    number
        The position of the step in the run, from 1.
    span_id
        The id of the node span.
    node_name
        The ``nodestep.node.name`` of the span.
    mermaid_id
        The id of the node in the diagram, from ``nodestep.node.mermaid_id``,
        or ``None`` when the span does not carry one.
    superstep
        The ``nodestep.step`` of the span, or ``None`` when it is not an
        integer. Nodes of one superstep ran side by side.
    entered_from
        The diagram ids of the nodes the run came from: those of the
        previous superstep, or ``START`` for the first one unless the run
        resumed. The edges from these nodes to this one are the ones taken.
    status
        Failed when the span has an error status, paused when it has a
        ``nodestep.interrupt`` event or is the last span of a task that a
        ``nodestep.interrupt`` event of the run names, stopped when it has a
        ``nodestep.cancelled`` event, otherwise completed.
    failed_tools
        The ``gen_ai.tool.name`` of each ``execute_tool`` span under the
        node span that has an error status, in the order they started.
    start_ns
        When the node started.
    duration_ns
        How long the node ran.
    update
        The node's return value, ``nodestep.update``, pretty-printed.
    state_before
        The node's input state, ``nodestep.state.before``, pretty-printed.
    state_after
        The state once the step's superstep finished, pretty-printed: the
        ``nodestep.state.before`` of the first node of the next superstep, or
        the run span's ``nodestep.state.after`` after the last one. ``None``
        for a step that did not complete, and for the last superstep of a
        run that has not ended.
    truncated
        Which of ``update``, ``state_before`` and ``state_after`` were
        shortened before they were sent, from ``nodestep.truncated``.

    The four values are left out of ``model_dump``, so the graph data that
    the page and the live stream send stays small. The page shows them for
    one step at a time.
    """

    number: int
    span_id: str
    node_name: str
    mermaid_id: str | None
    superstep: int | None
    entered_from: list[str]
    status: StepStatus
    failed_tools: list[str]
    start_ns: int
    duration_ns: int
    update: str | None = Field(exclude=True)
    state_before: str | None = Field(exclude=True)
    state_after: str | None = Field(default=None, exclude=True)
    truncated: list[str] = Field(default_factory=list, exclude=True)

    @computed_field
    @property
    def duration(self) -> str:
        """How long the node ran, as the trace pages write durations."""
        return format_duration(self.duration_ns)


class TraceGraph(Record):
    """The diagram of the graph a trace ran and the steps it took.

    Attributes
    ----------
    graph_name
        The ``nodestep.graph.name`` of the run.
    service_name
        The service that sent the run.
    source
        The Mermaid flowchart from ``nodestep.graph.mermaid``, or ``None``
        when it is not known.
    borrowed
        Whether ``source`` comes from an earlier run of the same graph,
        because the run span that carries it has not arrived yet.
    run_span_id
        The id of the run span, which may not have arrived yet.
    steps
        The node spans directly under the run span, ordered by start time.
    finished
        Whether the run span has arrived, which happens when the run ends.
    status
        How the run ended, from its ``nodestep.status``, or ``None`` while
        it runs or when the run span carries no known status.
    ended_from
        The diagram ids of the nodes the run went from to ``END``: the
        nodes of the last superstep of a completed run, otherwise none.
    open_nodes
        The ``nodestep.node.mermaid_id`` of the model and tool spans of the
        run's graph whose node span has not arrived: the nodes still running.
    """

    graph_name: str | None
    service_name: str | None
    source: str | None
    borrowed: bool = False
    run_span_id: str | None
    steps: list[GraphStep]
    finished: bool
    status: RunStatus | None
    ended_from: list[str]
    open_nodes: list[str] = Field(default_factory=list)

    @computed_field
    @property
    def current(self) -> str | None:
        """The diagram id of the node an open run is in, or ``None``.

        It is the node of ``open_nodes`` when there is exactly one. Without
        any, it is the only node other than ``END`` that the diagram leads
        to from the nodes of the last superstep. When neither names exactly
        one node, and for a finished run, it is ``None``.
        """
        if self.finished:
            return None
        if self.open_nodes:
            return self.open_nodes[0] if len(self.open_nodes) == 1 else None
        if self.source is None:
            return None
        successors = self._successors(self.source, self._last_ids(self.steps))
        return successors.pop() if len(successors) == 1 else None

    @classmethod
    def from_spans(cls, spans: Sequence[SpanRecord]) -> Self | None:
        """Find the outermost traced graph run among the spans of one trace.

        The run is the earliest span carrying ``nodestep.graph.mermaid``,
        unless a node span whose parent is missing started before it. Then
        the outer run has not arrived yet and that span belongs to a child
        graph, so the run is the missing parent of the earliest such node
        span, and the steps of a run in progress are known before it ends.

        Parameters
        ----------
        spans
            The spans of one trace, in any order.

        Returns
        -------
        TraceGraph | None
            The run, or ``None`` when no span belongs to a traced graph.
        """
        ordered = sorted(spans, key=lambda item: (item.start_ns, item.span_id))
        known = {span.span_id for span in ordered}
        nodes = [span for span in ordered if cls._text(span, NODE_NAME) is not None]
        first = next((node for node in nodes if node.parent_span_id not in known), None)
        run = next(
            (span for span in ordered if cls._text(span, MERMAID_ATTRIBUTE)), None
        )
        if run is not None and (first is None or run.start_ns <= first.start_ns):
            steps = cls._steps(run.span_id, nodes, run, ordered)
            status = NODESTEP_STATUSES.get(str(run.attributes.get("nodestep.status")))
            return cls(
                graph_name=cls._text(run, GRAPH_NAME),
                service_name=run.resource.service_name,
                source=cls._text(run, MERMAID_ATTRIBUTE),
                run_span_id=run.span_id,
                steps=steps,
                finished=True,
                status=status,
                ended_from=cls._last_ids(steps)
                if status == RunStatus.COMPLETED
                else [],
            )
        if first is None or first.parent_span_id is None:
            return None
        graph_name = cls._text(first, GRAPH_NAME)
        return cls(
            graph_name=graph_name,
            service_name=first.resource.service_name,
            source=None,
            run_span_id=first.parent_span_id,
            steps=cls._steps(first.parent_span_id, nodes, None, ordered),
            finished=False,
            status=None,
            ended_from=[],
            open_nodes=cls._open_nodes(
                ordered, known, first.parent_span_id, graph_name
            ),
        )

    @classmethod
    def from_store(cls, store: TraceStore, spans: Sequence[SpanRecord]) -> Self | None:
        """Find the run as ``from_spans`` does, with a diagram when one is known.

        A run without its own diagram borrows the latest stored diagram of
        the same graph and service.

        Parameters
        ----------
        store
            The store that holds the trace.
        spans
            The spans of one trace, in any order.

        Returns
        -------
        TraceGraph | None
            The run, or ``None`` when no span belongs to a traced graph.
        """
        graph = cls.from_spans(spans)
        if graph is None or graph.source is not None or not graph.graph_name:
            return graph
        return graph.borrow(
            store.latest_graph_source(graph.graph_name, graph.service_name)
        )

    def borrow(self, source: str | None) -> Self:
        """Use the diagram of an earlier run while this run has none.

        Parameters
        ----------
        source
            The Mermaid source of an earlier run of the same graph, or
            ``None`` when there is none.

        Returns
        -------
        TraceGraph
            A copy with the source marked as borrowed, or this graph when it
            has a source already or ``source`` is ``None``.
        """
        if self.source is not None or source is None:
            return self
        return self.model_copy(update={"source": source, "borrowed": True})

    def step(self, span_id: str) -> GraphStep | None:
        """Return the step of a node span, or ``None`` when it is not a step."""
        return next((step for step in self.steps if step.span_id == span_id), None)

    @classmethod
    def _steps(
        cls,
        run_span_id: str,
        nodes: Sequence[SpanRecord],
        run: SpanRecord | None,
        spans: Sequence[SpanRecord],
    ) -> list[GraphStep]:
        children = [node for node in nodes if node.parent_span_id == run_span_id]
        paused = cls._paused_spans(run, children)
        failed_tools = cls._failed_tools(spans)
        resumed = run is not None and run.attributes.get("nodestep.resuming") is True
        groups: list[list[GraphStep]] = []
        previous: list[str] = [] if resumed else [START_ID]
        numbered = enumerate(children, start=1)
        for superstep, group in groupby(numbered, key=cls._superstep_key):
            groups.append(
                [
                    GraphStep(
                        number=number,
                        span_id=node.span_id,
                        node_name=cls._text(node, NODE_NAME) or "",
                        mermaid_id=cls._text(node, MERMAID_ID),
                        superstep=superstep if isinstance(superstep, int) else None,
                        entered_from=previous,
                        status=cls._status(node, paused),
                        failed_tools=failed_tools.get(node.span_id, []),
                        start_ns=node.start_ns,
                        duration_ns=node.duration_ns,
                        update=cls._json(node, UPDATE),
                        state_before=cls._json(node, STATE_BEFORE),
                        truncated=[
                            view
                            for view, key in (
                                ("update", UPDATE),
                                ("state_before", STATE_BEFORE),
                            )
                            if key in cls._truncated(node)
                        ],
                    )
                    for number, node in group
                ]
            )
            previous = cls._ids(groups[-1])
        return [
            cls._with_state_after(step, after, shortened)
            for index, group in enumerate(groups)
            for after, shortened in [cls._state_after(groups, index, run)]
            for step in group
        ]

    @classmethod
    def _state_after(
        cls, groups: Sequence[Sequence[GraphStep]], index: int, run: SpanRecord | None
    ) -> tuple[str | None, bool]:
        if index + 1 < len(groups):
            following = groups[index + 1][0]
            return following.state_before, "state_before" in following.truncated
        if run is None:
            return None, False
        return cls._json(run, STATE_AFTER), STATE_AFTER in cls._truncated(run)

    @staticmethod
    def _with_state_after(
        step: GraphStep, after: str | None, shortened: bool
    ) -> GraphStep:
        if step.status != StepStatus.COMPLETED or after is None:
            return step
        return step.model_copy(
            update={
                "state_after": after,
                "truncated": [*step.truncated, *(["state_after"] if shortened else [])],
            }
        )

    @staticmethod
    def _truncated(span: SpanRecord) -> list[str]:
        value = span.attributes.get(TRUNCATED_ATTRIBUTE)
        return (
            [key for key in value if isinstance(key, str)]
            if isinstance(value, list)
            else []
        )

    @staticmethod
    def _superstep_key(item: tuple[int, SpanRecord]) -> int | str:
        number, node = item
        value = node.attributes.get("nodestep.step")
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        return f"step {number}"

    @classmethod
    def _last_ids(cls, steps: Sequence[GraphStep]) -> list[str]:
        if not steps:
            return []
        last = steps[-1]
        if last.superstep is None:
            return cls._ids([last])
        return cls._ids([step for step in steps if step.superstep == last.superstep])

    @staticmethod
    def _ids(steps: Sequence[GraphStep]) -> list[str]:
        return [step.mermaid_id for step in steps if step.mermaid_id is not None]

    @staticmethod
    def _paused_spans(
        run: SpanRecord | None, children: Sequence[SpanRecord]
    ) -> set[str]:
        last_of_task = {
            task: node.span_id
            for node in children
            if isinstance(task := node.attributes.get("nodestep.task_id"), str)
        }
        tasks = {
            task
            for item in (run.events if run is not None else [])
            if item.name == INTERRUPT_EVENT
            and isinstance(
                task := item.attributes.get("nodestep.interrupt.task_id"), str
            )
        }
        own = {
            node.span_id
            for node in children
            if any(item.name == INTERRUPT_EVENT for item in node.events)
        }
        return own | {last_of_task[task] for task in tasks & last_of_task.keys()}

    @classmethod
    def _failed_tools(cls, spans: Sequence[SpanRecord]) -> dict[str, list[str]]:
        failed: dict[str, list[str]] = {}
        for span in spans:
            if (
                span.parent_span_id is not None
                and span.status_code == StatusCode.ERROR
                and span.attributes.get(OPERATION) == TOOL_OPERATION
            ):
                name = cls._text(span, TOOL_NAME) or span.name
                failed.setdefault(span.parent_span_id, []).append(name)
        return failed

    @classmethod
    def _open_nodes(
        cls,
        spans: Sequence[SpanRecord],
        known: set[str],
        run_span_id: str,
        graph_name: str | None,
    ) -> list[str]:
        open_nodes: list[str] = []
        for span in spans:
            node_id = cls._text(span, MERMAID_ID)
            if (
                node_id is not None
                and node_id not in open_nodes
                and span.attributes.get(OPERATION) in CALL_OPERATIONS
                and span.parent_span_id not in (None, run_span_id, *known)
                and cls._text(span, GRAPH_NAME) == graph_name
            ):
                open_nodes.append(node_id)
        return open_nodes

    @staticmethod
    def _successors(source: str, sources: Iterable[str]) -> set[str]:
        origins = set(sources)
        return {
            target
            for origin, target in EDGE_LINE.findall(source)
            if origin in origins and target != END_ID
        }

    @staticmethod
    def _status(node: SpanRecord, paused: set[str]) -> StepStatus:
        names = {item.name for item in node.events}
        if node.status_code == StatusCode.ERROR:
            return StepStatus.FAILED
        if node.span_id in paused:
            return StepStatus.PAUSED
        if CANCELLED_EVENT in names:
            return StepStatus.STOPPED
        return StepStatus.COMPLETED

    @staticmethod
    def _text(span: SpanRecord, key: str) -> str | None:
        value = span.attributes.get(key)
        return value if isinstance(value, str) else None

    @staticmethod
    def _json(span: SpanRecord, key: str) -> str | None:
        return (
            format_attribute(span.attributes[key]) if key in span.attributes else None
        )
