"""Read models for the UI. Reads only through db.get_agent_collection(): labels and scores are unreachable."""
from datetime import datetime, timezone
from decimal import Decimal

import db
import ingest
import rulebook
from constants import OPEN_ENDED

# fact source document type -> timeline row
TIMELINE_ROW = {"policy": "policy", "endorsement": "policy", "estimate": "estimates", "bid": "estimates",
                "email": "promises", "notice": "living_expenses", "payments": "living_expenses"}


def _coll(name):
    return db.get_agent_collection(name)


def _iso(dt):
    return None if dt is None or dt == OPEN_ENDED else dt.isoformat()


def totals(claim_id: str) -> dict:
    """paid = dwelling payments; recoverable = sum of open findings; owed = paid + recoverable."""
    paid = sum((Decimal(str(p["amount"])) for p in _coll("payments").find(
        {"claimId": claim_id, "category": "dwelling"})), Decimal(0))
    recoverable = sum((Decimal(str(f["amount"])) for f in _coll("findings").find(
        {"claimId": claim_id, "status": {"$ne": "resolved"}})), Decimal(0))
    return {"paid": float(paid), "owed": float(paid + recoverable), "recoverable": float(recoverable)}


def summarize_call(name: str, args: dict, output: dict) -> str:
    """One short line describing a tool call, for the step list."""
    if "error" in output:
        return f"error: {output['error']}"[:160]
    if name == "read_document":
        return f"read {args.get('filename')}"
    if name == "record_fact":
        return f"fact: {args.get('label')}"[:120]
    if name == "supersede_fact":
        return "superseded an older fact"
    if name == "record_decision":
        return f"decision on {args.get('filename')}"
    if name == "replay":
        return f"replayed: {len(output.get('not_cited', []))} document(s) not cited"
    if name == "search_policy":
        return f"searched policy: {args.get('query')}"[:120]
    if name == "compute_amount":
        return f"{args.get('rule')} = {output.get('amount')}"
    if name == "resolve_finding":
        return f"finding {args.get('type')} resolved"
    if name == "upsert_finding":
        return f"finding {args.get('type')} {output.get('status')} ({output.get('amount')})"
    return name


def header(claim: dict) -> dict:
    family = claim["insuredName"].split()[-1] + " family"
    day = max((datetime.now(timezone.utc) - claim["lossDate"]).days, 0)
    return {"family": family, "lossType": claim["lossType"], "insurer": claim["insurer"], "day": day}


def timeline(claim_id: str) -> list:
    types = {d["filename"]: d["type"] for d in _coll("documents").find({"claimId": claim_id}, {"filename": 1, "type": 1})}
    rows = []
    for f in _coll("facts").find({"claimId": claim_id}).sort("validFrom", 1):
        row = TIMELINE_ROW.get(types.get(f["sourceFilename"], ingest.doc_type(f["sourceFilename"])))
        if row:
            rows.append({"row": row, "label": f["label"], "validFrom": _iso(f["validFrom"]),
                         "validTo": _iso(f["validTo"]), "superseded": f.get("supersededBy") is not None})
    return rows


def findings(claim_id: str, resolved: bool = False) -> list:
    versions = {}
    out = []
    query = {"claimId": claim_id, "status": "resolved"} if resolved else {
        "claimId": claim_id, "status": {"$ne": "resolved"}}
    for f in _coll("findings").find(query).sort("createdAt", 1):
        if f["runId"] not in versions:
            run = _coll("agent_runs").find_one({"runId": f["runId"]}, {"rulebookVersion": 1})
            versions[f["runId"]] = (run or {}).get("rulebookVersion")
        out.append({"type": f["type"], "title": f["title"], "detail": f["detail"], "amount": f["amount"],
                    "evidence": [{"filename": e["filename"], "quote": e["quote"]} for e in f["evidence"]],
                    "decisionId": f.get("decisionId"), "ruleVersion": versions[f["runId"]],
                    **({"resolvedAt": _iso(f.get("resolvedAt")), "resolvedByFilename": f.get("resolvedByFilename")}
                       if resolved else {})})
    return out


def latest_run(claim_id: str):
    run = _coll("agent_runs").find_one({"claimId": claim_id}, sort=[("startedAt", -1)])
    if not run:
        return None
    return {"id": run["runId"], "status": run["status"], "rulebookVersion": run.get("rulebookVersion"),
            "steps": [{"tool": c["name"], "summary": summarize_call(c["name"], c["input"], c["output"]),
                       "at": c["startedAt"].isoformat()} for c in run.get("toolCalls", [])]}


def workspace(claim_id: str):
    claim = _coll("claims").find_one({"claimId": claim_id})
    if not claim:
        return None
    return {"header": header(claim), "totals": totals(claim_id), "timeline": timeline(claim_id),
            "findings": findings(claim_id), "resolvedFindings": findings(claim_id, resolved=True),
            "latestRun": latest_run(claim_id)}


def rulebook_history(claim_id: str):
    insurer = rulebook.insurer_of(claim_id)
    if insurer is None:
        return None
    hdrs = rulebook.headers(insurer)
    out = []
    for v in sorted(rulebook.versions(insurer), reverse=True):
        h = hdrs.get(v, {})
        compact = lambda s: None if not s else {"caught": s["caughtCount"], "expected": s["expectedCount"]}
        out.append({"version": v, "status": rulebook.status(insurer, v, hdrs),
                    "rules": [{"type": r["type"], "instruction": r["instruction"]}
                              for r in rulebook.rules_for(insurer, v)],
                    "provenance": {"scoreBefore": compact(h.get("scoreBefore")),
                                   "scoreAfter": compact(h.get("scoreAfter")),
                                   "decision": h.get("decision"), "reason": h.get("reason")}})
    return out
