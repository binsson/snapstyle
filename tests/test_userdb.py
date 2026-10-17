import userdb


class _Result:
    def __init__(self, one=None, many=None):
        self._one = one
        self._many = many or []

    def fetchone(self):
        return self._one

    def fetchall(self):
        return self._many


class _Connection:
    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql, params=()):
        normalized = " ".join(sql.split())
        self.calls.append((normalized, params))
        return self.handler(normalized, params)


def test_credit_deduction_and_job_insert_share_one_connection(monkeypatch):
    def handler(sql, _params):
        if sql.startswith("UPDATE users SET credits = credits -"):
            return _Result({"credits": 4})
        return _Result()

    conn = _Connection(handler)
    monkeypatch.setattr(userdb, "_connect", lambda: conn)
    monkeypatch.setattr(userdb, "recover_stale_generation_jobs", lambda **kwargs: 0)

    remaining = userdb.consume_credit_and_create_generation_job("job-1", 9, 1)

    assert remaining == 4
    statements = [sql for sql, _ in conn.calls]
    assert any(sql.startswith("UPDATE users SET credits = credits -") for sql in statements)
    assert any(sql.startswith("INSERT INTO generation_jobs") for sql in statements)
    assert any("generate:job-1" in params for _, params in conn.calls if params)


def test_insufficient_credit_does_not_create_job(monkeypatch):
    def handler(sql, _params):
        if sql.startswith("UPDATE users SET credits = credits -"):
            return _Result(None)
        return _Result()

    conn = _Connection(handler)
    monkeypatch.setattr(userdb, "_connect", lambda: conn)
    monkeypatch.setattr(userdb, "recover_stale_generation_jobs", lambda **kwargs: 0)

    remaining = userdb.consume_credit_and_create_generation_job("job-2", 9, 1)

    assert remaining is None
    assert not any(sql.startswith("INSERT INTO generation_jobs") for sql, _ in conn.calls)
