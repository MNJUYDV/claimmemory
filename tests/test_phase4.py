"""Phase 4 tests that need no real LLM: proposal validation, the promotion decision, rulebook versions, and the
improvement loop driven by a scripted proposer and scripted review agents.
Real-Claude tests (T4.1, T4.2, T4.5-T4.10) are in test_phase4_llm.py (marker: llm)."""
import json
from types import SimpleNamespace

import pytest

import agent
import db
import events
import improve
import rulebook
import runs
import scorer
import seeding
from tests.test_phase2_tools import MARIA, PARK, fake_embed, integ, ok, seeded  # noqa: F401
from tests.test_phase3 import reply, say, use
from tools import call_tool

MISSED = {"labor_depreciation"}
GOOD = {"type": "labor_depreciation", "computeRule": "labor_depreciation_refund",
        "instruction": "Compare the labor depreciation applied in each estimate with the policy's labor clause "
                       "and report labor that was depreciated."}
INSURER = "Harborline Mutual"


# ---------- proposal validation (offline) ----------

def test_valid_proposal_accepted_as_dict_or_json_string():
    for raw in (GOOD, json.dumps(GOOD)):
        rule, problem = improve.validate_proposal(raw, MISSED)
        assert problem is None and rule["type"] == "labor_depreciation"


@pytest.mark.parametrize("raw,fragment", [
    ({**GOOD, "type": "made_up"}, "unknown finding type"),
    ({**GOOD, "computeRule": "made_up_rule"}, "unknown computeRule"),
    ({**GOOD, "computeRule": "unpaid_ale"}, "does not compute"),
    ({**GOOD, "type": "unpaid_ale", "computeRule": "unpaid_ale"}, "not a missed type"),
    ({**GOOD, "instruction": "x" * 19}, "characters"),
    ({**GOOD, "instruction": "x" * 501}, "characters"),
    ({**GOOD, "instruction": ""}, "characters"),
    ({**GOOD, "instruction": 42}, "characters"),
    ({**GOOD, "instruction": "Refund the $4,200 that was depreciated from labor on the estimate."}, "general"),
    ({**GOOD, "instruction": "On claim PK-20719 look for labor that was depreciated in estimates."}, "general"),
    ({"type": "labor_depreciation", "computeRule": "labor_depreciation_refund"}, "exactly"),
    ({**GOOD, "extra": 1}, "exactly"),
    ("not json at all", "not valid JSON"),
    ("[1, 2]", "not a JSON object"),
    (None, "not a JSON object"),
])
def test_invalid_proposals_rejected(raw, fragment):
    rule, problem = improve.validate_proposal(raw, MISSED)
    assert rule is None and fragment in problem


def test_instruction_length_boundaries_accepted():
    for n in (20, 500):
        rule, problem = improve.validate_proposal({**GOOD, "instruction": "x" * n}, MISSED)
        assert problem is None and len(rule["instruction"]) == n


# ---------- promotion decision (offline) ----------

def score(caught, expected=("labor_depreciation", "missing_coverage", "unpaid_ale"), fps=(), exact=True):
    missed = sorted(set(expected) - set(caught))
    return {"runId": "r", "caughtCount": len(caught), "expectedCount": len(expected), "caught": sorted(caught),
            "missed": missed, "falsePositives": list(fps), "exactAmountCount": len(caught) if exact else 0,
            "amounts": {t: {"found": 1.0, "exact": exact} for t in caught}}


AC = ["missing_coverage", "unpaid_ale"]


@pytest.mark.parametrize("before,after,promote,fragment", [
    (score(AC), score(AC + ["labor_depreciation"]), True, "gained labor_depreciation"),
    (score(AC), score(AC), False, "did not increase"),
    (score(AC), score(["missing_coverage", "labor_depreciation"]), False, "did not increase"),  # 2 -> 2, lost ale
    (score(AC), score(["labor_depreciation", "missing_coverage", "unpaid_ale"], fps=["x"]), False,
     "false positives increased"),
    (score(["missing_coverage"]), score(["labor_depreciation", "unpaid_ale"]), False, "lost previously caught"),
    (score(AC), score(AC + ["labor_depreciation"], exact=False), False, "not exact"),
    (score(AC, fps=["x"]), score(AC + ["labor_depreciation"], fps=["x"]), True, "gained"),  # not increased
    (score(AC, fps=["x", "y"]), score(AC + ["labor_depreciation"], fps=["x"]), True, "gained"),  # decreased
])
def test_decide(before, after, promote, fragment):
    ok_, reason = improve.decide(before, after)
    assert ok_ is promote and fragment in reason


