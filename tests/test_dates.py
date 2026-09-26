from datetime import datetime, timezone

import pytest

import db
from constants import OPEN_ENDED


@pytest.mark.parametrize("coll,field", [
    ("documents", "receivedAt"), ("facts", "validFrom"), ("facts", "validTo"),
    ("policy_clauses", "effectiveFrom"), ("policy_clauses", "effectiveTo"),
])
def test_string_date_rejected(coll, field):
    with pytest.raises(TypeError):
        db.prepare_doc(coll, {field: "2026-06-27"})


def test_naive_datetime_rejected():
    with pytest.raises(TypeError):
        db.prepare_doc("facts", {"validFrom": datetime(2026, 6, 27)})


def test_string_date_never_reaches_driver(monkeypatch):
    called = []
    monkeypatch.setattr(db, "get_collection", lambda n: called.append(n))
    with pytest.raises(TypeError):
        db.insert_one("policy_clauses", {"effectiveFrom": "2026-06-27"})
    assert called == []


def test_open_ended_defaults():
    assert db.prepare_doc("policy_clauses", {})["effectiveTo"] == OPEN_ENDED
    assert db.prepare_doc("facts", {"validTo": None})["validTo"] == OPEN_ENDED
    assert "validTo" not in db.prepare_doc("documents", {})


def test_aware_datetime_accepted():
    d = datetime(2026, 6, 27, tzinfo=timezone.utc)
    assert db.prepare_doc("facts", {"validFrom": d})["validFrom"] == d
