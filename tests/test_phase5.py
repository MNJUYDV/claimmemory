"""Phase 5 tests that need no LLM: upload, the change-stream trigger, the SSE stream and the read API.
The one live end-to-end check (real server, real Claude) is tests/test_phase5_live.py."""
import json
import shutil
import threading
import time
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

import api
import dataparse
import db
import ingest
import listener
import rulebook
import runs
import tools
import workspace
from events import bus
from listener import ClaimRunner, Listener
from main import app
from tests.test_phase2_tools import MARIA, PARK, fake_embed, integ, maria_plain, ok, seeded  # noqa: F401
from tests.test_phase3 import reply, say, use  # noqa: F401
from tests.test_phase4 import FilingAgent
from tools import call_tool

client = TestClient(app)  # no `with`: the lifespan (and so the real listener) does not start
V3 = dataparse.DATA_DIR / MARIA / "estimate_v3.txt"
INSURER = "Harborline Mutual"


@pytest.fixture
def scratch_data(tmp_path, monkeypatch):
    """A private copy of data/ so uploads never touch the repo's files."""
    copy = tmp_path / "data"
    shutil.copytree(dataparse.DATA_DIR, copy)
    monkeypatch.setattr(dataparse, "DATA_DIR", copy)
    return copy


def upload(claim=MARIA, name="estimate_v3.txt", data=None):
    data = V3.read_bytes() if data is None else data
    return client.post(f"/api/claims/{claim}/documents", files={"file": (name, data, "text/plain")})


# ---------- upload ----------

@integ
def test_upload_saves_registers_and_ingests_without_starting_the_agent(seeded, fake_embed, scratch_data):
    r = upload()
    assert r.status_code == 200 and r.json()["duplicate"] is False
    entry = next(e for e in json.loads((scratch_data / "manifest.json").read_text())
                 if e["claimId"] == MARIA and e["filename"] == "estimate_v3.txt")
    assert entry["hold"] is False
    received = datetime.fromisoformat(entry["receivedAt"])
    assert received.tzinfo is not None and abs((datetime.now(timezone.utc) - received).total_seconds()) < 60
    doc = db.documents.find_one({"claimId": MARIA, "filename": "estimate_v3.txt"})
    assert doc["source"] == "upload" and len(doc["embedding"]) == 1024 and doc["hold"] is False
    assert doc["receivedAt"].replace(microsecond=0) == received.replace(microsecond=0)
    assert db.agent_runs.count_documents({}) == 0  # the upload does not start a review


def test_repo_manifest_untouched_by_upload_tests():
    real = [e for e in json.loads((dataparse.DATA_DIR / "manifest.json").read_text()) if e["hold"]]
    assert [(e["claimId"], e["filename"]) for e in real] == [(MARIA, "estimate_v3.txt")]


@integ
def test_upload_of_a_new_file_appends_to_the_manifest(seeded, fake_embed, scratch_data):
    r = upload(name="adjuster_email_3.txt", data=b"Dear Ms. Alvarez, a short note about your claim.\n")
    assert r.status_code == 200
    entries = json.loads((scratch_data / "manifest.json").read_text())
    assert [e for e in entries if e["filename"] == "adjuster_email_3.txt"][0]["hold"] is False
    assert (scratch_data / MARIA / "adjuster_email_3.txt").exists()
    assert db.documents.find_one({"filename": "adjuster_email_3.txt"})["type"] == "email"


@integ
def test_uploading_the_same_file_again_is_a_no_op(seeded, fake_embed, scratch_data):
    first = upload().json()
    manifest_after_first = (scratch_data / "manifest.json").read_text()
    second = upload()
    assert second.status_code == 200 and second.json()["duplicate"] is True
    assert (scratch_data / "manifest.json").read_text() == manifest_after_first  # receivedAt not bumped
    assert db.documents.count_documents({"filename": "estimate_v3.txt"}) == 1 and first["duplicate"] is False


@integ
@pytest.mark.parametrize("claim,name,data,status", [
    ("NOPE-1", "estimate_v3.txt", b"x", 404),
    (MARIA, "../evil.txt", b"x", 400),
    (MARIA, "evil.exe", b"x", 400),
    (MARIA, ".hidden.txt", b"x", 400),
    (MARIA, "bin.txt", b"\xff\xfe\x00", 400),
    (MARIA, "big.txt", b"x" * (ingest.MAX_UPLOAD_BYTES + 10), 413),
])
def test_bad_uploads_rejected(seeded, fake_embed, scratch_data, claim, name, data, status):
    assert upload(claim, name, data).status_code == status
    assert db.documents.count_documents({}) == 0


