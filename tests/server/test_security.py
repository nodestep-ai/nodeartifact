import pytest
from starlette.testclient import TestClient

from nodeartifact.server.app import create_app
from nodeartifact.server.security import (
    AllowedHost,
    AllowedHostError,
    HostCheckMiddleware,
)


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("127.0.0.1:4318", "127.0.0.1"),
        ("localhost", "localhost"),
        ("[::1]:4318", "::1"),
        ("::1", "::1"),
        ("Example.COM:80", "example.com"),
        ("evil.example@127.0.0.1:4318", "evil.example@127.0.0.1"),
        ("", ""),
    ],
)
def test_hostname_drops_the_port_and_brackets(header, expected):
    assert HostCheckMiddleware.hostname(header) == expected


@pytest.mark.parametrize(
    ("bound", "host", "allowed"),
    [
        ("127.0.0.1", "127.0.0.1:4318", True),
        ("127.0.0.1", "127.0.0.1", True),
        ("127.0.0.1", "localhost:4318", True),
        ("127.0.0.1", "[::1]:4318", True),
        ("127.0.0.1", "evil.example:4318", False),
        ("127.0.0.1", "127.0.0.1.nip.io:4318", False),
        ("127.0.0.1", "evil.example@127.0.0.1:4318", False),
        ("::1", "[::1]:4318", True),
        ("::1", "localhost:4318", True),
        ("192.168.1.5", "192.168.1.5:4318", True),
        ("192.168.1.5", "localhost:4318", False),
        ("traces.internal", "TRACES.internal:4318", True),
    ],
)
def test_host_header_must_name_the_bound_host(store, bound, host, allowed):
    client = TestClient(create_app(store, host=bound))

    response = client.post(
        "/v1/traces",
        content=b"",
        headers={"host": host, "content-type": "application/x-protobuf"},
    )

    if allowed:
        assert response.status_code == 200
    else:
        assert response.status_code == 400
        assert response.text == "Host header does not match the bound host"


def post_with_host(store, host: str, allowed_hosts: list[str]) -> int:
    client = TestClient(create_app(store, host="0.0.0.0", allowed_hosts=allowed_hosts))
    return client.post(
        "/v1/traces",
        content=b"",
        headers={"host": host, "content-type": "application/x-protobuf"},
    ).status_code


@pytest.mark.parametrize(
    ("host", "status"),
    [
        ("localhost:4318", 200),
        ("LOCALHOST:4318", 200),
        ("nodeartifact:4318", 200),
        ("localhost:8080", 400),
        ("localhost", 400),
        ("127.0.0.1:4318", 400),
        ("0.0.0.0:4318", 200),
        ("evil.example:4318", 400),
        ("nodeartifact.evil.example:4318", 400),
    ],
)
def test_allowed_hosts_with_a_port_must_match_host_and_port(store, host, status):
    assert (
        post_with_host(store, host, ["localhost:4318", "nodeartifact:4318"]) == status
    )


@pytest.mark.parametrize(
    ("host", "status"),
    [("traces.internal", 200), ("traces.internal:9000", 200), ("other:9000", 400)],
)
def test_allowed_host_without_a_port_matches_any_port(store, host, status):
    assert post_with_host(store, host, ["traces.internal"]) == status


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("localhost", 80),
        ("[::1]", 80),
        ("localhost:4318", 4318),
        ("[::1]:4318", 4318),
    ],
)
def test_a_host_header_without_a_port_names_port_80(header, expected):
    assert HostCheckMiddleware.port(header) == expected


@pytest.mark.parametrize(
    ("host", "status"),
    [("localhost", 200), ("localhost:80", 200), ("localhost:4318", 400)],
)
def test_allowed_host_with_port_80_matches_a_header_without_a_port(store, host, status):
    assert post_with_host(store, host, ["localhost:80"]) == status


@pytest.mark.parametrize("host", ["evil.example", "localhost:8080", ""])
def test_health_answers_whatever_the_host_header(store, host):
    client = TestClient(create_app(store, host="0.0.0.0", allowed_hosts=[]))

    health = client.get("/health", headers={"host": host})
    trace_list = client.get("/", headers={"host": host})

    assert (health.status_code, health.text) == (200, "ok")
    assert trace_list.status_code == 400


def test_allowed_hosts_extend_a_loopback_bind(store):
    client = TestClient(
        create_app(store, host="127.0.0.1", allowed_hosts=["nodeartifact:4318"])
    )

    for host in ("localhost:4318", "nodeartifact:4318"):
        response = client.post(
            "/v1/traces",
            content=b"",
            headers={"host": host, "content-type": "application/x-protobuf"},
        )
        assert response.status_code == 200


@pytest.mark.parametrize(
    ("value", "parsed"),
    [
        ("localhost:4318", AllowedHost(hostname="localhost", port=4318)),
        ("Nodeartifact", AllowedHost(hostname="nodeartifact", port=None)),
        ("[::1]:4318", AllowedHost(hostname="::1", port=4318)),
        ("[::1]", AllowedHost(hostname="::1", port=None)),
        ("192.168.1.5:80", AllowedHost(hostname="192.168.1.5", port=80)),
    ],
)
def test_allowed_host_parses_a_host_and_an_optional_port(value, parsed):
    assert AllowedHost.parse(value) == parsed


@pytest.mark.parametrize(
    "value",
    [
        "",
        "*",
        "*.example.com",
        "http://nodeartifact:4318",
        "nodeartifact:",
        "nodeartifact:0",
        "nodeartifact:65536",
        "nodeartifact:abc",
        "::1",
        "user@nodeartifact",
        "node trace",
        "nodeartifact/x",
    ],
)
def test_allowed_host_rejects_anything_but_a_host_and_port(value):
    with pytest.raises(AllowedHostError):
        AllowedHost.parse(value)
