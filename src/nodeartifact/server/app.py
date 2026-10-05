import copy
import logging
import sys
from collections.abc import Sequence
from pathlib import Path

import uvicorn
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from uvicorn.config import LOGGING_CONFIG

from nodeartifact.server.receiver import DEFAULT_MAX_BODY_BYTES, OtlpReceiver
from nodeartifact.server.security import HEALTH_PATH, AllowedHost, HostCheckMiddleware
from nodeartifact.server.storage import TraceStore
from nodeartifact.server.ui import TraceUi

WILDCARD_HOSTS = frozenset({"0.0.0.0", "::"})
ACCESS_PATH_ARGUMENT = 2


class HealthCheckLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        arguments = record.args
        return not (
            isinstance(arguments, tuple)
            and len(arguments) > ACCESS_PATH_ARGUMENT
            and arguments[ACCESS_PATH_ARGUMENT] == HEALTH_PATH
        )


async def health(request: Request) -> PlainTextResponse:
    return PlainTextResponse("ok")


def base_url(hostname: str, port: int) -> str:
    address = f"[{hostname}]" if ":" in hostname else hostname
    return f"http://{address}:{port}"


def client_url(host: str, port: int, allowed_hosts: Sequence[str]) -> str | None:
    if host not in WILDCARD_HOSTS:
        return base_url(host, port)
    if not allowed_hosts:
        return None
    first = AllowedHost.parse(allowed_hosts[0])
    return base_url(first.hostname, first.port or port)


def log_config() -> dict[str, object]:
    config = copy.deepcopy(LOGGING_CONFIG)
    config["filters"] = {"health_check": {"()": HealthCheckLogFilter}}
    config["loggers"]["uvicorn.access"]["filters"] = ["health_check"]
    return config


def create_app(
    store: TraceStore,
    *,
    host: str,
    allowed_hosts: Sequence[str] = (),
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
) -> Starlette:
    """Build the nodeartifact web application.

    Parameters
    ----------
    store
        Where received spans are written and pages read from.
    host
        The address the server is bound to. Requests whose ``Host`` header
        names another host get 400, except ``GET /health``, which answers
        ``ok`` to any request. For a loopback address, ``localhost``,
        ``127.0.0.1`` and ``::1`` are all accepted.
    allowed_hosts
        More ``Host`` header values to accept, each ``host``, ``host:port``,
        ``[ipv6]`` or ``[ipv6]:port``. Without a port any port is accepted.
        A server bound to ``0.0.0.0`` in a container lists the names clients
        use, such as ``localhost:4318`` and ``nodeartifact:4318``.
    max_body_bytes
        Largest accepted OTLP request body, before and after gzip.

    Returns
    -------
    Starlette
        The ASGI application: ``POST /v1/traces``, the trace pages, the
        results of the trace list for a query at ``GET /results``, the live
        stream of the trace list at ``GET /live`` and of a trace at
        ``GET /traces/{trace_id}/live``, and ``GET /health``. Other paths
        get the not-found page.

    Raises
    ------
    AllowedHostError
        If an allowed host is not a host name or address with an optional
        port.
    """
    receiver = OtlpReceiver(store, max_body_bytes=max_body_bytes)
    ui = TraceUi(store)
    return Starlette(
        routes=[
            Route("/", ui.trace_list, name="trace_list"),
            Route("/live", ui.list_live, name="trace_list_live"),
            Route("/results", ui.list_results, name="trace_list_results"),
            Route("/traces/{trace_id}", ui.trace_view, name="trace_view"),
            Route(
                "/traces/{trace_id}/timeline", ui.timeline_view, name="trace_timeline"
            ),
            Route("/traces/{trace_id}/live", ui.live, name="trace_live"),
            Route("/traces/{trace_id}/spans/{span_id}", ui.span_view, name="span_view"),
            Route("/v1/traces", receiver.export, methods=["POST"], name="otlp_traces"),
            Route(HEALTH_PATH, health, name="health"),
            Mount(
                "/static",
                app=StaticFiles(packages=[("nodeartifact.server", "static")]),
                name="static",
            ),
        ],
        middleware=[
            Middleware(HostCheckMiddleware, host=host, allowed_hosts=allowed_hosts)
        ],
        exception_handlers={404: ui.unknown_page},
    )


def serve(
    *, host: str, port: int, database_path: Path, allowed_hosts: Sequence[str]
) -> None:
    """Run the server until it is interrupted.

    Requests for ``/health`` are left out of the access log.

    Parameters
    ----------
    host
        Address to bind. Requests must name it in their ``Host`` header.
    port
        Port to bind.
    database_path
        SQLite database file. It is created when missing; its directory must
        exist.
    allowed_hosts
        More ``Host`` header values to accept, as for ``create_app``.

    Raises
    ------
    TraceStoreError
        If the database cannot be opened or was not created by nodeartifact.
    AllowedHostError
        If an allowed host is not a host name or address with an optional
        port.
    """
    with TraceStore(database_path) as store:
        app = create_app(store, host=host, allowed_hosts=allowed_hosts)
        url = client_url(host, port, allowed_hosts)
        sys.stdout.write(f"nodeartifact: database {database_path.resolve()}\n")
        if url is not None:
            sys.stdout.write(
                f"nodeartifact: send OTLP/HTTP traces to {url}/v1/traces and open {url}/\n"
            )
        if allowed_hosts:
            sys.stdout.write(
                f"nodeartifact: also accepts the Host headers {', '.join(allowed_hosts)}\n"
            )
        elif host in WILDCARD_HOSTS:
            sys.stderr.write(
                f"nodeartifact: bound to {host} without --allowed-host, so only requests"
                f" whose Host header is {host} are answered; pass --allowed-host for"
                " each address clients use, such as localhost:4318\n"
            )
        sys.stdout.flush()
        uvicorn.run(
            app, host=host, port=port, proxy_headers=False, log_config=log_config()
        )
