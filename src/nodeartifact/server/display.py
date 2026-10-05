import json
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import ClassVar, Self

from nodeartifact.server.models import (
    JsonValue,
    Record,
    RunStatus,
    SpanKind,
    SpanSummary,
    StatusCode,
    TraceDetail,
    TraceSummary,
)

GEN_AI_PREFIX = "gen_ai."
NODESTEP_PREFIX = "nodestep."
STATE_ATTRIBUTES = ("nodestep.state.before", "nodestep.state.after")
TOKEN_ATTRIBUTES = ("gen_ai.usage.input_tokens", "gen_ai.usage.output_tokens")
TRUNCATED_ATTRIBUTE = "nodestep.truncated"
RUN_STATUS_CLASSES = {
    RunStatus.COMPLETED: "completed",
    RunStatus.PAUSED: "paused",
    RunStatus.STOPPED: "stopped",
    RunStatus.FAILED: "failed",
}
SPAN_STATUS_CLASSES: dict[int, str] = {
    StatusCode.OK: "completed",
    StatusCode.ERROR: "failed",
}
TIMELINE_WIDTH = 1000
SEARCH_WORD = re.compile(r'"([^"]*)"|(\S+)')
MIN_BAR_WIDTH = 2


def format_duration(ns: int) -> str:
    if ns < 0:
        return "-"
    if ns < 1_000:
        return f"{ns} ns"
    if ns < 1_000_000:
        return f"{ns / 1_000:.1f} µs"
    if ns < 1_000_000_000:
        return f"{ns / 1_000_000:.1f} ms"
    return f"{ns / 1_000_000_000:.2f} s"


def format_offset(ns: int) -> str:
    return f"+{format_duration(ns)}" if ns >= 0 else f"-{format_duration(-ns)}"


def format_timestamp(ns: int) -> str:
    seconds, remainder = divmod(ns, 1_000_000_000)
    moment = datetime.fromtimestamp(seconds, tz=UTC)
    return f"{moment:%Y-%m-%d %H:%M:%S}.{remainder // 1_000_000:03d} UTC"


def format_count(value: int | None) -> str:
    return "-" if value is None else f"{value:,}"


def format_value(value: JsonValue) -> str:
    match value:
        case str():
            return value
        case bool():
            return "true" if value else "false"
        case None:
            return "null"
        case int() | float():
            return str(value)
        case [str() | bool() | int() | float() | None]:
            return json.dumps(value, ensure_ascii=False)
        case _:
            return json.dumps(value, ensure_ascii=False, indent=2)


def format_attribute(value: JsonValue) -> str:
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (ValueError, RecursionError):
            return value
        if isinstance(parsed, (dict, list)):
            return format_value(parsed)
        return value
    return format_value(value)


def kind_name(kind: int) -> str:
    return SpanKind(kind).name.lower() if kind in SpanKind else f"kind {kind}"


def status_name(code: int) -> str:
    return StatusCode(code).name.lower() if code in StatusCode else f"status {code}"


def trace_status(summary: TraceSummary) -> str:
    kind = trace_status_class(summary)
    return kind.capitalize() if kind else status_name(summary.status_code)


def trace_status_class(summary: TraceSummary) -> str:
    if summary.running:
        return "running"
    if summary.run_status is None:
        return SPAN_STATUS_CLASSES.get(summary.status_code, "")
    return RUN_STATUS_CLASSES[summary.run_status]


def is_error(value: int) -> bool:
    return value == StatusCode.ERROR


class TraceSearch(Record):
    """The words the trace list is searched for.

    A trace matches when each word is part of its title, root span name,
    trace id, thread id, service or status, ignoring case. A phrase in
    double quotes counts as one word.

    Attributes
    ----------
    text
        The query with whitespace collapsed, cut to ``max_length``
        characters.
    """

    max_length: ClassVar[int] = 200

    text: str

    @classmethod
    def from_query(cls, query: str) -> Self:
        """Read the ``q`` query parameter of the trace list."""
        return cls(text=" ".join(query.split())[: cls.max_length].strip())

    @property
    def words(self) -> list[str]:
        """The case-folded words and quoted phrases of the query; none means every trace."""
        found = (
            phrase or word.strip('"')
            for phrase, word in SEARCH_WORD.findall(self.text.casefold())
        )
        return [word for word in found if word]

    def matches(self, summary: TraceSummary) -> bool:
        """Whether every word is part of one of the fields of ``summary``."""
        fields = [
            value.casefold()
            for value in (
                summary.input_text,
                summary.name,
                summary.trace_id,
                summary.thread_id,
                summary.service_name,
                trace_status(summary),
            )
            if value
        ]
        return all(any(word in field for field in fields) for word in self.words)


