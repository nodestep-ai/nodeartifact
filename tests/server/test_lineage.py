import json

from opentelemetry.proto.trace.v1.trace_pb2 import Status

from nodeartifact.server.models import TraceSummary
from payloads import ROOT_ID, START_NS, records, span

SECOND = 1_000_000_000
FIRST_QUESTION = "Which category brings in the most revenue?"
SECOND_QUESTION = "What is the revenue per month?"
THIRD_QUESTION = "Which store has the most returns?"


def human(message_id: str, text: str) -> dict:
    return {"id": message_id, "content": text, "type": "human"}


def ai(message_id: str, text: str) -> dict:
    return {"id": message_id, "content": text, "type": "ai"}


def trace_id_of(number: int) -> str:
    return f"{number:032x}"


def add_run(
    store,
    number: int,
    *,
    before: list[dict] | None = None,
    question: dict | None = None,
    branch: str = "main",
    status: str = "completed",
    resuming: bool = False,
    thread: str = "th-1",
    truncated: bool = False,
) -> str:
    trace_id = trace_id_of(number)
    messages = [*(before or []), *([question] if question else [])]
    attributes: dict = {
        "nodestep.thread_id": thread,
        "nodestep.branch_id": branch,
        "nodestep.status": status,
        "nodestep.resuming": resuming,
        "nodestep.state.before": json.dumps({"messages": messages}),
    }
    if question is not None:
        attributes["nodestep.input"] = json.dumps({"messages": [question]})
    if truncated:
        attributes["nodestep.truncated"] = ["nodestep.state.before"]
    start = START_NS + number * SECOND
    store.add(
        records(
            span(
                trace_id,
                ROOT_ID,
                "nodestep.graph text-to-sql-demo",
                start=start,
                end=start + SECOND // 2,
                attributes=attributes,
                status=Status.STATUS_CODE_UNSET,
            )
        )
    )
    return trace_id


def summaries(store) -> dict[str, TraceSummary]:
    return {summary.trace_id: summary for summary in store.list_traces()}


def labels(summary: TraceSummary) -> tuple[str | None, bool]:
    return summary.edit_of, summary.after_stop


def test_a_run_that_replaces_the_first_turn_is_an_edit_of_its_question(store):
    original = add_run(store, 1, question=human("h1", FIRST_QUESTION))
    edit = add_run(store, 2, question=human("h2", SECOND_QUESTION), branch="b-1")

    listed = summaries(store)

    assert labels(listed[edit]) == (FIRST_QUESTION, False)
    assert labels(listed[original]) == (None, False)


def test_an_edit_of_a_later_turn_names_the_question_it_replaced(store):
    add_run(store, 1, question=human("h1", FIRST_QUESTION))
    before = [human("h1", FIRST_QUESTION), ai("a1", "Coffee.")]
    add_run(store, 2, before=before, question=human("h2", SECOND_QUESTION))
    edit = add_run(
        store, 3, before=before, question=human("h3", THIRD_QUESTION), branch="b-1"
    )

    assert labels(summaries(store)[edit]) == (SECOND_QUESTION, False)


def test_editing_an_edited_turn_names_the_version_it_replaced(store):
    add_run(store, 1, question=human("h1", FIRST_QUESTION))
    add_run(store, 2, question=human("h2", SECOND_QUESTION), branch="b-1")
    again = add_run(store, 3, question=human("h3", THIRD_QUESTION), branch="b-2")

    assert labels(summaries(store)[again]) == (SECOND_QUESTION, False)


def test_a_new_message_after_a_stop_is_labelled_after_a_stop(store):
    add_run(store, 1, question=human("h1", FIRST_QUESTION))
    before = [human("h1", FIRST_QUESTION), ai("a1", "Coffee.")]
    stopped = add_run(
        store,
        2,
        before=before,
        question=human("h2", SECOND_QUESTION),
        status="cancelled",
    )
    after = add_run(
        store,
        3,
        before=[*before, human("h2", SECOND_QUESTION), ai("a2", "")],
        question=human("h3", THIRD_QUESTION),
        branch="b-1",
    )

    listed = summaries(store)

    assert labels(listed[after]) == (None, True)
    assert labels(listed[stopped]) == (None, False)


