"""Phase 6 offline checks: the agent is told which document triggered a run."""
from datetime import datetime, timezone

import agent
from listener import ClaimRunner


def test_new_document_line_format():
    got = agent._new_document_lines([{"filename": "estimate_v3.txt",
                                      "receivedAt": datetime(2026, 9, 26, 19, 52, 56, tzinfo=timezone.utc)}])
    assert got == "New document: estimate_v3.txt, received 2026-09-26T19:52:56+00:00."
    assert agent._new_document_lines(None) == ""


def test_runner_passes_the_triggering_documents():
    calls = []
    runner = ClaimRunner(run_fn=lambda c, triggered=None: calls.append((c, triggered)))
    runner.trigger("A", {"filename": "estimate_v3.txt", "receivedAt": "2026-09-26"})
    assert runner.wait_idle(10)
    assert calls == [("A", [{"filename": "estimate_v3.txt", "receivedAt": "2026-09-26"}])]


def test_runner_without_a_document_calls_run_fn_plainly():
    calls = []
    runner = ClaimRunner(run_fn=calls.append)
    runner.trigger("A")
    assert runner.wait_idle(10)
    assert calls == ["A"]


# ---------- deterministic timeline ----------

def _fake_docs(*paths):
    import ingest
    from pathlib import Path
    out = []
    for i, p in enumerate(paths):
        p = Path(p)
        out.append({"filename": p.name, "type": ingest.doc_type(p.name), "text": p.read_text(),
                    "receivedAt": datetime(2026, 6, 1 + i, 12, tzinfo=timezone.utc)})
    return out


class _Coll:
    def __init__(self, docs):
        self.docs = docs

    def find(self, *a, **k):
        return self

    def sort(self, *a, **k):
        return iter(self.docs)


def test_timeline_is_one_bar_per_row_and_document(monkeypatch):
    import workspace
    d, u = "data/HO-48213/", "data/test_uploads/HO-48213/"
    docs = _fake_docs(d + "policy.txt", d + "endorsement_code_upgrade.txt", d + "adjuster_email_1.txt",
                      d + "adjuster_email_2.txt", d + "estimate_v1.txt", d + "estimate_v2.txt", d + "ale_notice.txt",
                      d + "payments.json", u + "estimate_v3.txt", u + "ale_notice_revised.txt")
    monkeypatch.setattr(workspace, "_coll", lambda name: _Coll(docs + docs))  # duplicates must not matter
    tl = workspace.timeline("HO-48213")
    by = {(b["row"], b["sourceFilename"]): b for b in tl}
    assert len(tl) == len(by) == 10 - 1  # adjuster_email_2 makes no promise
    assert by["policy", "policy.txt"]["label"] == "Policy in force" and by["policy", "policy.txt"]["validFrom"] == "2026-04-01"
    end = by["policy", "endorsement_code_upgrade.txt"]
    assert end["label"] == "Code-upgrade coverage, $25,000" and end["note"].startswith("added to file ")
    assert by["promises", "adjuster_email_1.txt"]["label"] == "Living expenses promised to month 12"
    est = [by["estimates", f"estimate_v{n}.txt"] for n in (1, 2, 3)]
    assert [e["label"] for e in est] == ["Estimate v1 · $54,000", "Estimate v2 · $61,200", "Estimate v3 · $61,200"]
    assert [e["superseded"] for e in est] == [True, True, False]
    assert est[0]["validTo"] == est[1]["validFrom"] and est[2]["validTo"] is None
    assert by["living_expenses", "payments.json"]["label"] == "Paid, months 1–6"
    old, new = by["living_expenses", "ale_notice.txt"], by["living_expenses", "ale_notice_revised.txt"]
    assert (old["label"], old["superseded"]) == ("Cut off after month 6", True)
    assert (new["label"], new["superseded"]) == ("Cut off after month 10", False)
    assert all(len(b["label"]) <= 40 for b in tl)
