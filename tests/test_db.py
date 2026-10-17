import db


def test_sqlite_generation_job_lifecycle(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "jobs.db"))
    db.init_db()

    db.create_gen_job("job-1")
    assert db.get_gen_job("job-1")["status"] == "pending"

    db.finish_gen_job("job-1", image="base64-image", brief="soft daylight")
    completed = db.get_gen_job("job-1")
    assert completed["status"] == "done"
    assert completed["image"] == "base64-image"
    assert completed["brief"] == "soft daylight"
