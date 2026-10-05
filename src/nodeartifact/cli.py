import sys
from collections.abc import Sequence

SERVER_EXTRA_MESSAGE = (
    "nodeartifact serve needs the server extra: "
    'uv add "nodeartifact[server] @ git+https://github.com/nodestep-ai/nodeartifact"'
)
SERVER_MODULES = ("typer", "starlette", "jinja2", "uvicorn", "opentelemetry.proto")


def main(argv: Sequence[str] | None = None) -> None:
    """Run the ``nodeartifact`` command line and exit.

    The command line is a Typer app from the ``server`` extra. This entry
    point only imports the standard library, so it can print the install hint
    when the extra is missing.

    Parameters
    ----------
    argv
        Arguments without the program name. ``None`` reads ``sys.argv``.

    Raises
    ------
    SystemExit
        Always. The code is 0 on success, 1 when the database cannot be used
        and 2 on a usage error or when the ``server`` extra is not installed.
    """
    try:
        from nodeartifact.server.commands import app
    except ModuleNotFoundError as error:
        if not is_server_module(error.name):
            raise
        sys.stderr.write(f"{SERVER_EXTRA_MESSAGE}\n")
        sys.exit(2)
    app(args=None if argv is None else list(argv), prog_name="nodeartifact")


def is_server_module(name: str | None) -> bool:
    return name is not None and any(
        name == module or name.startswith(f"{module}.") for module in SERVER_MODULES
    )
