import asyncio
import json
import re
from contextlib import aclosing

import pytest
from nodestep import (
    END,
    START,
    AgentState,
    AgentTask,
    BaseState,
    Command,
    Graph,
    InMemoryStateStore,
    Middleware,
    NodeContext,
    Resume,
    ScriptedChat,
    Send,
    build_react_agent,
    interrupt,
    model_node,
    node,
    run_agent,
    tool,
)
from nodestep.chat import AIMessage, ChatRequest, ChatResponse, HumanMessage, ToolCall
from nodestep.exceptions import GraphTimeoutError, ModelProviderError
from nodestep.middleware import GraphMiddlewareContext
from opentelemetry import trace
from opentelemetry.trace import INVALID_SPAN, SpanKind, StatusCode

from nodeartifact import TracingMiddleware, instrument
from spans import FinishedSpans, attributes, parent_id, span_id, trace_id

MODEL = "gpt-6-luna"
MAX_ATTRIBUTE_BYTES = 16 * 1024


class LunaChat(ScriptedChat):
    model = MODEL


class Notes(AgentState):
    topic: str = ""


class Counter(BaseState):
    count: int = 0


class Refund(BaseState):
    amount: float = 0.0
    approved: bool | None = None


class Report(BaseState):
    text: str = ""


@node
def prepare(state: Notes) -> dict:
    return {"topic": "refunds"}


@node
def bump(state: Counter) -> dict:
    return {"count": state.count + 1}


@node
def double(state: Counter) -> dict:
    return {"count": state.count * 2}


@node
def review(state: Refund) -> dict:
    answer = interrupt({"question": f"Approve {state.amount}?"}, id="approve_refund")
    return {"approved": answer}


@node
def explode(state: Counter) -> dict:
    raise ValueError("stock file is missing")


@node
def write_report(state: Report) -> dict:
    return {"text": state.text + "."}


@tool
def get_order_status(order_id: str) -> str:
    """Look up the shipping status of an order."""
    return "shipped" if order_id == "A-1001" else "unknown"


@tool
def check_warehouse(order_id: str) -> str:
    """Ask the warehouse about an order."""
    raise ConnectionError("warehouse offline")


def question(text: str) -> dict:
    return {"messages": [HumanMessage(content=text)]}


def linear_graph() -> Graph:
    chat = LunaChat(
        [
            ChatResponse(
                content="Refunds take five days.",
                model=MODEL,
                finish_reason="stop",
                usage={"input_tokens": 12, "output_tokens": 5},
            )
        ]
    )
    answer = model_node("answer", chat=chat, system_prompt="Answer briefly.")
    return Graph(Notes, name="linear").flow(
        START >> prepare, prepare >> answer, answer >> END
    )


def tool_call(name: str, call_id: str) -> ChatResponse:
    return ChatResponse(
        tool_calls=[ToolCall(id=call_id, name=name, arguments={"order_id": "A-1001"})],
        model=MODEL,
        finish_reason="tool_calls",
        usage={"prompt_tokens": 20, "completion_tokens": 7},
    )


def attribute_json(span, key: str):
    return json.loads(attributes(span)[key])


async def test_linear_graph_nests_node_and_model_spans_under_the_run(
    provider, spans: FinishedSpans
):
    traced = instrument(linear_graph(), tracer_provider=provider)

    await traced.ainvoke(question("How long do refunds take?"), thread_id="thread-1")

    run = spans.one("nodestep.graph linear")
    first = spans.one("nodestep.node prepare")
    answer = spans.one("nodestep.node answer")
    model = spans.one(f"chat {MODEL}")
    assert len(spans.all) == 4
    assert {trace_id(span) for span in spans.all} == {trace_id(run)}
    assert parent_id(run) is None
    assert parent_id(first) == span_id(run)
    assert parent_id(answer) == span_id(run)
    assert parent_id(model) == span_id(answer)
    assert model.kind == SpanKind.CLIENT
    assert run.kind == SpanKind.INTERNAL


async def test_graph_span_carries_thread_input_state_and_status(
    provider, spans: FinishedSpans
):
    traced = instrument(linear_graph(), tracer_provider=provider)

    await traced.ainvoke(question("How long do refunds take?"), thread_id="thread-1")

    run = spans.one("nodestep.graph linear")
    assert attributes(run)["nodestep.graph.name"] == "linear"
    assert attributes(run)["nodestep.thread_id"] == "thread-1"
    assert attributes(run)["nodestep.status"] == "completed"
    assert attributes(run)["nodestep.resuming"] is False
    assert attributes(run)["nodestep.run_id"] == attributes(run)["nodestep.root_run_id"]
    assert attribute_json(run, "nodestep.input")["messages"][0]["content"] == (
        "How long do refunds take?"
    )
    assert attribute_json(run, "nodestep.state.before")["topic"] == ""
    after = attribute_json(run, "nodestep.state.after")
    assert after["topic"] == "refunds"
    assert after["final_text"] == "Refunds take five days."
    assert run.status.status_code == StatusCode.UNSET