# ---------- rulebook versions ----------

@integ
def test_versions_and_status(seeded):
    assert rulebook.versions(INSURER) == [1] and rulebook.active_version(INSURER) == 1
    v = rulebook.create_candidate(INSURER, 1, {"type": "labor_depreciation", "instruction": "i" * 30,
                                                "computeRule": "labor_depreciation_refund"}, {})
    assert v == 2 and rulebook.header(INSURER, 2)["status"] == "candidate"
    assert rulebook.active_version(INSURER) == 1  # a candidate is not active
    assert {r["type"] for r in rulebook.rules_for(INSURER, 2)} == {"labor_depreciation", "missing_coverage", "unpaid_ale"}
    assert {r["type"] for r in rulebook.rules_for(INSURER, 1)} == {"missing_coverage", "unpaid_ale"}  # v1 untouched
    added = lambda v: {r["id"]: r["addedInVersion"] for r in rulebook.rules_for(INSURER, v)}
    assert added(1) == {"HM-MC-001": 1, "HM-ALE-001": 1}  # seeded v1 rules
    assert added(2) == {"HM-MC-001": 1, "HM-ALE-001": 1, "labor_depreciation-v2": 2}  # copies keep 1; the new rule is 2
    rulebook.promote(INSURER, 2, decision="promoted")
    assert rulebook.active_version(INSURER) == 2
    assert rulebook.header(INSURER, 1)["status"] == "inactive" and rulebook.header(INSURER, 1)["supersededByVersion"] == 2
    assert len(rulebook.rules_for(INSURER, 1)) == 2  # kept as history


@integ
def test_stale_candidate_is_abandoned_when_a_new_one_is_created(seeded):
    rule = {"type": "labor_depreciation", "instruction": "i" * 30, "computeRule": "labor_depreciation_refund"}
    rulebook.create_candidate(INSURER, 1, rule, {})
    assert rulebook.create_candidate(INSURER, 1, rule, {}) == 3
    assert rulebook.header(INSURER, 2)["status"] == "rejected" and rulebook.header(INSURER, 3)["status"] == "candidate"


@integ
def test_run_pins_the_version_active_at_start(seeded, fake_embed):
    import ingest
    ingest.ingest_claim(PARK)
    first = runs.start_run(PARK)
    rule = {"type": "labor_depreciation", "instruction": "i" * 30, "computeRule": "labor_depreciation_refund"}
    v = rulebook.create_candidate(INSURER, 1, rule, {})
    candidate_run = runs.start_run(PARK, v)
    assert ok(call_tool(candidate_run, "get_rules", {}))["version"] == 2  # pinned to the candidate...
    assert rulebook.active_version(INSURER) == 1                            # ...while v1 stays active
    rulebook.promote(INSURER, v)
    after_run = runs.start_run(PARK)
    assert [r["rulebookVersion"] for r in db.agent_runs.find().sort("startedAt", 1)] == [1, 2, 2]
    assert ok(call_tool(first, "get_rules", {}))["version"] == 1  # an old run keeps its version
    assert ok(call_tool(after_run, "get_rules", {}))["version"] == 2


# ---------- scripted review agents ----------

CALC = {
    "labor_depreciation": ("labor_depreciation_refund", {"estimate": "estimate_v2.txt"},
                           {"filename": "policy.txt", "quote": "Labor is not subject to depreciation."}),
    "missing_coverage": ("missing_coverage", {"bid": "contractor_bid.txt", "estimate": "estimate_v2.txt",
                                              "endorsement": "endorsement_code_upgrade.txt"},
                         {"filename": "endorsement_code_upgrade.txt", "quote": "Limit of liability: $25,000"}),
    "unpaid_ale": ("unpaid_ale", {"promise": "adjuster_email_1.txt", "notice": "ale_notice.txt"},
                   {"filename": "ale_notice.txt", "quote": "ALE payments end after month 4"}),
}


