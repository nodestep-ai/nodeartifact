import logging

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

import nodeartifact.server.app
from nodeartifact.server import serve
from nodeartifact.server.app import HealthCheckLogFilter, create_app
from nodeartifact.server.storage import TraceStore


@pytest.fixture
def uvicorn_calls(monkeypatch) -> list[dict[str, object]]:
    calls: list[dict[str, object]] = []

    def run(app: object, **options: object) -> None:
        calls.append({"app": app, **options})

    monkeypatch.setattr(nodeartifact.server.app.uvicorn, "run", run)
    return calls


def test_serve_runs_uvicorn_with_the_app_and_a_database(
    tmp_path, uvicorn_calls, capsys
):
    database = tmp_path / "traces.db"

    serve(host="127.0.0.1", port=4318, database_path=database, allowed_hosts=())

    (call,) = uvicorn_calls
    assert isinstance(call.pop("app"), Starlette)
    call.pop("log_config")
    assert call == {"host": "127.0.0.1", "port": 4318, "proxy_headers": False}
    assert database.exists()
    output = capsys.readouterr().out
    assert f"database {database.resolve()}" in output
    assert "http://127.0.0.1:4318/v1/traces" in output


def test_serve_prints_ipv6_addresses_in_brackets(tmp_path, uvicorn_calls, capsys):
    serve(host="::1", port=4318, database_path=tmp_path / "traces.db", allowed_hosts=())

    assert "http://[::1]:4318/v1/traces" in capsys.readouterr().out


def test_serve_names_the_allowed_hosts(tmp_path, uvicorn_calls, capsys):
    serve(
        host="0.0.0.0",
        port=4318,
        database_path=tmp_path / "traces.db",
        allowed_hosts=("localhost:4318", "nodeartifact:4318"),
    )

    captured = capsys.readouterr()
    assert "accepts the Host headers localhost:4318, nodeartifact:4318" in captured.out
    assert captured.err == ""


@pytest.mark.parametrize(
    ("allowed_hosts", "url"),
    [
        (("localhost:18080", "nodeartifact:4318"), "http://localhost:18080"),
        (("traces.internal",), "http://traces.internal:4318"),
        (("[::1]:4318",), "http://[::1]:4318"),
    ],
)
def test_serve_on_a_wildcard_address_names_the_first_allowed_host(
    tmp_path, uvicorn_calls, capsys, allowed_hosts, url
):
    serve(
        host="0.0.0.0",
        port=4318,
        database_path=tmp_path / "traces.db",
        allowed_hosts=allowed_hosts,
    )

    output = capsys.readouterr().out
    assert f"send OTLP/HTTP traces to {url}/v1/traces and open {url}/" in output
    assert "http://0.0.0.0" not in output


def test_serve_on_a_wildcard_address_without_allowed_hosts_warns(
    tmp_path, uvicorn_calls, capsys
):
    serve(
        host="0.0.0.0",
        port=4318,
        database_path=tmp_path / "traces.db",
        allowed_hosts=(),
    )

    captured = capsys.readouterr()
    assert "--allowed-host" in captured.err
    assert "http://0.0.0.0" not in captured.out


def test_health_does_not_read_the_database(database_path):
    with TraceStore(database_path) as store:
        app = create_app(store, host="127.0.0.1")
    client = TestClient(app, base_url="http://127.0.0.1:4318")

    response = client.get("/health")

    assert (response.status_code, response.text) == (200, "ok")


def access_record(path: str) -> logging.LogRecord:
    return logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        0,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:50000", "GET", path, "1.1", 200),
        None,
    )


@pytest.mark.parametrize(("path", "kept"), [("/health", False), ("/", True)])
def test_the_access_log_leaves_out_health_checks(path, kept):
    assert HealthCheckLogFilter().filter(access_record(path)) is kept


def test_serve_filters_the_access_log(tmp_path, uvicorn_calls):
    serve(
        host="127.0.0.1",
        port=4318,
        database_path=tmp_path / "traces.db",
        allowed_hosts=(),
    )

    (call,) = uvicorn_calls
    log_config = call["log_config"]
    assert isinstance(log_config, dict)
    [name] = log_config["loggers"]["uvicorn.access"]["filters"]
    assert log_config["filters"][name] == {"()": HealthCheckLogFilter}