async def test_node_span_carries_its_input_and_update(provider, spans: FinishedSpans):
    traced = instrument(linear_graph(), tracer_provider=provider)

    await traced.ainvoke(question("How long do refunds take?"), thread_id="thread-1")

    first = spans.one("nodestep.node prepare")
    assert attributes(first)["nodestep.node.name"] == "prepare"
    assert attributes(first)["nodestep.graph.name"] == "linear"
    assert attributes(first)["nodestep.step"] == 0
    assert attributes(first)["nodestep.task_id"]
    assert attributes(first)["nodestep.thread_id"] == "thread-1"
    assert attribute_json(first, "nodestep.state.before")["topic"] == ""
    assert attribute_json(first, "nodestep.update") == {"topic": "refunds"}


async def test_model_span_follows_the_gen_ai_conventions(
    provider, spans: FinishedSpans
):
    traced = instrument(linear_graph(), tracer_provider=provider)

    await traced.ainvoke(question("How long do refunds take?"), thread_id="thread-1")

    model = spans.one(f"chat {MODEL}")
    assert attributes(model)["gen_ai.operation.name"] == "chat"
    assert attributes(model)["gen_ai.request.model"] == MODEL
    assert attributes(model)["gen_ai.provider.name"] == "openai"
    assert attributes(model)["gen_ai.response.model"] == MODEL
    assert attributes(model)["gen_ai.usage.input_tokens"] == 12
    assert attributes(model)["gen_ai.usage.output_tokens"] == 5
    assert attributes(model)["gen_ai.response.finish_reasons"] == ("stop",)
    assert attribute_json(model, "gen_ai.input.messages") == [
        {"role": "system", "parts": [{"type": "text", "content": "Answer briefly."}]},
        {
            "role": "user",
            "parts": [{"type": "text", "content": "How long do refunds take?"}],
        },
    ]
    assert attribute_json(model, "gen_ai.output.messages") == [
        {
            "role": "assistant",
            "parts": [{"type": "text", "content": "Refunds take five days."}],
            "finish_reason": "stop",
        }
    ]


async def test_tool_loop_records_tool_spans_under_the_tool_node(
    provider, spans: FinishedSpans
):
    chat = LunaChat(
        [
            tool_call("get_order_status", "call_1"),
            ChatResponse(content="Order A-1001 has shipped.", model=MODEL),
        ]
    )
    agent = build_react_agent(chat=chat, tools=[get_order_status])

    answer = await run_agent(
        instrument(agent, tracer_provider=provider), "Where is order A-1001?"
    )

    assert answer == "Order A-1001 has shipped."
    run = spans.one("nodestep.graph agent")
    thinking = spans.named("nodestep.node think")
    (acting,) = spans.named("nodestep.node act")
    (execution,) = spans.named("execute_tool get_order_status")
    models = spans.named(f"chat {MODEL}")
    assert len(thinking) == 2
    assert len(models) == 2
    assert [parent_id(span) for span in models] == [span_id(span) for span in thinking]
    assert parent_id(acting) == span_id(run)
    assert parent_id(execution) == span_id(acting)
    assert attributes(execution)["gen_ai.operation.name"] == "execute_tool"
    assert attributes(execution)["gen_ai.tool.name"] == "get_order_status"
    assert attributes(execution)["gen_ai.tool.call.id"] == "call_1"
    assert attributes(execution)["gen_ai.tool.type"] == "function"
    assert attribute_json(execution, "gen_ai.tool.call.arguments") == {
        "order_id": "A-1001"
    }
    assert attribute_json(execution, "gen_ai.tool.call.result") == {"result": "shipped"}
    assert execution.status.status_code == StatusCode.UNSET
    assert attributes(models[0])["gen_ai.usage.input_tokens"] == 20
    assert attributes(models[0])["gen_ai.usage.output_tokens"] == 7
    assert attributes(models[0])["gen_ai.response.finish_reasons"] == ("tool_calls",)
    assert attribute_json(models[0], "gen_ai.output.messages")[0]["parts"] == [
        {
            "type": "tool_call",
            "id": "call_1",
            "name": "get_order_status",
            "arguments": {"order_id": "A-1001"},
        }
    ]
    assert attribute_json(models[1], "gen_ai.input.messages")[-1] == {
        "role": "tool",
        "parts": [
            {
                "type": "tool_call_response",
                "id": "call_1",
                "response": '{"result":"shipped"}',
            }
        ],
    }


async def test_a_tool_that_raises_ends_its_span_with_an_error(
    provider, spans: FinishedSpans
):
    chat = LunaChat(
        [
            tool_call("check_warehouse", "call_7"),
            ChatResponse(content="The warehouse is offline.", model=MODEL),
        ]
    )
    agent = build_react_agent(chat=chat, tools=[check_warehouse], tool_errors="return")

    answer = await run_agent(
        instrument(agent, tracer_provider=provider), "Is A-1001 in stock?"
    )

    assert answer == "The warehouse is offline."
    execution = spans.one("execute_tool check_warehouse")
    assert execution.status.status_code == StatusCode.ERROR
    assert execution.status.description == "ConnectionError: warehouse offline"
    (event,) = [item for item in execution.events if item.name == "exception"]
    assert attributes(event)["exception.type"] == "ConnectionError"
    assert attributes(event)["exception.message"] == "warehouse offline"
    assert "gen_ai.tool.call.result" not in attributes(execution)
    second_call = spans.named(f"chat {MODEL}")[1]
    assert execution.end_time is not None
    assert second_call.start_time is not None
    assert execution.end_time <= second_call.start_time
    assert "ConnectionError: warehouse offline" in json.dumps(
        attribute_json(second_call, "gen_ai.input.messages")[-1]
    )
    assert parent_id(execution) == span_id(spans.one("nodestep.node act"))
    assert attributes(spans.one("nodestep.graph agent"))["nodestep.status"] == (
        "completed"
    )


