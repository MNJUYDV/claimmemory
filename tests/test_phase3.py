"""Phase 3 tests that need no LLM: the rulebook guardrail, the scorer, and the agent loop driven by a
scripted stand-in for Claude. Real-Claude tests (T3.2-T3.10) are in test_phase3_llm*.py (marker: llm)."""
from types import SimpleNamespace

import pytest

import agent
import db
import events
import runs
import scorer
import tools
from tests.test_phase2_tools import (  # noqa: F401
    DATA, MARIA, PARK, V2_AT, _compute_all, _finding_args, fake_embed, integ, maria_plain, ok, seeded)
from tools import ToolContext, call_tool

REAL = "Labor is not subject to depreciation."


# ---------- T3.1 / T3.11 ----------

@integ
def test_labor_depreciation_rejected_under_rulebook_v1(maria_plain):  # T3.1
    ctx = maria_plain
    calc = ok(call_tool(ctx, "compute_amount", {"rule": "labor_depreciation_refund",
                                                "inputs": {"estimate": "estimate_v2.txt"}}))
    assert calc["amount"] == 11300  # the math is right; the rulebook just doesn't authorize the finding
    r = call_tool(ctx, "upsert_finding", _finding_args(calc["calcId"]))
    assert r == {"error": "no active rule authorizes labor_depreciation findings"}
    assert db.findings.count_documents({}) == 0
    # the check comes before everything else, so even a bogus calcId gets this error
    assert call_tool(ctx, "upsert_finding", _finding_args("calc_nope")) == r


@integ
def test_authorized_types_still_work_under_v1(maria_plain):
    calcs = _compute_all(maria_plain)
    ev = {"missing_coverage": [{"filename": "endorsement_code_upgrade.txt",
                                "quote": "Limit of liability: $25,000"}],
          "unpaid_ale": [{"filename": "ale_notice.txt", "quote": "ALE payments end after month 6"}]}
    for t in ("missing_coverage", "unpaid_ale"):
        ok(call_tool(maria_plain, "upsert_finding", _finding_args(calcs[t]["calcId"], t, evidence=ev[t])))


@integ
@pytest.mark.parametrize("rule", [
    {"active": False, "version": 1},   # right version, switched off
    {"active": True, "version": 0},    # active, but belongs to an older rulebook version
    {"active": True, "version": 1, "insurer": "Some Other Mutual"},  # another insurer's rule
])
def test_inactive_old_or_foreign_rules_do_not_authorize(maria_plain, rule):
    from datetime import datetime, timezone
    doc = {"id": "X-LD", "insurer": "Harborline Mutual", "type": "labor_depreciation", **rule,
           "computeRule": "labor_depreciation_refund", "instruction": "x",
           "createdAt": datetime(2026, 1, 1, tzinfo=timezone.utc)}
    db.upsert_one("rules", {"id": "X-LD", "version": doc["version"]}, doc)
    calc = ok(call_tool(maria_plain, "compute_amount", {"rule": "labor_depreciation_refund",
                                                        "inputs": {"estimate": "estimate_v2.txt"}}))
    r = call_tool(maria_plain, "upsert_finding", _finding_args(calc["calcId"]))
    assert "no active rule authorizes labor_depreciation findings" in r["error"]


@integ
def test_adding_an_active_rule_authorizes_the_type(maria_plain):
    from tests.test_phase2_tools import authorize_labor_depreciation
    authorize_labor_depreciation()
    calc = ok(call_tool(maria_plain, "compute_amount", {"rule": "labor_depreciation_refund",
                                                        "inputs": {"estimate": "estimate_v2.txt"}}))
    ok(call_tool(maria_plain, "upsert_finding", _finding_args(calc["calcId"])))
    assert [r["type"] for r in ok(call_tool(maria_plain, "get_rules", {}))["rules"]].count("labor_depreciation") == 1


def test_tool_list_has_no_scorer_labels_or_scores_access():  # T3.11
    defs = tools.tool_definitions()
    import re
    blob = repr(defs).lower()
    # "label" alone is fine: it is a record_fact field. What must never appear is the ground-truth side.
    assert not re.search(r"\b(scorer|scores?|labels|ground[ _]truth|score_run)\b", blob)
    assert {d["name"] for d in defs} == {t.name for t in tools.TOOLS}
    assert "scorer" not in vars(tools) and "scorer" not in vars(agent)
    import inspect
    assert "import scorer" not in inspect.getsource(tools) + inspect.getsource(agent)


@integ
def test_record_fact_and_compute_are_idempotent(maria_plain):
    args = {"row": "estimate_total", "label": "v2", "validFrom": V2_AT, "sourceFilename": "policy.txt",
            "quote": REAL}
    a, b = ok(call_tool(maria_plain, "record_fact", args)), ok(call_tool(maria_plain, "record_fact", args))
    assert a["factId"] == b["factId"] and db.facts.count_documents({}) == 1
    c = {"rule": "unpaid_ale", "inputs": {"promise": "adjuster_email_1.txt", "notice": "ale_notice.txt"}}
    assert ok(call_tool(maria_plain, "compute_amount", c))["calcId"] == \
        ok(call_tool(maria_plain, "compute_amount", c))["calcId"]
    assert len(db.agent_runs.find_one({"runId": maria_plain.runId})["calcs"]) == 1


