"""Put one claim back in its demo starting state, so a fresh review can be run and watched.

Deletes that claim's review data (documents, clauses, facts, decisions, findings, agent runs, scores),
re-holds its held document in data/manifest.json, and stamps addedInVersion on older rules. It does NOT touch
the rulebook (learned versions stay), labels, payments or any other claim.

Usage: python scripts/reset_demo.py --db claimmemory --claim HO-48213 --yes
"""
import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

REVIEW_COLLECTIONS = ("documents", "policy_clauses", "facts", "decisions", "findings", "agent_runs", "scores")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="database name")
    parser.add_argument("--claim", default="HO-48213", help="claim to reset (default HO-48213)")
    parser.add_argument("--yes", action="store_true", help="really delete (without it, only shows what would go)")
    args = parser.parse_args()
    os.environ["DB_NAME"] = args.db  # must precede importing config

    import dataparse
    import db
    import generate_data
    import rulebook

    canonical = generate_data.CLAIMS.get(args.claim)
    if canonical is None or not db.get_collection("claims").find_one({"claimId": args.claim}):
        sys.exit(f"ERROR: unknown claim {args.claim!r}; seed the database first (scripts/seed.py)")

    counts = {c: db.get_collection(c).count_documents({"claimId": args.claim}) for c in REVIEW_COLLECTIONS}
    print(f"{args.db}: review data for {args.claim}: " + ", ".join(f"{c} {n}" for c, n in counts.items()))
    if not args.yes:
        sys.exit("Dry run. Re-run with --yes to delete it.")
    for c in REVIEW_COLLECTIONS:
        db.get_collection(c).delete_many({"claimId": args.claim})

    # the held document goes back on hold, with its original receivedAt
    path = dataparse.DATA_DIR / "manifest.json"
    entries = json.loads(path.read_text())
    changed = 0
    for e in entries:
        if e["claimId"] != args.claim or e["filename"] not in canonical["received"]:
            continue
        want = {"receivedAt": f"{canonical['received'][e['filename']]}{canonical['tz']}",
                "hold": e["filename"] == canonical["hold"]}
        if any(e[k] != v for k, v in want.items()):
            e.update(want)
            changed += 1
    if changed:
        path.write_text(json.dumps(entries, indent=2) + "\n")
    stamped = rulebook.backfill_added_in_version(rulebook.insurer_of(args.claim))
    print(f"Deleted the review data. Manifest entries restored: {changed}. Rules stamped with addedInVersion: {stamped}.")
    print(f"Held again: {canonical['hold']}. Active rulebook: v{rulebook.active_version_for_claim(args.claim)}.")


if __name__ == "__main__":
    main()