async def test_interrupt_and_resume_are_two_traces_of_one_thread(
    provider, spans: FinishedSpans
):
    graph = Graph(Refund, name="refund", state_store=InMemoryStateStore()).flow(
        START >> review, review >> END
    )
    traced = instrument(graph, tracer_provider=provider)

    paused = await traced.ainvoke({"amount": 80}, thread_id="refund-1")

    assert paused.status == "interrupted"
    ((key, pending),) = paused.interrupts.items()
    run = spans.one("nodestep.graph refund")
    waiting = spans.one("nodestep.node review")
    assert attributes(run)["nodestep.status"] == "paused"
    assert attribute_json(run, "nodestep.state.after")["approved"] is None
    assert parent_id(waiting) == span_id(run)
    for span in (run, waiting):
        (event,) = [item for item in span.events if item.name == "nodestep.interrupt"]
        assert attributes(event)["nodestep.interrupt.key"] == key
        assert attributes(event)["nodestep.interrupt.id"] == "approve_refund"
        assert attributes(event)["nodestep.interrupt.node"] == "review"
        assert attributes(event)["nodestep.interrupt.task_id"] == pending.task_id
        assert json.loads(attributes(event)["nodestep.interrupt.payload"]) == {
            "question": "Approve 80.0?"
        }
    assert run.status.status_code == StatusCode.UNSET

    done = await traced.ainvoke(None, thread_id="refund-1", resume=Resume(True))

    assert done.state.approved is True
    first, second = spans.named("nodestep.graph refund")
    assert trace_id(first) != trace_id(second)
    assert attributes(second)["nodestep.resuming"] is True
    assert attributes(second)["nodestep.thread_id"] == "refund-1"
    assert attributes(second)["nodestep.status"] == "completed"
    assert (
        attributes(second)["nodestep.root_run_id"]
        == (attributes(first)["nodestep.run_id"])
    )
    assert json.loads(attributes(second)["nodestep.resume"]) is True
    assert "nodestep.input" not in attributes(second)
    resumed = spans.named("nodestep.node review")[1]
    assert parent_id(resumed) == span_id(second)
    assert attribute_json(resumed, "nodestep.update") == {"approved": True}


async def test_a_failing_node_marks_its_span_and_the_run_as_errors(
    provider, spans: FinishedSpans
):
    graph = Graph(Counter, name="stock").flow(
        START >> bump, bump >> explode, explode >> END
    )
    traced = instrument(graph, tracer_provider=provider)

    with pytest.raises(ValueError, match="stock file is missing"):
        await traced.ainvoke({"count": 1})

    run = spans.one("nodestep.graph stock")
    failed = spans.one("nodestep.node explode")
    assert spans.one("nodestep.node bump").status.status_code == StatusCode.UNSET
    for span in (run, failed):
        assert span.status.status_code == StatusCode.ERROR
        assert span.status.description == "ValueError: stock file is missing"
        (event,) = [item for item in span.events if item.name == "exception"]
        assert attributes(event)["exception.type"] == "ValueError"
    assert attributes(run)["nodestep.status"] == "failed"
    assert attribute_json(run, "nodestep.state.after") == {"count": 2}


async def test_a_child_graph_run_inside_a_node_nests_under_that_node(
    provider, spans: FinishedSpans
):
    child = instrument(
        Graph(Counter, name="child").flow(START >> bump, bump >> END),
        tracer_provider=provider,
    )

    @node
    async def delegate(state: Counter) -> dict:
        result = await child.ainvoke({"count": state.count})
        return {"count": result.state.count}

    parent = instrument(
        Graph(Counter, name="parent").flow(
            START >> delegate, delegate >> double, double >> END
        ),
        tracer_provider=provider,
    )
    tracer = provider.get_tracer("tests")

    with tracer.start_as_current_span("request") as request:
        result = await parent.ainvoke({"count": 2})

    assert result.state.count == 6
    outer = spans.one("nodestep.graph parent")
    inner = spans.one("nodestep.graph child")
    assert parent_id(outer) == request.get_span_context().span_id
    assert parent_id(inner) == span_id(spans.one("nodestep.node delegate"))
    assert parent_id(spans.one("nodestep.node bump")) == span_id(inner)
    assert parent_id(spans.one("nodestep.node double")) == span_id(outer)
    assert {trace_id(span) for span in spans.all} == {trace_id(outer)}


async def test_a_stream_closed_early_ends_the_run_as_cancelled(
    provider, spans: FinishedSpans
):
    graph = Graph(Counter, name="steps").flow(
        START >> bump, bump >> double, double >> END
    )
    traced = instrument(graph, tracer_provider=provider)

    async with aclosing(traced.astream({"count": 1}, stream_mode="updates")) as events:
        async for event in events:
            assert event.node == "bump"
            break

    run = spans.one("nodestep.graph steps")
    assert attributes(run)["nodestep.status"] == "cancelled"
    assert run.status.status_code == StatusCode.UNSET
    assert [item.name for item in run.events] == ["nodestep.cancelled"]
    assert attribute_json(run, "nodestep.state.after") == {"count": 2}
    assert [span.name for span in spans.all if span is not run] == [
        "nodestep.node bump"
    ]


