# Tracing

`instrument()` returns a copy of a graph that records each run as one trace: a span for the run, each node, each model call and each tool call. `configure()` makes the provider that sends the spans to `nodeartifact serve`.

## Trace an agent

This agent calls one tool, then answers. Start `nodeartifact serve` first.

```python
import asyncio

from nodestep import ScriptedChat, build_react_agent, run_agent, tool
from nodestep.chat import ChatResponse, ToolCall

import nodeartifact


@tool
def get_order_status(order_id: str) -> str:
    """Look up the shipping status of an order."""
    return "shipped" if order_id == "A-1001" else "unknown"


chat = ScriptedChat(
    [
        ChatResponse(
            tool_calls=[
                ToolCall(
                    id="call_1",
                    name="get_order_status",
                    arguments={"order_id": "A-1001"},
                )
            ],
            model="gpt-6-luna",
            usage={"input_tokens": 120, "output_tokens": 18},
        ),
        ChatResponse(
            content="Order A-1001 has shipped.",
            model="gpt-6-luna",
            usage={"input_tokens": 160, "output_tokens": 9},
        ),
    ]
)
agent = build_react_agent(chat=chat, tools=[get_order_status])

provider = nodeartifact.configure("http://127.0.0.1:4318", service_name="my-app")
traced = nodeartifact.instrument(agent, tracer_provider=provider)

print(asyncio.run(run_agent(traced, "Where is order A-1001?", thread_id="order-1")))
provider.shutdown()
```

```text
Order A-1001 has shipped.
```

The run is one trace with these spans:

```text
nodestep.graph agent
  nodestep.node think
    chat gpt-6-luna
  nodestep.node act
    execute_tool get_order_status
  nodestep.node think
    chat gpt-6-luna
```

