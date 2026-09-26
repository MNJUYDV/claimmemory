"""Phase 2 tests. Spec test IDs are noted next to each test:

T2.1 schemas | T2.2 ingest twice | T2.3 search 4.2 first | T2.4 endorsement hidden before effectiveFrom
T2.5 bad quote | T2.6 supersede | T2.7 replay finds uncited endorsement | T2.8 replay unknown decision
T2.9 Maria amounts | T2.10 Park amounts | T2.11 bad calcIds | T2.12 no amount field | T2.13 upsert twice
T2.14 labels/scores unreachable

Tests using Atlas or Voyage carry @pytest.mark.integration (the `integ` alias); the rest run offline.
"""
import hashlib
import inspect
import json
import random
import time
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import jsonschema
import pytest

import dataparse
import db
import embeddings
import ingest
import runs
import seeding
import tools
from constants import OPEN_ENDED
from tools import TOOLS, ToolContext, call_tool

integ = pytest.mark.integration
DATA = dataparse.DATA_DIR
LABELS = json.loads((DATA / "labels.json").read_text())
MARIA, PARK = "HO-48213", "PK-20719"
V1_AT, V2_AT = "2026-06-10T00:00:00-05:00", "2026-07-18T00:00:00-05:00"


def labelled(claim_id):
    return {f["type"]: f["amount"] for f in LABELS[claim_id]["findings"]}


def fake_vec(text):
    rnd = random.Random(int(hashlib.sha256(text.encode()).hexdigest()[:8], 16))
    return [rnd.uniform(-1, 1) for _ in range(1024)]


@pytest.fixture
def fake_embed(monkeypatch):
    monkeypatch.setattr(embeddings, "embed", lambda texts, input_type: [fake_vec(t) for t in texts])


@pytest.fixture
def seeded():
    seeding.seed()


def authorize_labor_depreciation():
    """Rulebook v1 has no labor_depreciation rule, so upsert_finding refuses that type (Phase 3).
    The Phase 2 tests exercise the tool mechanics for all three types, so they add the rule."""
    from datetime import datetime, timezone
    db.upsert_one("rules", {"id": "TEST-LD-001", "version": 1}, {
        "id": "TEST-LD-001", "version": 1, "insurer": "Harborline Mutual", "type": "labor_depreciation",
        "active": True, "computeRule": "labor_depreciation_refund", "instruction": "test rule",
        "createdAt": datetime(2026, 1, 1, tzinfo=timezone.utc)})


@pytest.fixture
def maria_plain(seeded, fake_embed):
    """Maria with the real rulebook v1 (no labor_depreciation rule)."""
    ingest.ingest_claim(MARIA, exclude=("estimate_v3.txt",))
    return runs.start_run(MARIA)


@pytest.fixture
def maria(maria_plain):
    authorize_labor_depreciation()
    return maria_plain


@pytest.fixture
def park(seeded, fake_embed):
    ingest.ingest_claim(PARK)
    authorize_labor_depreciation()
    return runs.start_run(PARK)


def ok(result):
    assert "error" not in result, result
    return result


def facts(ctx):
    return {f["row"] + "|" + f["label"]: f for f in db.get_collection("facts").find({"claimId": ctx.claimId})}


# ---------- offline ----------

def _objects(schema):
    if isinstance(schema, dict):
        if schema.get("type") == "object":
            yield schema
        for v in schema.values():
            yield from _objects(v)
    elif isinstance(schema, list):
        for v in schema:
            yield from _objects(v)


def _property_names(schema):
    for o in _objects(schema):
        yield from o.get("properties", {})


def test_tool_schemas_valid_and_closed():  # T2.1
    assert len({t.name for t in TOOLS}) == len(TOOLS) == 11
    for t in TOOLS:
        jsonschema.Draft202012Validator.check_schema(t.input_schema)
        assert t.input_schema["type"] == "object"
        for o in _objects(t.input_schema):
            assert o["additionalProperties"] is False, t.name
        assert not {"claimId", "runId", "amount"} & set(_property_names(t.input_schema)), t.name
    assert [d["name"] for d in tools.tool_definitions()] == [t.name for t in TOOLS]


