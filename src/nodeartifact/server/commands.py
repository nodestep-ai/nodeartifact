from pathlib import Path
from typing import Annotated

import typer

from nodeartifact.server.app import serve
from nodeartifact.server.security import AllowedHost, AllowedHostError
from nodeartifact.server.storage import TraceStoreError

app = typer.Typer(
    name="nodeartifact",
    help="Collect OpenTelemetry traces and look at them in a local web UI.",
    add_completion=False,
    no_args_is_help=True,
)


def check_allowed_hosts(values: list[str] | None) -> list[str] | None:
    for value in values or []:
        try:
            AllowedHost.parse(value)
        except AllowedHostError as error:
            raise typer.BadParameter(str(error)) from None
    return values


@app.callback()
def nodeartifact() -> None:
    pass


@app.command(
    "serve",
    help="Receive OTLP/HTTP traces on /v1/traces, store them in SQLite and show them in a web UI.",
)
def serve_command(
    host: Annotated[
        str,
        typer.Option(
            "--host",
            metavar="ADDRESS",
            help="Address to bind. Clients must name it in the Host header, or name a host given to --allowed-host.",
        ),
    ] = "127.0.0.1",
    port: Annotated[
        int,
        typer.Option(
            "--port",
            metavar="PORT",
            help="Port to bind. 4318 is the standard OTLP/HTTP port.",
        ),
    ] = 4318,
    database: Annotated[
        Path,
        typer.Option(
            "--database",
            metavar="FILE",
            help="SQLite database file. It is created when missing; its directory must exist.",
        ),
    ] = Path("nodeartifact.db"),
    allowed_hosts: Annotated[
        list[str] | None,
        typer.Option(
            "--allowed-host",
            metavar="HOST",
            callback=check_allowed_hosts,
            help=(
                "Another Host header to accept, such as localhost:4318 or nodeartifact:4318; "
                "without a port any port is accepted. Repeat it for each name clients use, "
                "for example when bound to 0.0.0.0 in a container."
            ),
        ),
    ] = None,
) -> None:
    try:
        serve(
            host=host,
            port=port,
            database_path=database,
            allowed_hosts=allowed_hosts or [],
        )
    except TraceStoreError as error:
        typer.echo(f"nodeartifact: {error}", err=True)
        raise typer.Exit(1) from None
