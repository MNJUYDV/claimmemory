"""Improve the rulebook from one claim: review it (if needed), propose a rule for what was missed,
re-run with the candidate, and promote it only if the score strictly improves.

Usage: python scripts/improve.py --claim PK-20719 --db claimmemory
The database must already be seeded (scripts/seed.py). Calls Claude and Voyage.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--claim", required=True, help="training claim id, e.g. PK-20719")
    parser.add_argument("--db", required=True, help="database name")
    args = parser.parse_args()
    os.environ["DB_NAME"] = args.db  # must precede importing config

    import agent
    import db
    import improve
    import rulebook
    import scorer
    from events import bus

    if not db.get_collection("claims").find_one({"claimId": args.claim}):
        sys.exit(f"ERROR: unknown claim {args.claim!r}; run scripts/seed.py first")

    def show(event):
        if event["type"] in ("miss_detected", "rule_proposed", "candidate_scored", "rule_promoted",
                             "rule_rejected", "no_change"):
            print(f"  [{event['type']}]")
    bus.subscribe(show)

    active = rulebook.active_version_for_claim(args.claim)
    run = db.get_collection("agent_runs").find_one(
        {"claimId": args.claim, "status": "completed", "rulebookVersion": active}, sort=[("startedAt", -1)])
    if run:
        print(f"Using existing run {run['runId']} on rulebook v{active}")
    else:
        print(f"Running a review of {args.claim} on rulebook v{active} ...")
        result = agent.run_agent(args.claim)
        if result.status != "completed":
            sys.exit(f"ERROR: review ended {result.status}: {result.error}")
        run = {"runId": result.runId}
    before = scorer.score_run(run["runId"])
    print("\n== Score before ==")
    print(scorer.format_score(before))

    out = improve.improve_rulebook(args.claim, run["runId"])
    if out["decision"] == "no_change":
        print("\nNo change: nothing was missed.")
        return
    if out.get("proposal"):
        p = out["proposal"]
        print(f"\n== Proposed rule ==\n  type: {p['type']}\n  computeRule: {p['computeRule']}\n"
              f"  instruction: {p['instruction']}")
    if "candidateRunId" in out:
        print("\n== Candidate score ==")
        print(scorer.format_score(scorer.score_run(out["candidateRunId"])))
    print(f"\n== Decision: {out['decision'].upper()} ==\n  {out['reason']}")
    print(f"  active rulebook version is now v{rulebook.active_version_for_claim(args.claim)}")
    sys.exit(0 if out["decision"] == "promoted" else 1)


if __name__ == "__main__":
    main()
