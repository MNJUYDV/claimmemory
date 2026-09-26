import pytest
from fastapi.testclient import TestClient

import config
import db
from main import app

client = TestClient(app)


@pytest.mark.integration
def test_health_ok():  # T0.4
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["db"] == "claimmemory_test"
    assert len(body["collections"]) == 11
    assert set(body["collections"]) == set(db.COLLECTION_NAMES)


def test_health_unreachable(monkeypatch):  # T0.5
    secret_uri = "mongodb://user:" + "hunter2" + "@localhost:1/?serverSelectionTimeoutMS=500"
    monkeypatch.setattr(config, "MONGODB_URI", secret_uri)
    db.get_client.cache_clear()
    try:
        r = client.get("/health")
    finally:
        db.get_client.cache_clear()
    assert r.status_code == 503
    assert r.json() == {"ok": False, "error": "database unreachable"}
    assert "hunter2" not in r.text and "Traceback" not in r.text
