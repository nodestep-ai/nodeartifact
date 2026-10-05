from collections.abc import AsyncGenerator, Mapping, Sequence
from contextlib import aclosing
from contextvars import ContextVar, Token
from importlib.metadata import version
from typing import Any, Self

from nodestep import Graph, Interrupt, Middleware, Resume, RunOutcome, StreamEvent
from nodestep.core.render import mermaid_id
from nodestep.middleware import (
    GraphMiddlewareContext,
    ModelMiddlewareContext,
    NodeMiddlewareContext,
    ToolMiddlewareContext,
)
from nodestep.state import StateStore
from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.trace import Span, SpanKind, Status, StatusCode, TracerProvider
from pydantic import BaseModel, ConfigDict

from nodeartifact.attributes import (
    TRUNCATED_ATTRIBUTE,
    GenAiMessages,
    JsonAttribute,
    JsonSpan,
)

TRACER_NAME = "nodeartifact"
INTERRUPT_EVENT = "nodestep.interrupt"
CANCELLED_EVENT = "nodestep.cancelled"
DEFAULT_PROVIDER_NAME = "openai"
UNFINISHED_CALL = "the call raised an error before it returned a result"
UNFINISHED_NODE = "the run failed before this node finished"
RESERVED_MERMAID_IDS = frozenset({"START", "END", "end"})


def mark_error(span: Span, error: BaseException) -> None:
    span.record_exception(error)
    span.set_status(Status(StatusCode.ERROR, f"{type(error).__name__}: {error}"))


def token_count(usage: Mapping[str, Any], *keys: str) -> int | None:
    for key in keys:
        value = usage.get(key)
        if type(value) is int:
            return value
    return None


class GraphDiagram(BaseModel):
    """The Mermaid source of a graph and the id it draws for each node.

    Attributes
    ----------
    source
        The flowchart from ``Graph.to_mermaid()``.
    node_ids
        The Mermaid id of each node by node name. Ids come from
        ``nodestep.core.render.mermaid_id``, with the suffix ``_2``, ``_3``
        and so on that ``to_mermaid`` gives an id that is already taken.
    """

    model_config = ConfigDict(frozen=True)

    source: str
    node_ids: dict[str, str]

    @classmethod
    def of(cls, graph: Graph) -> Self:
        """Draw a graph.

        Parameters
        ----------
        graph
            The graph to draw.

        Returns
        -------
        GraphDiagram
        """
        taken = set(RESERVED_MERMAID_IDS)
        node_ids: dict[str, str] = {}
        for name in graph.nodes:
            base = mermaid_id(name)
            candidate = base
            suffix = 1
            while candidate in taken:
                suffix += 1
                candidate = f"{base}_{suffix}"
            taken.add(candidate)
            node_ids[name] = candidate
        return cls(source=graph.to_mermaid(), node_ids=node_ids)


class CallRun(JsonSpan):
    def __init__(self, span: Span) -> None:
        super().__init__(span)
        self._context_token = otel_context.attach(trace.set_span_in_context(span))

    def leave(self) -> None:
        otel_context.detach(self._context_token)


class NodeRun(JsonSpan):
    def __init__(self, span: Span) -> None:
        super().__init__(span)
        self.calls: list[Span] = []
        self._context_token: Token[Context] | None = None
        self._node_token: Token[NodeRun | None] | None = None

    def enter(self) -> None:
        self._context_token = otel_context.attach(trace.set_span_in_context(self.span))
        self._node_token = CURRENT_NODE.set(self)

    def complete(self, update: Any) -> None:
        self.set_json("nodestep.update", update)
        self._leave()
        self.end()

    def fail(self, error: Exception) -> None:
        mark_error(self.span, error)
        self._leave()
        self.end(error)

    def end(self, error: BaseException | None = None, *, finished: bool = True) -> None:
        for call in self.calls:
            if error is not None:
                mark_error(call, error)
            elif finished:
                call.set_status(Status(StatusCode.ERROR, UNFINISHED_CALL))
            call.end()
        self.calls.clear()
        self.span.end()

    def _leave(self) -> None:
        if self._node_token is not None:
            CURRENT_NODE.reset(self._node_token)
        if self._context_token is not None:
            otel_context.detach(self._context_token)


