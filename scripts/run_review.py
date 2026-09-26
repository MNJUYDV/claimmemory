"""Run an agent review of one claim, then score it against the labels.

Usage: python scripts/run_review.py --claim HO-48213 --db claimmemory
The database must already be seeded (scripts/seed.py). Calls Claude and Voyage.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--claim", required=True, help="claim id, e.g. HO-48213")
    parser.add_argument("--db", required=True, help="database name")
    parser.add_argument("--max-steps", type=int, default=40)
    args = parser.parse_args()
    os.environ["DB_NAME"] = args.db  # must precede importing config

    import agent
    import scorer
    from events import bus

    def show(event):
        if event["type"] == "tool_call":
            flag = "ERR" if event["isError"] else "ok "
            print(f"  step {event['step']:>2} {flag} {event['name']} ({event['durationMs']} ms)")
    bus.subscribe(show)

    try:
        result = agent.run_agent(args.claim, max_steps=args.max_steps)
    except ValueError as e:
        sys.exit(f"ERROR: {e}")
    print(f"\nRun {result.runId}: {result.status} after {result.steps} steps")
    if result.summary:
        print(f"\nSummary: {result.summary}\n")
    if result.error:
        print(f"Note: {result.error}")
    print(scorer.format_score(scorer.score_run(result.runId)))
    sys.exit(0 if result.status == "completed" else 1)


if __name__ == "__main__":
    main()