# ---------- trigger logic (no database) ----------

def test_runner_runs_one_at_a_time_and_queues_a_single_follow_up():
    gate, started, calls = threading.Event(), threading.Event(), []

    def slow(claim_id):
        calls.append(claim_id)
        started.set()
        gate.wait(10)
    runner = ClaimRunner(run_fn=slow)
    assert runner.trigger("A") == "started"
    assert started.wait(5)
    assert [runner.trigger("A") for _ in range(4)] == ["queued"] * 4  # four arrivals, one follow-up
    assert runner.trigger("B") == "started"  # other claims are independent
    gate.set()
    assert runner.wait_idle(10)
    assert sorted(calls) == ["A", "A", "B"]


def test_runner_survives_a_crashing_run():
    calls = []

    def boom(claim_id):
        calls.append(claim_id)
        raise RuntimeError("boom")
    runner = ClaimRunner(run_fn=boom)
    runner.trigger("A")
    assert runner.wait_idle(10) and calls == ["A"]
    assert runner.trigger("A") == "started"  # not stuck "running"
    runner.wait_idle(10)


def test_seed_and_bulk_inserts_are_ignored():
    runner = ClaimRunner(run_fn=lambda c: pytest.fail("must not run"))
    for source in ("seed", "bulk"):
        assert listener.handle_insert({"claimId": MARIA, "filename": "x.txt", "source": source}, runner) == "ignored"


# ---------- the real change stream (Atlas, no LLM) ----------

def wait_for(cond, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.25)
    return False


@integ
def test_change_stream_starts_a_review_only_for_uploads(seeded, fake_embed, scratch_data):
    calls = []
    runner = ClaimRunner(run_fn=calls.append)
    lst = Listener(runner).start()
    try:
        ingest.ingest_claim(MARIA, exclude=("estimate_v3.txt",))  # bulk inserts: no trigger
        ingest.ingest_document(PARK, "policy.txt", source="seed")
        time.sleep(4)
        assert calls == []
        assert upload().status_code == 200  # an upload: trigger
        assert wait_for(lambda: calls == [MARIA]), calls
        runner.wait_idle(10)
        assert upload().json()["duplicate"] is True  # same file again: no new insert, no trigger
        time.sleep(4)
        assert calls == [MARIA]
    finally:
        lst.stop()


@integ
def test_documents_a_run_already_reviewed_are_skipped(seeded, fake_embed, scratch_data):
    ctx = runs.start_run(MARIA)
    db.agent_runs.update_one({"runId": ctx.runId}, {"$set": {"documentFilenames": ["estimate_v3.txt"]}})
    runner = ClaimRunner(run_fn=lambda c: pytest.fail("must not run"))
    assert listener.handle_insert({"claimId": MARIA, "filename": "estimate_v3.txt", "source": "upload"}, runner) \
        == "reviewed"
    db.agent_runs.update_one({"runId": ctx.runId}, {"$set": {"status": "failed"}})  # failed runs don't count
    assert listener.already_reviewed(MARIA, "estimate_v3.txt") is False


# ---------- SSE (no database) ----------

def publish(claim, etype, **f):
    bus.publish({"type": etype, "runId": "r1", "claimId": claim, "ts": "t", **f})


def test_sse_stream_filters_by_claim_formats_events_and_heartbeats():
    before = len(bus._subs)
    gen = api.sse_stream("A", heartbeat=0.05)
    assert next(gen) == ": connected\n\n"
    publish("B", "run_started")  # another claim: never delivered
    publish("A", "tool_call", seq=1, name="get_rules", output={"big": "x" * 5000}, summary="s")
    publish("A", "finding_created", findingType="unpaid_ale", amount=8400)
    chunk = next(gen)
    assert chunk.startswith("event: tool_call\ndata: ")
    data = json.loads(chunk.split("data: ", 1)[1])
    assert data["seq"] == 1 and "output" not in data and data["summary"] == "s"
    assert next(gen).startswith("event: finding_created\n")
    assert next(gen).startswith(": heartbeat")  # idle -> heartbeat
    gen.close()
    assert len(bus._subs) == before  # unsubscribed


