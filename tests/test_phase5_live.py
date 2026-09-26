"""The one live end-to-end check for Phase 5: a real server, a real change stream, real Claude.

Upload Maria's held estimate_v3 through the API with an SSE client connected; the review must start by itself.
Marker: llm (costs money). The server runs as a subprocess against claimmemory_test, on a scratch copy of data/.
"""
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest

import dataparse
import db
import rulebook
import seeding

pytestmark = pytest.mark.llm
ROOT = Path(__file__).resolve().parent.parent
PORT = 8765
BASE = f"http://127.0.0.1:{PORT}"
MARIA, INSURER = "HO-48213", "Harborline Mutual"
# The rule Phase 4 learned on the Park claim (its proposer's actual output), so this run is on rulebook v2.
LEARNED = {"type": "labor_depreciation", "computeRule": "labor_depreciation_refund",
           "instruction": "Check the policy for a clause stating labor is not subject to depreciation. Then review "
                          "the estimate's line items for any depreciation applied to labor amounts. If policy "
                          "prohibits labor depreciation but the estimate's depreciation method or line items "
                          "depreciate labor, sum the labor_depreciation column across all line items and report "
                          "that sum as owed."}


def wipe():
    for name in db.COLLECTION_NAMES:
        db.get_collection(name).delete_many({})


