"""Phase 6 live check: upload Maria's five follow-up documents in order, through a real server.

After each upload the review must finish and the recoverable total must move as expected; findings the
newer documents fix are resolved, and the timeline ends with v5 current. Marker: llm (costs money).
Runs against claimmemory_test on a scratch copy of data/, on rulebook v2.
"""
import os
import re
import shutil
import subprocess
import sys
import time

import httpx
import pytest

import dataparse
import rulebook
import seeding
from tests.test_phase5_live import BASE, INSURER, LEARNED, MARIA, PORT, ROOT, SseClient, wipe

pytestmark = pytest.mark.llm
UPLOADS = ["estimate_v3.txt", "rebuild_schedule.txt", "estimate_v4.txt", "ale_notice_revised.txt", "estimate_v5.txt"]
# v4 adds CU1+CU4+CU5 ($10,500) from the bid; the revised notice cuts unpaid ALE from $8,400 to $2,800
EXPECTED = [38400, 38400, 27900, 22300, 2800]
RUN_TIMEOUT = 1500


@pytest.fixture(scope="module")
def steps(vector_indexes, tmp_path_factory):
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
    sse = None
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
        assert sse.connected.wait(10)
        out = []
        for name in UPLOADS:
            before = sum(e["type"] == "run_finished" for e in sse.events)
            data = (ROOT / "data" / "test_uploads" / MARIA / name).read_bytes()
            t0 = time.monotonic()
            r = httpx.post(f"{BASE}/api/claims/{MARIA}/documents", files={"file": (name, data)}, timeout=120)
            assert r.status_code == 200, r.text
            deadline = time.monotonic() + RUN_TIMEOUT
            while sum(e["type"] == "run_finished" for e in sse.events) <= before and time.monotonic() < deadline:
                time.sleep(0.5)
            done = sum(e["type"] == "run_finished" for e in sse.events) > before
            elapsed = time.monotonic() - t0
            ws = httpx.get(f"{BASE}/api/claims/{MARIA}/workspace").json()
            out.append({"file": name, "finished": done, "seconds": round(elapsed, 1), "ws": ws})
            print(f"[phase6] {name}: {elapsed:.1f}s, recoverable {ws['totals']['recoverable']}, "
                  f"open {[f['type'] for f in ws['findings']]}, resolved {[f['type'] for f in ws['resolvedFindings']]}")
        yield out
    finally:
        if sse:
            sse.stop.set()
        server.terminate()
        try:
            server.wait(10)
        except subprocess.TimeoutExpired:  # uvicorn waits on the open SSE stream
            server.kill()
        wipe()


def test_recoverable_after_each_upload(steps):
    assert [s["finished"] for s in steps] == [True] * len(UPLOADS)
    assert [round(s["ws"]["totals"]["recoverable"]) for s in steps] == EXPECTED


def test_findings_resolved_by_v5(steps):
    ws = steps[-1]["ws"]
    assert [f["type"] for f in ws["findings"]] == ["unpaid_ale"]
    resolved = {f["type"]: f for f in ws["resolvedFindings"]}
    assert set(resolved) == {"labor_depreciation", "missing_coverage"}
    assert all(f["resolvedByFilename"] == "estimate_v5.txt" for f in resolved.values())


def test_timeline_v5_current(steps):
    est = [t for t in steps[-1]["ws"]["timeline"] if t["row"] == "estimates"
           and re.search(r"\bestimate v[1-5]\b", t["label"], re.I)]
    by_version = {}
    for t in est:
        by_version.setdefault(int(re.search(r"v([1-5])", t["label"], re.I).group(1)), []).append(t["superseded"])
    assert sorted(by_version) == [1, 2, 3, 4, 5], by_version
    assert all(by_version[v] == [True] * len(by_version[v]) for v in (1, 2, 3, 4)), by_version
    assert by_version[5] == [False] * len(by_version[5]), by_version
