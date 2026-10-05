import json
from collections.abc import Sequence
from typing import Any, ClassVar

from nodestep.chat import AIMessage, ChatResponse, ToolCall, ToolMessage
from nodestep.state import to_json_value
from opentelemetry.trace import Span
from pydantic import BaseModel, ConfigDict

MAX_ATTRIBUTE_BYTES = 16 * 1024
TRUNCATED_ATTRIBUTE = "nodestep.truncated"
ITEM_SEPARATOR_BYTES = len(", ")

type MessagePart = dict[str, Any]
type GenAiMessage = dict[str, Any]


class EncodedJson(BaseModel):
    """JSON text for a span attribute.

    Attributes
    ----------
    text
        The JSON text.
    truncated
        Whether the value was shortened to fit.
    """

    model_config = ConfigDict(frozen=True)

    text: str
    truncated: bool = False


class JsonAttribute:
    """Span attribute values that hold JSON."""

    max_bytes = MAX_ATTRIBUTE_BYTES

    @classmethod
    def encode(cls, value: Any) -> EncodedJson:
        """Encode a value as JSON text of at most ``max_bytes`` UTF-8 bytes.

        A value that is too long loses the oldest items of its lists first:
        whole items are dropped from the front of a list, or of the longest
        list fields of an object, so the latest messages of a conversation
        are kept and the text stays valid JSON. The last item of a list is
        always kept. Only when that is not enough is the text cut at
        ``max_bytes``, and it is then no longer valid JSON.

        Parameters
        ----------
        value
            Any value. ``nodestep.state.to_json_value`` converts it, so
            pydantic models are dumped and secrets masked. A value it does
            not know is written as its ``repr``.

        Returns
        -------
        EncodedJson
            The text, and whether anything was dropped or cut.
        """
        data = to_json_value(value, fallback=repr)
        text = cls._dump(data)
        excess = cls._size(text) - cls.max_bytes
        if excess <= 0:
            return EncodedJson(text=text)
        encoded = cls._dump(cls._drop_oldest(data, excess)).encode()
        return EncodedJson(
            text=encoded[: cls.max_bytes].decode(errors="ignore"), truncated=True
        )

    @classmethod
    def _drop_oldest(cls, data: Any, excess: int) -> Any:
        if isinstance(data, list):
            return cls._latest(data, excess)
        if not isinstance(data, dict):
            return data
        shortened = dict(data)
        sizes = {
            key: cls._size(cls._dump(item))
            for key, item in data.items()
            if isinstance(item, list)
        }
        for key in sorted(sizes, key=sizes.__getitem__, reverse=True):
            if excess <= 0:
                break
            shortened[key] = cls._latest(data[key], excess)
            excess -= sizes[key] - cls._size(cls._dump(shortened[key]))
        return shortened

    @classmethod
    def _latest(cls, items: list[Any], excess: int) -> list[Any]:
        start = 0
        removed = 0
        while start < len(items) - 1 and removed < excess:
            removed += cls._size(cls._dump(items[start])) + ITEM_SEPARATOR_BYTES
            start += 1
        return items[start:]

    @staticmethod
    def _dump(data: Any) -> str:
        return json.dumps(data, ensure_ascii=False)

    @staticmethod
    def _size(text: str) -> int:
        return len(text.encode())


class JsonSpan:
    """A span that records which of its JSON attributes were shortened.

    Parameters
    ----------
    span
        The span to write to.
    """

    def __init__(self, span: Span) -> None:
        self.span = span
        self.truncated: list[str] = []

    def set_json(self, key: str, value: Any) -> None:
        """Set ``key`` to ``value`` as JSON.

        When the value had to be shortened, ``key`` is added to the
        ``nodestep.truncated`` attribute, a list of the shortened keys.

        Parameters
        ----------
        key
            Attribute name.
        value
            Any value ``JsonAttribute.encode`` accepts.
        """
        encoded = JsonAttribute.encode(value)
        self.span.set_attribute(key, encoded.text)
        if encoded.truncated and key not in self.truncated:
            self.truncated.append(key)
            self.span.set_attribute(TRUNCATED_ATTRIBUTE, self.truncated)


class GenAiMessages:
    """Chat messages in the shape of the OpenTelemetry GenAI conventions."""

    roles: ClassVar[dict[str, str]] = {
        "system": "system",
        "human": "user",
        "ai": "assistant",
        "tool": "tool",
    }

    @classmethod
    def from_request(cls, messages: Sequence[Any]) -> list[GenAiMessage]:
        """Convert the messages of a chat request.

        Parameters
        ----------
        messages
            nodestep chat messages.

        Returns
        -------
        list[dict]
            One ``{"role", "parts"}`` item per message.
        """
        return [
            {"role": cls.roles[message.type], "parts": cls._parts(message)}
            for message in messages
        ]

    @classmethod
    def from_response(cls, response: ChatResponse) -> list[GenAiMessage]:
        """Convert a chat response to the one output message it holds.

        Parameters
        ----------
        response
            The model's response.

        Returns
        -------
        list[dict]
            One ``{"role": "assistant", "parts", "finish_reason"}`` item; the
            finish reason only when the response has one.
        """
        message: GenAiMessage = {
            "role": "assistant",
            "parts": cls._text_parts(response.content)
            + cls._call_parts(response.tool_calls),
        }
        if response.finish_reason is not None:
            message["finish_reason"] = response.finish_reason
        return [message]

    @classmethod
    def _parts(cls, message: Any) -> list[MessagePart]:
        if isinstance(message, ToolMessage):
            return [
                {
                    "type": "tool_call_response",
                    "id": message.tool_call_id,
                    "response": message.content,
                }
            ]
        parts = cls._text_parts(message.content)
        if isinstance(message, AIMessage):
            parts += cls._call_parts(message.tool_calls)
        return parts

    @staticmethod
    def _text_parts(content: str | None) -> list[MessagePart]:
        return [] if content is None else [{"type": "text", "content": content}]

    @staticmethod
    def _call_parts(calls: Sequence[ToolCall]) -> list[MessagePart]:
        return [
            {
                "type": "tool_call",
                "id": call.id,
                "name": call.name,
                "arguments": call.arguments,
            }
            for call in calls
        ]