def test_the_synchronous_stream_is_traced(provider, spans: FinishedSpans):
    graph = Graph(Counter, name="steps").flow(START >> bump, bump >> END)
    traced = instrument(graph, tracer_provider=provider)

    events = list(traced.stream({"count": 1}, stream_mode="updates"))

    assert [event.mode for event in events] == ["updates", "final"]
    assert attributes(spans.one("nodestep.graph steps"))["nodestep.status"] == (
        "completed"
    )


async def test_state_attributes_are_cut_at_16_kb(provider, spans: FinishedSpans):
    graph = Graph(Report, name="report").flow(
        START >> write_report, write_report >> END
    )
    traced = instrument(graph, tracer_provider=provider)

    await traced.ainvoke({"text": "é" * 20_000})

    run = spans.one("nodestep.graph report")
    for key in ("nodestep.state.before", "nodestep.state.after", "nodestep.input"):
        value = attributes(run)[key]
        assert value.startswith('{"text": "éé')
        assert len(value.encode()) <= MAX_ATTRIBUTE_BYTES
        assert len(value.encode()) > MAX_ATTRIBUTE_BYTES - 4
    assert attributes(run)["nodestep.truncated"] == (
        "nodestep.state.before",
        "nodestep.input",
        "nodestep.state.after",
    )


async def test_instrument_leaves_the_original_graph_untouched(
    provider, spans: FinishedSpans
):
    graph = Graph(Counter, name="steps").flow(
        START >> bump, bump >> double, double >> END
    )

    traced = instrument(graph, tracer_provider=provider)
    plain = await graph.ainvoke({"count": 1})

    assert graph.middleware == ()
    assert type(graph) is Graph
    assert isinstance(traced, Graph)
    assert traced is not graph
    assert traced.nodes is graph.nodes
    assert [type(item) for item in traced.middleware] == [TracingMiddleware]
    assert spans.all == []
    assert (await traced.ainvoke({"count": 1})).data == plain.data
    assert len(spans.all) == 3


class SpanningChat(LunaChat):
    def __init__(self, responses: list[ChatResponse], tracer) -> None:
        super().__init__(responses)
        self.tracer = tracer

    async def complete(self, request: ChatRequest) -> ChatResponse:
        with self.tracer.start_as_current_span("provider request"):
            return await super().complete(request)


class FailingAfterGraph(Middleware):
    def after_graph(self, ctx: GraphMiddlewareContext) -> None:
        raise RuntimeError("audit log is full")


class Pair(BaseState):
    left: bool | None = None
    right: bool | None = None


@node
def ask_left(state: Pair) -> dict:
    return {"left": interrupt({"question": "Left?"}, id="left")}


@node
def ask_right(state: Pair) -> dict:
    return {"right": interrupt({"question": "Right?"}, id="right")}


@node(goto=[ask_left, ask_right])
def split(state: Pair) -> Command:
    return Command(goto=[Send(ask_left), Send(ask_right)])


@node
async def slow_bump(state: Counter) -> dict:
    await asyncio.sleep(0.01)
    return {"count": state.count + 1}


async def test_spans_started_in_a_tool_nest_under_its_tool_span(
    provider, spans: FinishedSpans
):
    tracer = provider.get_tracer("tests")
    helper = instrument(
        build_react_agent(
            chat=LunaChat([ChatResponse(content="It left today.", model=MODEL)]),
            tools=[],
            name="helper",
        ),
        tracer_provider=provider,
    )

    @tool
    async def ask_helper(question: str) -> str:
        """Ask the helper agent."""
        with tracer.start_as_current_span("lookup"):
            return await run_agent(helper, question)

    chat = LunaChat(
        [
            ChatResponse(
                tool_calls=[
                    ToolCall(
                        id="call_1", name="ask_helper", arguments={"question": "?"}
                    )
                ],
                model=MODEL,
            ),
            ChatResponse(content="It left today.", model=MODEL),
        ]
    )
    agent = instrument(
        build_react_agent(chat=chat, tools=[ask_helper]), tracer_provider=provider
    )

    await run_agent(agent, "When did A-1001 leave?")

    execution = spans.one("execute_tool ask_helper")
    lookup = spans.one("lookup")
    inner = spans.one("nodestep.graph helper")
    outer = span_id(spans.one("nodestep.graph agent"))
    thinking = {
        span_id(span)
        for span in spans.named("nodestep.node think")
        if parent_id(span) == outer
    }
    outer_models = [
        span for span in spans.named(f"chat {MODEL}") if parent_id(span) in thinking
    ]
    assert parent_id(execution) == span_id(spans.one("nodestep.node act"))
    assert parent_id(lookup) == span_id(execution)
    assert parent_id(inner) == span_id(lookup)
    assert len(outer_models) == 2
    assert {trace_id(span) for span in spans.all} == {trace_id(execution)}
    assert trace.get_current_span() is INVALID_SPAN


