"""T3.2-T3.7, T3.10: real Claude reviews Park and Maria under rulebook v1. Marker: llm (costs money).

Each claim is reviewed once per module; the tests below assert different things about those two runs.
"""
from types import SimpleNamespace

import pytest

import agent
import dataparse
import db
import scorer
import seeding
import tools
from constants import OPEN_ENDED
from events import bus
from tools import ToolContext, call_tool

pytestmark = pytest.mark.llm
DATA = dataparse.DATA_DIR
MARIA, PARK = "HO-48213", "PK-20719"


def wipe():
    for name in db.COLLECTION_NAMES:
        db.get_collection(name).delete_many({})


def review(claim_id):
    seen = []
    unsubscribe = bus.subscribe(seen.append)
    try:
        result = agent.run_agent(claim_id)
    finally:
        unsubscribe()
    return SimpleNamespace(result=result, events=[e for e in seen if e["runId"] == result.runId],
                           score=scorer.score_run(result.runId))


@pytest.fixture(scope="module")
def reviews(vector_indexes):
    wipe()
    seeding.seed()
    out = {PARK: review(PARK), MARIA: review(MARIA)}
    yield out
    wipe()


def findings(claim_id):
    return {f["type"]: f for f in db.findings.find({"claimId": claim_id})}


def test_park_review_files_two_findings_no_labor(reviews):  # T3.2
    assert reviews[PARK].result.status == "completed", reviews[PARK].result
    f = findings(PARK)
    assert set(f) == {"missing_coverage", "unpaid_ale"}
    assert (f["missing_coverage"]["amount"], f["unpaid_ale"]["amount"]) == (6900, 2800)
    assert "labor_depreciation" not in f


def test_park_score_two_of_three(reviews):  # T3.3
    s = reviews[PARK].score
    assert (s["caughtCount"], s["expectedCount"]) == (2, 3)
    assert s["falsePositives"] == [] and s["missed"] == ["labor_depreciation"]
    assert s["exactAmountCount"] == 2 and s["rulebookVersion"] == 1
    assert s["citations"]["total"] > 0 and s["citations"]["verified"] == s["citations"]["total"]
    assert db.scores.find_one({"runId": reviews[PARK].result.runId})["missed"] == ["labor_depreciation"]


def test_maria_review_and_score(reviews):  # T3.4
    assert reviews[MARIA].result.status == "completed", reviews[MARIA].result
    f = findings(MARIA)
    assert set(f) == {"missing_coverage", "unpaid_ale"}
    assert (f["missing_coverage"]["amount"], f["unpaid_ale"]["amount"]) == (18700, 8400)
    assert db.documents.count_documents({"claimId": MARIA, "filename": "estimate_v3.txt"}) == 0  # held
    s = reviews[MARIA].score
    assert (s["caughtCount"], s["expectedCount"]) == (2, 3) and s["falsePositives"] == []
    assert s["exactAmountCount"] == 2 and s["missed"] == ["labor_depreciation"]


def test_maria_facts_estimate_v1_superseded_by_v2(reviews):  # T3.5
    facts = list(db.facts.find({"claimId": MARIA, "row": "estimate_total"}))
    v1 = [f for f in facts if f["sourceFilename"] == "estimate_v1.txt"]
    v2 = [f for f in facts if f["sourceFilename"] == "estimate_v2.txt"]
    assert v1 and v2, [(f["sourceFilename"], f["label"]) for f in facts]
    by_id = {f["_id"]: f for f in facts}
    for old in v1:
        new = by_id[old["supersededBy"]]
        assert new["sourceFilename"] == "estimate_v2.txt"
        assert old["validTo"] == new["validFrom"] < OPEN_ENDED
    for new in v2:
        assert new["validTo"] == OPEN_ENDED and new["supersededBy"] is None


def test_maria_decisions_one_per_estimate_and_replay(reviews):  # T3.6
    decisions = list(db.decisions.find({"claimId": MARIA}))
    assert sorted(d["filename"] for d in decisions) == ["estimate_v1.txt", "estimate_v2.txt"]
    for d in decisions:
        header = dataparse.parse_estimate(DATA / MARIA / d["filename"]).relied_on
        assert sorted(d["citedFilenames"]) == sorted(header), d["filename"]
    v2 = next(d for d in decisions if d["filename"] == "estimate_v2.txt")
    ctx = ToolContext(MARIA, reviews[MARIA].result.runId)
    r = call_tool(ctx, "replay", {"decisionId": v2["_id"]})
    assert "endorsement_code_upgrade.txt" in {d["filename"] for d in r["not_cited"]}
    assert "endorsement_code_upgrade.txt" not in {d["filename"] for d in r["cited"]}


def test_completed_run_log_is_complete(reviews):  # T3.7
    run = db.agent_runs.find_one({"runId": reviews[MARIA].result.runId})
    assert run["status"] == "completed" and run["summary"]
    calls = run["toolCalls"]
    assert calls and [c["seq"] for c in calls] == list(range(1, len(calls) + 1))
    uses = [b for m in run["messages"] if m["role"] == "assistant" and isinstance(m["content"], list)
            for b in m["content"] if b["type"] == "tool_use"]
    assert len(uses) == len(calls)
    results = {b["tool_use_id"] for m in run["messages"] if m["role"] == "user" and isinstance(m["content"], list)
               for b in m["content"] if b["type"] == "tool_result"}
    for c in calls:
        assert c["name"] in {t.name for t in tools.TOOLS} and isinstance(c["input"], dict)
        assert isinstance(c["output"], dict) and c["durationMs"] >= 0 and c["startedAt"].tzinfo
        assert c["toolUseId"] in results
    assert run["stepCount"] == max(c["step"] for c in calls) + 1  # the last step is the summary turn
    assert {c["name"] for c in calls} >= {"get_claim_state", "get_rules", "read_document", "compute_amount",
                                          "upsert_finding"}
    assert run["usage"]["inputTokens"] > 0 and run["usage"]["outputTokens"] > 0


def test_event_bus_streams_steps_in_order(reviews):  # T3.10
    run = db.agent_runs.find_one({"runId": reviews[MARIA].result.runId})
    ev = reviews[MARIA].events
    assert ev[0]["type"] == "run_started" and ev[-1]["type"] == "run_finished"
    tool_events = [e for e in ev if e["type"] == "tool_call"]
    assert len(tool_events) == len(run["toolCalls"])  # one event per tool call
    assert [e["seq"] for e in tool_events] == list(range(1, len(tool_events) + 1))
    assert [e["name"] for e in tool_events] == [c["name"] for c in run["toolCalls"]]
    assert [e["step"] for e in tool_events] == sorted(e["step"] for e in tool_events)
    positions = {e["step"]: i for i, e in enumerate(ev) if e["type"] == "assistant_message"}
    for e in tool_events:  # each call arrives after its step's assistant message, before the next step's
        i = ev.index(e)
        assert positions[e["step"]] < i and (e["step"] + 1 not in positions or i < positions[e["step"] + 1])
