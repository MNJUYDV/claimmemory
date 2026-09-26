"""Attempts to break the Phase 2 guardrails. Each test is one attack and states what must hold."""
import pytest

import db
import ingest
import runs
import tools
from constants import OPEN_ENDED
from tools import TOOLS, ToolContext, call_tool

# Reuse the fixtures and helpers from the main tools tests.
from tests.test_phase2_tools import (  # noqa: F401
    DATA, MARIA, PARK, V2_AT, _compute_all, _finding_args, _poll, _v2_decision, fake_embed, fake_vec,
    integ, maria, maria_plain, ok, park, seeded)

REAL = "Labor is not subject to depreciation."
FORGED = ToolContext(MARIA, "run_forged")


def fact(ctx, quote, filename="policy.txt", valid_from=V2_AT, label="l"):
    return call_tool(ctx, "record_fact", {"row": "r", "label": label, "validFrom": valid_from,
                                          "sourceFilename": filename, "quote": quote})


def finding_with_quote(ctx, calc_id, quote, filename="policy.txt"):
    return call_tool(ctx, "upsert_finding", _finding_args(
        calc_id, evidence=[{"filename": filename, "quote": quote}]))


# ---------- fabricated or slightly altered quotes ----------

BAD_QUOTES = [
    ("fabricated", "Labor may be depreciated at 50 percent.", "policy.txt"),
    ("case changed", "labor is not subject to depreciation.", "policy.txt"),
    ("double space", "Labor is not  subject to depreciation.", "policy.txt"),
    ("one word changed", "Labor is not subject to depreciations.", "policy.txt"),
    ("cyrillic lookalike", "Lаbor is not subject to depreciation.", "policy.txt"),
    ("real quote, wrong file", REAL, "estimate_v2.txt"),
    ("line-number prefix", "   1: HARBORLINE MUTUAL - HOMEOWNERS POLICY", "policy.txt"),
    ("single character", "L", "policy.txt"),
    ("common word", "the", "policy.txt"),
    ("true substring under the minimum length", "depreciat", "policy.txt"),
    ("whitespace only", "          ", "policy.txt"),
    ("other claim's policy number", "HM-HO-0059172", "policy.txt"),
]


@integ
@pytest.mark.parametrize("label,quote,filename", BAD_QUOTES, ids=[b[0] for b in BAD_QUOTES])
def test_bad_quotes_rejected_everywhere(maria, label, quote, filename):
    assert "error" in fact(maria, quote, filename)
    calc = _compute_all(maria)["labor_depreciation"]["calcId"]
    assert "error" in finding_with_quote(maria, calc, quote, filename)
    assert db.facts.count_documents({}) == 0 and db.findings.count_documents({}) == 0


@integ
def test_verbatim_substring_is_accepted(maria):
    """A truncated quote is still verbatim; that is allowed by design."""
    ok(fact(maria, "Labor is not subject to depreciation"))


def test_quote_type_confusion_rejected_by_schema():
    for bad in (123, None, ["x"], {"$ne": None}, {"$regex": ".*"}):
        assert "error" in call_tool(FORGED, "record_fact", {
            "row": "r", "label": "l", "validFrom": V2_AT, "sourceFilename": "policy.txt", "quote": bad})
    assert "error" in call_tool(FORGED, "read_document", {"filename": {"$ne": None}})


@integ
def test_filename_path_traversal_is_just_an_unknown_document(maria):
    for name in ("../PK-20719/policy.txt", "/etc/passwd", "data/HO-48213/policy.txt"):
        assert "no document named" in call_tool(maria, "read_document", {"filename": name})["error"]


# ---------- amounts ----------

@pytest.mark.parametrize("extra", [{"amount": "11300"}, {"amount": 1}, {"amount": 11300.0},
                                   {"amountCents": 1130000}, {"claimId": "PK-20719"}, {"runId": "r"}])
