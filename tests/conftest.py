from collections.abc import Iterator
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from nodeartifact.server.app import create_app
from nodeartifact.server.storage import TraceStore

BASE_URL = "http://127.0.0.1:4318"


@pytest.fixture
def database_path(tmp_path: Path) -> Path:
    return tmp_path / "traces.db"


@pytest.fixture
def store(database_path: Path) -> Iterator[TraceStore]:
    with TraceStore(database_path) as trace_store:
        yield trace_store


@pytest.fixture
def client(store: TraceStore) -> Iterator[TestClient]:
    with TestClient(
        create_app(store, host="127.0.0.1"), base_url=BASE_URL
    ) as test_client:
        yield test_client
