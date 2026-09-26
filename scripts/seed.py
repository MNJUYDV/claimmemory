"""Load claims, payments, labels and rulebook v1. Idempotent.

Does NOT load documents: the agent ingests those later. Labels are ground truth for the
scorer only; the agent must never read them (see db.get_agent_collection).

Usage: python scripts/seed.py --db claimmemory
"""
import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

DATA = os.path.join(ROOT, "data")


def load(name):
    with open(os.path.join(DATA, name)) as f:
        return json.load(f)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="database name")
    args = parser.parse_args()
    os.environ["DB_NAME"] = args.db  # must precede importing config

    import db
    from constants import parse_dt

    def with_dates(doc, *fields):
        doc = dict(doc)
        for f in fields:
            doc[f] = parse_dt(doc[f])
        return doc

    claims = load("claims.json")
    for c in claims:
        db.upsert_one("claims", {"claimId": c["claimId"]}, with_dates(c, "lossDate"))

    n_pay = 0
    for c in claims:
        with open(os.path.join(DATA, c["claimId"], "payments.json")) as f:
            for p in json.load(f):
                db.upsert_one("payments", {"paymentId": p["paymentId"]}, with_dates(p, "paidAt"))
                n_pay += 1

    n_lab = 0
    for claim_id, entry in load("labels.json").items():
        for finding in entry["findings"]:
            db.upsert_one("labels", {"claimId": claim_id, "type": finding["type"]},
                          {"claimId": claim_id, **finding})
            n_lab += 1

    rulebook = load("seed_rules.json")
    for rule in rulebook["rules"]:
        db.upsert_one("rules", {"id": rule["id"], "version": rule["version"]},
                      with_dates(rule, "createdAt"))

    print(f"Seeded {args.db}: {len(claims)} claims, {n_pay} payments, {n_lab} labels, "
          f"{len(rulebook['rules'])} rules (v{rulebook['version']})")


if __name__ == "__main__":
    main()