class GraphRun(JsonSpan):
    def __init__(self, span: Span) -> None:
        super().__init__(span)
        self.nodes: dict[str, NodeRun] = {}
        self.node_ids: Mapping[str, str] = {}

    def record_interrupt(self, pending: Interrupt) -> None:
        payload = JsonAttribute.encode(pending.payload)
        attributes: dict[str, str | tuple[str, ...]] = {
            "nodestep.interrupt.key": pending.key,
            "nodestep.interrupt.id": pending.id,
            "nodestep.interrupt.node": pending.node,
            "nodestep.interrupt.task_id": pending.task_id,
            "nodestep.interrupt.payload": payload.text,
        }
        if payload.truncated:
            attributes[TRUNCATED_ATTRIBUTE] = ("nodestep.interrupt.payload",)
        self.span.add_event(INTERRUPT_EVENT, attributes)
        node = self.nodes.get(pending.task_id)
        if node is not None:
            node.span.add_event(INTERRUPT_EVENT, attributes)

    def finish(self, state: Any, outcome: RunOutcome) -> None:
        self.set_json("nodestep.state.after", state)
        self.span.set_attribute("nodestep.status", outcome.status)
        for pending in outcome.interrupts.values():
            self.record_interrupt(pending)
        cancelled = outcome.status == "cancelled"
        if cancelled:
            self.span.add_event(CANCELLED_EVENT)
        for node in self.nodes.values():
            if outcome.error is not None:
                node.span.set_status(Status(StatusCode.ERROR, UNFINISHED_NODE))
            elif cancelled:
                node.span.add_event(CANCELLED_EVENT)
            node.end(finished=False)
        self.nodes.clear()
        if outcome.error is not None:
            mark_error(self.span, outcome.error)
        self.span.end()


class RunScope:
    def __init__(
        self,
        owner: Middleware,
        initial_state: Any,
        resume: Resume | None,
        diagram: GraphDiagram,
    ) -> None:
        self.owner = owner
        self.initial_state = initial_state
        self.resume = resume
        self.diagram = diagram
        self.run_id: str | None = None


CURRENT_SCOPE: ContextVar[RunScope | None] = ContextVar(
    "nodeartifact_run_scope", default=None
)
CURRENT_NODE: ContextVar[NodeRun | None] = ContextVar("nodeartifact_node", default=None)
CURRENT_MODEL_CALL: ContextVar[CallRun | None] = ContextVar(
    "nodeartifact_model_call", default=None
)
CURRENT_TOOL_CALL: ContextVar[CallRun | None] = ContextVar(
    "nodeartifact_tool_call", default=None
)


