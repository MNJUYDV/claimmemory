"""Scores an agent run against ground-truth labels. Harness/eval code only.

Not a tool: the agent cannot call it and never sees labels (see db.get_agent_collection).
"""
from datetime import datetime, timezone

import db


def score_run(run_id: str) -> dict:
    run = db.get_collection("agent_runs").find_one({"runId": run_id})
    if not run:
        raise KeyError(f"unknown run {run_id!r}")
    claim_id = run["claimId"]
    labels = {l["type"]: l for l in db.get_collection("labels").find({"claimId": claim_id})}
    findings = {f["type"]: f for f in db.get_collection("findings").find({"claimId": claim_id, "runId": run_id})}
    docs = {d["filename"]: d["text"] for d in db.get_collection("documents").find({"claimId": claim_id})}

    caught = sorted(set(labels) & set(findings))
    missed = sorted(set(labels) - set(findings))
    false_positives = sorted(set(findings) - set(labels))
    amounts = {}
    for t in sorted(labels):
        f = findings.get(t)
        expected = int(round(float(labels[t]["amount"]) * 100))
        found = f["amountCents"] if f else None
        amounts[t] = {"expected": expected / 100, "found": None if found is None else found / 100,
                      "exact": found == expected}
    quotes = [(e["filename"], e["quote"]) for f in findings.values() for e in f.get("evidence", [])]
    verified = sum(1 for name, q in quotes if q and q in docs.get(name, ""))

    score = {
        "runId": run_id, "claimId": claim_id,
        "rulebookVersion": run.get("rulebookVersion"),
        "expected": sorted(labels), "caught": caught, "missed": missed, "falsePositives": false_positives,
        "caughtCount": len(caught), "expectedCount": len(labels),
        "amounts": amounts, "exactAmountCount": sum(1 for t in caught if amounts[t]["exact"]),
        "citations": {"total": len(quotes), "verified": verified},
        "scoredAt": datetime.now(timezone.utc),
    }
    db.upsert_one("scores", {"runId": run_id}, score)
    return score


def format_score(s: dict) -> str:
    lines = [f"Claim {s['claimId']}  run {s['runId']}  rulebook v{s['rulebookVersion']}",
             f"  caught {s['caughtCount']} of {s['expectedCount']}: {', '.join(s['caught']) or '-'}",
             f"  missed: {', '.join(s['missed']) or '-'}",
             f"  false positives: {', '.join(s['falsePositives']) or '-'}",
             f"  exact amounts: {s['exactAmountCount']} of {s['caughtCount']} caught",
             f"  citations verified: {s['citations']['verified']} of {s['citations']['total']}"]
    for t, a in s["amounts"].items():
        lines.append(f"    {t}: expected {a['expected']:,.2f}, found "
                     f"{'-' if a['found'] is None else format(a['found'], ',.2f')}"
                     f"{'  (exact)' if a['exact'] else ''}")
    return "\n".join(lines)