def test_amount_like_fields_rejected_by_schema(extra):
    args = {"type": "labor_depreciation", "title": "t", "detail": "d", "calcId": "calc_x",
            "evidence": [{"filename": "policy.txt", "quote": REAL}]} | extra
    assert "Additional properties" in call_tool(FORGED, "upsert_finding", args)["error"]


def test_amount_nested_in_evidence_and_compute_inputs_rejected():
    ev = [{"filename": "policy.txt", "quote": REAL, "amount": 5}]
    r = call_tool(FORGED, "upsert_finding", {"type": "unpaid_ale", "title": "t", "detail": "d",
                                             "calcId": "c", "evidence": ev})
    assert "Additional properties" in r["error"]
    for inputs in ({"amount": "99"}, {"promise": 5}, {"promise": ["a"]}, {"promise": {"$ne": 1}}):
        assert "error" in call_tool(FORGED, "compute_amount", {"rule": "unpaid_ale", "inputs": inputs})


@integ
@pytest.mark.parametrize("field,text", [
    ("detail", "Harborline owes $50,000 in refunds."),
    ("title", "Labor depreciation of $1,000,000"),
    ("detail", "Refund of USD 20,000 is due."),
    ("detail", "About $11,300.50 was withheld."),   # close to the real amount, but not it
    ("detail", "Roughly $11.3k was withheld."),
])
def test_invented_dollar_figures_in_prose_rejected(maria, field, text):
    calc = _compute_all(maria)["labor_depreciation"]["calcId"]
    assert "dollar figure" in call_tool(maria, "upsert_finding", _finding_args(calc, **{field: text}))["error"]
    assert db.findings.count_documents({}) == 0


@integ
def test_real_dollar_figures_in_prose_allowed(maria):
    calc = _compute_all(maria)["labor_depreciation"]["calcId"]
    detail = "Labor depreciation totals $11,300 across 41 lines; the estimate total is $61,200.00."
    ok(call_tool(maria, "upsert_finding", _finding_args(calc, detail=detail)))


@integ
def test_zero_amount_calc_cannot_back_a_finding_or_overwrite_one(maria):
    good = _compute_all(maria)["labor_depreciation"]["calcId"]
    ok(call_tool(maria, "upsert_finding", _finding_args(good)))
    zero = ok(call_tool(maria, "compute_amount", {"rule": "labor_depreciation_refund",
                                                  "inputs": {"estimate": "estimate_v1.txt"}}))
    assert zero["amount"] == 0
    assert "nothing to report" in call_tool(maria, "upsert_finding", _finding_args(zero["calcId"]))["error"]
    assert db.findings.find_one({})["amount"] == 11300


@integ
def test_wrong_documents_for_a_rule_are_json_errors(maria):
    bad = [("unpaid_ale", {"promise": "ale_notice.txt", "notice": "adjuster_email_1.txt"}),
           ("missing_coverage", {"bid": "contractor_bid.txt", "estimate": "contractor_bid.txt",
                                 "endorsement": "endorsement_code_upgrade.txt"}),
           ("missing_coverage", {"bid": "contractor_bid.txt", "estimate": "estimate_v2.txt",
                                 "endorsement": "policy.txt"}),
           ("labor_depreciation_refund", {"estimate": "payments.json"}),
           ("missing_coverage", {"bid": "contractor_bid.txt"})]
    for rule, inputs in bad:
        assert "error" in call_tool(maria, "compute_amount", {"rule": rule, "inputs": inputs}), (rule, inputs)


def test_oversized_and_empty_strings_rejected_by_schema():
    base = {"type": "unpaid_ale", "title": "t", "detail": "d", "calcId": "c",
            "evidence": [{"filename": "policy.txt", "quote": REAL}]}
    assert "error" in call_tool(FORGED, "upsert_finding", base | {"title": "x" * 1_000_000})
    assert "error" in call_tool(FORGED, "upsert_finding", base | {"detail": "x" * 1_000_000})
    assert "error" in call_tool(FORGED, "upsert_finding", base | {"evidence": [{"filename": "f", "quote": "q" * 5000}]})
    assert "error" in call_tool(FORGED, "search_policy", {"query": "", "asOf": V2_AT})
    assert "error" in call_tool(FORGED, "search_policy", {"query": "x" * 10_000, "asOf": V2_AT})