class InputPreview:
    """One line of text that tells a run apart by its input."""

    max_length = 80

    @classmethod
    def line(cls, value: JsonValue) -> str | None:
        """Turn the input of a run into one line of text.

        Parameters
        ----------
        value
            A ``nodestep.input`` or ``gen_ai.input.messages`` attribute
            value. A string holding JSON is parsed first. For chat messages
            the text of the last user message is used.

        Returns
        -------
        str | None
            The text with whitespace collapsed, or ``None`` when there is no
            text.
        """
        return " ".join((cls._text(value) or "").split()) or None

    @classmethod
    def user_line(cls, value: JsonValue) -> str | None:
        """Turn the last user message of a state into one line of text.

        Parameters
        ----------
        value
            A ``nodestep.state.before`` attribute value. A string holding
            JSON is parsed first.

        Returns
        -------
        str | None
            The text of the last user message in the state's ``messages``
            with whitespace collapsed, or ``None`` when the state has no
            user message.
        """
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (ValueError, RecursionError):
                return None
        match value:
            case {"messages": list() as messages}:
                users = [
                    item
                    for item in messages
                    if isinstance(item, dict) and cls._is_user(item)
                ]
                if not users:
                    return None
                return " ".join(cls._message_text(users[-1]).split()) or None
            case _:
                return None

    @classmethod
    def shorten(cls, line: str, length: int | None = None) -> str:
        """Cut a line to at most ``length`` characters.

        Parameters
        ----------
        line
            A line returned by ``line``.
        length
            The most characters to keep, ``max_length`` when not given.

        Returns
        -------
        str
            The line itself when it fits, otherwise its start followed by an
            ellipsis.
        """
        limit = cls.max_length if length is None else length
        if len(line) <= limit:
            return line
        return line[: limit - 1].rstrip() + "…"

    @classmethod
    def answers(cls, value: JsonValue) -> str | None:
        """Turn the resume answers of a run into one line of text.

        Parameters
        ----------
        value
            A ``nodestep.resume`` attribute value: the answers by interrupt
            key, or a single answer. A string holding JSON is parsed first.

        Returns
        -------
        str | None
            The answer alone when there is one key, otherwise ``key: answer``
            pairs joined by commas, or ``None`` when there is no text.
        """
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (ValueError, RecursionError):
                return cls.line(value)
        match value:
            case dict() if len(value) == 1:
                (answer,) = value.values()
                text = cls._answer(answer)
            case dict():
                text = ", ".join(
                    f"{key}: {cls._answer(item)}" for key, item in value.items()
                )
            case _:
                text = cls._answer(value)
        return " ".join(text.split()) or None

    @staticmethod
    def _answer(value: JsonValue) -> str:
        return (
            value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        )

    @classmethod
    def _text(cls, value: JsonValue) -> str | None:
        match value:
            case None:
                return None
            case str():
                try:
                    parsed = json.loads(value)
                except (ValueError, RecursionError):
                    return value
                return parsed if isinstance(parsed, str) else cls._text(parsed)
            case list():
                messages = [
                    item
                    for item in value
                    if isinstance(item, dict) and ("role" in item or "type" in item)
                ]
                if not messages:
                    return json.dumps(value, ensure_ascii=False)
                users = [item for item in messages if cls._is_user(item)]
                return cls._message_text((users or messages)[-1])
            case {"messages": list() as messages}:
                return cls._text(messages)
            case dict():
                return json.dumps(value, ensure_ascii=False)
            case _:
                return format_value(value)

    @staticmethod
    def _is_user(message: dict[str, JsonValue]) -> bool:
        return message.get("role") == "user" or message.get("type") == "human"

    @staticmethod
    def _message_text(message: dict[str, JsonValue]) -> str:
        content = message.get("content")
        if isinstance(content, str):
            return content
        parts = message.get("parts", content)
        texts = [
            text
            for part in (parts if isinstance(parts, list) else [])
            if isinstance(part, dict)
            for text in (part.get("content"), part.get("text"))
            if isinstance(text, str)
        ]
        return " ".join(texts) if texts else json.dumps(message, ensure_ascii=False)