class FilingAgent:
    """A scripted review agent: computes each type in `types`, files a finding for each, then finishes."""

    def __init__(self, types):
        self.types, self.step = list(types), 0
        self.messages = self

    def create(self, **kw):
        self.step += 1
        if self.step == 1:
            return reply([use("compute_amount", {"rule": CALC[t][0], "inputs": CALC[t][1]}, f"c_{t}")
                          for t in self.types])
        if self.step == 2:
            results = {b["tool_use_id"]: json.loads(b["content"]) for b in kw["messages"][-1]["content"]}
            return reply([use("upsert_finding", {
                "type": t, "title": f"{t} finding", "summary": "Filed by the scripted agent.",
                "points": ["Policy: Scripted point one.", "On file: Scripted point two."],
                "calcId": results[f"c_{t}"].get("calcId", "calc_missing"), "evidence": [CALC[t][2]]}, f"f_{t}")
                for t in self.types])
        return reply([say("Done.")], "end_turn")


def baseline(types=("missing_coverage", "unpaid_ale")):
    result = agent.run_agent(PARK, client=FilingAgent(types))
    assert result.status == "completed"
    return result, scorer.score_run(result.runId)


class Proposer:
    def __init__(self, raw):
        self.raw, self.calls = raw, []

    def __call__(self, miss_report, context):
        self.calls.append((miss_report, context))
        return self.raw


def types_of(seen, wanted=("miss_detected", "rule_proposed", "candidate_scored", "rule_promoted",
                           "rule_rejected", "no_change")):
    return [e["type"] for e in seen if e["type"] in wanted]


def capture(run_id=None):
    seen = []
    return seen, events.bus.subscribe(seen.append, run_id)


def snapshot_rules():
    return sorted((json.dumps({k: str(v) for k, v in d.items() if k != "_id"}, sort_keys=True)
                   for d in db.rules.find()))


@integ
def test_promotes_when_candidate_rerun_gets_three_of_three(seeded, fake_embed):  # T4.2/4.6/4.9/4.10, scripted
    first, before = baseline()
    assert (before["caughtCount"], before["missed"]) == (2, ["labor_depreciation"])
    seen, unsub = capture()
    out = improve.improve_rulebook(PARK, first.runId, proposer=Proposer(GOOD),
                                   agent_client=FilingAgent(["labor_depreciation", "missing_coverage", "unpaid_ale"]))
    unsub()
    assert out["decision"] == "promoted" and out["candidateVersion"] == 2
    assert types_of(seen) == ["miss_detected", "rule_proposed", "candidate_scored", "rule_promoted"]
    assert rulebook.active_version(INSURER) == 2
    assert rulebook.header(INSURER, 1)["status"] == "inactive" and len(rulebook.rules_for(INSURER, 1)) == 2
    h = rulebook.header(INSURER, 2)
    assert h["status"] == "active" and h["decision"] == "promoted" and h["proposedFromRunId"] == first.runId
    assert (h["scoreBefore"]["caughtCount"], h["scoreBefore"]["expectedCount"]) == (2, 3)
    assert (h["scoreAfter"]["caughtCount"], h["scoreAfter"]["expectedCount"]) == (3, 3)
    assert h["proposal"] == {k: GOOD[k] for k in ("type", "instruction", "computeRule")}
    assert h["createdAt"] < h["decidedAt"] and h["decidedAt"].tzinfo is not None and h["reason"]
    versions = {r["runId"]: r["rulebookVersion"] for r in db.agent_runs.find()}
    assert versions[first.runId] == 1 and versions[out["candidateRunId"]] == 2
    assert db.scores.find_one({"runId": out["candidateRunId"]})["rulebookVersion"] == 2