class TracingMiddleware(Middleware):
    """Middleware that records nodestep runs as OpenTelemetry spans.

    Each graph run gets a ``nodestep.graph {name}`` span, each node run a
    ``nodestep.node {name}`` span under it, each model call made by
    ``model_node`` a ``chat {model}`` span and each tool call an
    ``execute_tool {tool}`` span under the node. While a node, a model call
    or a tool call runs, its span is the current OpenTelemetry span, so
    spans started by that code, and the runs of instrumented graphs it
    calls, nest under it. The run span of a top-level run is a child of the
    span that is current when the run starts.

    Graph and node spans carry ``nodestep.*`` attributes, with the state as
    JSON in ``nodestep.state.before`` and ``nodestep.state.after`` and a
    node's return value in ``nodestep.update``. Node spans also carry the
    ``nodestep.thread_id`` of the run. Model and tool spans follow the
    OpenTelemetry GenAI conventions and name the graph of the node that
    calls them in ``nodestep.graph.name``. JSON values longer than 16 KiB lose
    their oldest list items first, and the keys of shortened values are
    listed in ``nodestep.truncated``.

    The run span ends in ``on_run_end``, with ``nodestep.status`` set to the
    run's outcome: ``completed``, ``paused`` with one ``nodestep.interrupt``
    event per pending interrupt, ``failed`` with the error, or ``cancelled``
    with a ``nodestep.cancelled`` event. A tool that raises gets the error as
    its status and an ``exception`` event from ``on_tool_error``.

    ``nodeartifact.instrument`` adds this middleware and also records the run
    input in ``nodestep.input``, the resume answers in ``nodestep.resume``
    and the graph's Mermaid source in ``nodestep.graph.mermaid`` on the run
    span, and the Mermaid id of each node in ``nodestep.node.mermaid_id`` on
    its span and on the spans of its model and tool calls. No middleware hook
    sees these. Added to a graph directly, the middleware records everything
    else.

    Parameters
    ----------
    tracer_provider
        Provider of the tracer. ``None`` uses the global provider.
    provider_name
        The ``gen_ai.provider.name`` of every model span. nodestep does not
        tell middleware which provider a chat model uses.
    """

    def __init__(
        self,
        tracer_provider: TracerProvider | None = None,
        *,
        provider_name: str = DEFAULT_PROVIDER_NAME,
    ) -> None:
        self._tracer = trace.get_tracer(
            TRACER_NAME, version("nodeartifact"), tracer_provider=tracer_provider
        )
        self._provider_name = provider_name
        self._runs: dict[str, GraphRun] = {}

    def before_graph(self, ctx: GraphMiddlewareContext) -> None:
        span = self._tracer.start_span(
            f"nodestep.graph {ctx.graph_name}",
            kind=SpanKind.INTERNAL,
            attributes={
                "nodestep.graph.name": ctx.graph_name,
                "nodestep.run_id": ctx.run_id,
                "nodestep.root_run_id": ctx.root_run_id,
                "nodestep.thread_id": ctx.thread_id,
                "nodestep.branch_id": ctx.branch_id,
                "nodestep.resuming": ctx.resuming,
            },
        )
        run = GraphRun(span)
        run.set_json("nodestep.state.before", ctx.state)
        scope = CURRENT_SCOPE.get()
        if scope is not None and scope.owner is self and scope.run_id is None:
            scope.run_id = ctx.run_id
            span.set_attribute("nodestep.graph.mermaid", scope.diagram.source)
            run.node_ids = scope.diagram.node_ids
            if scope.initial_state is not None:
                run.set_json("nodestep.input", scope.initial_state)
            if scope.resume is not None:
                answers = scope.resume.answers
                run.set_json(
                    "nodestep.resume",
                    scope.resume.value if answers is None else answers,
                )
        self._runs[ctx.run_id] = run

    def on_run_end(self, ctx: GraphMiddlewareContext, outcome: RunOutcome) -> None:
        run = self._runs.pop(ctx.run_id, None)
        if run is not None:
            run.finish(ctx.state, outcome)

    def before_node(self, ctx: NodeMiddlewareContext) -> None:
        run = self._runs.get(ctx.run_id)
        span = self._tracer.start_span(
            f"nodestep.node {ctx.node_name}",
            context=None if run is None else trace.set_span_in_context(run.span),
            kind=SpanKind.INTERNAL,
            attributes={
                "nodestep.graph.name": ctx.graph_name,
                "nodestep.node.name": ctx.node_name,
                "nodestep.step": ctx.step,
                "nodestep.task_id": ctx.task_id,
                "nodestep.thread_id": ctx.thread_id,
            },
        )
        if run is not None and ctx.node_name in run.node_ids:
            span.set_attribute("nodestep.node.mermaid_id", run.node_ids[ctx.node_name])
        node = NodeRun(span)
        node.set_json("nodestep.state.before", ctx.state)
        node.enter()
        if run is not None:
            run.nodes[ctx.task_id] = node

    def after_node(self, ctx: NodeMiddlewareContext) -> None:
        node = self._pop_node(ctx)
        if node is not None:
            node.complete(ctx.state)

    def on_error(self, ctx: NodeMiddlewareContext, error: Exception) -> None:
        node = self._pop_node(ctx)
        if node is not None:
            node.fail(error)

    def before_model(self, ctx: ModelMiddlewareContext) -> None:
        span = self._tracer.start_span(
            f"chat {ctx.model}",
            kind=SpanKind.CLIENT,
            attributes={
                "gen_ai.operation.name": "chat",
                "gen_ai.request.model": ctx.model,
                "gen_ai.provider.name": self._provider_name,
                **self._caller(ctx.run_id, ctx.graph_name, ctx.node_name),
            },
        )
        call = self._open_call(span, CURRENT_MODEL_CALL)
        call.set_json(
            "gen_ai.input.messages", GenAiMessages.from_request(ctx.request.messages)
        )

    def after_model(self, ctx: ModelMiddlewareContext) -> None:
        call = self._close_call(CURRENT_MODEL_CALL)
        response = ctx.response
        if call is None or response is None:
            return
        span = call.span
        if response.model is not None:
            span.update_name(f"chat {response.model}")
            span.set_attribute("gen_ai.response.model", response.model)
        input_tokens = token_count(response.usage, "input_tokens", "prompt_tokens")
        if input_tokens is not None:
            span.set_attribute("gen_ai.usage.input_tokens", input_tokens)
        output_tokens = token_count(
            response.usage, "output_tokens", "completion_tokens"
        )
        if output_tokens is not None:
            span.set_attribute("gen_ai.usage.output_tokens", output_tokens)
        if response.finish_reason is not None:
            span.set_attribute(
                "gen_ai.response.finish_reasons", [response.finish_reason]
            )
        call.set_json("gen_ai.output.messages", GenAiMessages.from_response(response))
        span.end()

    def before_tool(self, ctx: ToolMiddlewareContext) -> None:
        attributes = {
            "gen_ai.operation.name": "execute_tool",
            "gen_ai.tool.name": ctx.tool_name,
            "gen_ai.tool.type": "function",
        }
        if ctx.tool_call_id is not None:
            attributes["gen_ai.tool.call.id"] = ctx.tool_call_id
        attributes.update(self._caller(ctx.run_id, ctx.graph_name, ctx.node_name))
        span = self._tracer.start_span(
            f"execute_tool {ctx.tool_name}",
            kind=SpanKind.INTERNAL,
            attributes=attributes,
        )
        call = self._open_call(span, CURRENT_TOOL_CALL)
        call.set_json("gen_ai.tool.call.arguments", ctx.value)

    def after_tool(self, ctx: ToolMiddlewareContext) -> None:
        call = self._close_call(CURRENT_TOOL_CALL)
        if call is None:
            return
        call.set_json("gen_ai.tool.call.result", ctx.value)
        call.span.end()

    def on_tool_error(self, ctx: ToolMiddlewareContext, error: Exception) -> None:
        call = self._close_call(CURRENT_TOOL_CALL)
        if call is None:
            return
        mark_error(call.span, error)
        call.span.end()

    def _caller(
        self, run_id: str | None, graph_name: str | None, node_name: str | None
    ) -> dict[str, str]:
        if graph_name is None:
            return {}
        caller = {"nodestep.graph.name": graph_name}
        run = None if run_id is None else self._runs.get(run_id)
        if run is not None and node_name is not None and node_name in run.node_ids:
            caller["nodestep.node.mermaid_id"] = run.node_ids[node_name]
        return caller

    def _pop_node(self, ctx: NodeMiddlewareContext) -> NodeRun | None:
        run = self._runs.get(ctx.run_id)
        return None if run is None else run.nodes.pop(ctx.task_id, None)

    @staticmethod
    def _open_call(span: Span, current: ContextVar[CallRun | None]) -> CallRun:
        call = CallRun(span)
        current.set(call)
        node = CURRENT_NODE.get()
        if node is not None:
            node.calls.append(span)
        return call

    @staticmethod
    def _close_call(current: ContextVar[CallRun | None]) -> CallRun | None:
        call = current.get()
        if call is None:
            return None
        current.set(None)
        call.leave()
        node = CURRENT_NODE.get()
        if node is not None and call.span in node.calls:
            node.calls.remove(call.span)
        return call