class ToolCallView(Record):
    """A tool call of a chat message as its message row shows it.

    Attributes
    ----------
    name
        The name of the tool.
    arguments
        The arguments as compact JSON.
    line
        The call on one line as ``name(key: value)``, cut to
        ``DataTree.preview_length`` characters.
    """

    name: str
    arguments: str
    line: str


class MessagePreview(Record):
    """What the folded row of a chat message shows.

    Attributes
    ----------
    text
        The content on one line: the text with whitespace collapsed, or a JSON
        object as ``key: value`` pairs, cut to ``DataTree.preview_length``
        characters; ``None`` without content.
    prose
        Whether the content is the text of a human, ai or system message.
    short
        Whether that text is short enough to show in full on about two lines.
    calls
        One line per tool call, as ``ToolCallView.line``. With more than
        ``DataTree.max_calls`` calls, the first lines are followed by one that
        says how many more calls there are.
    """

    text: str | None
    prose: bool
    short: bool
    calls: list[str]


class DataTree:
    """How the data viewer macro reads a JSON value.

    The macro in ``data.html`` renders objects and arrays as nested
    ``<details>`` that are open for the first ``open_depth`` levels, and a
    list of chat messages as folded message rows. Below ``max_depth`` levels
    a value is shown as compact JSON text.
    """

    open_depth = 2
    max_depth = 24
    preview_length = 240
    short_length = 160
    max_calls = 3
    roles: ClassVar[dict[str, str]] = {
        "human": "human",
        "ai": "ai",
        "tool": "tool",
        "system": "system",
        "user": "human",
        "assistant": "ai",
    }

    @classmethod
    def kind(cls, value: JsonValue) -> str:
        """Name how ``value`` is drawn.

        Returns
        -------
        str
            ``messages`` for a list of chat messages, ``object`` or ``array``
            for one with entries, ``empty`` for an empty object or array, and
            otherwise ``string``, ``number``, ``boolean`` or ``null``.
        """
        match value:
            case list() if value and all(cls.is_message(item) for item in value):
                return "messages"
            case {} | [] if not value:
                return "empty"
            case dict():
                return "object"
            case list():
                return "array"
            case str():
                return "string"
            case bool():
                return "boolean"
            case None:
                return "null"
            case _:
                return "number"

    @classmethod
    def is_message(cls, value: JsonValue) -> bool:
        """Whether ``value`` is a chat message.

        nodestep stores an object with ``content`` and a ``type`` or ``role``
        of ``human``, ``ai``, ``tool`` or ``system``. The OpenTelemetry GenAI
        shape is an object with a ``parts`` list and a ``role`` of ``user``,
        ``assistant``, ``tool`` or ``system``.
        """
        return (
            isinstance(value, dict)
            and ("content" in value or isinstance(value.get("parts"), list))
            and bool(cls.role(value))
        )

    @classmethod
    def role(cls, message: dict[str, JsonValue]) -> str:
        """The role of a message as ``human``, ``ai``, ``tool`` or ``system``.

        ``user`` reads as ``human`` and ``assistant`` as ``ai``. A message
        without a known ``type`` or ``role`` has ``""``.
        """
        for key in ("type", "role"):
            value = message.get(key)
            if isinstance(value, str) and value in cls.roles:
                return cls.roles[value]
        return ""

    @staticmethod
    def name(message: dict[str, JsonValue]) -> str | None:
        """The ``name`` of a message, such as the tool a tool message answers."""
        value = message.get("name")
        return value if isinstance(value, str) and value else None

    @classmethod
    def content(cls, message: dict[str, JsonValue]) -> JsonValue:
        """The ``content`` of a message as the message row shows it.

        Returns
        -------
        JsonValue
            The text, or for a message with ``parts`` the text parts and tool
            results joined by blank lines; the parsed value when the text is a
            JSON object or an array that is not empty, as tool results often
            are; ``None`` for no content or empty text.
        """
        content = message.get("content")
        if "content" not in message:
            content = cls.parts_text(message.get("parts"))
        if isinstance(content, str):
            if content[:1] in ("{", "["):
                try:
                    parsed = json.loads(content)
                except (ValueError, RecursionError):
                    return content
                if cls.kind(parsed) in ("object", "array", "messages"):
                    return parsed
            return content or None
        return content

    @classmethod
    def parts_text(cls, parts: JsonValue) -> str | None:
        """The text parts and tool results of a GenAI message, joined by blank lines."""
        texts: list[str] = []
        for part in parts if isinstance(parts, list) else []:
            if not isinstance(part, dict):
                continue
            match part.get("type"):
                case "text":
                    text = part.get("content")
                case "tool_call_response":
                    text = part.get("response")
                case _:
                    continue
            if isinstance(text, str):
                texts.append(text)
            elif text is not None:
                texts.append(cls.compact(text))
        return "\n\n".join(texts) or None

    @classmethod
    def previews(cls, messages: list[JsonValue]) -> list[MessagePreview]:
        """What the folded rows of a list of chat messages show.

        A tool result is read with the call it answers, found by its id, so the
        keys that the call was given come last in its preview.
        """
        given: dict[str, list[str]] = {
            identifier: list(arguments)
            for message in messages
            if isinstance(message, dict)
            for call in cls.call_items(message)
            if isinstance(identifier := call.get("id"), str)
            and isinstance(arguments := call.get("arguments"), dict)
        }
        return [
            cls.preview(message, given.get(cls.answered(message) or "", []))
            for message in messages
            if isinstance(message, dict)
        ]

    @classmethod
    def preview(
        cls, message: dict[str, JsonValue], given: Sequence[str] = ()
    ) -> MessagePreview:
        """What the folded row of ``message`` shows: its content and calls on one line each.

        Parameters
        ----------
        message
            A chat message.
        given
            The arguments of the call that a tool result answers. Its keys
            that repeat them come last, after what the tool found.
        """
        content = cls.content(message)
        if isinstance(content, dict) and given:
            content = dict(sorted(content.items(), key=lambda item: item[0] in given))
        text = None if content is None else cls.line(content) or None
        prose = isinstance(content, str) and cls.role(message) != "tool"
        lines = [call.line for call in cls.calls(message)]
        if len(lines) > cls.max_calls:
            more = len(lines) - cls.max_calls + 1
            lines = [*lines[: cls.max_calls - 1], f"{more} more calls"]
        return MessagePreview(
            text=None
            if text is None
            else InputPreview.shorten(text, cls.preview_length),
            prose=prose,
            short=prose and text is not None and len(text) <= cls.short_length,
            calls=lines,
        )

    @staticmethod
    def answered(message: dict[str, JsonValue]) -> str | None:
        """The id of the tool call that a tool result answers, from ``tool_call_id`` or its ``tool_call_response`` part."""
        identifier = message.get("tool_call_id")
        if isinstance(identifier, str):
            return identifier
        parts = message.get("parts")
        for part in parts if isinstance(parts, list) else []:
            if (
                isinstance(part, dict)
                and part.get("type") == "tool_call_response"
                and isinstance(identifier := part.get("id"), str)
            ):
                return identifier
        return None

    @classmethod
    def shown(cls, message: dict[str, JsonValue]) -> dict[str, JsonValue]:
        """The parts of ``message`` that its row shows outside its JSON.

        The JSON of the message marks them, so a search counts each match
        once.

        Returns
        -------
        dict[str, JsonValue]
            ``true`` for ``name`` and ``content`` when the row shows them. For
            ``tool_calls`` and ``parts``, one object per item with ``true``
            for the keys the row shows: the name and the arguments of a call,
            and the text or the response of a part when the row shows the
            parts as its content.
        """
        shown: dict[str, JsonValue] = {}
        if cls.name(message):
            shown["name"] = True
        if message.get("content") not in (None, ""):
            shown["content"] = True
        for key in ("tool_calls", "parts"):
            items = message.get(key)
            if isinstance(items, list):
                shown[key] = [
                    cls._shown_item(item, key, text="content" not in message)
                    for item in items
                ]
        return shown

    @staticmethod
    def _shown_item(item: JsonValue, key: str, *, text: bool) -> JsonValue:
        if not isinstance(item, dict):
            return None
        match "tool_call" if key == "tool_calls" else item.get("type"):
            case "tool_call" if isinstance(item.get("name"), str):
                return {"name": True, "arguments": True}
            case "text" if text and item.get("content") is not None:
                return {"content": True}
            case "tool_call_response" if text and item.get("response") is not None:
                return {"response": True}
            case _:
                return None

    @staticmethod
    def shown_at(shown: JsonValue, key: str | int) -> JsonValue:
        """The part of a value returned by ``shown`` for one key or index of the value it describes."""
        match shown:
            case dict() if isinstance(key, str):
                return shown.get(key)
            case list() if isinstance(key, int) and key < len(shown):
                return shown[key]
            case _:
                return None

    @classmethod
    def line(cls, value: JsonValue) -> str:
        """``value`` on one line: text with whitespace collapsed, an object as ``key: value`` pairs, else compact JSON."""
        match value:
            case str():
                text = value
            case dict():
                text = cls.pairs(value)
            case _:
                text = cls.compact(cls.flattened(value))
        return " ".join(text.split())

    @classmethod
    def pairs(cls, value: dict[str, JsonValue]) -> str:
        """The entries of an object as ``key: value`` pairs, with the values as compact JSON on one line."""
        return ", ".join(
            f"{key}: {cls.compact(cls.flattened(item))}" for key, item in value.items()
        )

    @classmethod
    def flattened(cls, value: JsonValue, depth: int = 0) -> JsonValue:
        """``value`` with the whitespace in its strings collapsed to single spaces, down to ``max_depth`` levels."""
        match value:
            case str():
                return " ".join(value.split())
            case list() if depth < cls.max_depth:
                return [cls.flattened(item, depth + 1) for item in value]
            case dict() if depth < cls.max_depth:
                return {
                    key: cls.flattened(item, depth + 1) for key, item in value.items()
                }
            case _:
                return value

    @staticmethod
    def call_items(message: dict[str, JsonValue]) -> list[dict[str, JsonValue]]:
        """The tool calls of a message as stored: its ``tool_calls`` and its ``tool_call`` parts that have a name."""
        calls = message.get("tool_calls")
        parts = message.get("parts")
        return [
            call
            for call in [
                *(calls if isinstance(calls, list) else []),
                *(
                    part
                    for part in (parts if isinstance(parts, list) else [])
                    if isinstance(part, dict) and part.get("type") == "tool_call"
                ),
            ]
            if isinstance(call, dict) and isinstance(call.get("name"), str)
        ]

    @classmethod
    def calls(cls, message: dict[str, JsonValue]) -> list[ToolCallView]:
        """The tool calls of a message, from ``tool_calls`` or its ``tool_call`` parts."""
        found: list[ToolCallView] = []
        for call in cls.call_items(message):
            if isinstance(name := call.get("name"), str):
                arguments = call.get("arguments")
                match arguments:
                    case dict():
                        listed = cls.pairs(arguments)
                    case None:
                        listed = ""
                    case _:
                        listed = cls.compact(cls.flattened(arguments))
                found.append(
                    ToolCallView(
                        name=name,
                        arguments=cls.compact(arguments),
                        line=InputPreview.shorten(
                            f"{name}({listed})", cls.preview_length
                        ),
                    )
                )
        return found

    @classmethod
    def size(cls, value: JsonValue) -> str:
        """Count the keys, items or messages of an object or array."""
        count = len(value) if isinstance(value, (dict, list)) else 0
        if isinstance(value, dict):
            noun = "key"
        elif cls.kind(value) == "messages":
            noun = "message"
        else:
            noun = "item"
        return f"{count} {noun}" if count == 1 else f"{count} {noun}s"

    @classmethod
    def leaf(cls, value: JsonValue) -> tuple[str, str]:
        """The code token kind and the text of a value shown on one line."""
        match cls.kind(value):
            case "string":
                return "string", str(value)
            case "number":
                return "number", json.dumps(value)
            case "boolean":
                return "keyword", "true" if value else "false"
            case "null":
                return "comment", "null"
            case _:
                return "operator", cls.compact(value)

    @staticmethod
    def compact(value: JsonValue) -> str:
        """``value`` as JSON on one line, with a space after commas and colons."""
        return json.dumps(value, ensure_ascii=False, separators=(", ", ": "))