def test_extra_amount_field_rejected_by_schema():  # T2.12 (schema half)
    ctx = ToolContext("X", "run_x")
    args = {"type": "labor_depreciation", "title": "t", "detail": "d", "calcId": "calc_x",
            "evidence": [{"filename": "f", "quote": "q"}], "amount": 999}
    r = call_tool(ctx, "upsert_finding", args)
    assert "error" in r and "amount" in r["error"]
    r = call_tool(ctx, "get_claim_state", {"claimId": "HO-48213"})
    assert "error" in r


def test_unknown_tool_is_json_error():
    assert "error" in call_tool(ToolContext("X", "r"), "no_such_tool", {})


def test_split_clauses_policy_and_endorsement():
    policy = ingest.split_clauses((DATA / MARIA / "policy.txt").read_text())
    by = {c.section: c for c in policy}
    assert {"1.1", "4.2", "6.1"} <= set(by)
    assert by["4.2"].text.startswith("Labor is not subject to depreciation.")
    assert by["4.2"].heading == "LOSS SETTLEMENT"
    endo = ingest.split_clauses((DATA / MARIA / "endorsement_code_upgrade.txt").read_text())
    assert len(endo) >= 2 and any("Limit of liability: $25,000" in c.text for c in endo)


def test_missing_coverage_capped_at_endorsement_limit(monkeypatch):
    texts = {"bid": (DATA / MARIA / "contractor_bid.txt").read_text(),
             "est": (DATA / MARIA / "estimate_v2.txt").read_text(),
             "end": (DATA / MARIA / "endorsement_code_upgrade.txt").read_text()
             .replace("$25,000", "$10,000")}
    monkeypatch.setattr(tools, "_document", lambda ctx, name: {"text": texts[name]})
    amount, details = tools._calc_missing_coverage(
        None, {"bid": "bid", "estimate": "est", "endorsement": "end"})
    assert amount == Decimal(10000) and details["cappedAtLimit"] and details["absentTotal"] == 18700


def test_code_item_present_in_estimate_is_not_counted(monkeypatch):
    bid = (DATA / MARIA / "contractor_bid.txt").read_text()
    est = (DATA / MARIA / "estimate_v2.txt").read_text() + \
        "L999 | Interconnected smoke and CO alarm system | 1150.00 | 0.00 | 0.00\n"
    end = (DATA / MARIA / "endorsement_code_upgrade.txt").read_text()
    texts = {"bid": bid, "est": est, "end": end}
    monkeypatch.setattr(tools, "_document", lambda ctx, name: {"text": texts[name]})
    amount, _ = tools._calc_missing_coverage(None, {"bid": "bid", "estimate": "est", "endorsement": "end"})
    assert amount == Decimal(18700 - 1150)


def test_tools_never_touch_scorer_collections():  # T2.14 (static half)
    src = inspect.getsource(tools)
    assert "get_collection(" not in src.replace("get_agent_collection(", "")
    assert '"labels"' not in src and '"scores"' not in src
    for name in ("labels", "scores"):
        with pytest.raises(PermissionError):
            db.get_agent_collection(name)
        with pytest.raises(PermissionError):
            tools._coll(name)


# ---------- integration: ingestion (real Voyage) ----------

def _poll(fn, cond, timeout=120):
    deadline = time.monotonic() + timeout
    while True:
        result = fn()
        if cond(result) or time.monotonic() >= deadline:
            return result
        time.sleep(3)


@integ
def test_ingest_idempotent_voyage(seeded):  # T2.2
    def counts():
        return (db.documents.count_documents({}), db.policy_clauses.count_documents({}))

    ingest.ingest_claim(MARIA, exclude=("estimate_v3.txt",))
    first = counts()
    ingest.ingest_claim(MARIA, exclude=("estimate_v3.txt",))
    assert counts() == first
    assert first[0] == 9 and first[1] > 0
    assert db.documents.find_one({"filename": "estimate_v3.txt"}) is None
    for d in db.documents.find():
        assert len(d["embedding"]) == 1024 and d["receivedAt"].tzinfo is not None
    files = set()
    for c in db.policy_clauses.find():
        assert len(c["embedding"]) == 1024
        assert c["effectiveTo"] == OPEN_ENDED and isinstance(c["effectiveFrom"], datetime)
        files.add(c["filename"])
    assert files == {"policy.txt", "endorsement_code_upgrade.txt"}