# ---------- scorer (no LLM) ----------

def _file_park_findings(ctx):
    calcs = _compute_all(ctx)
    ev = {"missing_coverage": [{"filename": "endorsement_code_upgrade.txt", "quote": "Limit of liability: $25,000"},
                               {"filename": "contractor_bid.txt", "quote": "Code upgrade subtotal: $6,900.00"}],
          "unpaid_ale": [{"filename": "ale_notice.txt", "quote": "ALE payments end after month 4"}]}
    for t in ("missing_coverage", "unpaid_ale"):
        ok(call_tool(ctx, "upsert_finding", _finding_args(calcs[t]["calcId"], t, evidence=ev[t])))


@pytest.fixture
def park_plain(seeded, fake_embed):
    import ingest
    ingest.ingest_claim(PARK)
    return runs.start_run(PARK)


@integ
def test_scorer_scores_park_run_two_of_three(park_plain):
    _file_park_findings(park_plain)
    s = scorer.score_run(park_plain.runId)
    assert (s["caughtCount"], s["expectedCount"]) == (2, 3)
    assert s["caught"] == ["missing_coverage", "unpaid_ale"] and s["missed"] == ["labor_depreciation"]
    assert s["falsePositives"] == [] and s["exactAmountCount"] == 2
    assert s["amounts"]["missing_coverage"] == {"expected": 6900, "found": 6900, "exact": True}
    assert s["amounts"]["labor_depreciation"] == {"expected": 4200, "found": None, "exact": False}
    assert s["citations"] == {"total": 3, "verified": 3} and s["rulebookVersion"] == 1
    stored = db.scores.find_one({"runId": park_plain.runId})
    assert stored["rulebookVersion"] == 1 and stored["scoredAt"].tzinfo is not None
    scorer.score_run(park_plain.runId)  # scoring twice replaces, not duplicates
    assert db.scores.count_documents({}) == 1


@integ
def test_scorer_flags_wrong_amount_false_positive_and_bad_citation(park_plain):
    _file_park_findings(park_plain)
    db.findings.update_one({"type": "unpaid_ale"}, {"$set": {"amountCents": 250000}})
    db.findings.update_one({"type": "missing_coverage"}, {"$set": {"evidence.0.quote": "invented text here"}})
    db.findings.insert_one({"claimId": PARK, "runId": park_plain.runId, "type": "made_up", "amountCents": 100,
                            "evidence": []})
    s = scorer.score_run(park_plain.runId)
    assert s["falsePositives"] == ["made_up"] and s["caughtCount"] == 2
    assert s["amounts"]["unpaid_ale"]["exact"] is False and s["exactAmountCount"] == 1
    assert s["citations"] == {"total": 3, "verified": 2}


@integ
def test_scorer_ignores_findings_from_other_runs(park_plain):
    _file_park_findings(park_plain)
    other = runs.start_run(PARK)
    s = scorer.score_run(other.runId)
    assert s["caughtCount"] == 0 and s["missed"] == ["labor_depreciation", "missing_coverage", "unpaid_ale"]


# ---------- the loop, driven by a scripted model ----------

def use(name, args, id_):
    return SimpleNamespace(type="tool_use", id=id_, name=name, input=args)


def say(text):
    return SimpleNamespace(type="text", text=text)


def reply(blocks, stop="tool_use"):
    return SimpleNamespace(content=blocks, stop_reason=stop,
                           usage=SimpleNamespace(input_tokens=10, output_tokens=5,
                                                 cache_read_input_tokens=0, cache_creation_input_tokens=0))


class Scripted:
    """Stands in for anthropic.Anthropic: replays canned responses and records what it was sent."""

    def __init__(self, script):
        self.script, self.sent = list(script), []
        self.messages = self

    def create(self, **kw):
        assert kw["system"] and kw["tools"] and kw["model"]
        self.sent.append(len(kw["messages"]))
        return self.script.pop(0)


FACT = {"row": "estimate_total", "label": "v2 total", "validFrom": V2_AT, "sourceFilename": "policy.txt",
        "quote": REAL}


def happy_script():
    return [reply([use("get_claim_state", {}, "tu_1")]),
            reply([use("get_rules", {}, "tu_2"), use("record_fact", FACT, "tu_3")]),
            reply([say("All done.")], "end_turn")]


