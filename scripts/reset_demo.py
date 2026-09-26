"""Put one claim back in its demo starting state, so a fresh review can be run and watched.

Deletes that claim's review data (documents, clauses, facts, decisions, findings, agent runs, scores),
re-holds its held document in data/manifest.json, and stamps addedInVersion on older rules. It does NOT touch
the rulebook (learned versions stay), labels, payments or any other claim. With --stop-before-improve it resets
both claims, removes learned rulebook versions and reviews both on v1.

Usage: python scripts/reset_demo.py --db claimmemory --claim HO-48213 --yes
       python scripts/reset_demo.py --db claimmemory --yes --stop-before-improve   (the full pre-improve demo state)
"""
import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

REVIEW_COLLECTIONS = ("documents", "policy_clauses", "facts", "decisions", "findings", "agent_runs", "scores")


def restore_manifest(dataparse, claim_id, canonical):
    """Original receivedAt/hold for the claim's own documents; drop and delete anything uploaded since."""
    path = dataparse.DATA_DIR / "manifest.json"
    entries = json.loads(path.read_text())
    changed, kept = 0, []
    for e in entries:
        if e["claimId"] != claim_id:
            kept.append(e)
        elif e["filename"] not in canonical["received"]:  # an uploaded document: remove its entry and copy
            (dataparse.DATA_DIR / claim_id / e["filename"]).unlink(missing_ok=True)
            changed += 1
        else:
            want = {"receivedAt": f"{canonical['received'][e['filename']]}{canonical['tz']}",
                    "hold": e["filename"] == canonical["hold"]}
            if any(e[k] != v for k, v in want.items()):
                e.update(want)
                changed += 1
            kept.append(e)
    if changed:
        path.write_text(json.dumps(kept, indent=2) + "\n")
    return changed


def reset_claim(db, dataparse, generate_data, claim_id):
    canonical = generate_data.CLAIMS.get(claim_id)
    if canonical is None or not db.get_collection("claims").find_one({"claimId": claim_id}):
        sys.exit(f"ERROR: unknown claim {claim_id!r}; seed the database first (scripts/seed.py)")
    for c in REVIEW_COLLECTIONS:
        db.get_collection(c).delete_many({"claimId": claim_id})
    return restore_manifest(dataparse, claim_id, canonical), canonical


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="database name")
    parser.add_argument("--claim", default="HO-48213", help="claim to reset (default HO-48213)")
    parser.add_argument("--yes", action="store_true", help="really delete (without it, only shows what would go)")
    parser.add_argument("--stop-before-improve", action="store_true",
                        help="reset Maria and Park, drop learned rulebook versions (back to v1), review both on v1 "
                             "(Park scores 2/3), and stop: uploading Park's settlement letter then runs the improvement")
    args = parser.parse_args()
    os.environ["DB_NAME"] = args.db  # must precede importing config

    import dataparse
    import db
    import generate_data
    import rulebook

    claims = ["HO-48213", "PK-20719"] if args.stop_before_improve else [args.claim]
    for claim_id in claims:
        if generate_data.CLAIMS.get(claim_id) is None or not db.get_collection("claims").find_one({"claimId": claim_id}):
            sys.exit(f"ERROR: unknown claim {claim_id!r}; seed the database first (scripts/seed.py)")
        counts = {c: db.get_collection(c).count_documents({"claimId": claim_id}) for c in REVIEW_COLLECTIONS}
        print(f"{args.db}: review data for {claim_id}: " + ", ".join(f"{c} {n}" for c, n in counts.items()))
    if not args.yes:
        sys.exit("Dry run. Re-run with --yes to delete it.")
    for claim_id in claims:
        changed, canonical = reset_claim(db, dataparse, generate_data, claim_id)
        print(f"{claim_id}: deleted the review data. Manifest entries restored: {changed}. "
              f"Held again: {canonical['hold']}.")
        insurer = rulebook.insurer_of(claim_id)
        stamped = rulebook.backfill_added_in_version(insurer)
    print(f"Rules stamped with addedInVersion: {stamped}. Active rulebook: v{rulebook.active_version_for_claim(claims[0])}.")
    if not args.stop_before_improve:
        return

    insurer = rulebook.insurer_of(claims[0])
    gone = db.get_collection("rules").delete_many({"insurer": insurer, "version": {"$gt": 1}}).deleted_count
    db.get_collection("rules").update_one(
        {"insurer": insurer, "version": 1, **rulebook.VERSION_DOC},
        {"$set": {"status": "active"}, "$unset": {"supersededByVersion": "", "retiredAt": ""}})
    print(f"Removed {gone} learned rulebook documents. Active rulebook: v{rulebook.active_version(insurer)}.")

    import agent
    import scorer
    for claim_id in claims:  # a review on v1 each; the agent ingests the documents itself
        result = agent.run_agent(claim_id, rulebook_version=1)
        print(f"\n{claim_id}: review {result.status} in {result.steps} steps")
        if result.status != "completed":
            sys.exit(f"ERROR: {result.error}")
        print(scorer.format_score(scorer.score_run(result.runId)))
    print("\nStopped before improve. Upload data/test_uploads/PK-20719/settlement_letter.txt to Park to run it.")


if __name__ == "__main__":
    main()