@integ
def test_search_policy_labor_depreciation_voyage(seeded, vector_indexes):  # T2.3
    ingest.ingest_claim(MARIA, exclude=("estimate_v3.txt",))
    ctx = runs.start_run(MARIA)
    search = lambda: call_tool(ctx, "search_policy",
                               {"query": "labor depreciation", "asOf": "2026-07-18T00:00:00-05:00"})
    r = _poll(search, lambda r: r.get("results") and r["results"][0]["section"] == "4.2")
    top = ok(r)["results"][0]
    assert (top["filename"], top["section"]) == ("policy.txt", "4.2")


@integ
def test_search_policy_respects_effective_dates_voyage(seeded, vector_indexes):  # T2.4
    ingest.ingest_claim(MARIA, exclude=("estimate_v3.txt",))
    ctx = runs.start_run(MARIA)

    def search(as_of):
        return lambda: call_tool(ctx, "search_policy", {
            "query": "ordinance or law code upgrade coverage limit", "asOf": as_of})

    def has_endorsement(r):
        return any(x["filename"] == "endorsement_code_upgrade.txt" for x in r.get("results", []))

    after = _poll(search("2026-07-18T00:00:00-05:00"), has_endorsement)
    assert has_endorsement(ok(after))
    before = ok(search("2026-03-01T00:00:00-06:00")())
    assert before["results"], "policy clauses (effective 2025-04-01) should still be returned"
    assert not has_endorsement(before)


# ---------- integration: read tools ----------

@integ
def test_get_claim_state_and_rules(maria_plain):
    maria = maria_plain
    s = ok(call_tool(maria, "get_claim_state", {}))
    assert s["claim"]["claimId"] == MARIA and s["claim"]["insurer"] == "Harborline Mutual"
    assert s["claim"]["lossDate"].startswith("2026-05-30")
    assert s["paymentsByCategory"]["dwelling"]["total"] == 61200
    assert s["paymentsByCategory"]["ale"]["total"] == 8400
    assert len(s["documents"]) == 9 and s["openFindings"] == []
    r = ok(call_tool(maria, "get_rules", {}))
    assert r["version"] == 1 and {x["type"] for x in r["rules"]} == {"missing_coverage", "unpaid_ale"}


@integ
def test_list_and_read_documents(maria):
    names = lambda r: [d["filename"] for d in ok(r)["documents"]]
    early = names(call_tool(maria, "list_documents", {"asOf": "2026-06-30T00:00:00-05:00"}))
    assert "endorsement_code_upgrade.txt" in early and "estimate_v2.txt" not in early
    assert len(names(call_tool(maria, "list_documents", {}))) == 9
    assert "error" in call_tool(maria, "list_documents", {"asOf": "2026-06-30"})  # naive
    doc = ok(call_tool(maria, "read_document", {"filename": "estimate_v2.txt"}))
    assert doc["text"].splitlines()[0] == "   1: ESTIMATE"
    assert "error" in call_tool(maria, "read_document", {"filename": "estimate_v3.txt"})


# ---------- integration: facts and decisions ----------

def _fact(ctx, quote, valid_from, label, filename="estimate_v1.txt"):
    return call_tool(ctx, "record_fact", {"row": "estimate_total", "label": label, "validFrom": valid_from,
                                          "sourceFilename": filename, "quote": quote})


@integ
def test_record_fact_rejects_bad_quote(maria):  # T2.5
    r = _fact(maria, "Total: $99,999.00", V1_AT, "v1 total")
    assert "error" in r and "verbatim" in r["error"]
    assert db.facts.count_documents({}) == 0
    assert "error" in _fact(maria, "   1: ESTIMATE", V1_AT, "line-numbered quote")
    assert "error" in _fact(maria, "Total: $54,000.00", "2026-06-10", "naive date")
    assert "error" in _fact(maria, "Total: $54,000.00", V1_AT, "no such file", "nope.txt")
    ok(_fact(maria, "Total: $54,000.00", V1_AT, "v1 total"))
    stored = db.facts.find_one({})
    assert stored["validTo"] == OPEN_ENDED and isinstance(stored["validFrom"], datetime)