@integ
def test_loop_completes_logs_every_call_and_emits_events(seeded, fake_embed):
    seen = []
    unsubscribe = events.bus.subscribe(seen.append)
    client = Scripted(happy_script())
    result = agent.run_agent(MARIA, client=client)
    unsubscribe()
    assert (result.status, result.steps, result.summary) == ("completed", 3, "All done.")
    assert client.sent == [1, 3, 5]  # history grows by an assistant and a tool-result message per tool step
    run = db.agent_runs.find_one({"runId": result.runId})
    assert run["status"] == "completed" and run["stepCount"] == 3 and run["rulebookVersion"] == 1
    assert [m["role"] for m in run["messages"]] == ["user", "assistant", "user", "assistant", "user", "assistant"]
    calls = run["toolCalls"]
    assert [(c["seq"], c["step"], c["name"]) for c in calls] == [
        (1, 1, "get_claim_state"), (2, 2, "get_rules"), (3, 2, "record_fact")]
    for c in calls:
        assert c["input"] is not None and "error" not in c["output"] and c["durationMs"] >= 0
        assert c["startedAt"].tzinfo is not None
    assert run["usage"]["inputTokens"] == 30
    ev = [e for e in seen if e["runId"] == result.runId]
    assert [e["type"] for e in ev if e["type"] in ("run_started", "run_finished")] == ["run_started", "run_finished"]
    assert ev[0]["type"] == "run_started" and ev[-1]["type"] == "run_finished"
    tool_events = [e for e in ev if e["type"] == "tool_call"]
    assert [e["seq"] for e in tool_events] == [1, 2, 3] and [e["name"] for e in tool_events] == \
        [c["name"] for c in calls]
    assert [e["step"] for e in ev if e["type"] == "assistant_message"] == [1, 2, 3]


@integ
def test_held_document_is_not_ingested(seeded, fake_embed):
    result = agent.run_agent(MARIA, client=Scripted([reply([say("ok")], "end_turn")]))
    assert result.status == "completed"
    names = {d["filename"] for d in db.documents.find({"claimId": MARIA})}
    assert "estimate_v3.txt" not in names and len(names) == 9
    assert agent.held_filenames(MARIA) == ["estimate_v3.txt"] and agent.held_filenames(PARK) == []


@integ
def test_step_limit_gives_needs_review_without_crashing(seeded, fake_embed):
    endless = [reply([use("get_rules", {}, f"tu_{i}")]) for i in range(10)]
    result = agent.run_agent(MARIA, client=Scripted(endless), max_steps=3)
    assert (result.status, result.steps) == ("needs_review", 3)
    run = db.agent_runs.find_one({"runId": result.runId})
    assert run["status"] == "needs_review" and "step limit" in run["note"] and len(run["toolCalls"]) == 3


@integ
def test_failure_then_resume_continues_without_duplicates(seeded, fake_embed):
    def crash_after_two(step):
        if step == 2:
            raise RuntimeError("simulated crash")

    first = Scripted(happy_script())
    result = agent.run_agent(MARIA, client=first, after_step=crash_after_two)
    assert (result.status, result.steps) == ("failed", 2) and "simulated crash" in result.error
    saved = db.agent_runs.find_one({"runId": result.runId})
    assert saved["status"] == "failed" and len(saved["messages"]) == 5  # steps 1-2 are durable

    # The resumed model replays step 2's record_fact (as a re-executed step would), then finishes.
    second = Scripted([reply([use("record_fact", FACT, "tu_9")]), reply([say("Resumed and done.")], "end_turn")])
    seen = []
    unsubscribe = events.bus.subscribe(seen.append, run_id=result.runId)
    resumed = agent.resume_run(result.runId, client=second)
    unsubscribe()
    assert (resumed.status, resumed.steps, resumed.summary) == ("completed", 4, "Resumed and done.")
    assert second.sent == [5, 7]  # continues from the saved history, not from scratch
    run = db.agent_runs.find_one({"runId": result.runId})
    assert run["messages"][:5] == saved["messages"]  # append-only
    assert [(c["seq"], c["step"]) for c in run["toolCalls"]] == [(1, 1), (2, 2), (3, 2), (4, 3)]
    assert db.facts.count_documents({}) == 1  # the replayed record_fact did not duplicate
    assert [e["type"] for e in seen][:1] == ["run_resumed"] and seen[0]["fromStep"] == 2
    assert agent.resume_run(result.runId, client=Scripted([])).status == "completed"  # nothing left to do


@integ
def test_api_error_marks_run_failed_and_resumable(seeded, fake_embed):
    class Boom:
        messages = None

        def __init__(self):
            self.messages = self

        def create(self, **kw):
            raise ConnectionError("network down")

    result = agent.run_agent(MARIA, client=Boom())
    assert result.status == "failed" and "network down" in result.error and result.steps == 0
    done = agent.resume_run(result.runId, client=Scripted([reply([say("recovered")], "end_turn")]))
    assert done.status == "completed"


@integ
def test_unknown_claim_and_task_rejected(seeded, fake_embed):
    with pytest.raises(ValueError):
        agent.run_agent("NOPE-1", client=Scripted([]))
    with pytest.raises(ValueError):
        agent.run_agent(MARIA, task="write_poetry", client=Scripted([]))
