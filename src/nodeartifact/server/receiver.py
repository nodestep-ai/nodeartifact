import json
import zlib
from enum import StrEnum
from typing import Self

from google.protobuf import json_format
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceResponse,
)
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response

from nodeartifact.server.models import ExportBatch
from nodeartifact.server.otlp import OtlpDecodeError, OtlpTraceDecoder
from nodeartifact.server.storage import TraceStore

DEFAULT_MAX_BODY_BYTES = 32 * 1024 * 1024
STATUS_MESSAGE_FIELD = 2
LENGTH_DELIMITED_WIRE_TYPE = 2
SUPPORTED_ENCODINGS = ("identity", "gzip")


class OtlpRequestError(Exception):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


class OtlpFormat(StrEnum):
    PROTOBUF = "application/x-protobuf"
    JSON = "application/json"

    @classmethod
    def from_content_type(cls, content_type: str) -> Self | None:
        media_type = content_type.partition(";")[0].strip().lower()
        return next((item for item in cls if item.value == media_type), None)

    def encode_response(self, response: ExportTraceServiceResponse) -> bytes:
        if self is OtlpFormat.JSON:
            return json_format.MessageToJson(response, indent=None).encode()
        return response.SerializeToString()

    def encode_status(self, message: str) -> bytes:
        if self is OtlpFormat.JSON:
            return json.dumps({"message": message}).encode()
        text = message.encode()
        tag = STATUS_MESSAGE_FIELD << 3 | LENGTH_DELIMITED_WIRE_TYPE
        return bytes([tag]) + self._varint(len(text)) + text

    @staticmethod
    def _varint(value: int) -> bytes:
        encoded = bytearray()
        while value > 0x7F:
            encoded.append(value & 0x7F | 0x80)
            value >>= 7
        encoded.append(value)
        return bytes(encoded)


class OtlpReceiver:
    def __init__(
        self, store: TraceStore, *, max_body_bytes: int = DEFAULT_MAX_BODY_BYTES
    ) -> None:
        self._store = store
        self._max_body_bytes = max_body_bytes
        self._decoder = OtlpTraceDecoder()

    async def export(self, request: Request) -> Response:
        otlp_format = OtlpFormat.from_content_type(
            request.headers.get("content-type", "")
        )
        if otlp_format is None:
            return PlainTextResponse(
                "Content-Type must be application/x-protobuf or application/json",
                status_code=415,
            )
        try:
            batch = self._decode(otlp_format, await self._body(request))
        except OtlpRequestError as error:
            return Response(
                otlp_format.encode_status(str(error)),
                status_code=error.status_code,
                media_type=otlp_format.value,
            )
        await run_in_threadpool(self._store.add, batch.spans)
        return Response(
            otlp_format.encode_response(self._response(batch)),
            media_type=otlp_format.value,
        )

    async def _body(self, request: Request) -> bytes:
        encoding = request.headers.get("content-encoding", "identity").strip().lower()
        if encoding not in SUPPORTED_ENCODINGS:
            raise OtlpRequestError(
                415, f"Content-Encoding {encoding!r} is not supported; use gzip or none"
            )
        declared = request.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > self._max_body_bytes:
            raise self._too_large()
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > self._max_body_bytes:
                raise self._too_large()
        return self._gunzip(bytes(body)) if encoding == "gzip" else bytes(body)

    def _gunzip(self, body: bytes) -> bytes:
        inflater = zlib.decompressobj(wbits=zlib.MAX_WBITS | 16)
        try:
            data = inflater.decompress(body, self._max_body_bytes + 1)
        except zlib.error as error:
            raise OtlpRequestError(400, f"invalid gzip body: {error}") from error
        if len(data) > self._max_body_bytes:
            raise self._too_large()
        if not inflater.eof:
            raise OtlpRequestError(400, "invalid gzip body: the stream is truncated")
        if inflater.unused_data:
            raise OtlpRequestError(
                400, "invalid gzip body: data after the end of the stream"
            )
        return data

    def _decode(self, otlp_format: OtlpFormat, body: bytes) -> ExportBatch:
        try:
            if otlp_format is OtlpFormat.PROTOBUF:
                return self._decoder.from_protobuf(body)
            return self._decoder.from_json(body)
        except OtlpDecodeError as error:
            raise OtlpRequestError(400, str(error)) from error

    def _too_large(self) -> OtlpRequestError:
        return OtlpRequestError(
            413, f"request body exceeds {self._max_body_bytes} bytes"
        )

    @staticmethod
    def _response(batch: ExportBatch) -> ExportTraceServiceResponse:
        response = ExportTraceServiceResponse()
        if batch.rejected:
            response.partial_success.rejected_spans = batch.rejected
            response.partial_success.error_message = (
                f"rejected {batch.rejected} span(s) with an invalid trace id, span id, "
                "parent span id or timestamp"
            )
        return response