async def test_parallel_tool_calls_get_one_span_each(provider, spans: FinishedSpans):
    tracer = provider.get_tracer("tests")

    @tool
    async def find_order(order_id: str) -> str:
        """Find an order."""
        with tracer.start_as_current_span(f"query {order_id}"):
            await asyncio.sleep(0.01)
        return f"found {order_id}"

    chat = LunaChat(
        [
            ChatResponse(
                tool_calls=[
                    ToolCall(
                        id="call_1", name="find_order", arguments={"order_id": "1"}
                    ),
                    ToolCall(
                        id="call_2", name="find_order", arguments={"order_id": "2"}
                    ),
                ],
                model=MODEL,
            ),
            ChatResponse(content="Both exist.", model=MODEL),
        ]
    )
    agent = instrument(
        build_react_agent(chat=chat, tools=[find_order]), tracer_provider=provider
    )

    await run_agent(agent, "Do orders 1 and 2 exist?")

    acting = spans.one("nodestep.node act")
    executions = {
        attributes(span)["gen_ai.tool.call.id"]: span
        for span in spans.named("execute_tool find_order")
    }
    assert set(executions) == {"call_1", "call_2"}
    for call_id, order_id in (("call_1", "1"), ("call_2", "2")):
        execution = executions[call_id]
        assert parent_id(execution) == span_id(acting)
        assert attribute_json(execution, "gen_ai.tool.call.arguments") == {
            "order_id": order_id
        }
        assert attribute_json(execution, "gen_ai.tool.call.result") == {
            "result": f"found {order_id}"
        }
        assert execution.status.status_code == StatusCode.UNSET
        assert parent_id(spans.one(f"query {order_id}")) == span_id(execution)


@pytest.mark.parametrize("stream", [False, True])
async def test_spans_started_during_a_model_call_nest_under_its_chat_span(
    provider, spans: FinishedSpans, stream: bool
):
    chat = SpanningChat(
        [
            ChatResponse(
                content="Refunds take five days.",
                model=MODEL,
                finish_reason="stop",
                usage={"input_tokens": 12, "output_tokens": 5},
            )
        ],
        provider.get_tracer("tests"),
    )
    answer = model_node("answer", chat=chat, stream=stream)
    graph = Graph(Notes, name="answering").flow(START >> answer, answer >> END)

    await instrument(graph, tracer_provider=provider).ainvoke(
        question("How long do refunds take?")
    )

    model = spans.one(f"chat {MODEL}")
    assert parent_id(model) == span_id(spans.one("nodestep.node answer"))
    assert parent_id(spans.one("provider request")) == span_id(model)
    assert attributes(model)["gen_ai.response.finish_reasons"] == ("stop",)
    assert attributes(model)["gen_ai.usage.input_tokens"] == 12
    assert attributes(model)["gen_ai.usage.output_tokens"] == 5
    assert attribute_json(model, "gen_ai.output.messages")[0]["parts"] == [
        {"type": "text", "content": "Refunds take five days."}
    ]


async def test_a_failing_model_call_ends_its_chat_span_with_the_error(
    provider, spans: FinishedSpans
):
    answer = model_node("answer", chat=LunaChat([]))
    graph = Graph(Notes, name="answering").flow(START >> answer, answer >> END)

    with pytest.raises(ModelProviderError) as raised:
        await instrument(graph, tracer_provider=provider).ainvoke(question("Hello?"))

    model = spans.one(f"chat {MODEL}")
    assert attributes(model)["gen_ai.request.model"] == MODEL
    assert attributes(model)["gen_ai.provider.name"] == "openai"
    assert "gen_ai.response.model" not in attributes(model)
    answering = spans.one("nodestep.node answer")
    assert parent_id(model) == span_id(answering)
    for span in (model, answering, spans.one("nodestep.graph answering")):
        assert span.status.status_code == StatusCode.ERROR
        assert span.status.description == f"ModelProviderError: {raised.value}"
        (event,) = [item for item in span.events if item.name == "exception"]
        assert attributes(event)["exception.type"] == (
            "nodestep.exceptions.ModelProviderError"
        )
    assert attribute_json(model, "gen_ai.input.messages")[-1]["parts"] == [
        {"type": "text", "content": "Hello?"}
    ]
    assert trace.get_current_span() is INVALID_SPAN


async def test_a_spawned_sub_agent_nests_under_the_node_that_spawned_it(
    provider, spans: FinishedSpans
):
    child = instrument(
        Graph(Counter, name="child").flow(START >> bump, bump >> END),
        tracer_provider=provider,
    )

    @node
    async def fan_out(state: Counter, ctx: NodeContext) -> dict:
        handles = await ctx.spawn([AgentTask(graph=child, input={"count": 1})])
        (result,) = await ctx.gather(handles)
        return {"count": result.data["count"]}

    parent = instrument(
        Graph(Counter, name="parent").flow(START >> fan_out, fan_out >> END),
        tracer_provider=provider,
    )

    result = await parent.ainvoke({"count": 0})

    assert result.state.count == 2
    inner = spans.one("nodestep.graph child")
    assert parent_id(inner) == span_id(spans.one("nodestep.node fan_out"))
    assert parent_id(spans.one("nodestep.node bump")) == span_id(inner)
    assert attributes(inner)["nodestep.status"] == "completed"
    assert len({trace_id(span) for span in spans.all}) == 1