# ---------- dates ----------

@integ
@pytest.mark.parametrize("value", ["2026-07-18T00:00:00", "2026-07-18", "0001-01-01T00:00:00+05:00",
                                   "9999-12-31T12:00:00+00:00", "9999-12-30T00:00:00+00:00",
                                   "1999-12-31T00:00:00+00:00", "not a date", ""])
def test_bad_dates_rejected_as_json_errors(maria, value):
    r = fact(maria, REAL, valid_from=value)
    assert "error" in r and "internal error" not in r["error"], r
    assert "error" in call_tool(maria, "record_decision", {"filename": "estimate_v2.txt", "madeAt": value,
                                                          "citedFilenames": []})
    assert db.facts.count_documents({}) == 0 and db.decisions.count_documents({}) == 0


# ---------- cross-claim and identity ----------

def test_claim_and_run_ids_rejected_by_every_tool():
    for t in TOOLS:
        r = call_tool(FORGED, t.name, {"claimId": PARK, "runId": "run_other"})
        assert "Additional properties" in r["error"], t.name


@integ
def test_cross_claim_ids_are_unknown(maria, park):
    theirs = _compute_all(park)
    park_fact = ok(fact(park, REAL))["factId"]
    my_fact = ok(fact(maria, REAL))["factId"]
    park_dec = ok(call_tool(park, "record_decision", {"filename": "estimate_v2.txt", "madeAt": V2_AT,
                                                     "citedFilenames": []}))["decisionId"]
    calc = _compute_all(maria)["labor_depreciation"]["calcId"]
    assert "different run or claim" in call_tool(
        maria, "upsert_finding", _finding_args(theirs["labor_depreciation"]["calcId"]))["error"]
    assert "unknown decision" in call_tool(maria, "upsert_finding", _finding_args(calc, decisionId=park_dec))["error"]
    assert "unknown decision" in call_tool(maria, "replay", {"decisionId": park_dec})["error"]
    assert "unknown fact" in call_tool(maria, "supersede_fact",
                                       {"oldFactId": my_fact, "newFactId": park_fact})["error"]
    assert "unknown fact" in call_tool(maria, "supersede_fact",
                                       {"oldFactId": park_fact, "newFactId": my_fact})["error"]
    assert db.findings.count_documents({}) == 0


@integ
def test_same_filenames_resolve_to_own_claim(maria, park):
    m = ok(call_tool(maria, "compute_amount", {"rule": "labor_depreciation_refund",
                                              "inputs": {"estimate": "estimate_v2.txt"}}))
    p = ok(call_tool(park, "compute_amount", {"rule": "labor_depreciation_refund",
                                             "inputs": {"estimate": "estimate_v2.txt"}}))
    assert (m["amount"], p["amount"]) == (11300, 4200)


@integ
def test_forged_or_finished_run_cannot_write(maria):
    forged_calls = [
        ("record_fact", {"row": "r", "label": "l", "validFrom": V2_AT, "sourceFilename": "policy.txt", "quote": REAL}),
        ("record_decision", {"filename": "estimate_v2.txt", "madeAt": V2_AT, "citedFilenames": []}),
        ("compute_amount", {"rule": "unpaid_ale", "inputs": {"promise": "adjuster_email_1.txt",
                                                            "notice": "ale_notice.txt"}}),
        ("supersede_fact", {"oldFactId": "a", "newFactId": "b"}),
        ("upsert_finding", _finding_args("calc_x")),
    ]
    runs.finish_run(maria, "done")
    for ctx in (FORGED, maria):  # a made-up run, and a run that has finished
        for name, args in forged_calls:
            assert "error" in call_tool(ctx, name, args), (ctx.runId, name)
    assert db.facts.count_documents({}) == 0 and db.decisions.count_documents({}) == 0
    assert db.findings.count_documents({}) == 0
    assert db.agent_runs.find_one({"runId": maria.runId})["calcs"] == []


