import pytest

import server


def test_auth_identifier_is_stable_and_redacted(monkeypatch):
    monkeypatch.setattr(server, "SESSION_SECRET", "unit-test-secret")
    first = server._auth_identifier("login", "User@Example.com")
    second = server._auth_identifier("login", " user@example.com ")

    assert first == second
    assert "user@example.com" not in first
    assert len(first) == 64


def test_register_rate_limit_blocks_before_account_creation(client, monkeypatch):
    monkeypatch.setattr(server, "ACCOUNTS_ENABLED", True)
    monkeypatch.setattr(server, "REGISTRATION_ENABLED", True)
    monkeypatch.setattr(server, "_consume_account_auth_attempt", lambda *args: False)
    created = []
    monkeypatch.setattr(server.userdb, "create_user", lambda *args: created.append(args))

    response = client.post(
        "/api/register",
        json={"email": "new@example.com", "password": "secret1"},
    )

    assert response.status_code == 429
    assert created == []


def test_register_requires_turnstile_when_configured(client, monkeypatch):
    monkeypatch.setattr(server, "ACCOUNTS_ENABLED", True)
    monkeypatch.setattr(server, "REGISTRATION_ENABLED", True)
    monkeypatch.setattr(server, "_consume_account_auth_attempt", lambda *args: True)
    monkeypatch.setattr(server, "_verify_turnstile", lambda token, ip: False)

    response = client.post(
        "/api/register",
        json={"email": "new@example.com", "password": "secret1"},
    )

    assert response.status_code == 400
    assert response.json()["error"] == "人机验证失败，请重试"


def test_registration_is_closed_by_default(client, monkeypatch):
    monkeypatch.setattr(server, "ACCOUNTS_ENABLED", True)
    monkeypatch.setattr(server, "REGISTRATION_ENABLED", False)

    response = client.post(
        "/api/register",
        json={"email": "new@example.com", "password": "secret1"},
    )

    assert response.status_code == 403
    assert response.json()["error"] == "暂未开放注册"


def test_successful_login_does_not_consume_failure_budget(client, monkeypatch):
    monkeypatch.setattr(server, "ACCOUNTS_ENABLED", True)
    monkeypatch.setattr(server, "AUTH_REQUIRED", True)
    monkeypatch.setattr(server, "_account_auth_attempt_allowed", lambda *args: True)
    monkeypatch.setattr(server, "_verify_credentials", lambda username, password: "admin")
    consumed = []
    cleared = []
    monkeypatch.setattr(
        server,
        "_consume_account_auth_attempt",
        lambda *args: consumed.append(args) or True,
    )
    monkeypatch.setattr(
        server,
        "_clear_login_account_failures",
        lambda account: cleared.append(account),
    )

    response = client.post(
        "/api/login",
        json={"username": "admin", "password": "correct"},
    )

    assert response.status_code == 200
    assert consumed == []
    assert cleared == ["admin"]


def test_failed_login_records_failure(client, monkeypatch):
    monkeypatch.setattr(server, "ACCOUNTS_ENABLED", True)
    monkeypatch.setattr(server, "AUTH_REQUIRED", True)
    monkeypatch.setattr(server, "_account_auth_attempt_allowed", lambda *args: True)
    monkeypatch.setattr(server, "_verify_credentials", lambda *args: None)
    monkeypatch.setattr(server.userdb, "verify_login", lambda *args: None)
    failures = []
    monkeypatch.setattr(
        server,
        "_record_login_failure",
        lambda ip, account: failures.append((ip, account)),
    )

    response = client.post(
        "/api/login",
        json={"email": "user@example.com", "password": "wrong"},
    )

    assert response.status_code == 401
    assert failures == [("testclient", "user@example.com")]


def test_async_account_job_reserves_credit_atomically(monkeypatch):
    params = {"charge": {"kind": "credits", "user_id": 7, "amount": 1}}
    calls = []

    def consume(job_id, user_id, amount):
        calls.append((job_id, user_id, amount))
        return 4

    monkeypatch.setattr(
        server.userdb,
        "consume_credit_and_create_generation_job",
        consume,
    )

    server._create_async_job("job-1", params)

    assert calls == [("job-1", 7, 1)]
    assert params["charge"]["job_id"] == "job-1"
    assert params["charge"]["remaining"] == 4


def test_async_account_job_rejects_insufficient_credit(monkeypatch):
    params = {"charge": {"kind": "credits", "user_id": 7, "amount": 1}}
    monkeypatch.setattr(
        server.userdb,
        "consume_credit_and_create_generation_job",
        lambda *args: None,
    )

    with pytest.raises(server.InsufficientCredits):
        server._create_async_job("job-2", params)


def test_account_job_lookup_is_scoped_to_owner(monkeypatch):
    seen = []
    monkeypatch.setattr(server, "_get_account_user_id", lambda request: 42)
    monkeypatch.setattr(
        server.userdb,
        "get_generation_job",
        lambda job_id, user_id: seen.append((job_id, user_id)) or {"status": "pending"},
    )

    job = server._get_async_job("job-owned", object())

    assert job["status"] == "pending"
    assert seen == [("job-owned", 42)]