@integ
def test_rejects_candidate_that_causes_a_false_positive(seeded, fake_embed):  # T4.3
    # Park has no unpaid_ale label in this test, so filing one is a false positive.
    db.labels.delete_one({"claimId": PARK, "type": "unpaid_ale"})
    first, before = baseline(("missing_coverage",))
    assert (before["caughtCount"], before["falsePositives"], before["missed"]) == (1, [], ["labor_depreciation"])
    seen, unsub = capture()
    out = improve.improve_rulebook(PARK, first.runId, proposer=Proposer(GOOD),
                                   agent_client=FilingAgent(["labor_depreciation", "missing_coverage", "unpaid_ale"]))
    unsub()
    assert out["decision"] == "rejected" and "false positives increased" in out["reason"]
    assert out["scoreAfter"]["caughtCount"] == 2  # it did catch more; it was rejected for the false positive
    assert types_of(seen) == ["miss_detected", "rule_proposed", "candidate_scored", "rule_rejected"]
    assert rulebook.active_version(INSURER) == 1
    h = rulebook.header(INSURER, 2)
    assert h["status"] == "rejected" and h["decision"] == "rejected" and "false positives" in h["reason"]
    assert ok(call_tool(runs.start_run(PARK), "get_rules", {}))["version"] == 1  # v1 still what new runs use


@integ
def test_rejects_candidate_that_loses_a_caught_type(seeded, fake_embed):
    first, _ = baseline()
    out = improve.improve_rulebook(PARK, first.runId, proposer=Proposer(GOOD),
                                   agent_client=FilingAgent(["labor_depreciation", "missing_coverage"]))
    assert out["decision"] == "rejected" and "lost previously caught types: unpaid_ale" in out["reason"]
    assert rulebook.active_version(INSURER) == 1


@integ
def test_rejects_candidate_that_does_not_improve(seeded, fake_embed):
    first, _ = baseline()
    out = improve.improve_rulebook(PARK, first.runId, proposer=Proposer(GOOD),
                                   agent_client=FilingAgent(["missing_coverage", "unpaid_ale"]))
    assert out["decision"] == "rejected" and "did not increase" in out["reason"]
    assert rulebook.active_version(INSURER) == 1 and rulebook.header(INSURER, 2)["status"] == "rejected"


@integ
def test_failed_candidate_run_is_rejected(seeded, fake_embed):
    first, _ = baseline()

    class Boom:
        def __init__(self):
            self.messages = self

        def create(self, **kw):
            raise ConnectionError("network down")
    out = improve.improve_rulebook(PARK, first.runId, proposer=Proposer(GOOD), agent_client=Boom())
    assert out["decision"] == "rejected" and out["stage"] == "candidate_run"
    assert rulebook.active_version(INSURER) == 1 and rulebook.header(INSURER, 2)["status"] == "rejected"


BAD_PROPOSALS = [
    {**GOOD, "type": "made_up"},
    {**GOOD, "computeRule": "made_up_rule"},
    {**GOOD, "computeRule": "unpaid_ale"},
    {**GOOD, "instruction": "too short"},
    {**GOOD, "instruction": "x" * 501},
    {"type": "unpaid_ale", "computeRule": "unpaid_ale", "instruction": "y" * 40},  # not a missed type
    "garbage",
]


@integ
@pytest.mark.parametrize("raw", BAD_PROPOSALS, ids=[str(i) for i in range(len(BAD_PROPOSALS))])
def test_invalid_proposal_rejected_before_any_database_change(seeded, fake_embed, raw):  # T4.4
    first, _ = baseline()
    before_rules, before_runs = snapshot_rules(), db.agent_runs.count_documents({})
    seen, unsub = capture()
    out = improve.improve_rulebook(PARK, first.runId, proposer=Proposer(raw), agent_client=FilingAgent([]))
    unsub()
    assert out["decision"] == "rejected" and out["stage"] == "proposal" and out["reason"]
    assert snapshot_rules() == before_rules and db.agent_runs.count_documents({}) == before_runs
    assert rulebook.versions(INSURER) == [1] and rulebook.headers(INSURER) == {}
    assert types_of(seen) == ["miss_detected", "rule_rejected"]


@integ
def test_proposer_exception_is_a_rejection_not_a_crash(seeded, fake_embed):
    first, _ = baseline()

    def broken(report, context):
        raise TimeoutError("api timeout")
    out = improve.improve_rulebook(PARK, first.runId, proposer=broken)
    assert out["decision"] == "rejected" and out["stage"] == "proposal" and rulebook.versions(INSURER) == [1]