@integ
def test_supersede_cycles_and_self_rejected(maria):
    a = ok(fact(maria, REAL, label="a"))["factId"]
    b = ok(fact(maria, REAL, label="b"))["factId"]  # same validFrom as a
    ok(call_tool(maria, "supersede_fact", {"oldFactId": a, "newFactId": b}))
    assert "cycle" in call_tool(maria, "supersede_fact", {"oldFactId": b, "newFactId": a})["error"]
    assert "itself" in call_tool(maria, "supersede_fact", {"oldFactId": a, "newFactId": a})["error"]
    c = ok(fact(maria, REAL, label="c"))["factId"]
    assert "already superseded" in call_tool(maria, "supersede_fact", {"oldFactId": a, "newFactId": c})["error"]
    assert db.facts.find_one({"_id": b})["supersededBy"] is None
    assert db.facts.find_one({"_id": a})["validTo"] < OPEN_ENDED


# ---------- replay edge cases ----------

@integ
def test_replay_with_madeat_before_any_document(maria):
    dec = ok(call_tool(maria, "record_decision", {
        "filename": "estimate_v2.txt", "madeAt": "2020-01-01T00:00:00+00:00",
        "citedFilenames": ["policy.txt"]}))["decisionId"]
    r = ok(call_tool(maria, "replay", {"decisionId": dec}))
    assert r["cited"] == [] and r["not_cited"] == []  # nothing had been received yet; no crash


@integ
def test_replay_with_far_future_madeat_lists_everything_else(maria):
    dec = ok(call_tool(maria, "record_decision", {
        "filename": "estimate_v2.txt", "madeAt": "2099-01-01T00:00:00+00:00",
        "citedFilenames": ["policy.txt"]}))["decisionId"]
    r = ok(call_tool(maria, "replay", {"decisionId": dec}))
    assert [d["filename"] for d in r["cited"]] == ["policy.txt"]
    assert len(r["cited"]) + len(r["not_cited"]) == 8  # 9 ingested minus the decision document


@integ
def test_replay_cited_file_received_after_decision_is_not_listed(maria):
    dec = ok(call_tool(maria, "record_decision", {
        "filename": "estimate_v2.txt", "madeAt": "2026-06-30T00:00:00-05:00",
        "citedFilenames": ["ale_notice.txt"]}))["decisionId"]  # cites a document received in August
    r = ok(call_tool(maria, "replay", {"decisionId": dec}))
    assert "ale_notice.txt" not in {d["filename"] for d in r["cited"] + r["not_cited"]}


# ---------- dispatch robustness ----------

def test_call_tool_never_raises_on_garbage():
    ctx = ToolContext(MARIA, "run_x")
    for name in (["x"], None, 5, {"a": 1}, "", "get_rules "):
        assert "error" in call_tool(ctx, name, {})
    for args in (None, [], "x", 5, [1, 2]):
        assert "error" in call_tool(ctx, "get_rules", args)


# ---------- search isolation ----------

@integ
def test_search_policy_never_returns_another_claims_clauses(seeded, fake_embed, vector_indexes):
    ingest.ingest_claim(MARIA, exclude=("estimate_v3.txt",))  # Park is NOT ingested
    m, p = runs.start_run(MARIA), runs.start_run(PARK)
    q = {"query": "labor depreciation", "asOf": "2026-07-18T00:00:00-05:00"}
    mine = _poll(lambda: call_tool(m, "search_policy", q), lambda r: r.get("results"))
    assert ok(mine)["results"], "Maria's clauses should be searchable once the index catches up"
    assert ok(call_tool(p, "search_policy", q))["results"] == []