class DataView(Record):
    """A JSON object or array that the pages show in the data viewer.

    Attributes
    ----------
    text
        The value as pretty-printed JSON. The viewer's Copy JSON button
        copies it.
    """

    text: str

    @property
    def value(self) -> JsonValue:
        """The parsed value."""
        return json.loads(self.text)

    @classmethod
    def from_text(cls, text: str) -> Self | None:
        """Read the text of a JSON value for the data viewer.

        Parameters
        ----------
        text
            A value as ``format_attribute`` writes it.

        Returns
        -------
        DataView | None
            The view when the text is a JSON object with keys, or an array
            that holds an object or an array; otherwise ``None``, and the
            pages show the text as it is.
        """
        try:
            value = json.loads(text)
        except (ValueError, RecursionError):
            return None
        match value:
            case dict() if value:
                return cls(text=text)
            case list() if any(isinstance(item, (dict, list)) for item in value):
                return cls(text=text)
            case _:
                return None


class TimelineRow(Record):
    span: SpanSummary
    depth: int
    offset: float
    width: float


class Timeline(Record):
    rows: list[TimelineRow]

    @classmethod
    def from_trace(cls, detail: TraceDetail) -> Self:
        known = {span.span_id for span in detail.spans}
        children: defaultdict[str, list[SpanSummary]] = defaultdict(list)
        tops: list[SpanSummary] = []
        for span in detail.spans:
            parent = span.parent_span_id
            if parent is None or parent not in known or parent == span.span_id:
                tops.append(span)
            else:
                children[parent].append(span)
        placed: list[tuple[SpanSummary, int]] = []
        visited: set[str] = set()
        for span in [*tops, *detail.spans]:
            stack = [(span, 0)]
            while stack:
                current, depth = stack.pop()
                if current.span_id in visited:
                    continue
                visited.add(current.span_id)
                placed.append((current, depth))
                stack.extend(
                    (child, depth + 1) for child in reversed(children[current.span_id])
                )
        start = detail.summary.start_ns
        total = max(detail.summary.end_ns - start, 1)
        return cls(rows=[cls._row(span, depth, start, total) for span, depth in placed])

    @staticmethod
    def _row(span: SpanSummary, depth: int, start: int, total: int) -> TimelineRow:
        offset = min(
            max((span.start_ns - start) / total * TIMELINE_WIDTH, 0),
            TIMELINE_WIDTH - MIN_BAR_WIDTH,
        )
        width = min(
            max(span.duration_ns / total * TIMELINE_WIDTH, MIN_BAR_WIDTH),
            TIMELINE_WIDTH - offset,
        )
        return TimelineRow(
            span=span, depth=depth, offset=round(offset, 1), width=round(width, 1)
        )


