"""T4.1, T4.2, T4.5-T4.10: the whole self-improvement story with real Claude. Marker: llm (costs money).

One flow per module: review Park on v1 -> improve (real proposer, real candidate re-run) -> review Maria on
the promoted v2 -> improve again on the now-perfect run.
"""
import json
from types import SimpleNamespace

import anthropic
import pytest

import agent
import config
import db
import improve
import rulebook
import scorer
import seeding
from constants import OPEN_ENDED  # noqa: F401
from events import bus

pytestmark = pytest.mark.llm
PARK, MARIA, INSURER = "PK-20719", "HO-48213", "Harborline Mutual"
IMPROVEMENT = ("miss_detected", "rule_proposed", "candidate_scored", "rule_promoted", "rule_rejected", "no_change")


def wipe():
    for name in db.COLLECTION_NAMES:
        db.get_collection(name).delete_many({})


class Recorder:
    """Wraps a real client and keeps every request it sends."""

    def __init__(self, real):
        self.real, self.calls, self.messages = real, [], self

    def create(self, **kw):
        self.calls.append(kw)
        return self.real.messages.create(**kw)


@pytest.fixture(scope="module")
def flow(vector_indexes):
    wipe()
    seeding.seed()
    seen = []
    unsubscribe = bus.subscribe(seen.append)
    recorder = Recorder(anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY))
    f = SimpleNamespace(recorder=recorder)
    f.park_v1 = agent.run_agent(PARK)
    f.park_v1_score = scorer.score_run(f.park_v1.runId)
    seen_before_improve = len(seen)
    f.improved = improve.improve_rulebook(PARK, f.park_v1.runId, proposer=improve.claude_proposer(recorder))
    f.improvement_events = [e for e in seen[seen_before_improve:] if e["type"] in IMPROVEMENT]
    f.maria = agent.run_agent(MARIA)
    f.maria_score = scorer.score_run(f.maria.runId)

    def must_not_be_called(report, context):
        raise AssertionError("the proposer must not be called when nothing was missed")
    seen_before_again = len(seen)
    f.again = improve.improve_rulebook(PARK, f.improved["candidateRunId"], proposer=must_not_be_called)
    f.again_events = [e for e in seen[seen_before_again:] if e["type"] in IMPROVEMENT]
    unsubscribe()
    yield f
    wipe()


def test_proposes_labor_depreciation_rule(flow):  # T4.1
    assert flow.park_v1_score["caughtCount"] == 2 and flow.park_v1_score["missed"] == ["labor_depreciation"]
    p = flow.improved["proposal"]
    assert p["type"] == "labor_depreciation" and p["computeRule"] == "labor_depreciation_refund"
    assert 20 <= len(p["instruction"]) <= 500
    print("\nproposed instruction:", p["instruction"])


def test_candidate_rerun_gets_three_of_three_and_is_promoted(flow):  # T4.2
    assert flow.improved["decision"] == "promoted", flow.improved["reason"]
    after = flow.improved["scoreAfter"]
    assert (after["caughtCount"], after["expectedCount"], after["falsePositives"]) == (3, 3, [])
    assert after["amounts"]["labor_depreciation"] == {"found": 4200, "exact": True}
    assert after["exactAmountCount"] == 3
    assert rulebook.active_version(INSURER) == 2
    assert rulebook.header(INSURER, 1)["status"] == "inactive"
    assert {r["type"] for r in rulebook.rules_for(INSURER, 1)} == {"missing_coverage", "unpaid_ale"}  # history kept


def test_maria_gets_three_findings_on_v2(flow):  # T4.5
    assert flow.maria.status == "completed", flow.maria
    f = {x["type"]: x for x in db.findings.find({"claimId": MARIA})}
    assert {t: x["amount"] for t, x in f.items()} == {
        "missing_coverage": 18700, "unpaid_ale": 8400, "labor_depreciation": 11300}
    calc = next(c for c in db.agent_runs.find_one({"runId": flow.maria.runId})["calcs"]
                if c["calcId"] == f["labor_depreciation"]["calcId"])
    assert calc["details"]["lineCount"] == 41
    assert db.documents.count_documents({"claimId": MARIA, "filename": "estimate_v3.txt"}) == 0  # still held
    s = flow.maria_score
    assert (s["caughtCount"], s["expectedCount"], s["falsePositives"], s["exactAmountCount"]) == (3, 3, [], 3)


def test_provenance_on_v2(flow):  # T4.6
    h = rulebook.header(INSURER, 2)
    assert h["status"] == "active" and h["decision"] == "promoted" and h["reason"]
    assert h["proposedFromRunId"] == flow.park_v1.runId and h["candidateRunId"] == flow.improved["candidateRunId"]
    assert (h["scoreBefore"]["caughtCount"], h["scoreBefore"]["expectedCount"]) == (2, 3)
    assert (h["scoreAfter"]["caughtCount"], h["scoreAfter"]["expectedCount"]) == (3, 3)
    assert h["proposal"]["type"] == "labor_depreciation" and h["baseVersion"] == 1
    assert h["createdAt"] <= h["scoredAt"] <= h["decidedAt"] and h["decidedAt"].tzinfo is not None


def test_proposer_received_only_parks_miss_report(flow):  # T4.7
    calls = flow.recorder.calls
    assert len(calls) == 1  # the proposer's single call (the recorder wraps only the proposer)
    payload = json.loads(calls[0]["messages"][0]["content"])
    assert set(payload) == {"missReport", "allowedTypes", "computeRules", "currentRules"}
    assert [m["type"] for m in payload["missReport"]] == ["labor_depreciation"]
    assert payload["missReport"][0]["expectedAmount"] == 4200
    sent = json.dumps(calls[0], default=str)
    for leak in (MARIA, "Maria Alvarez", "11300", "18700", "8400", "Copperleaf", "estimate_v3"):
        assert leak not in sent, leak
    assert calls[0]["system"] == improve.PROPOSER_SYSTEM


def test_improve_again_is_no_change(flow):  # T4.8
    assert flow.again["decision"] == "no_change" and "no change" in flow.again["reason"]
    assert [e["type"] for e in flow.again_events] == ["no_change"]
    assert rulebook.versions(INSURER) == [1, 2] and set(rulebook.headers(INSURER)) == {1, 2}
    assert rulebook.active_version(INSURER) == 2


def test_improvement_events_arrive_in_order(flow):  # T4.9
    assert [e["type"] for e in flow.improvement_events] == [
        "miss_detected", "rule_proposed", "candidate_scored", "rule_promoted"]
    stamps = [e["ts"] for e in flow.improvement_events]
    assert stamps == sorted(stamps)
    miss, proposed, scored, promoted = flow.improvement_events
    assert miss["missReport"][0]["type"] == "labor_depreciation"
    assert proposed["proposal"]["computeRule"] == "labor_depreciation_refund"
    assert scored["scoreAfter"]["caughtCount"] == 3 and promoted["version"] == 2


def test_each_run_recorded_its_rulebook_version(flow):  # T4.10
    version = lambda run_id: db.agent_runs.find_one({"runId": run_id})["rulebookVersion"]
    assert version(flow.park_v1.runId) == 1
    assert version(flow.improved["candidateRunId"]) == 2
    assert version(flow.maria.runId) == 2
    assert db.scores.find_one({"runId": flow.maria.runId})["rulebookVersion"] == 2