class SseClient(threading.Thread):
    """Reads /events in the background and timestamps every event as it arrives."""

    def __init__(self, url):
        super().__init__(daemon=True)
        self.url, self.events, self.connected, self.stop = url, [], threading.Event(), threading.Event()

    def run(self):
        event = None
        with httpx.stream("GET", self.url, timeout=httpx.Timeout(None, connect=10)) as r:
            for line in r.iter_lines():
                if self.stop.is_set():
                    return
                if line.startswith(": connected"):
                    self.connected.set()
                elif line.startswith("event: "):
                    event = line[7:]
                elif line.startswith("data: ") and event:
                    self.events.append({"type": event, "at": time.monotonic(), **json.loads(line[6:])})
                    event = None

    def wait_for(self, etype, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            hit = next((e for e in self.events if e["type"] == etype), None)
            if hit:
                return hit
            time.sleep(0.5)
        return None


@pytest.fixture(scope="module")
def live(vector_indexes, tmp_path_factory):
    wipe()
    seeding.seed()
    v = rulebook.create_candidate(INSURER, 1, LEARNED, {
        "proposedFromRunId": "run_from_phase4_park", "scoreBefore": {"caughtCount": 2, "expectedCount": 3}})
    rulebook.promote(INSURER, v, decision="promoted", scoreAfter={"caughtCount": 3, "expectedCount": 3},
                     reason="caught 2 -> 3 of 3 on the training claim (values from the Phase 4 run)")
    scratch = tmp_path_factory.mktemp("scratch") / "data"
    shutil.copytree(dataparse.DATA_DIR, scratch)
    env = {**os.environ, "DB_NAME": "claimmemory_test", "CLAIMMEMORY_DATA_DIR": str(scratch)}
    server = subprocess.Popen([sys.executable, "-m", "uvicorn", "main:app", "--port", str(PORT)],
                              cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        for _ in range(60):
            try:
                if httpx.get(f"{BASE}/health", timeout=2).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.5)
        else:
            pytest.fail("server did not start")
        sse = SseClient(f"{BASE}/api/claims/{MARIA}/events")
        sse.start()
        assert sse.connected.wait(10), "SSE client never connected"
        assert db.agent_runs.count_documents({}) == 0  # nothing has run yet

        v3 = (dataparse.DATA_DIR / MARIA / "estimate_v3.txt").read_bytes()
        t0 = time.monotonic()
        first = httpx.post(f"{BASE}/api/claims/{MARIA}/documents", files={"file": ("estimate_v3.txt", v3)}, timeout=120)
        started = sse.wait_for("run_started", 60)
        finished = sse.wait_for("run_finished", 1500)
        elapsed = (finished["at"] - t0) if finished else None
        runs_after = db.agent_runs.count_documents({})
        counts = {"facts": db.facts.count_documents({}), "findings": db.findings.count_documents({}),
                  "decisions": db.decisions.count_documents({})}

        again = httpx.post(f"{BASE}/api/claims/{MARIA}/documents", files={"file": ("estimate_v3.txt", v3)}, timeout=120)
        time.sleep(10)  # long enough for a (wrongly) triggered run to show up
        yield type("Live", (), dict(
            sse=sse, first=first, again=again, started=started, finished=finished, elapsed=elapsed,
            runs_after=runs_after, counts=counts, t0=t0,
            workspace=lambda: httpx.get(f"{BASE}/api/claims/{MARIA}/workspace").json()))
    finally:
        sse.stop.set()
        server.terminate()
        server.wait(10)
        wipe()


def test_upload_starts_a_run_by_itself_and_streams_steps_in_order(live):  # T5.1
    assert live.first.status_code == 200 and live.first.json()["duplicate"] is False
    assert live.started is not None, "no run_started event: the change stream did not trigger a review"
    assert live.finished is not None and live.finished["status"] == "completed", live.finished
    print(f"\nupload -> run finished: {live.elapsed:.1f} s")
    ev = [e for e in live.sse.events]
    types = [e["type"] for e in ev]
    assert types.index("run_started") < types.index("tool_call") < types.index("run_finished")
    tool_events = [e for e in ev if e["type"] == "tool_call"]
    assert [e["seq"] for e in tool_events] == list(range(1, len(tool_events) + 1))
    assert [e["step"] for e in tool_events] == sorted(e["step"] for e in tool_events)
    assert all(e["summary"] and "output" not in e for e in tool_events)
    assert [e["at"] for e in ev] == sorted(e["at"] for e in ev)  # arrived in order
    assert "finding_created" in types and "totals_changed" in types
    assert db.agent_runs.count_documents({"status": "completed"}) == 1


def test_three_findings_totaling_38400_citing_v3(live):  # T5.2
    ws = live.workspace()
    f = {x["type"]: x for x in ws["findings"]}
    assert {t: x["amount"] for t, x in f.items()} == {
        "labor_depreciation": 11300, "missing_coverage": 18700, "unpaid_ale": 8400}
    assert sum(x["amount"] for x in ws["findings"]) == 38400 and ws["totals"]["recoverable"] == 38400
    assert ws["latestRun"]["rulebookVersion"] == 2 and all(x["ruleVersion"] == 2 for x in ws["findings"])
    for t in ("labor_depreciation", "missing_coverage"):
        assert "estimate_v3.txt" in {e["filename"] for e in f[t]["evidence"]}, t
    run = db.agent_runs.find_one({"status": "completed"})
    calc = next(c for c in run["calcs"] if c["rule"] == "labor_depreciation_refund")
    assert calc["details"]["estimate"] == "estimate_v3.txt" and calc["details"]["lineCount"] == 41


def test_timeline_v3_supersedes_v2(live):  # T5.3
    facts = {f["sourceFilename"]: f for f in db.facts.find({"row": "estimate_total"})}
    v2, v3 = facts["estimate_v2.txt"], facts["estimate_v3.txt"]
    assert v2["supersededBy"] == v3["_id"] and v2["validTo"] == v3["validFrom"]
    assert v3["supersededBy"] is None and v3["validTo"].year == 9999
    rows = [r for r in live.workspace()["timeline"] if r["row"] == "estimates"]
    by_from = {r["validFrom"]: r for r in rows}
    assert by_from[v2["validFrom"].isoformat()]["superseded"] is True
    assert by_from[v3["validFrom"].isoformat()]["superseded"] is False
    assert by_from[v3["validFrom"].isoformat()]["validTo"] is None  # open-ended


def test_uploading_the_same_file_again_changes_nothing(live):  # T5.4
    assert live.again.status_code == 200 and live.again.json()["duplicate"] is True
    assert db.agent_runs.count_documents({}) == live.runs_after == 1
    assert {"facts": db.facts.count_documents({}), "findings": db.findings.count_documents({}),
            "decisions": db.decisions.count_documents({})} == live.counts
    assert len([e for e in live.sse.events if e["type"] == "run_started"]) == 1


def test_workspace_for_maria(live):  # T5.9
    ws = live.workspace()
    assert ws["header"]["family"] == "Alvarez family" and ws["header"]["insurer"] == INSURER
    assert ws["totals"] == {"paid": 61200.0, "recoverable": 38400.0, "owed": 99600.0}
    assert {"policy", "estimates", "promises"} <= {r["row"] for r in ws["timeline"]}
    assert ws["latestRun"]["status"] == "completed" and ws["latestRun"]["steps"]
    assert set(ws["latestRun"]["steps"][0]) == {"tool", "summary", "at"}
    blob = json.dumps(ws)
    for word in ("missDetails", "expectedAmount", "falsePositives", "caughtCount"):
        assert word not in blob


def test_replay_for_estimate_v2(live):  # T5.10
    dec = db.decisions.find_one({"claimId": MARIA, "filename": "estimate_v2.txt"})
    assert dec, "the agent should have recorded a decision for estimate v2"
    r = httpx.get(f"{BASE}/api/decisions/{dec['_id']}/replay").json()
    assert "endorsement_code_upgrade.txt" in {d["filename"] for d in r["notCited"]}
    assert "endorsement_code_upgrade.txt" not in {d["filename"] for d in r["cited"]}


def test_rulebook_v2_active_v1_inactive(live):  # T5.11
    hist = httpx.get(f"{BASE}/api/claims/{MARIA}/rulebook").json()
    assert [(h["version"], h["status"]) for h in hist] == [(2, "active"), (1, "inactive")]
    assert "labor_depreciation" in {r["type"] for r in hist[0]["rules"]}
    assert hist[0]["provenance"]["decision"] == "promoted"
    assert hist[0]["provenance"]["scoreBefore"] == {"caught": 2, "expected": 3}
    assert hist[0]["provenance"]["scoreAfter"] == {"caught": 3, "expected": 3}