@integ
def test_supersede_fact(maria):  # T2.6
    old = ok(_fact(maria, "Total: $54,000.00", V1_AT, "v1 total"))["factId"]
    new = ok(_fact(maria, "Total: $61,200.00", V2_AT, "v2 total", "estimate_v2.txt"))["factId"]
    ok(call_tool(maria, "supersede_fact", {"oldFactId": old, "newFactId": new}))
    o, n = db.facts.find_one({"_id": old}), db.facts.find_one({"_id": new})
    assert o["validTo"] == n["validFrom"] and o["supersededBy"] == new
    assert n["validTo"] == OPEN_ENDED and n["supersededBy"] is None
    ok(call_tool(maria, "supersede_fact", {"oldFactId": old, "newFactId": new}))  # idempotent
    assert "error" in call_tool(maria, "supersede_fact", {"oldFactId": new, "newFactId": old})  # backwards
    assert "error" in call_tool(maria, "supersede_fact", {"oldFactId": old, "newFactId": "fact_nope"})


def _v2_decision(ctx):
    v2 = dataparse.parse_estimate_text((DATA / MARIA / "estimate_v2.txt").read_text())
    return call_tool(ctx, "record_decision", {
        "filename": "estimate_v2.txt", "madeAt": "2026-07-18T15:45:00-05:00",
        "citedFilenames": v2.relied_on})


@integ
def test_replay_finds_uncited_endorsement(maria):  # T2.7
    dec = ok(_v2_decision(maria))["decisionId"]
    r = ok(call_tool(maria, "replay", {"decisionId": dec}))
    cited = {d["filename"] for d in r["cited"]}
    not_cited = {d["filename"] for d in r["not_cited"]}
    assert cited == {"policy.txt", "estimate_v1.txt", "contractor_bid.txt"}
    assert "endorsement_code_upgrade.txt" in not_cited
    assert "ale_notice.txt" not in cited | not_cited  # received after the decision
    assert "estimate_v2.txt" not in cited | not_cited  # the decision itself
    again = ok(_v2_decision(maria))
    assert again["decisionId"] == dec and db.decisions.count_documents({}) == 1


@integ
def test_replay_unknown_decision_is_json_error(maria):  # T2.8
    r = call_tool(maria, "replay", {"decisionId": "dec_nonexistent"})
    assert set(r) == {"error"}


# ---------- integration: compute_amount ----------

CASES = {
    MARIA: [("labor_depreciation_refund", {"estimate": "estimate_v2.txt"}, "labor_depreciation"),
            ("missing_coverage", {"bid": "contractor_bid.txt", "estimate": "estimate_v2.txt",
                                  "endorsement": "endorsement_code_upgrade.txt"}, "missing_coverage"),
            ("unpaid_ale", {"promise": "adjuster_email_1.txt", "notice": "ale_notice.txt"}, "unpaid_ale")],
}
CASES[PARK] = CASES[MARIA]


def _compute_all(ctx):
    out = {}
    for rule, inputs, ftype in CASES[ctx.claimId]:
        out[ftype] = ok(call_tool(ctx, "compute_amount", {"rule": rule, "inputs": inputs}))
    return out


@integ
def test_compute_amount_maria(maria):  # T2.9
    calcs = _compute_all(maria)
    assert {t: c["amount"] for t, c in calcs.items()} == labelled(MARIA)
    assert calcs["labor_depreciation"]["details"]["lineCount"] == 41
    assert calcs["missing_coverage"]["details"]["cappedAtLimit"] is False
    stored = db.agent_runs.find_one({"runId": maria.runId})["calcs"]
    assert [c["calcId"] for c in stored] == [c["calcId"] for c in calcs.values()]
    assert sum(labelled(MARIA).values()) == 38400


@integ
def test_compute_amount_park(park):  # T2.10
    calcs = _compute_all(park)
    assert {t: c["amount"] for t, c in calcs.items()} == labelled(PARK) == {
        "labor_depreciation": 4200, "missing_coverage": 6900, "unpaid_ale": 2800}
    assert calcs["labor_depreciation"]["details"]["lineCount"] == 18


@integ
def test_compute_amount_errors_are_json(maria):
    assert "error" in call_tool(maria, "compute_amount", {"rule": "made_up", "inputs": {}})
    assert "error" in call_tool(maria, "compute_amount", {"rule": "unpaid_ale", "inputs": {}})
    assert "error" in call_tool(maria, "compute_amount", {
        "rule": "labor_depreciation_refund", "inputs": {"estimate": "estimate_v3.txt"}})
    assert "error" in call_tool(maria, "compute_amount", {
        "rule": "labor_depreciation_refund", "inputs": {"estimate": "policy.txt"}})


# ---------- integration: findings ----------