@integ
def test_proposer_receives_only_the_miss_report(seeded, fake_embed):  # T4.7 (scripted client, full prompt captured)
    first, _ = baseline()

    class Recorder:
        def __init__(self):
            self.calls, self.messages = [], self

        def create(self, **kw):
            self.calls.append(kw)
            return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(GOOD))])
    rec = Recorder()
    improve.improve_rulebook(PARK, first.runId, proposer=improve.claude_proposer(rec),
                             agent_client=FilingAgent(["labor_depreciation", "missing_coverage", "unpaid_ale"]))
    assert len(rec.calls) == 1
    sent = json.dumps(rec.calls[0], default=str)
    payload = json.loads(rec.calls[0]["messages"][0]["content"])
    assert set(payload) == {"missReport", "allowedTypes", "computeRules", "currentRules"}
    assert [m["type"] for m in payload["missReport"]] == ["labor_depreciation"]
    assert payload["missReport"][0]["expectedAmount"] == 4200
    assert {e["filename"] for e in payload["missReport"][0]["evidence"]} >= {"policy.txt"}
    assert {r["type"] for r in payload["currentRules"]} == {"missing_coverage", "unpaid_ale"}
    maria_labels = json.dumps(db.labels.find_one({"claimId": MARIA, "type": "labor_depreciation"}), default=str)
    for leak in (MARIA, "Maria Alvarez", "11300", "18700", "8400", "HM-HO-0071843", "77 Copperleaf",
                 "Daniel and Grace Park", "PK-20719"):
        assert leak not in sent, leak
    assert "labels" not in sent.lower() and maria_labels not in sent


@integ
def test_no_change_when_nothing_was_missed(seeded, fake_embed):  # T4.8 (scripted)
    db.labels.delete_one({"claimId": PARK, "type": "labor_depreciation"})  # nothing left for v1 to miss
    first, s = baseline()
    assert s["missed"] == []
    proposer = Proposer(GOOD)
    seen, unsub = capture()
    out = improve.improve_rulebook(PARK, first.runId, proposer=proposer)
    unsub()
    assert out["decision"] == "no_change" and "no change" in out["reason"]
    assert proposer.calls == [] and rulebook.versions(INSURER) == [1] and rulebook.headers(INSURER) == {}
    assert db.agent_runs.count_documents({}) == 1 and types_of(seen) == ["no_change"]


@integ
def test_run_must_belong_to_the_training_claim(seeded, fake_embed):
    first, _ = baseline()
    with pytest.raises(ValueError):
        improve.improve_rulebook(MARIA, first.runId, proposer=Proposer(GOOD))
    with pytest.raises(ValueError):
        improve.improve_rulebook(PARK, "run_nope", proposer=Proposer(GOOD))


def test_proposer_prompt_never_mentions_labels_in_source():
    import inspect
    src = inspect.getsource(improve)
    assert "get_collection(\"labels\")" not in src and "labels.find" not in src


@integ
def test_added_in_version_falls_back_and_backfills_for_older_rules(seeded):
    """Rules created before addedInVersion existed: min version holding that rule id; backfill stamps them."""
    rule = {"type": "labor_depreciation", "instruction": "i" * 30, "computeRule": "labor_depreciation_refund"}
    rulebook.create_candidate(INSURER, 1, rule, {})
    db.rules.update_many({}, {"$unset": {"addedInVersion": ""}})  # simulate data from before the field existed
    assert rulebook.added_in_version(INSURER, "HM-MC-001") == 1  # exists in v1 and its v2 copy
    assert rulebook.added_in_version(INSURER, "labor_depreciation-v2") == 2
    assert rulebook.backfill_added_in_version(INSURER) == 5 and rulebook.backfill_added_in_version(INSURER) == 0
    assert {r["id"]: r["addedInVersion"] for r in rulebook.rules_for(INSURER, 2)} == {
        "HM-MC-001": 1, "HM-ALE-001": 1, "labor_depreciation-v2": 2}