The run span holds the input, the state before and after, and the status. A node span holds the state the node read and the update it returned. A model span holds the messages and the token usage, and a tool span the arguments and the result. The trace list shows 280 tokens in and 27 out, the sum of the two model calls. The [span reference](#span-reference) lists every attribute.

## Use a real model

Replace `ScriptedChat` with `OpenAIChat`, which reads `OPENAI_API_KEY`:

```diff
-chat = ScriptedChat([...])
+chat = OpenAIChat.from_env(model="gpt-6-luna")
```

`OpenAIChat` needs nodestep's `openai` extra:

```bash
uv add "nodestep[openai] @ git+https://github.com/nodestep-ai/nodestep"
```

Model spans record `gen_ai.provider.name` as `"openai"`. nodestep does not tell middleware which provider a chat model uses, so for another provider pass `instrument(graph, tracer_provider=provider, provider_name="...")`.

## Configure the provider

`configure()` returns a `TracerProvider` with one `BatchSpanProcessor` that sends spans over OTLP/HTTP to `{endpoint}/v1/traces`.

| Argument | Default | Meaning |
|---|---|---|
| `endpoint` | `"http://127.0.0.1:4318"` | Base URL of the receiver. A trailing slash is ignored |
| `service_name` | `"nodestep"` | The `service.name` resource attribute of every span |
| `headers` | `None` | HTTP headers sent with every export, for example for authentication |
| `schedule_delay_millis` | `None` | Time between batch exports in milliseconds. `None` keeps the SDK default: 5000, or `OTEL_BSP_SCHEDULE_DELAY`. Use `500` to watch runs live |

- `configure()` does not set the global OpenTelemetry provider. Pass the provider to `instrument()`, or to `opentelemetry.trace.set_tracer_provider` yourself.
- Call `provider.shutdown()` before the process exits, or the last batch is lost.
- nodeartifact reads no environment variables. The OpenTelemetry SDK reads its own `OTEL_*` variables for what `configure()` does not set, such as the sampler, the batch sizes and the export timeout, and `OTEL_EXPORTER_OTLP_HEADERS` when `headers` is `None`. For full control, build the `TracerProvider` yourself.

## Sub-agents and your own spans

While a node, a model call or a tool call runs, its span is the current OpenTelemetry span. Spans your code starts there nest under it, such as a SQL span in a tool or an HTTP span in a model call.

Child graphs, sub-agents and background agents nest the same way when you instrument their graphs too. A sub-agent called from a tool nests under that tool's span.

## Paused, stopped and failed runs

- A run that pauses at `interrupt()` ends with `nodestep.status` set to `paused`, and gets one `nodestep.interrupt` event per pending interrupt, on the run span and on the node that paused. The resume is a new trace with the same `nodestep.thread_id` and `nodestep.resuming` set to `true`.
- A stopped run ends with `nodestep.status` set to `cancelled`, and gets a `nodestep.cancelled` event on the run span and on the nodes that were still running.
- A failing node and its run, a model call that raises and a tool that raises get the error status and an `exception` event. [Rules and defaults](#rules-and-defaults) has the edge cases.

## Send traces from other code

The server takes traces from any OpenTelemetry SDK or collector that exports OTLP over HTTP. With the OpenTelemetry Python SDK:

```python
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

provider = TracerProvider(resource=Resource({"service.name": "my-app"}))
exporter = OTLPSpanExporter(endpoint="http://127.0.0.1:4318/v1/traces")
provider.add_span_processor(BatchSpanProcessor(exporter))
tracer = provider.get_tracer("my-app")

with tracer.start_as_current_span("checkout"):
    with tracer.start_as_current_span("chat gpt-6-luna") as span:
        span.set_attribute("gen_ai.operation.name", "chat")
        span.set_attribute("gen_ai.usage.input_tokens", 120)
        span.set_attribute("gen_ai.usage.output_tokens", 40)

provider.shutdown()
```

The trace list adds up `gen_ai.usage.input_tokens` and `gen_ai.usage.output_tokens` of every span, so this trace shows 120 tokens in and 40 out. [What the trace list reads](ui.md#what-the-trace-list-reads) has the other attributes the UI uses.

- The server accepts `application/x-protobuf` and `application/json`, plain or gzip.
- It takes traces over HTTP only: no gRPC, metrics or logs.
- Spans with an invalid trace id, span id, parent span id or timestamp are rejected and counted in `partial_success`.

## Span reference

| Span | Parent | Attributes |
|---|---|---|
| `nodestep.graph {graph}` | the span that is current when the run starts | `nodestep.graph.name`, `nodestep.thread_id`, `nodestep.run_id`, `nodestep.root_run_id`, `nodestep.branch_id`, `nodestep.resuming`, `nodestep.input` (the run input), `nodestep.resume` (the resume answers), `nodestep.state.before`, `nodestep.state.after`, `nodestep.status` (`completed`, `paused`, `failed` or `cancelled`), `nodestep.graph.mermaid` (the graph's `Graph.to_mermaid()` source) |
| `nodestep.node {node}` | the run span | `nodestep.graph.name`, `nodestep.node.name`, `nodestep.step`, `nodestep.task_id`, `nodestep.thread_id`, `nodestep.state.before` (the node input), `nodestep.update` (what the node returned), `nodestep.node.mermaid_id` (the node's id in `nodestep.graph.mermaid`) |
| `chat {model}` | the node span | `gen_ai.operation.name`, `gen_ai.request.model`, `gen_ai.provider.name`, `gen_ai.response.model`, `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`, `gen_ai.response.finish_reasons`, `gen_ai.input.messages`, `gen_ai.output.messages`, and `nodestep.graph.name` and `nodestep.node.mermaid_id` of the node |
| `execute_tool {tool}` | the node span | `gen_ai.operation.name`, `gen_ai.tool.name`, `gen_ai.tool.call.id`, `gen_ai.tool.type`, `gen_ai.tool.call.arguments`, `gen_ai.tool.call.result`, and `nodestep.graph.name` and `nodestep.node.mermaid_id` of the node |

The `nodestep.interrupt` event has `nodestep.interrupt.key`, `.id`, `.node`, `.task_id` and `.payload`. Any span may also have `nodestep.truncated`, the list of its shortened attributes.

## Rules and defaults

| Rule or setting | What happens |
|---|---|
| `instrument(graph, *, tracer_provider=None, provider_name="openai")` | Returns an `InstrumentedGraph`, a copy that shares the nodes, flow, state store and workspace and adds a `TracingMiddleware` after the graph's own [middleware](https://nodestep-ai.github.io/nodestep/concepts/middleware/). It adds no tools or routes, and the graph passed in is not changed |
| `tracer_provider=None` | Uses the global provider, which records nothing until one is set |
| Instrumenting an instrumented graph | Replaces its `TracingMiddleware`, so a run is traced once |
| `TracingMiddleware` added by hand | Records everything except `nodestep.input`, `nodestep.resume`, `nodestep.graph.mermaid` and `nodestep.node.mermaid_id`: the run input, the resume answers and the diagram the graph view needs. Use `instrument()` |
| Model span name | `chat` and the model the response names, or else the requested one. `gen_ai.request.model` is the model the chat object names |
| Node ids | `nodestep.node.mermaid_id` comes from `nodestep.core.render.mermaid_id`, with the `_2`, `_3` suffix that `to_mermaid()` gives an id already taken, so it matches the drawn diagram |
| Edge ids | `<source id>-<target id>`, with `-2`, `-3` for a pair drawn again |
| Model and tool spans | Carry the node id of the call, so the viewer knows which node is running before the node span arrives |
| Large values | Stored as JSON through nodestep's `to_json_value`, which masks secrets. A value over 16 KiB drops its oldest list items first, so a long conversation keeps its latest messages, and is cut only when that is not enough |
| Other middleware, inputs | Recorded after the other `before_*` hooks |
| Other middleware, outputs | `nodestep.update` and `gen_ai.tool.call.result` are recorded before the other `after_node` and `after_tool` hooks. If one of them replaces the update or the result, the new value is stored or sent to the model, and the span keeps the original |
| Tool error returned to the model | Still recorded on the tool span. With `tool_errors="return"` the next `chat` span shows it in `gen_ai.input.messages` |
| Model or tool call still open when its node ends | Error status "the call raised an error before it returned a result" |
| Node returns a value of the wrong type | The run fails without `after_node` or `on_error`. The node span gets the error status "the run failed before this node finished", and the error is on the run span |
| Route to an undeclared node, or an update that fails validation | The run fails after `after_node`, so the node span ends without an error |