async def test_concurrent_runs_of_one_graph_are_separate_traces(
    provider, spans: FinishedSpans
):
    graph = Graph(Counter, name="steps").flow(
        START >> slow_bump, slow_bump >> double, double >> END
    )
    traced = instrument(graph, tracer_provider=provider)

    first, second = await asyncio.gather(
        traced.ainvoke({"count": 1}), traced.ainvoke({"count": 5})
    )

    assert (first.state.count, second.state.count) == (4, 12)
    runs = spans.named("nodestep.graph steps")
    assert len(runs) == 2
    assert len({trace_id(run) for run in runs}) == 2
    for run in runs:
        children = [span for span in spans.all if parent_id(span) == span_id(run)]
        assert sorted(span.name for span in children) == [
            "nodestep.node double",
            "nodestep.node slow_bump",
        ]
        assert {trace_id(span) for span in children} == {trace_id(run)}
        assert attributes(run)["nodestep.status"] == "completed"
    assert len(spans.all) == 6


async def test_resume_with_answers_records_the_answers(provider, spans: FinishedSpans):
    graph = Graph(Pair, name="pair", state_store=InMemoryStateStore()).flow(
        START >> split, ask_left >> END, ask_right >> END
    )
    traced = instrument(graph, tracer_provider=provider)

    paused = await traced.ainvoke({}, thread_id="pair-1")
    answers = dict.fromkeys(paused.interrupts, True)
    done = await traced.ainvoke(
        None, thread_id="pair-1", resume=Resume(answers=answers)
    )

    assert (done.state.left, done.state.right) == (True, True)
    first, second = spans.named("nodestep.graph pair")
    events = [item for item in first.events if item.name == "nodestep.interrupt"]
    assert {attributes(event)["nodestep.interrupt.id"] for event in events} == {
        "left",
        "right",
    }
    assert json.loads(attributes(second)["nodestep.resume"]) == answers
    assert attributes(second)["nodestep.status"] == "completed"


async def test_instrumenting_twice_traces_each_run_once(provider, spans: FinishedSpans):
    graph = Graph(Counter, name="steps").flow(START >> bump, bump >> END)

    traced = instrument(
        instrument(graph, tracer_provider=provider), tracer_provider=provider
    )
    await traced.ainvoke({"count": 1})

    assert [type(item) for item in traced.middleware] == [TracingMiddleware]
    run = spans.one("nodestep.graph steps")
    assert parent_id(spans.one("nodestep.node bump")) == span_id(run)
    assert len(spans.all) == 2


async def test_an_error_after_the_tracing_hook_marks_the_run_failed(
    provider, spans: FinishedSpans
):
    graph = Graph(Counter, name="audited", middleware=[FailingAfterGraph()]).flow(
        START >> bump, bump >> END
    )

    with pytest.raises(RuntimeError, match="audit log is full"):
        await instrument(graph, tracer_provider=provider).ainvoke({"count": 1})

    run = spans.one("nodestep.graph audited")
    assert attributes(run)["nodestep.status"] == "failed"
    assert run.status.status_code == StatusCode.ERROR
    assert run.status.description == "RuntimeError: audit log is full"
    assert spans.one("nodestep.node bump").status.status_code == StatusCode.UNSET


async def test_a_node_that_returns_an_invalid_value_fails_its_span_and_run(
    provider, spans: FinishedSpans
):
    @node
    def broken(state: Counter):
        return 42

    graph = Graph(Counter, name="broken").flow(START >> broken, broken >> END)

    with pytest.raises(TypeError, match="returned int") as raised:
        await instrument(graph, tracer_provider=provider).ainvoke({"count": 1})

    run = spans.one("nodestep.graph broken")
    failed = spans.one("nodestep.node broken")
    assert failed.status.status_code == StatusCode.ERROR
    assert failed.status.description == "the run failed before this node finished"
    assert run.status.description == f"TypeError: {raised.value}"


async def test_long_message_histories_keep_their_latest_messages(
    provider, spans: FinishedSpans
):
    history: list[HumanMessage | AIMessage] = []
    for number in range(40):
        history += [
            HumanMessage(content=f"question {number} " + "x" * 500),
            AIMessage(content=f"answer {number}"),
        ]
    history.append(HumanMessage(content="latest question"))
    answer = model_node("answer", chat=ScriptedChat(["Done."]))
    graph = Graph(Notes, name="long").flow(START >> answer, answer >> END)

    await instrument(graph, tracer_provider=provider).ainvoke({"messages": history})

    model = spans.one("chat scripted")
    assert attributes(model)["gen_ai.request.model"] == "scripted"
    sent = attribute_json(model, "gen_ai.input.messages")
    assert 1 < len(sent) < len(history)
    assert sent[-1]["parts"] == [{"type": "text", "content": "latest question"}]
    assert len(attributes(model)["gen_ai.input.messages"].encode()) <= (
        MAX_ATTRIBUTE_BYTES
    )
    assert attributes(model)["nodestep.truncated"] == ("gen_ai.input.messages",)
    assert "gen_ai.output.messages" in attributes(model)
    answering = spans.one("nodestep.node answer")
    state = attribute_json(answering, "nodestep.state.before")
    assert state["messages"][-1]["content"] == "latest question"
    assert state["topic"] == ""
    assert attributes(answering)["nodestep.truncated"] == ("nodestep.state.before",)
    run = spans.one("nodestep.graph long")
    assert attribute_json(run, "nodestep.state.after")["final_text"] == "Done."
    assert attribute_json(run, "nodestep.input")["messages"][-1]["content"] == (
        "latest question"
    )