def _finding_args(calc_id, ftype="labor_depreciation", **over):
    args = {"type": ftype, "title": "Labor depreciated", "detail": "Estimate depreciates labor.",
            "calcId": calc_id, "evidence": [{"filename": "policy.txt",
                                             "quote": "Labor is not subject to depreciation."}]}
    return args | over


@integ
def test_upsert_finding_rejects_bad_calcs(maria, park):  # T2.11
    mine = _compute_all(maria)
    theirs = _compute_all(park)
    other_run = runs.start_run(MARIA)
    assert "unknown calcId" in call_tool(maria, "upsert_finding", _finding_args("calc_nope"))["error"]
    r = call_tool(maria, "upsert_finding", _finding_args(theirs["labor_depreciation"]["calcId"]))
    assert "different run or claim" in r["error"]
    r = call_tool(other_run, "upsert_finding", _finding_args(mine["labor_depreciation"]["calcId"]))
    assert "different run or claim" in r["error"]
    r = call_tool(maria, "upsert_finding", _finding_args(
        mine["unpaid_ale"]["calcId"], "labor_depreciation"))
    assert "cannot support" in r["error"]
    assert db.findings.count_documents({}) == 0


@integ
def test_upsert_finding_rejects_bad_evidence_and_decision(maria):
    calc = _compute_all(maria)["labor_depreciation"]["calcId"]
    bad_quote = _finding_args(calc, evidence=[{"filename": "policy.txt", "quote": "Labor is depreciated."}])
    assert "verbatim" in call_tool(maria, "upsert_finding", bad_quote)["error"]
    assert "error" in call_tool(maria, "upsert_finding", _finding_args(calc, decisionId="dec_nope"))
    assert "error" in call_tool(maria, "upsert_finding", _finding_args(calc, evidence=[]))
    assert db.findings.count_documents({}) == 0


@integ
def test_finding_amount_always_from_calc(maria):  # T2.12
    calcs = _compute_all(maria)
    calc = calcs["labor_depreciation"]
    assert "error" in call_tool(maria, "upsert_finding", _finding_args(calc["calcId"], amount=1))
    assert db.findings.count_documents({}) == 0
    dec = ok(_v2_decision(maria))["decisionId"]
    r = ok(call_tool(maria, "upsert_finding", _finding_args(calc["calcId"], decisionId=dec)))
    stored = db.findings.find_one({"claimId": MARIA, "type": "labor_depreciation"})
    assert stored["amount"] == calc["amount"] == r["amount"] == 11300
    assert stored["amountCents"] == 1130000 and stored["status"] == "open"
    assert stored["decisionId"] == dec and stored["createdAt"].tzinfo is not None


@integ
def test_upsert_finding_twice_updates_one(maria):  # T2.13
    calc = _compute_all(maria)["labor_depreciation"]["calcId"]
    first = ok(call_tool(maria, "upsert_finding", _finding_args(calc)))
    created = db.findings.find_one({})["createdAt"]
    second = ok(call_tool(maria, "upsert_finding", _finding_args(calc, title="Updated title")))
    assert (first["status"], second["status"]) == ("created", "updated")
    assert first["findingId"] == second["findingId"]
    assert db.findings.count_documents({"claimId": MARIA, "type": "labor_depreciation"}) == 1
    stored = db.findings.find_one({})
    assert stored["title"] == "Updated title" and stored["createdAt"] == created
    state = ok(call_tool(maria, "get_claim_state", {}))
    assert [f["type"] for f in state["openFindings"]] == ["labor_depreciation"]


@integ
def test_tools_never_read_labels_or_scores_at_runtime(maria, monkeypatch):  # T2.14
    touched = []
    real = db.get_agent_collection
    monkeypatch.setattr(db, "get_agent_collection", lambda n: (touched.append(n), real(n))[1])
    calcs = _compute_all(maria)
    dec = ok(_v2_decision(maria))["decisionId"]
    call_tool(maria, "get_claim_state", {})
    call_tool(maria, "get_rules", {})
    call_tool(maria, "list_documents", {})
    call_tool(maria, "read_document", {"filename": "policy.txt"})
    call_tool(maria, "replay", {"decisionId": dec})
    call_tool(maria, "upsert_finding", _finding_args(calcs["labor_depreciation"]["calcId"]))
    assert touched and not set(touched) & db.SCORER_ONLY
    assert db.labels.count_documents({}) > 0  # labels exist, but no tool reads them
