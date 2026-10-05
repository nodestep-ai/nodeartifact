import json
from collections import defaultdict
from collections.abc import Iterable, Sequence
from typing import Self

from nodeartifact.server.display import InputPreview
from nodeartifact.server.models import (
    NODESTEP_STATUSES,
    JsonValue,
    Record,
    RunStatus,
    TraceSummary,
)

STATE_BEFORE = "nodestep.state.before"
THREAD_START = ""


class RunStart(Record):
    """Where a run of a thread started, read from its root span.

    ``previous_message`` is the id of the last message in
    ``nodestep.state.before`` that is not part of the run input:
    ``THREAD_START`` when there is none, and ``None`` when it cannot be told,
    such as for a state without message ids or one whose oldest messages
    were dropped.
    """

    trace_id: str
    start_ns: int
    thread_id: str
    branch_id: str
    question: str | None
    input_ids: tuple[str, ...]
    previous_message: str | None
    stopped: bool
    resuming: bool

    @classmethod
    def from_row(cls, row: tuple) -> Self:
        """Read a run from a row of ``TraceStore``'s root run query.

        Parameters
        ----------
        row
            Trace id, start, thread id, branch id, ``nodestep.input``,
            ``nodestep.state.before``, ``nodestep.status``,
            ``nodestep.resuming`` and ``nodestep.truncated``, the last four as
            JSON text.

        Returns
        -------
        RunStart
        """
        (
            trace_id,
            start_ns,
            thread_id,
            branch_id,
            run_input,
            state,
            status,
            resuming,
            truncated,
        ) = row
        input_value = cls._parse(run_input)
        input_ids = tuple(cls._message_ids(cls._messages(input_value)))
        shortened = cls._parse(truncated)
        run_status = cls._parse(status)
        return cls(
            trace_id=trace_id,
            start_ns=start_ns,
            thread_id=thread_id,
            branch_id=branch_id if isinstance(branch_id, str) else "main",
            question=None if input_value is None else InputPreview.line(input_value),
            input_ids=input_ids,
            previous_message=cls._previous_message(
                cls._messages(cls._parse(state)),
                input_ids,
                shortened=isinstance(shortened, list) and STATE_BEFORE in shortened,
            ),
            stopped=isinstance(run_status, str)
            and NODESTEP_STATUSES.get(run_status) == RunStatus.STOPPED,
            resuming=cls._parse(resuming) is True,
        )

    @staticmethod
    def _parse(text: str | None) -> JsonValue:
        value: JsonValue = text
        while isinstance(value, str):
            try:
                value = json.loads(value)
            except (ValueError, RecursionError):
                return value
        return value

    @staticmethod
    def _messages(value: JsonValue) -> list[JsonValue] | None:
        match value:
            case {"messages": list() as messages}:
                return messages
            case list():
                return value
            case _:
                return None

    @staticmethod
    def _message_ids(messages: list[JsonValue] | None) -> Iterable[str]:
        for message in messages or []:
            message_id = message.get("id") if isinstance(message, dict) else None
            if isinstance(message_id, str):
                yield message_id

    @classmethod
    def _previous_message(
        cls,
        messages: list[JsonValue] | None,
        input_ids: tuple[str, ...],
        *,
        shortened: bool,
    ) -> str | None:
        if messages is None or not input_ids:
            return None
        earlier = [
            message
            for message in messages
            if not (isinstance(message, dict) and message.get("id") in input_ids)
        ]
        if not earlier:
            return None if shortened else THREAD_START
        last = earlier[-1]
        message_id = last.get("id") if isinstance(last, dict) else None
        return message_id if isinstance(message_id, str) else None


class RunLineage:
    """How the runs of a thread relate, for labelling edits and stops.

    A run with new input is labelled from the runs of its thread that started
    before it:

    - after a stop, when the run just before it was stopped, unless it
      replaces a turn of another run than that one;
    - an edit of the question of the latest run on another branch that
      started after the same message, so that its input replaces that run's
      input.

    Parameters
    ----------
    runs
        The root runs of the threads to label, in any order.
    """

    def __init__(self, runs: Iterable[RunStart]) -> None:
        self._threads: defaultdict[str, list[RunStart]] = defaultdict(list)
        self._positions: dict[str, int] = {}
        self._thread_of: dict[str, str] = {}
        for run in sorted(runs, key=lambda item: (item.start_ns, item.trace_id)):
            if run.trace_id in self._positions:
                continue
            thread = self._threads[run.thread_id]
            self._positions[run.trace_id] = len(thread)
            self._thread_of[run.trace_id] = run.thread_id
            thread.append(run)

    def label(self, summary: TraceSummary) -> TraceSummary:
        """Add the edit and stop labels to a trace summary.

        Parameters
        ----------
        summary
            The summary of a trace.

        Returns
        -------
        TraceSummary
            The summary with ``edit_of`` and ``after_stop`` set, or the
            summary itself when its trace has no run in ``runs``.
        """
        position = self._positions.get(summary.trace_id)
        if position is None:
            return summary
        thread = self._threads[self._thread_of[summary.trace_id]]
        run = thread[position]
        if run.resuming or run.question is None:
            return summary
        earlier = thread[:position]
        replaced = self._replaced(run, earlier)
        preceding = earlier[-1] if earlier else None
        if (
            preceding is not None
            and preceding.stopped
            and (replaced is None or replaced is preceding)
        ):
            return summary.model_copy(update={"after_stop": True})
        if replaced is not None:
            return summary.model_copy(update={"edit_of": replaced.question})
        return summary

    @staticmethod
    def _replaced(run: RunStart, earlier: Sequence[RunStart]) -> RunStart | None:
        if run.previous_message is None:
            return None
        return next(
            (
                other
                for other in reversed(earlier)
                if other.branch_id != run.branch_id
                and not other.resuming
                and other.question is not None
                and other.previous_message == run.previous_message
            ),
            None,
        )