class InstrumentedGraph[StateT](Graph[StateT]):
    """A copy of a graph whose runs are traced, made by ``instrument``.

    It shares the nodes, flow, state store and workspace of the graph it was
    made from, and its middleware is that graph's middleware followed by
    ``tracing``. Every run goes through ``astream``, which passes the run
    input and the resume answers to ``tracing``.

    Parameters
    ----------
    graph
        The graph to copy. It is not changed. When it is an
        ``InstrumentedGraph`` itself, its ``TracingMiddleware`` is left out,
        so each run is traced once.
    tracing
        The middleware that records the runs.
    """

    def __init__(self, graph: Graph[StateT], tracing: TracingMiddleware) -> None:
        vars(self).update(vars(graph))
        middleware = graph.middleware
        if isinstance(graph, InstrumentedGraph):
            middleware = tuple(item for item in middleware if item is not graph.tracing)
        self.middleware = (*middleware, tracing)
        self.tracing = tracing

    async def astream(
        self,
        initial_state: StateT | dict[str, Any] | None = None,
        *,
        stream_mode: str | Sequence[str],
        thread_id: str | None = None,
        branch_id: str = "main",
        resume: Resume | None = None,
        state_store: StateStore | None = None,
        context: object | None = None,
    ) -> AsyncGenerator[StreamEvent]:
        """Run the graph like ``Graph.astream`` and trace the run.

        The run span also records the input as ``nodestep.input``, the
        resume answers as ``nodestep.resume`` and the graph's Mermaid source
        as ``nodestep.graph.mermaid``. Each node span records the Mermaid id
        of its node as ``nodestep.node.mermaid_id``.

        Parameters
        ----------
        initial_state, stream_mode, thread_id, branch_id, resume, state_store, context
            Same as for ``Graph.astream``.

        Returns
        -------
        AsyncGenerator[StreamEvent]
        """
        scope = RunScope(self.tracing, initial_state, resume, GraphDiagram.of(self))
        events = super().astream(
            initial_state,
            stream_mode=stream_mode,
            thread_id=thread_id,
            branch_id=branch_id,
            resume=resume,
            state_store=state_store,
            context=context,
        )
        async with aclosing(events):
            while True:
                token = CURRENT_SCOPE.set(scope)
                try:
                    event = await anext(events)
                except StopAsyncIteration:
                    return
                finally:
                    CURRENT_SCOPE.reset(token)
                yield event


def instrument[StateT](
    graph: Graph[StateT],
    *,
    tracer_provider: TracerProvider | None = None,
    provider_name: str = DEFAULT_PROVIDER_NAME,
) -> Graph[StateT]:
    """Return a copy of a graph that records its runs as spans.

    The copy shares the graph's nodes, flow, state store and workspace and
    adds a ``TracingMiddleware`` after the graph's own middleware. It adds no
    tools and changes no routing. The graph passed in is not changed. A graph
    that is already instrumented gets the new middleware in place of its
    old one. Child graphs run by a node, by ``subgraph`` or as background
    agents are traced when they are instrumented too; their spans nest under
    the node that runs them.

    Parameters
    ----------
    graph
        The graph to trace.
    tracer_provider
        Provider for the spans, such as the one returned by
        ``nodeartifact.configure``. ``None`` uses the global OpenTelemetry
        provider.
    provider_name
        The ``gen_ai.provider.name`` of every model span, such as
        ``"openai"`` for ``OpenAIChat``.

    Returns
    -------
    Graph
        The traced copy, an ``InstrumentedGraph``.
    """
    return InstrumentedGraph(
        graph, TracingMiddleware(tracer_provider, provider_name=provider_name)
    )
