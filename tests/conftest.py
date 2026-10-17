import os

import pytest
from fastapi.testclient import TestClient


# server.py validates the upstream key at import time. Tests never call the
# upstream image API, so a sentinel value is sufficient and keeps CI secretless.
os.environ.setdefault("API_KEY", "test-api-key")
os.environ.setdefault("API_BASE", "https://provider.example/v1")
os.environ.pop("DATABASE_URL", None)

import db  # noqa: E402
import server  # noqa: E402


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(db, "migrate_from_json", lambda: None)
    monkeypatch.setattr(db, "cleanup_sessions", lambda: None)
    monkeypatch.setattr(server, "ACCOUNTS_ENABLED", False)
    monkeypatch.setattr(server, "AUTH_REQUIRED", False)
    with TestClient(server.app) as test_client:
        yield test_client