def test_a_message_after_a_run_stopped_before_its_first_step_is_after_a_stop(store):
    add_run(store, 1, question=human("h1", FIRST_QUESTION))
    before = [human("h1", FIRST_QUESTION), ai("a1", "Coffee.")]
    add_run(
        store,
        2,
        before=before,
        question=human("h2", SECOND_QUESTION),
        status="cancelled",
    )
    after = add_run(
        store, 3, before=before, question=human("h3", THIRD_QUESTION), branch="b-1"
    )

    assert labels(summaries(store)[after]) == (None, True)


def test_an_edit_of_an_earlier_turn_after_a_stop_is_an_edit(store):
    add_run(store, 1, question=human("h1", FIRST_QUESTION))
    before = [human("h1", FIRST_QUESTION), ai("a1", "Coffee.")]
    add_run(
        store,
        2,
        before=before,
        question=human("h2", SECOND_QUESTION),
        status="cancelled",
    )
    edit = add_run(store, 3, question=human("h3", THIRD_QUESTION), branch="b-1")

    assert labels(summaries(store)[edit]) == (FIRST_QUESTION, False)


def test_resumed_runs_and_later_turns_on_a_branch_have_no_label(store):
    add_run(store, 1, question=human("h1", FIRST_QUESTION))
    add_run(
        store, 2, question=human("h2", SECOND_QUESTION), branch="b-1", status="paused"
    )
    resumed = add_run(
        store,
        3,
        before=[human("h2", SECOND_QUESTION), ai("a2", "")],
        branch="b-1",
        resuming=True,
    )
    later = add_run(
        store,
        4,
        before=[human("h2", SECOND_QUESTION), ai("a2", "Monthly.")],
        question=human("h4", THIRD_QUESTION),
        branch="b-1",
    )

    listed = summaries(store)

    assert labels(listed[resumed]) == (None, False)
    assert labels(listed[later]) == (None, False)


def test_runs_of_other_threads_are_not_compared(store):
    add_run(store, 1, question=human("h1", FIRST_QUESTION), thread="th-1")
    other = add_run(
        store, 2, question=human("h2", SECOND_QUESTION), branch="b-1", thread="th-2"
    )

    assert labels(summaries(store)[other]) == (None, False)


def test_a_shortened_state_without_earlier_messages_is_not_compared(store):
    add_run(store, 1, question=human("h1", FIRST_QUESTION))
    shortened = add_run(
        store,
        2,
        question=human("h2", SECOND_QUESTION),
        branch="b-1",
        truncated=True,
    )

    assert labels(summaries(store)[shortened]) == (None, False)


def test_an_input_without_message_ids_is_not_compared(store):
    add_run(store, 1, question={"content": FIRST_QUESTION, "type": "human"})
    other = add_run(
        store, 2, question={"content": SECOND_QUESTION, "type": "human"}, branch="b-1"
    )

    assert labels(summaries(store)[other]) == (None, False)


def test_labels_use_runs_older_than_the_listed_page(store):
    add_run(store, 1, question=human("h1", FIRST_QUESTION))
    edit = add_run(store, 2, question=human("h2", SECOND_QUESTION), branch="b-1")

    (summary,) = store.list_traces(1)

    assert summary.trace_id == edit
    assert labels(summary) == (FIRST_QUESTION, False)


def test_the_trace_view_has_the_same_labels(store):
    add_run(store, 1, question=human("h1", FIRST_QUESTION))
    edit = add_run(store, 2, question=human("h2", SECOND_QUESTION), branch="b-1")

    detail = store.get_trace(edit)

    assert detail is not None
    assert labels(detail.summary) == (FIRST_QUESTION, False)