def test_sse_heartbeat_default_is_fifteen_seconds():
    assert api.HEARTBEAT_SECONDS == 15


def test_cors_allows_only_the_vite_dev_server():
    ok_ = client.options("/api/claims/X/workspace", headers={
        "Origin": "http://localhost:5173", "Access-Control-Request-Method": "GET"})
    assert ok_.headers["access-control-allow-origin"] == "http://localhost:5173"
    other = client.options("/api/claims/X/workspace", headers={
        "Origin": "http://evil.example", "Access-Control-Request-Method": "GET"})
    assert "access-control-allow-origin" not in other.headers


# ---------- finding events from the loop ----------

@integ
def test_findings_emit_created_events_and_totals(seeded, fake_embed):
    import agent
    seen = []
    unsub = bus.subscribe(seen.append)
    result = agent.run_agent(PARK, client=FilingAgent(["missing_coverage", "unpaid_ale"]))
    unsub()
    assert result.status == "completed"
    names = [e["type"] for e in seen if e["runId"] == result.runId]
    assert names.count("finding_created") == 2 and names.count("totals_changed") == 2
    last = [e for e in seen if e["type"] == "totals_changed"][-1]["totals"]
    assert last == {"paid": 34500.0, "recoverable": 9700.0, "owed": 44200.0}
    tool_events = [e for e in seen if e["type"] == "tool_call"]
    assert all(e["summary"] for e in tool_events)
    run = db.agent_runs.find_one({"runId": result.runId})
    assert "policy.txt" in run["documentFilenames"] and len(run["documentFilenames"]) == 9


# ---------- read API ----------

def build_maria_state(ctx):
    """Facts (v1 superseded by v2), a decision on v2, and two findings, filed through the real tools."""
    def fact(label, filename, quote, at):
        return ok(call_tool(ctx, "record_fact", {"row": "estimate_total", "label": label, "validFrom": at,
                                                 "sourceFilename": filename, "quote": quote}))["factId"]
    f1 = fact("Estimate v1 total", "estimate_v1.txt", "Total: $54,000.00", "2026-06-10T17:30:00-05:00")
    f2 = fact("Estimate v2 total", "estimate_v2.txt", "Total: $61,200.00", "2026-07-18T15:45:00-05:00")
    ok(call_tool(ctx, "supersede_fact", {"oldFactId": f1, "newFactId": f2}))
    fact("ALE promised through month 12", "adjuster_email_1.txt", "through month 12 at $1,400 per month",
         "2026-06-03T14:20:00-05:00")
    fact("Policy: labor not depreciated", "policy.txt", "Labor is not subject to depreciation.",
         "2025-04-01T00:00:00+00:00")
    v2 = dataparse.parse_estimate(dataparse.DATA_DIR / MARIA / "estimate_v2.txt")
    dec = ok(call_tool(ctx, "record_decision", {"filename": "estimate_v2.txt", "madeAt": "2026-07-18T15:45:00-05:00",
                                                "citedFilenames": v2.relied_on}))["decisionId"]
    for rule, inputs, ftype, evidence in (
            ("missing_coverage", {"bid": "contractor_bid.txt", "estimate": "estimate_v2.txt",
                                  "endorsement": "endorsement_code_upgrade.txt"}, "missing_coverage",
             [{"filename": "endorsement_code_upgrade.txt", "quote": "Limit of liability: $25,000"}]),
            ("unpaid_ale", {"promise": "adjuster_email_1.txt", "notice": "ale_notice.txt"}, "unpaid_ale",
             [{"filename": "ale_notice.txt", "quote": "ALE payments end after month 6"}])):
        calc = ok(call_tool(ctx, "compute_amount", {"rule": rule, "inputs": inputs}))
        ok(call_tool(ctx, "upsert_finding", {"type": ftype, "title": f"{ftype} finding", "summary": "s",
                                             "points": ["Policy: p1 text.", "On file: p2 text."],
                                             "calcId": calc["calcId"], "evidence": evidence, "decisionId": dec}))
    return dec