class AttributeRow(Record):
    key: str
    text: str
    truncated: bool = False


class StateView(Record):
    key: str
    text: str
    valid_json: bool
    truncated: bool = False


class AttributeGroups(Record):
    state: list[StateView]
    gen_ai: list[AttributeRow]
    nodestep: list[AttributeRow]
    other: list[AttributeRow]
    truncated: list[str]

    @property
    def empty(self) -> bool:
        return not (self.state or self.gen_ai or self.nodestep or self.other)

    @property
    def grouped(self) -> bool:
        return bool(self.gen_ai or self.nodestep)

    @classmethod
    def from_attributes(cls, attributes: Mapping[str, JsonValue]) -> Self:
        truncated = cls._truncated(attributes.get(TRUNCATED_ATTRIBUTE))
        gen_ai: list[AttributeRow] = []
        nodestep: list[AttributeRow] = []
        other: list[AttributeRow] = []
        for key in sorted(attributes):
            if key in STATE_ATTRIBUTES:
                continue
            row = AttributeRow(
                key=key,
                text=cls._text(key, attributes[key]),
                truncated=key in truncated,
            )
            if key.startswith(GEN_AI_PREFIX):
                gen_ai.append(row)
            elif key.startswith(NODESTEP_PREFIX):
                nodestep.append(row)
            else:
                other.append(row)
        return cls(
            state=[
                cls._state(key, attributes[key], truncated=key in truncated)
                for key in STATE_ATTRIBUTES
                if key in attributes
            ],
            gen_ai=gen_ai,
            nodestep=nodestep,
            other=other,
            truncated=truncated,
        )

    @staticmethod
    def _truncated(value: JsonValue) -> list[str]:
        if not isinstance(value, list):
            return []
        return [key for key in value if isinstance(key, str)]

    @staticmethod
    def _text(key: str, value: JsonValue) -> str:
        if key in TOKEN_ATTRIBUTES and type(value) is int:
            return format_count(value)
        return format_attribute(value)

    @staticmethod
    def _state(key: str, value: JsonValue, *, truncated: bool) -> StateView:
        if not isinstance(value, str):
            return StateView(
                key=key, text=format_value(value), valid_json=True, truncated=truncated
            )
        try:
            parsed = json.loads(value)
        except (ValueError, RecursionError):
            return StateView(key=key, text=value, valid_json=False, truncated=truncated)
        return StateView(
            key=key,
            text=json.dumps(parsed, ensure_ascii=False, indent=2),
            valid_json=True,
            truncated=truncated,
        )
