"""Read models for the UI. Reads only through db.get_agent_collection(): labels and scores are unreachable."""
import json
import re
from datetime import datetime, timezone
from decimal import Decimal

import dataparse
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


def _header_date(text: str, key: str = "Date"):
    m = re.search(rf"^{key}: (\d{{4}}-\d{{2}}-\d{{2}})", text, re.M)
    return m.group(1) if m else None


def _month_span(memo: str):
    m = re.search(r"months? (\d+)(?:\s*[-\u2013]\s*(\d+))?", memo or "", re.I)
    return (int(m.group(1)), int(m.group(2) or m.group(1))) if m else None


def timeline(claim_id: str) -> list:
    """One bar per (row, source document), built from the documents and the data parsed out of them.
    Not from agent facts, so it is the same however many times a claim is reviewed."""
    docs = list(_coll("documents").find({"claimId": claim_id}).sort("receivedAt", 1))
    bars = {}  # (row, filename) -> bar

    def add(row, filename, label, valid_from, valid_to=None, superseded=False, note=None):
        bar = {"row": row, "label": label, "validFrom": valid_from, "validTo": valid_to,
               "superseded": superseded, "sourceFilename": filename}
        if note:
            bar["note"] = note
        bars.setdefault((row, filename), bar)

    def chain(row, entries):
        """entries: [(date, filename, label)] oldest first; each ends where the next begins."""
        entries = list({e[1]: e for e in entries}.values())  # one per filename
        for i, (when, filename, label) in enumerate(entries):
            nxt = entries[i + 1][0] if i + 1 < len(entries) else None
            add(row, filename, label, when, nxt, superseded=nxt is not None)

    estimates, notices, renewal, policy_doc = [], [], None, None
    for d in docs:
        text, name, kind = d.get("text") or "", d["filename"], d["type"]
        received = _iso(d["receivedAt"])
        try:
            if kind == "policy":
                policy_doc = d
                renewal = renewal or dataparse.parse_effective_from_text(text).isoformat()
            elif kind == "endorsement":
                eff = dataparse.parse_effective_from_text(text).isoformat()
                limit = dataparse.parse_endorsement_limit_text(text)
                add("policy", name, f"Code-upgrade coverage, ${limit:,.0f}", eff,
                    note=f"added to file {received}")
                if "policy renewal date" in text:
                    renewal = eff  # the current term starts at the renewal
            elif kind == "estimate":
                est = dataparse.parse_estimate_text(text)
                total = dataparse.parse_money(est.header["Total"].lstrip("$"))
                estimates.append((est.header["Date written"], name,
                                  f"Estimate {est.header['Version']} \u00b7 ${total:,.0f}"))
            elif kind == "email":
                m = re.search(r"through month (\d+) at \$([\d,]+) per month", text)
                if m:
                    add("promises", name, f"Living expenses promised to month {m.group(1)}",
                        _header_date(text) or received)
            elif kind == "notice":
                m = re.search(r"ALE payments end after month (\d+)", text)
                if m:
                    notices.append((_header_date(text) or received, name, f"Cut off after month {m.group(1)}"))
            elif kind == "payments":
                ale = [p for p in json.loads(text) if p.get("category") == "ale"]
                if ale:
                    ale.sort(key=lambda p: p["paidAt"])
                    spans = [s for s in (_month_span(p.get("memo")) for p in ale) if s]
                    last = max((s[1] for s in spans), default=None)
                    first = min((s[0] for s in spans), default=1)
                    label = f"Paid, months {first}\u2013{last}" if last else "Paid"
                    add("living_expenses", name, label, ale[0]["paidAt"])
        except (ValueError, KeyError, TypeError, json.JSONDecodeError):
            continue  # a document that does not parse gets no bar rather than a wrong one
    if renewal and policy_doc:
        add("policy", policy_doc["filename"], "Policy in force", renewal)
    chain("estimates", sorted(estimates))
    chain("living_expenses", sorted(notices))
    order = {"policy": 0, "promises": 1, "estimates": 2, "living_expenses": 3}
    return sorted(bars.values(), key=lambda b: (order[b["row"]], b["validFrom"]))


def findings(claim_id: str, resolved: bool = False) -> list:
    versions = {}
    out = []
    insurer = rulebook.insurer_of(claim_id)
    query = {"claimId": claim_id, "status": "resolved"} if resolved else {
        "claimId": claim_id, "status": {"$ne": "resolved"}}
    for f in _coll("findings").find(query).sort("createdAt", 1):
        if f["runId"] not in versions:
            run = _coll("agent_runs").find_one({"runId": f["runId"]}, {"rulebookVersion": 1})
            versions[f["runId"]] = (run or {}).get("rulebookVersion")
        rule_id = f.get("ruleId")
        added = None
        if rule_id and insurer:
            added = rulebook.added_in_version(insurer, rule_id)
        out.append({"type": f["type"], "title": f["title"], "summary": f.get("summary") or f.get("detail", ""),
                    "points": f.get("points", []), "amount": f["amount"],
                    "evidence": [{"filename": e["filename"], "quote": e["quote"]} for e in f["evidence"]],
                    "decisionId": f.get("decisionId"), "ruleVersion": versions[f["runId"]],
                    "ruleId": rule_id, "ruleAddedInVersion": added,
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
