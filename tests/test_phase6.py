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