def test_without_a_provider_the_global_provider_is_used(
    provider, spans: FinishedSpans, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(trace, "_TRACER_PROVIDER", provider)
    graph = Graph(Counter, name="steps").flow(START >> bump, bump >> END)

    instrument(graph).invoke({"count": 1})

    assert [span.name for span in spans.all] == [
        "nodestep.node bump",
        "nodestep.graph steps",
    ]


async def test_the_model_span_records_the_requested_and_the_responding_model(
    provider, spans: FinishedSpans
):
    answer = model_node(
        "answer",
        chat=LunaChat([ChatResponse(content="Five days.", model=f"{MODEL}-2026-09")]),
    )
    graph = Graph(Notes, name="answering").flow(START >> answer, answer >> END)

    await instrument(graph, tracer_provider=provider).ainvoke(question("Refunds?"))

    model = spans.one(f"chat {MODEL}-2026-09")
    assert attributes(model)["gen_ai.request.model"] == MODEL
    assert attributes(model)["gen_ai.response.model"] == f"{MODEL}-2026-09"
    assert "gen_ai.response.finish_reasons" not in attributes(model)


async def test_the_provider_name_can_be_given(provider, spans: FinishedSpans):
    graph = instrument(linear_graph(), tracer_provider=provider, provider_name="azure")

    await graph.ainvoke(question("How long do refunds take?"), thread_id="thread-1")

    assert attributes(spans.one(f"chat {MODEL}"))["gen_ai.provider.name"] == "azure"


async def test_a_tool_error_that_fails_the_run_is_recorded_once_on_the_tool(
    provider, spans: FinishedSpans
):
    chat = LunaChat([tool_call("check_warehouse", "call_7")])
    agent = build_react_agent(chat=chat, tools=[check_warehouse], tool_errors="raise")

    with pytest.raises(ConnectionError, match="warehouse offline"):
        await run_agent(
            instrument(agent, tracer_provider=provider), "Is A-1001 in stock?"
        )

    execution = spans.one("execute_tool check_warehouse")
    exceptions = [item for item in execution.events if item.name == "exception"]
    assert len(exceptions) == 1
    assert execution.status.description == "ConnectionError: warehouse offline"
    assert parent_id(execution) == span_id(spans.one("nodestep.node act"))
    run = spans.one("nodestep.graph agent")
    assert attributes(run)["nodestep.status"] == "failed"
    assert run.status.description == "ConnectionError: warehouse offline"


async def test_the_middleware_alone_ends_runs_that_pause_or_fail(
    provider, spans: FinishedSpans
):
    tracing = TracingMiddleware(provider)
    paused = Graph(
        Refund, name="refund", state_store=InMemoryStateStore(), middleware=[tracing]
    ).flow(START >> review, review >> END)
    failing = Graph(Counter, name="stock", middleware=[tracing]).flow(
        START >> explode, explode >> END
    )

    await paused.ainvoke({"amount": 80}, thread_id="refund-1")
    with pytest.raises(ValueError, match="stock file is missing"):
        await failing.ainvoke({"count": 1})

    refund = spans.one("nodestep.graph refund")
    assert attributes(refund)["nodestep.status"] == "paused"
    (event,) = [item for item in refund.events if item.name == "nodestep.interrupt"]
    assert attributes(event)["nodestep.interrupt.id"] == "approve_refund"
    assert attribute_json(refund, "nodestep.state.after")["amount"] == 80
    stock = spans.one("nodestep.graph stock")
    assert attributes(stock)["nodestep.status"] == "failed"
    assert stock.status.description == "ValueError: stock file is missing"
    assert "nodestep.input" not in attributes(stock)
    assert {span.name for span in spans.all} == {
        "nodestep.graph refund",
        "nodestep.node review",
        "nodestep.graph stock",
        "nodestep.node explode",
    }


async def test_a_cancelled_task_ends_the_run_and_its_open_node(
    provider, spans: FinishedSpans
):
    started = asyncio.Event()

    @node
    async def wait_forever(state: Counter) -> dict:
        started.set()
        await asyncio.Event().wait()
        return {}

    graph = Graph(Counter, name="waiting").flow(
        START >> bump, bump >> wait_forever, wait_forever >> END
    )
    running = asyncio.create_task(
        instrument(graph, tracer_provider=provider).ainvoke({"count": 1})
    )
    await started.wait()
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running

    run = spans.one("nodestep.graph waiting")
    waiting = spans.one("nodestep.node wait_forever")
    assert attributes(run)["nodestep.status"] == "cancelled"
    assert attribute_json(run, "nodestep.state.after") == {"count": 2}
    for span in (run, waiting):
        assert span.status.status_code == StatusCode.UNSET
        assert [item.name for item in span.events] == ["nodestep.cancelled"]
    assert spans.one("nodestep.node bump").events == ()


async def test_a_graph_timeout_fails_the_run(provider, spans: FinishedSpans):
    @node
    async def stall(state: Counter) -> dict:
        await asyncio.sleep(10)
        return {}

    graph = Graph(Counter, name="slow", timeout=0.05).flow(START >> stall, stall >> END)

    with pytest.raises(GraphTimeoutError):
        await instrument(graph, tracer_provider=provider).ainvoke({"count": 1})

    run = spans.one("nodestep.graph slow")
    assert attributes(run)["nodestep.status"] == "failed"
    assert run.status.status_code == StatusCode.ERROR
    (event,) = [item for item in run.events if item.name == "exception"]
    assert attributes(event)["exception.type"] == (
        "nodestep.exceptions.GraphTimeoutError"
    )
    stalled = spans.one("nodestep.node stall")
    assert stalled.status.description == "the run failed before this node finished"


@node(name="check-stock")
def check_stock_dashed(state: Counter) -> dict:
    return {"count": state.count + 1}


@node(name="check_stock")
def check_stock_underscored(state: Counter) -> dict:
    return {"count": state.count + 10}


@node(name="end")
def end_node(state: Counter) -> dict:
    return {"count": state.count * 2}


def colliding_graph() -> Graph:
    return Graph(Counter, name="stock").flow(
        START >> check_stock_dashed,
        check_stock_dashed >> check_stock_underscored,
        check_stock_underscored >> end_node,
        end_node >> END,
    )


def declared_ids(source: str) -> dict[str, str]:
    return {
        label: node_id
        for node_id, label in re.findall(r'^  (\w+)\["([^"]*)"\]$', source, re.M)
    }


async def test_run_span_carries_the_mermaid_source_of_the_graph(
    provider, spans: FinishedSpans
):
    graph = linear_graph()
    traced = instrument(graph, tracer_provider=provider)

    await traced.ainvoke(question("How long do refunds take?"), thread_id="thread-1")

    run = spans.one("nodestep.graph linear")
    assert attributes(run)["nodestep.graph.mermaid"] == graph.to_mermaid()


async def test_node_spans_carry_the_mermaid_id_drawn_for_them(
    provider, spans: FinishedSpans
):
    traced = instrument(colliding_graph(), tracer_provider=provider)

    await traced.ainvoke({"count": 1})

    drawn = declared_ids(
        attributes(spans.one("nodestep.graph stock"))["nodestep.graph.mermaid"]
    )
    recorded = {
        attributes(span)["nodestep.node.name"]: attributes(span)[
            "nodestep.node.mermaid_id"
        ]
        for span in spans.all
        if span.name.startswith("nodestep.node ")
    }
    assert recorded == drawn
    assert recorded == {
        "check-stock": "check_stock",
        "check_stock": "check_stock_2",
        "end": "end_2",
    }


async def test_each_instrumented_graph_records_its_own_mermaid_source(
    provider, spans: FinishedSpans
):
    child_graph = Graph(Counter, name="child").flow(START >> bump, bump >> END)
    child = instrument(child_graph, tracer_provider=provider)

    @node
    async def delegate(state: Counter) -> dict:
        result = await child.ainvoke({"count": state.count})
        return {"count": result.state.count}

    parent_graph = Graph(Counter, name="parent").flow(
        START >> delegate, delegate >> double, double >> END
    )
    parent = instrument(parent_graph, tracer_provider=provider)

    await parent.ainvoke({"count": 2})

    outer = attributes(spans.one("nodestep.graph parent"))
    inner = attributes(spans.one("nodestep.graph child"))
    assert outer["nodestep.graph.mermaid"] == parent_graph.to_mermaid()
    assert inner["nodestep.graph.mermaid"] == child_graph.to_mermaid()
    assert (
        attributes(spans.one("nodestep.node bump"))["nodestep.node.mermaid_id"]
        == "bump"
    )
    assert (
        attributes(spans.one("nodestep.node delegate"))["nodestep.node.mermaid_id"]
        == "delegate"
    )


async def test_model_and_tool_spans_name_the_graph_and_diagram_id_of_their_node(
    provider, spans: FinishedSpans
):
    chat = LunaChat(
        [
            tool_call("get_order_status", "call_1"),
            ChatResponse(content="Order A-1001 has shipped.", model=MODEL),
        ]
    )
    agent = build_react_agent(chat=chat, tools=[get_order_status])

    await run_agent(instrument(agent, tracer_provider=provider), "Where is A-1001?")

    calls = [
        *spans.named(f"chat {MODEL}"),
        *spans.named("execute_tool get_order_status"),
    ]
    assert [
        (
            attributes(span)["nodestep.graph.name"],
            attributes(span)["nodestep.node.mermaid_id"],
        )
        for span in calls
    ] == [("agent", "think"), ("agent", "think"), ("agent", "act")]
    assert not any("nodestep.node.name" in attributes(span) for span in calls)


async def test_the_middleware_alone_records_no_mermaid_source(
    provider, spans: FinishedSpans
):
    graph = Graph(Counter, name="plain", middleware=[TracingMiddleware(provider)]).flow(
        START >> bump, bump >> END
    )

    await graph.ainvoke({"count": 1})

    assert "nodestep.graph.mermaid" not in attributes(spans.one("nodestep.graph plain"))
    assert "nodestep.node.mermaid_id" not in attributes(spans.one("nodestep.node bump"))


async def test_the_middleware_alone_names_the_graph_of_a_call_but_no_diagram_id(
    provider, spans: FinishedSpans
):
    chat = LunaChat(
        [
            tool_call("get_order_status", "call_1"),
            ChatResponse(content="Order A-1001 has shipped.", model=MODEL),
        ]
    )
    agent = build_react_agent(
        chat=chat,
        tools=[get_order_status],
        middleware=[TracingMiddleware(provider)],
    )

    await run_agent(agent, "Where is A-1001?")

    (execution,) = spans.named("execute_tool get_order_status")
    assert attributes(execution)["nodestep.graph.name"] == "agent"
    assert "nodestep.node.mermaid_id" not in attributes(execution)
