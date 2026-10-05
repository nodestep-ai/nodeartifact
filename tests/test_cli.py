import re
import subprocess
import sys
from importlib.metadata import entry_points
from pathlib import Path

import pytest

from nodeartifact import cli
from nodeartifact.server import commands

HINT = (
    "nodeartifact serve needs the server extra: "
    'uv add "nodeartifact[server] @ git+https://github.com/nodestep-ai/nodeartifact"'
)
SERVER_MODULES = ("typer", "starlette", "jinja2", "uvicorn", "opentelemetry.proto")
ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def run_python(code: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=60,
        encoding="utf-8",
    )


def plain(text: str) -> str:
    return ANSI_ESCAPE.sub("", text)


def exit_code(argv: list[str]) -> int | str | None:
    with pytest.raises(SystemExit) as exit_info:
        cli.main(argv)
    return exit_info.value.code


def block_modules(names: tuple[str, ...], argv: list[str]) -> str:
    return (
        "import sys\n"
        f"for name in {names!r}:\n"
        "    sys.modules[name] = None\n"
        "from nodeartifact.cli import main\n"
        f"main({argv!r})\n"
    )


@pytest.fixture(autouse=True)
def wide_terminal(monkeypatch):
    monkeypatch.setenv("COLUMNS", "200")


@pytest.fixture
def serve_calls(monkeypatch) -> list[dict[str, object]]:
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(commands, "serve", lambda **options: calls.append(options))
    return calls


def test_help_lists_the_serve_command(capsys):
    assert exit_code(["--help"]) == 0

    output = plain(capsys.readouterr().out)
    assert "Usage: nodeartifact" in output
    assert "serve" in output
    assert "--install-completion" not in output


def test_no_arguments_shows_the_help(capsys):
    exit_code([])

    captured = capsys.readouterr()
    assert "serve" in plain(captured.out + captured.err)


def test_serve_help_shows_the_options_and_defaults(capsys):
    assert exit_code(["serve", "--help"]) == 0

    output = plain(capsys.readouterr().out)
    for option, metavar in (
        ("host", "ADDRESS"),
        ("port", "PORT"),
        ("database", "FILE"),
        ("allowed-host", "HOST"),
    ):
        assert re.search(rf"--{option}\s+{metavar}\s", output)
    for text in (
        "[default: 127.0.0.1]",
        "[default: 4318]",
        "[default: nodeartifact.db]",
        "/v1/traces",
    ):
        assert text in output


def test_serve_passes_the_options_to_the_server(serve_calls):
    argv = [
        "serve",
        "--host",
        "0.0.0.0",
        "--port",
        "9999",
        "--database",
        "traces/x.db",
        "--allowed-host",
        "localhost:9999",
        "--allowed-host",
        "nodeartifact:9999",
    ]

    assert exit_code(argv) == 0

    assert serve_calls == [
        {
            "host": "0.0.0.0",
            "port": 9999,
            "database_path": Path("traces/x.db"),
            "allowed_hosts": ["localhost:9999", "nodeartifact:9999"],
        }
    ]


def test_serve_uses_the_defaults(serve_calls):
    assert exit_code(["serve"]) == 0

    assert serve_calls == [
        {
            "host": "127.0.0.1",
            "port": 4318,
            "database_path": Path("nodeartifact.db"),
            "allowed_hosts": [],
        }
    ]


@pytest.mark.parametrize(
    "value", ["*", "", "http://nodeartifact:4318", "nodeartifact:99999"]
)
def test_invalid_allowed_host_is_a_usage_error(value, serve_calls, capsys):
    assert exit_code(["serve", "--allowed-host", value]) == 2

    error = plain(capsys.readouterr().err)
    assert "--allowed-host" in error
    assert serve_calls == []


@pytest.mark.parametrize(
    "argv", [["serve", "--port", "abc"], ["replay"], ["serve", "--db", "x.db"]]
)
def test_usage_errors_exit_with_code_2(argv, serve_calls, capsys):
    assert exit_code(argv) == 2

    assert "Usage: nodeartifact" in plain(capsys.readouterr().err)
    assert serve_calls == []


def test_database_problems_are_reported_without_a_traceback(tmp_path, capsys):
    database = tmp_path / "missing" / "traces.db"

    assert exit_code(["serve", "--database", str(database)]) == 1

    error = plain(capsys.readouterr().err)
    assert error.startswith(f"nodeartifact: cannot open {database}")
    assert "Traceback" not in error


@pytest.mark.parametrize(
    ("blocked", "argv"),
    [
        (("typer",), ["serve"]),
        (("starlette", "jinja2", "uvicorn"), ["serve"]),
        (("opentelemetry.proto",), ["serve"]),
        (("typer",), ["--help"]),
    ],
)
def test_missing_server_extra_prints_the_install_hint(blocked, argv):
    result = run_python(block_modules(blocked, argv))

    assert result.returncode == 2
    assert result.stderr == f"{HINT}\n"
    assert result.stdout == ""


def test_other_import_errors_are_not_hidden():
    result = run_python(block_modules(("nodeartifact.server.storage",), ["serve"]))

    assert result.returncode == 1
    assert "ModuleNotFoundError" in result.stderr
    assert HINT not in result.stderr


def test_cli_imports_without_the_server_modules():
    result = run_python(
        "import sys\n"
        "import nodeartifact.cli\n"
        f"print(sorted(name for name in {SERVER_MODULES!r} if name in sys.modules))\n"
    )

    assert result.returncode == 0
    assert result.stdout.strip() == "[]"


def test_console_script_runs_main():
    (script,) = entry_points(group="console_scripts", name="nodeartifact")

    assert script.load() is cli.main
