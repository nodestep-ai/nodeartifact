import re
from collections.abc import Mapping, Sequence
from typing import Self

from pydantic import BaseModel, ConfigDict
from starlette.datastructures import Headers
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
HOST_NAME = re.compile(
    r"[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*"
)
ALLOWED_HOST = re.compile(
    r"(?:\[(?P<ipv6>[0-9a-f:.]+)\]|(?P<name>[^:\[\]]+))(?::(?P<port>[0-9]+))?"
)
MAX_PORT = 65535
DEFAULT_HTTP_PORT = 80
HEALTH_PATH = "/health"

MERMAID_URL = "https://cdn.jsdelivr.net/npm/mermaid@12.0.0/dist/mermaid.min.js"
MERMAID_INTEGRITY = (
    "sha384-xzghz1GQ5u9HCpVskeDPqMsdogD1yvuMQbEK53+wi+G70+6J1AG0L2cfi9PHjDWI"
)
PAGE_POLICY = (
    "default-src 'none'; script-src 'self'; style-src 'self'; "
    "img-src 'self'; base-uri 'none'; form-action 'self'; "
    "frame-ancestors 'none'"
)
LIST_PAGE_POLICY = (
    "default-src 'none'; script-src 'self'; style-src 'self'; "
    "img-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'self'; "
    "frame-ancestors 'none'"
)
GRAPH_PAGE_POLICY = (
    f"default-src 'none'; script-src 'self' {MERMAID_URL}; "
    "style-src 'self' 'unsafe-inline'; img-src 'self'; connect-src 'self'; "
    "base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)
SECURITY_HEADERS: Mapping[str, str] = {
    "Content-Security-Policy": PAGE_POLICY,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}
LIST_SECURITY_HEADERS: Mapping[str, str] = {
    **SECURITY_HEADERS,
    "Content-Security-Policy": LIST_PAGE_POLICY,
}
GRAPH_SECURITY_HEADERS: Mapping[str, str] = {
    **SECURITY_HEADERS,
    "Content-Security-Policy": GRAPH_PAGE_POLICY,
}


class AllowedHostError(ValueError):
    """Raised when an allowed host is not a host name or address with an optional port."""


class AllowedHost(BaseModel):
    """A ``Host`` header value the server accepts besides the bound address.

    Attributes
    ----------
    hostname
        The host name or IP address, lowercased, without brackets.
    port
        The port the header must name, or ``None`` to accept any port.
    """

    model_config = ConfigDict(frozen=True)

    hostname: str
    port: int | None

    @classmethod
    def parse(cls, value: str) -> Self:
        """Read ``host``, ``host:port``, ``[ipv6]`` or ``[ipv6]:port``.

        Parameters
        ----------
        value
            The text given to ``--allowed-host``.

        Returns
        -------
        AllowedHost
            The host and port.

        Raises
        ------
        AllowedHostError
            If the text is empty, has a scheme, path, user or wildcard, or
            the port is not between 1 and 65535.
        """
        match = ALLOWED_HOST.fullmatch(value.strip().lower())
        if match is None:
            raise AllowedHostError(cls._message(value))
        hostname = match["ipv6"] or match["name"]
        if match["name"] is not None and not HOST_NAME.fullmatch(hostname):
            raise AllowedHostError(cls._message(value))
        port = None if match["port"] is None else int(match["port"])
        if port is not None and not 1 <= port <= MAX_PORT:
            raise AllowedHostError(cls._message(value))
        return cls(hostname=hostname, port=port)

    def accepts(self, hostname: str, port: int | None) -> bool:
        return hostname == self.hostname and self.port in (None, port)

    @staticmethod
    def _message(value: str) -> str:
        return (
            f"{value!r} is not a host name or address with an optional port, "
            "such as localhost:4318 or [::1]:4318"
        )


class HostCheckMiddleware:
    def __init__(
        self, app: ASGIApp, *, host: str, allowed_hosts: Sequence[str] = ()
    ) -> None:
        self.app = app
        bound = self.hostname(host)
        self.bound_hosts = (
            LOOPBACK_HOSTS if bound in LOOPBACK_HOSTS else frozenset({bound})
        )
        self.allowed_hosts = [AllowedHost.parse(value) for value in allowed_hosts]

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] == "http"
            and scope["path"] != HEALTH_PATH
            and not self.accepts(Headers(scope=scope).get("host", ""))
        ):
            response = PlainTextResponse(
                "Host header does not match the bound host", status_code=400
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)

    def accepts(self, header: str) -> bool:
        hostname = self.hostname(header)
        if hostname in self.bound_hosts:
            return True
        port = self.port(header)
        return any(allowed.accepts(hostname, port) for allowed in self.allowed_hosts)

    @staticmethod
    def hostname(value: str) -> str:
        host = value.strip().lower()
        if host.startswith("["):
            end = host.find("]")
            return host[1:end] if end != -1 else host
        if host.count(":") > 1:
            return host
        return host.partition(":")[0]

    @staticmethod
    def port(value: str) -> int | None:
        host = value.strip()
        if host.startswith("["):
            host = host[host.find("]") + 1 :]
        elif host.count(":") > 1:
            return None
        _, separator, port = host.rpartition(":")
        if not separator:
            return DEFAULT_HTTP_PORT
        return int(port) if port.isdigit() else None
