"""Load claims, payments, labels and rulebook v1 into the current DB (config.DB_NAME). Idempotent.

Does NOT load documents (see ingest.py). Labels are ground truth for the scorer only.
"""
import json

import db
from constants import parse_dt
from dataparse import DATA_DIR


def _load(name):
    return json.loads((DATA_DIR / name).read_text())


def _with_dates(doc, *fields):
    doc = dict(doc)
    for f in fields:
        doc[f] = parse_dt(doc[f])
    return doc


def seed() -> dict:
    claims = _load("claims.json")
    for c in claims:
        db.upsert_one("claims", {"claimId": c["claimId"]}, _with_dates(c, "lossDate"))

    n_pay = 0
    for c in claims:
        for p in _load(f"{c['claimId']}/payments.json"):
            db.upsert_one("payments", {"paymentId": p["paymentId"]}, _with_dates(p, "paidAt"))
            n_pay += 1

    n_lab = 0
    for claim_id, entry in _load("labels.json").items():
        for finding in entry["findings"]:
            db.upsert_one("labels", {"claimId": claim_id, "type": finding["type"]},
                          {"claimId": claim_id, **finding})
            n_lab += 1

    rulebook = _load("seed_rules.json")
    for rule in rulebook["rules"]:
        db.upsert_one("rules", {"id": rule["id"], "version": rule["version"]},
                      _with_dates(rule, "createdAt"))
    return {"claims": len(claims), "payments": n_pay, "labels": n_lab,
            "rules": len(rulebook["rules"]), "rulesVersion": rulebook["version"]}
