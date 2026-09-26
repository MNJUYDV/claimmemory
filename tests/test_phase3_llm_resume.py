"""T3.8 and T3.9: durability of a real-Claude run. Marker: llm (costs money)."""
import pytest

import agent
import db
import scorer
import seeding

pytestmark = pytest.mark.llm
PARK = "PK-20719"


def wipe():
    for name in db.COLLECTION_NAMES:
        db.get_collection(name).delete_many({})


@pytest.fixture(scope="module", autouse=True)
def fresh_db(vector_indexes):
    wipe()
    seeding.seed()
    yield
    wipe()


def test_forced_failure_then_resume_completes_cleanly():  # T3.8
    def crash(step):
        if step == 5:
            raise RuntimeError("forced failure after step 5")

    failed = agent.run_agent(PARK, after_step=crash)
    assert (failed.status, failed.steps) == ("failed", 5)
    before = db.agent_runs.find_one({"runId": failed.runId})
    assert before["status"] == "failed" and before["stepCount"] == 5 and "forced failure" in before["error"]
    calls_before = len(before["toolCalls"])

    done = agent.resume_run(failed.runId)
    assert done.status == "completed" and done.steps > 5
    run = db.agent_runs.find_one({"runId": failed.runId})
    assert run["status"] == "completed" and run["messages"][:len(before["messages"])] == before["messages"]
    later = run["toolCalls"][calls_before:]
    assert later and min(c["step"] for c in later) == 6  # continued from step 6, did not restart
    assert [c["seq"] for c in run["toolCalls"]] == list(range(1, len(run["toolCalls"]) + 1))

    facts = list(db.facts.find({"claimId": PARK}))
    identities = [(f["row"], f["label"], f["validFrom"], f["sourceFilename"], f["quote"]) for f in facts]
    assert len(identities) == len(set(identities))  # no duplicate facts
    decisions = [d["filename"] for d in db.decisions.find({"claimId": PARK})]
    assert len(decisions) == len(set(decisions))
    types = [f["type"] for f in db.findings.find({"claimId": PARK})]
    assert sorted(types) == ["missing_coverage", "unpaid_ale"]  # one finding per type, none doubled
    s = scorer.score_run(failed.runId)
    assert (s["caughtCount"], s["falsePositives"], s["exactAmountCount"]) == (2, [], 2)


def test_step_limit_of_three_gives_needs_review():  # T3.9
    result = agent.run_agent(PARK, max_steps=3)
    assert result.status == "needs_review" and result.steps == 3
    run = db.agent_runs.find_one({"runId": result.runId})
    assert run["status"] == "needs_review" and "step limit" in run["note"]
    assert run["stepCount"] == 3 and len(run["messages"]) == 7  # user + 3 x (assistant, tool results)
    assert scorer.score_run(result.runId)["caughtCount"] <= 2  # scoring a partial run works too
