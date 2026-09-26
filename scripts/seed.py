"""Load claims, payments, labels and rulebook v1. Idempotent.

Does NOT load documents: the agent ingests those later. Labels are ground truth for the
scorer only; the agent must never read them (see db.get_agent_collection).

Usage: python scripts/seed.py --db claimmemory
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="database name")
    args = parser.parse_args()
    os.environ["DB_NAME"] = args.db  # must precede importing config

    import seeding
    r = seeding.seed()
    print(f"Seeded {args.db}: {r['claims']} claims, {r['payments']} payments, {r['labels']} labels, "
          f"{r['rules']} rules (v{r['rulesVersion']})")


if __name__ == "__main__":
    main()
