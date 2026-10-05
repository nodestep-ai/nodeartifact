# Web UI

`nodeartifact serve` shows the traces at http://127.0.0.1:4318/. Click a run in the trace list to replay it on its graph, read its timeline or open a span.

## Find a run

The trace list shows the 100 traces that started last and updates while new spans arrive. The search field in the top bar matches the run input, root span name, trace id, thread, service and status, ignoring case.

| Search | Finds |
|---|---|
| `refund failed` | Traces that match every word, here the failed runs about refunds |
| `"sales by store"` | Traces with the phrase |
| `th-c04e` | The runs of a thread, or a trace by its id |

The search is the `q` query parameter, as in `/?q=th-c04e`, so a link keeps it. It reads at most the 5,000 traces that started last, and says so when it left older ones out.

## Replay a run

A trace recorded with `instrument()` opens on the graph view: the graph's diagram with the nodes the run visited numbered in order, and the steps beside it. Replay plays the steps on the diagram, and First, Previous, Next and Last move through them by hand. Each step shows its update and the state before and after it.

A trace without a traced graph, such as one sent by other OpenTelemetry code, opens on the timeline instead.

The diagram is drawn with Mermaid 12.0.0 from jsdelivr, pinned with an integrity hash. Without access to that site there is no diagram, and the steps still work.

## Follow a live run

Open a run while it is going. The graph view shows the steps whose spans have arrived, highlights the node the run is in when the spans tell which one it is, and reloads on the same step when the run ends.

- Spans arrive when the exporter sends a batch. Pass `schedule_delay_millis=500` to `configure()` to follow a run closely.
- A run sends its own diagram only when it ends, so until then the page uses the diagram of the latest earlier run of the same graph and service.
- After ten minutes without new spans the page stops following the run. Reload it to check again.

## Read the state and the messages

Step values and the JSON attributes of a span open as a tree you can fold and search. Enter and Shift+Enter go from match to match.

A list of chat messages, in nodestep's shape or the GenAI one, shows one row per message: the role, the text, one line per tool call such as `run_sql(sql: "SELECT …")`, and for a tool result the tool name and the start of the result. Click a row for the whole message.

## Timeline and spans

The Timeline tab shows every span of the trace, indented under its parent, with a bar for its duration. A nested run, such as a sub-agent, shows its input after its name. Spans whose parent has not arrived are shown at the top level.

Click a span for its attributes, events and resource. The `gen_ai.*` and `nodestep.*` attributes are grouped, and those listed in `nodestep.truncated` are marked as shortened.

## What the trace list reads

These are the attributes behind the columns, for traces sent from other code.

| Column | Comes from |
|---|---|
| Input | `nodestep.input` from the root span, or else from the earliest span that has it; for chat messages, the text of the last user message. Without `nodestep.input`, the last user message in `gen_ai.input.messages`, then in `nodestep.state.before`, else the root span name. The list cuts it at 80 characters and shows the full input on hover |
| Thread | `nodestep.thread_id`, from the root span or else the earliest span that has it |
| Status | `nodestep.status` on the root span: Completed, Paused, Stopped (`cancelled`) or Failed. A completed run whose failed tool call went back to the model stays Completed. A trace with node spans and no root span yet is Running. Other traces are Failed when any span has the error status, else they take the status of the root span |
| Tokens | The sum of the integer `gen_ai.usage.input_tokens` and `gen_ai.usage.output_tokens` attributes of all spans |

## Run labels

Labels next to a run are read from its root span.

| Label | When |
|---|---|
| resumed | `nodestep.resuming` is set. It shows the answer from `nodestep.resume`: the answer alone for one interrupt, otherwise `key: answer` pairs |
| edit of | A run with new input that replaces the question of a run on another branch: its `nodestep.state.before` ends, before that input, with the same message (by message id) as that run's |
| after a stop | A run with new input whose previous run in the thread was stopped, unless it replaces the turn of another run |
| root span missing | The root span has not arrived, and the trace is not a run in progress |

Runs without message ids, or whose state lost its oldest messages to shortening, are not compared, so they get neither "edit of" nor "after a stop".

## Rules and defaults

| Rule | What happens |
|---|---|
| Steps | The node spans directly under the run span, in start order, matched to the diagram by `nodestep.node.mermaid_id`. Edges are matched by their ids, or by Mermaid's default `L_` ids in diagrams stored before edges had ids |
| Child graphs | With child graphs in the same trace, the graph view shows the outer run. Its run span does not have to be the root span of the trace |
| Paused step | Its node span has a `nodestep.interrupt` event. When only the run span has the event, the last node span of the task it names is paused |
| Stopped step | The node where a stopped run ended |
| Tool error | A step whose node made a failed tool call. The step names the failed tools |