@integ
def test_workspace_totals_rows_findings_and_no_label_data(maria_plain):  # T5.9
    build_maria_state(maria_plain)
    spy, real = [], db.get_agent_collection
    db.get_agent_collection = lambda n: (spy.append(n), real(n))[1]
    try:
        r = client.get(f"/api/claims/{MARIA}/workspace")
    finally:
        db.get_agent_collection = real
    assert r.status_code == 200
    ws = r.json()
    assert set(ws) == {"header", "totals", "timeline", "findings", "resolvedFindings", "latestRun"}
    assert ws["header"]["family"] == "Alvarez family" and ws["header"]["lossType"] == "fire"
    assert ws["header"]["insurer"] == INSURER and ws["header"]["day"] > 0
    assert ws["totals"] == {"paid": 61200.0, "recoverable": 27100.0, "owed": 88300.0}
    rows = ws["timeline"]
    assert {r_["row"] for r_ in rows} == {"policy", "estimates", "promises"}
    est = [r_ for r_ in rows if r_["row"] == "estimates"]
    assert [r_["superseded"] for r_ in est] == [True, False]
    assert est[0]["validTo"] == est[1]["validFrom"] and est[1]["validTo"] is None  # v2 is open-ended
    assert {f["type"]: f["amount"] for f in ws["findings"]} == {"missing_coverage": 18700, "unpaid_ale": 8400}
    f = ws["findings"][0]
    assert set(f) == {"type", "title", "summary", "points", "amount", "evidence", "decisionId", "ruleVersion",
                "ruleId", "ruleAddedInVersion"}
    assert f["ruleVersion"] == 1 and f["decisionId"].startswith("dec_") and f["evidence"][0]["quote"]
    assert ws["latestRun"]["id"] == maria_plain.runId and ws["latestRun"]["rulebookVersion"] == 1
    # no label data: never touched labels/scores, and nothing label-shaped in the payload
    assert not {"labels", "scores"} & set(spy) and spy
    blob = r.text
    for word in ("missDetails", "expectedAmount", "falsePositives", "caughtCount", "\"labels\""):
        assert word not in blob
    assert client.get("/api/claims/NOPE/workspace").status_code == 404


@integ
def test_replay_endpoint_lists_the_uncited_endorsement(maria_plain):  # T5.10
    dec = build_maria_state(maria_plain)
    r = client.get(f"/api/decisions/{dec}/replay")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"filename", "madeAt", "cited", "notCited"} and body["filename"] == "estimate_v2.txt"
    assert {d["filename"] for d in body["cited"]} == {"policy.txt", "estimate_v1.txt", "contractor_bid.txt"}
    assert "endorsement_code_upgrade.txt" in {d["filename"] for d in body["notCited"]}
    assert all(set(d) == {"filename", "receivedAt"} for d in body["cited"] + body["notCited"])
    assert client.get("/api/decisions/dec_nope/replay").status_code == 404


@integ
def test_rulebook_endpoint_newest_first_with_provenance(maria_plain):  # T5.11 (mapping; live flow checks it again)
    assert [v["version"] for v in client.get(f"/api/claims/{MARIA}/rulebook").json()] == [1]
    rule = {"type": "labor_depreciation", "instruction": "i" * 40, "computeRule": "labor_depreciation_refund"}
    score = lambda n: {"caughtCount": n, "expectedCount": 3}
    v = rulebook.create_candidate(INSURER, 1, rule, {"proposedFromRunId": "run_x", "scoreBefore": score(2)})
    rulebook.promote(INSURER, v, decision="promoted", reason="caught 2 -> 3", scoreAfter=score(3))
    hist = client.get(f"/api/claims/{MARIA}/rulebook").json()
    assert [(h["version"], h["status"]) for h in hist] == [(2, "active"), (1, "inactive")]
    assert {r_["type"] for r_ in hist[0]["rules"]} == {"labor_depreciation", "missing_coverage", "unpaid_ale"}
    assert hist[0]["provenance"] == {"scoreBefore": {"caught": 2, "expected": 3},
                                     "scoreAfter": {"caught": 3, "expected": 3},
                                     "decision": "promoted", "reason": "caught 2 -> 3"}
    assert {r_["type"] for r_ in hist[1]["rules"]} == {"missing_coverage", "unpaid_ale"}
    assert hist[1]["provenance"] == {"scoreBefore": None, "scoreAfter": None, "decision": None, "reason": None}
    assert client.get("/api/claims/NOPE/rulebook").status_code == 404
