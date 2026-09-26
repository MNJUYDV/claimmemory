import random
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

import config
import db
from constants import OPEN_ENDED

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parent.parent


def vec(seed=0):
    rnd = random.Random(seed)
    return [rnd.uniform(-1, 1) for _ in range(1024)]


def search(coll, index, query, flt, limit=5, wait_for_hit=False):
    pipeline = [
        {"$vectorSearch": {"index": index, "path": "embedding", "queryVector": query,
                           "numCandidates": 50, "limit": limit, "filter": flt}},
        {"$project": {"_id": 1, "claimId": 1, "score": {"$meta": "vectorSearchScore"}}},
    ]
    deadline = time.monotonic() + (90 if wait_for_hit else 0)
    while True:
        res = list(db.get_collection(coll).aggregate(pipeline))
        if res or time.monotonic() >= deadline:
            return res
        time.sleep(3)


def test_create_indexes_twice_and_inspect(vector_indexes):  # T0.6, T0.7
    for _ in range(2):
        r = subprocess.run([sys.executable, "scripts/create_indexes.py", "--db", "claimmemory_test"],
                           cwd=ROOT, capture_output=True, text=True)
        assert r.returncode == 0, r.stdout + r.stderr
    expected = {
        ("documents", "doc_vectors"): {"claimId", "type"},
        ("policy_clauses", "clause_vectors"): {"claimId", "effectiveFrom", "effectiveTo"},
    }
    for (coll, name), filters in expected.items():
        idxs = list(db.get_collection(coll).list_search_indexes())
        assert [i["name"] for i in idxs].count(name) == 1
        idx = next(i for i in idxs if i["name"] == name)
        assert idx["status"] == "READY"
        fields = idx["latestDefinition"]["fields"]
        v = next(f for f in fields if f["type"] == "vector")
        assert (v["numDimensions"], v["similarity"], v["path"]) == (1024, "cosine", "embedding")
        assert {f["path"] for f in fields if f["type"] == "filter"} == filters
    names = [i["name"] for i in db.documents.list_indexes()]
    assert "claimId_1" in names and "receivedAt_1" in names


def test_vector_filter_by_claim(vector_indexes):  # T0.8, T0.9
    v = vec(1)
    db.documents.insert_one({"claimId": "T1", "type": "note", "embedding": v,
                             "receivedAt": datetime.now(timezone.utc)})
    hits = search("documents", "doc_vectors", v, {"claimId": "T1"}, wait_for_hit=True)
    assert hits and hits[0]["claimId"] == "T1"
    assert search("documents", "doc_vectors", v, {"claimId": "T2"}) == []


def test_clause_effective_dates(vector_indexes):  # T0.10
    v = vec(2)
    db.policy_clauses.insert_one({
        "claimId": "T1", "embedding": v,
        "effectiveFrom": datetime(2026, 6, 27, tzinfo=timezone.utc),
        "effectiveTo": OPEN_ENDED,
    })

    def as_of(d):
        dt = datetime.fromisoformat(d).replace(tzinfo=timezone.utc)
        return {"$and": [{"claimId": "T1"}, {"effectiveFrom": {"$lte": dt}},
                         {"effectiveTo": {"$gte": dt}}]}

    # Wait until the clause is indexed, then check the earlier date is excluded.
    assert search("policy_clauses", "clause_vectors", v, as_of("2026-07-18"), wait_for_hit=True)
    assert search("policy_clauses", "clause_vectors", v, as_of("2026-06-01")) == []


def test_voyage_embedding():  # T0.11
    import voyageai
    out = voyageai.Client(api_key=config.VOYAGE_API_KEY).embed(["test"], model=config.VOYAGE_MODEL)
    assert len(out.embeddings[0]) == 1024


def test_claude_tool_use():  # T0.12
    import anthropic
    tool = {"name": "add", "description": "Add two integers.",
            "input_schema": {"type": "object",
                             "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
                             "required": ["a", "b"]}}
    msg = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY).messages.create(
        model=config.CLAUDE_MODEL, max_tokens=256, tools=[tool],
        messages=[{"role": "user", "content": "add 2 and 3"}])
    block = next(b for b in msg.content if b.type == "tool_use")
    assert block.name == "add" and block.input == {"a": 2, "b": 3}
