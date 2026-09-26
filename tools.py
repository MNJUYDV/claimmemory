"""Agent tools: plain Python functions plus JSON schemas for Claude tool use.

Guardrails live here, not in the prompt:
  * claimId and runId are injected via ToolContext; the model never supplies them.
  * Dollar amounts come only from compute_amount; upsert_finding copies them from the calc.
  * Quotes must appear verbatim in the cited document; dates must be timezone-aware.
  * Data is read only through db.get_agent_collection(), which refuses labels and scores.
  * Tools return JSON-able dicts; failures are {"error": "..."} and never raise to the model.
"""
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

import jsonschema

import dataparse
import db
import embeddings
import rulebook
from constants import OPEN_ENDED, parse_dt

log = logging.getLogger("claimmemory.tools")

FINDING_TYPES = ("labor_depreciation", "missing_coverage", "unpaid_ale")
# finding type -> the compute rule that must back it
RULE_FOR_TYPE = {"labor_depreciation": "labor_depreciation_refund",
                 "missing_coverage": "missing_coverage",
                 "unpaid_ale": "unpaid_ale"}
COMPUTE_RULES = tuple(RULE_FOR_TYPE.values())

MIN_QUOTE_CHARS = 10          # a quote of "L" or "the" proves nothing
MIN_YEAR, MAX_YEAR = 2000, 2100  # model-supplied dates outside this range are mistakes
_MONEY_RE = re.compile(r"(?:\$|USD\s?)\s?(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?")
_NUMBER_RE = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?")


@dataclass(frozen=True)
class ToolContext:
    claimId: str
    runId: str


class ToolError(Exception):
    """Raised inside a tool; call_tool turns it into {"error": message}."""


# ---------- helpers ----------

def _coll(name):
    return db.get_agent_collection(name)


def _new_id(prefix):
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _now():
    return datetime.now(timezone.utc)


def _dt(value, field):
    try:
        dt = parse_dt(value)
    except (ValueError, TypeError):
        raise ToolError(f"{field} must be an ISO-8601 datetime with a UTC offset, "
                        f"e.g. 2026-07-18T00:00:00-05:00; got {value!r}") from None
    if not MIN_YEAR <= dt.year <= MAX_YEAR:  # not astimezone(): that overflows near year 1 / 9999
        raise ToolError(f"{field} must be between the years {MIN_YEAR} and {MAX_YEAR}; got {value!r}")
    return dt


def _require_run(ctx):
    """Writes are only allowed inside a real, still-running agent run for this claim."""
    run = _coll("agent_runs").find_one({"runId": ctx.runId, "claimId": ctx.claimId}, {"status": 1})
    if not run:
        raise ToolError("no active agent run for this context")
    if run.get("status") != "running":
        raise ToolError(f"this run is {run.get('status')!r}, not running")


def _run_rules(ctx):
    """(insurer, version, rules) for this run: the rulebook version pinned when the run started."""
    claim = _coll("claims").find_one({"claimId": ctx.claimId}, {"insurer": 1})
    if not claim:
        raise ToolError("claim not found")
    run = _coll("agent_runs").find_one({"runId": ctx.runId, "claimId": ctx.claimId}, {"rulebookVersion": 1})
    version = (run or {}).get("rulebookVersion")
    if version is None:
        version = rulebook.active_version(claim["insurer"])
    return claim["insurer"], version, ([] if version is None else rulebook.rules_for(claim["insurer"], version))


def _authorized_types(ctx) -> set:
    """Finding types the run's rulebook version has an active rule for."""
    return {r["type"] for r in _run_rules(ctx)[2]}


def _numbers(text):
    return {Decimal(t.replace(",", "")) for t in _NUMBER_RE.findall(text)}


def _walk_numbers(obj):
    if isinstance(obj, bool):
        return
    if isinstance(obj, (int, float, Decimal)):
        yield Decimal(str(obj))
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _walk_numbers(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk_numbers(v)


_JARGON = {"ALE": "living expenses", "ACV": "actual cash value", "RCV": "replacement cost",
           "O&L": "code-upgrade coverage"}
_JARGON_RE = re.compile(r"\b(" + "|".join(re.escape(k) for k in _JARGON) + r")\b")


def _check_plain_english(*texts):
    """Findings are read by policyholders: no insurance acronyms in the text the model writes."""
    for text in texts:
        m = _JARGON_RE.search(text or "")
        if m:
            raise ToolError(f"avoid jargon: write '{_JARGON[m.group(1)]}' instead of '{m.group(1)}'")


def _check_money_in_prose(ctx, calc, *texts):
    """Every dollar figure the model writes must be the calc's amount, a number in the calc,
    or a number that appears in this claim's documents. Invented figures are rejected."""
    allowed = {Decimal(str(calc["amount"]))} | set(_walk_numbers(calc.get("details", {})))
    for d in _coll("documents").find({"claimId": ctx.claimId}, {"text": 1}):
        allowed |= _numbers(d["text"])
    for text in texts:
        for m in _MONEY_RE.finditer(text or ""):
            figure = Decimal(m.group(1).replace(",", "") + (m.group(2) or ""))
            if figure not in allowed:
                raise ToolError(f"the dollar figure {m.group(0).strip()} in the finding text is not the "
                                f"calculated amount or a number from the claim documents; "
                                f"refer to the amount as {calc['amount']:,.2f} or omit figures")


def _jsonable(obj):
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items() if k not in ("embedding", "_id")} | \
               ({"id": obj["_id"]} if isinstance(obj.get("_id"), str) else {})
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, Decimal):
        return float(obj)
    return obj


def _cents(amount: Decimal) -> int:
    return int((amount * 100).to_integral_value())


def _document(ctx, filename):
    doc = _coll("documents").find_one({"claimId": ctx.claimId, "filename": filename})
    if not doc:
        raise ToolError(f"no document named {filename!r} on file for this claim")
    return doc


def _check_quote(ctx, filename, quote):
    if not isinstance(quote, str) or len(quote.strip()) < MIN_QUOTE_CHARS:
        raise ToolError(f"quote must be at least {MIN_QUOTE_CHARS} characters of the source text")
    if quote not in _document(ctx, filename)["text"]:
        raise ToolError(f"quote is not verbatim in {filename}; copy the exact text without line numbers")


# ---------- read tools ----------

def get_claim_state(ctx):
    claim = _coll("claims").find_one({"claimId": ctx.claimId})
    if not claim:
        raise ToolError("claim not found")
    by_cat = {}
    for p in _coll("payments").find({"claimId": ctx.claimId}).sort("paidAt", 1):
        cat = by_cat.setdefault(p["category"], {"total": Decimal(0), "payments": []})
        cat["total"] += Decimal(str(p["amount"]))
        cat["payments"].append({"paidAt": p["paidAt"], "amount": p["amount"], "memo": p.get("memo")})
    findings = _coll("findings").find({"claimId": ctx.claimId, "status": "open"})
    resolved = _coll("findings").find({"claimId": ctx.claimId, "status": "resolved"})
    docs = list(_coll("documents").find({"claimId": ctx.claimId}).sort("receivedAt", 1))
    prev = _coll("agent_runs").find_one({"claimId": ctx.claimId, "status": "completed",
                                         "runId": {"$ne": ctx.runId}}, sort=[("startedAt", -1)])
    since = prev["startedAt"] if prev else None  # a run reviews what was on file when it started
    return _jsonable({
        "claim": {k: claim[k] for k in ("claimId", "insurer", "insuredName", "propertyAddress",
                                        "policyNumber", "lossType", "lossDate", "status")},
        "paymentsByCategory": by_cat,
        "openFindings": [{k: f[k] for k in ("type", "title", "amount", "calcId")} for f in findings],
        "resolvedFindings": [{"type": f["type"], "resolvedByFilename": f.get("resolvedByFilename")}
                             for f in resolved],
        "documents": [{"filename": d["filename"], "type": d["type"], "receivedAt": d["receivedAt"]}
                      for d in docs],
        "newSinceLastRun": [{"filename": d["filename"], "type": d["type"], "receivedAt": d["receivedAt"]}
                            for d in docs if since is None or d["receivedAt"] > since],
    })


def get_rules(ctx):
    insurer, version, rules = _run_rules(ctx)
    return _jsonable({"insurer": insurer, "version": version, "rules": [
        {k: r[k] for k in ("id", "type", "instruction", "computeRule")} for r in rules]})


def list_documents(ctx, asOf=None):
    query = {"claimId": ctx.claimId}
    if asOf is not None:
        query["receivedAt"] = {"$lte": _dt(asOf, "asOf")}
    docs = _coll("documents").find(query).sort("receivedAt", 1)
    return _jsonable({"documents": [{"filename": d["filename"], "type": d["type"],
                                     "receivedAt": d["receivedAt"], "hold": d.get("hold", False)}
                                    for d in docs]})


def read_document(ctx, filename):
    doc = _document(ctx, filename)
    numbered = "\n".join(f"{i:>4}: {line}" for i, line in enumerate(doc["text"].splitlines(), 1))
    return _jsonable({"filename": filename, "type": doc["type"], "receivedAt": doc["receivedAt"],
                      "text": numbered})


def search_policy(ctx, query, asOf):
    as_of = _dt(asOf, "asOf")
    try:
        vector = embeddings.embed([query], "query")[0]
    except Exception:
        log.exception("query embedding failed")
        raise ToolError("policy search is temporarily unavailable") from None
    pipeline = [
        {"$vectorSearch": {
            "index": "clause_vectors", "path": "embedding", "queryVector": vector,
            "numCandidates": 50, "limit": 5,
            "filter": {"$and": [{"claimId": ctx.claimId},
                                {"effectiveFrom": {"$lte": as_of}},
                                {"effectiveTo": {"$gte": as_of}}]}}},
        {"$project": {"score": {"$meta": "vectorSearchScore"}, "filename": 1,
                      "section": 1, "heading": 1, "text": 1, "effectiveFrom": 1, "effectiveTo": 1}},
    ]
    results = list(_coll("policy_clauses").aggregate(pipeline))
    return _jsonable({"asOf": as_of, "results": results})


# ---------- fact / decision tools ----------

def record_fact(ctx, row, label, validFrom, sourceFilename, quote):
    _require_run(ctx)
    valid_from = _dt(validFrom, "validFrom")
    _check_quote(ctx, sourceFilename, quote)
    identity = {"claimId": ctx.claimId, "row": row, "label": label, "validFrom": valid_from,
                "sourceFilename": sourceFilename, "quote": quote}
    same = _coll("facts").find_one(identity)  # re-running a step must not duplicate facts
    if same:
        return {"factId": same["_id"], "validFrom": same["validFrom"].isoformat(),
                "validTo": same["validTo"].isoformat(), "duplicate": True}
    fact_id = _new_id("fact")
    doc = db.prepare_doc("facts", {
        "_id": fact_id, "claimId": ctx.claimId, "runId": ctx.runId, "row": row, "label": label,
        "validFrom": valid_from, "validTo": OPEN_ENDED, "sourceFilename": sourceFilename,
        "quote": quote, "supersededBy": None, "createdAt": _now()})
    _coll("facts").insert_one(doc)
    _supersede_by_newer_documents(ctx.claimId, fact_id)
    fact = _coll("facts").find_one({"_id": fact_id})
    return {"factId": fact_id, "validFrom": valid_from.isoformat(), "validTo": fact["validTo"].isoformat()}


def _supersede_by_newer_documents(claim_id, fact_id):
    """A newer estimate or ALE notice replaces the older ones: facts sourced from an older document of
    that type end when the newer document's facts begin, whatever the model remembered to supersede."""
    facts = _coll("facts")
    docs = {d["filename"]: d for d in _coll("documents").find({"claimId": claim_id},
                                                               {"filename": 1, "type": 1, "receivedAt": 1})}
    new = facts.find_one({"_id": fact_id})
    src = docs.get(new["sourceFilename"])
    if not src or src["type"] not in ("estimate", "notice"):
        return
    for other in facts.find({"claimId": claim_id, "_id": {"$ne": fact_id}}):
        od = docs.get(other["sourceFilename"])
        if not od or od["type"] != src["type"] or od["filename"] == src["filename"]:
            continue
        older, newer = (other, new) if od["receivedAt"] < src["receivedAt"] else (new, other)
        if older.get("supersededBy") is None:
            facts.update_one({"_id": older["_id"]}, {"$set": db.prepare_doc("facts", {
                "validTo": max(newer["validFrom"], older["validFrom"]), "supersededBy": newer["_id"]})})


def supersede_fact(ctx, oldFactId, newFactId):
    _require_run(ctx)
    facts = _coll("facts")
    old = facts.find_one({"_id": oldFactId, "claimId": ctx.claimId})
    new = facts.find_one({"_id": newFactId, "claimId": ctx.claimId})
    if not old:
        raise ToolError(f"unknown fact {oldFactId!r} for this claim")
    if not new:
        raise ToolError(f"unknown fact {newFactId!r} for this claim")
    if oldFactId == newFactId:
        raise ToolError("a fact cannot supersede itself")
    if old.get("supersededBy") not in (None, newFactId):
        raise ToolError(f"{oldFactId} is already superseded by {old['supersededBy']}")
    if new["validFrom"] < old["validFrom"]:
        raise ToolError("the new fact's validFrom is earlier than the old fact's validFrom")
    node, hops = new, 0  # refuse cycles: new must not (transitively) be superseded by old
    while node and node.get("supersededBy") and hops < 1000:
        if node["supersededBy"] == oldFactId:
            raise ToolError(f"{newFactId} is already superseded by {oldFactId}; this would create a cycle")
        node = facts.find_one({"_id": node["supersededBy"], "claimId": ctx.claimId})
        hops += 1
    facts.update_one({"_id": oldFactId}, {"$set": db.prepare_doc(
        "facts", {"validTo": new["validFrom"], "supersededBy": newFactId})})
    return {"oldFactId": oldFactId, "newFactId": newFactId, "validTo": new["validFrom"].isoformat()}


def record_decision(ctx, filename, madeAt, citedFilenames):
    _require_run(ctx)
    made_at = _dt(madeAt, "madeAt")
    _document(ctx, filename)
    for name in citedFilenames:
        _document(ctx, name)
    key = {"claimId": ctx.claimId, "filename": filename}
    existing = _coll("decisions").find_one(key)
    decision_id = existing["_id"] if existing else _new_id("dec")
    doc = db.prepare_doc("decisions", {
        "claimId": ctx.claimId, "runId": ctx.runId, "filename": filename, "madeAt": made_at,
        "citedFilenames": list(dict.fromkeys(citedFilenames)), "createdAt": _now()})
    _coll("decisions").update_one({"_id": decision_id}, {"$set": doc}, upsert=True)
    return {"decisionId": decision_id, "updated": existing is not None}


def replay(ctx, decisionId):
    decision = _coll("decisions").find_one({"_id": decisionId, "claimId": ctx.claimId})
    if not decision:
        raise ToolError(f"unknown decision {decisionId!r} for this claim")
    cited = set(decision["citedFilenames"])
    docs = _coll("documents").find({"claimId": ctx.claimId,
                                    "receivedAt": {"$lte": decision["madeAt"]}}).sort("receivedAt", 1)
    out = {"cited": [], "not_cited": []}
    for d in docs:
        if d["filename"] == decision["filename"]:
            continue  # the decision document itself
        row = {"filename": d["filename"], "type": d["type"], "receivedAt": d["receivedAt"]}
        out["cited" if d["filename"] in cited else "not_cited"].append(row)
    return _jsonable({"decisionId": decisionId, "filename": decision["filename"],
                      "madeAt": decision["madeAt"], **out})


# ---------- compute_amount ----------

def _text(ctx, inputs, key, rule):
    name = inputs.get(key)
    if not name:
        raise ToolError(f"rule {rule} requires inputs.{key} (a filename)")
    return _document(ctx, name)["text"], name


def _calc_labor_depreciation(ctx, inputs):
    text, name = _text(ctx, inputs, "estimate", "labor_depreciation_refund")
    est = dataparse.parse_estimate_text(text)
    lines = est.labor_depreciation_lines
    return est.labor_depreciation_total, {
        "estimate": name, "lineCount": len(lines), "lineIds": [l.line_id for l in lines]}


def _calc_missing_coverage(ctx, inputs):
    bid_text, bid = _text(ctx, inputs, "bid", "missing_coverage")
    est_text, est_name = _text(ctx, inputs, "estimate", "missing_coverage")
    end_text, end = _text(ctx, inputs, "endorsement", "missing_coverage")
    items = dataparse.parse_code_upgrades_text(bid_text)
    descriptions = [l.description.lower() for l in dataparse.parse_estimate_text(est_text).lines]
    rows, absent_total = [], Decimal(0)
    for i in items:
        want = i.description.lower()
        present = any(want in d or d in want for d in descriptions)
        if not present:
            absent_total += i.amount
        rows.append({"itemId": i.item_id, "description": i.description, "amount": i.amount,
                     "inEstimate": present})
    limit = dataparse.parse_endorsement_limit_text(end_text)
    return min(absent_total, limit), {
        "bid": bid, "estimate": est_name, "endorsement": end, "items": rows,
        "absentTotal": absent_total, "endorsementLimit": limit, "cappedAtLimit": absent_total > limit}


def _calc_unpaid_ale(ctx, inputs):
    promise_text, promise = _text(ctx, inputs, "promise", "unpaid_ale")
    notice_text, notice = _text(ctx, inputs, "notice", "unpaid_ale")
    terms = dataparse.parse_ale_text(promise_text, notice_text)
    unpaid_months = max(terms.promised_months - terms.cutoff_months, 0)
    return unpaid_months * terms.monthly_rate, {
        "promise": promise, "notice": notice, "promisedMonths": terms.promised_months,
        "paidMonths": terms.cutoff_months, "unpaidMonths": unpaid_months,
        "monthlyRate": terms.monthly_rate}


_CALCS = {"labor_depreciation_refund": _calc_labor_depreciation,
          "missing_coverage": _calc_missing_coverage,
          "unpaid_ale": _calc_unpaid_ale}


def compute_amount(ctx, rule, inputs):
    _require_run(ctx)
    if rule not in _CALCS:
        raise ToolError(f"unknown rule {rule!r}; expected one of {', '.join(COMPUTE_RULES)}")
    try:
        amount, details = _CALCS[rule](ctx, inputs)
    except ValueError as e:  # parse failures from dataparse
        raise ToolError(str(e)) from None
    prior = _coll("agent_runs").find_one({"runId": ctx.runId, "claimId": ctx.claimId}, {"calcs": 1})
    for c in (prior or {}).get("calcs", []):
        if c["rule"] == rule and c["inputs"] == inputs:  # same question, same answer
            return {"calcId": c["calcId"], "amount": c["amount"], "details": c["details"]}
    calc_id = _new_id("calc")
    calc = _jsonable({"calcId": calc_id, "rule": rule, "inputs": inputs, "amount": amount,
                      "amountCents": _cents(amount), "details": details})
    calc["createdAt"] = _now()
    res = _coll("agent_runs").update_one({"runId": ctx.runId, "claimId": ctx.claimId},
                                         {"$push": {"calcs": db.prepare_doc("agent_runs", calc)}})
    if res.matched_count == 0:
        raise ToolError("no active agent run for this context")
    return {"calcId": calc_id, "amount": calc["amount"], "details": calc["details"]}


# ---------- findings ----------

def _decision_that_missed(claim_id):
    """The latest decision whose estimate left an endorsement on file uncited, so a missing_coverage
    finding can link to its replay even when the agent did not pass decisionId."""
    for d in _coll("decisions").find({"claimId": claim_id}).sort("madeAt", -1):
        cited = set(d["citedFilenames"])
        if _coll("documents").find_one({"claimId": claim_id, "type": "endorsement",
                                        "receivedAt": {"$lte": d["madeAt"]},
                                        "filename": {"$nin": list(cited)}}):
            return d["_id"]
    return None


def upsert_finding(ctx, type, title, summary, points, calcId, evidence, decisionId=None):
    _require_run(ctx)
    rule = next((r for r in _run_rules(ctx)[2] if r["type"] == type), None)
    if rule is None:
        raise ToolError(f"no active rule authorizes {type} findings")
    run = _coll("agent_runs").find_one({"runId": ctx.runId, "claimId": ctx.claimId,
                                        "calcs.calcId": calcId}, {"calcs.$": 1})
    if not run:
        if _coll("agent_runs").find_one({"calcs.calcId": calcId}, {"_id": 1}):
            raise ToolError(f"calcId {calcId!r} belongs to a different run or claim")
        raise ToolError(f"unknown calcId {calcId!r}; call compute_amount first")
    calc = run["calcs"][0]
    if calc["rule"] != RULE_FOR_TYPE[type]:
        raise ToolError(f"calc {calcId} used rule {calc['rule']!r}, which cannot support a "
                        f"{type!r} finding (needs {RULE_FOR_TYPE[type]!r})")
    if calc["amountCents"] <= 0:
        raise ToolError(f"calc {calcId} computed {calc['amount']}: there is nothing to report; "
                        f"do not create a finding for a zero amount")
    _check_plain_english(title, summary, *points)
    _check_money_in_prose(ctx, calc, title, summary, *points)
    if not evidence:
        raise ToolError("evidence must contain at least one {filename, quote}")
    for ev in evidence:
        _check_quote(ctx, ev["filename"], ev["quote"])
    if decisionId is not None and not _coll("decisions").find_one(
            {"_id": decisionId, "claimId": ctx.claimId}):
        raise ToolError(f"unknown decision {decisionId!r} for this claim")

    if decisionId is None and type == "missing_coverage":
        decisionId = _decision_that_missed(ctx.claimId)
    finding_id = f"finding_{ctx.claimId}_{type}"
    doc = db.prepare_doc("findings", {
        "claimId": ctx.claimId, "runId": ctx.runId, "type": type, "title": title, "summary": summary,
        "points": list(points),
        "calcId": calcId, "rule": calc["rule"], "ruleId": rule["id"],  # the rule that authorized it
        "amount": calc["amount"],  # always from the calc
        "amountCents": calc["amountCents"], "evidence": evidence, "decisionId": decisionId,
        "status": "open", "updatedAt": _now()})
    res = _coll("findings").update_one(  # a resolved finding whose amount came back is open again
        {"_id": finding_id}, {"$set": doc, "$setOnInsert": {"createdAt": _now()},
                              "$unset": {"resolvedAt": "", "resolvedByFilename": "", "resolution": "", "detail": ""}},
        upsert=True)
    return {"findingId": finding_id, "status": "created" if res.upserted_id is not None else "updated",
            "amount": calc["amount"]}


# which input of each compute rule is the document the amount was computed on, and its type
_BASIS = {"labor_depreciation_refund": ("estimate", "estimate"),
          "missing_coverage": ("estimate", "estimate"),
          "unpaid_ale": ("notice", "notice")}


def resolve_finding(ctx, type, calcId, reason):
    """Close an open finding whose amount is now 0 on the claim's latest estimate / ALE notice."""
    _require_run(ctx)
    if type not in _authorized_types(ctx):
        raise ToolError(f"no active rule authorizes {type} findings")
    run = _coll("agent_runs").find_one({"runId": ctx.runId, "claimId": ctx.claimId,
                                        "calcs.calcId": calcId}, {"calcs.$": 1})
    if not run:
        raise ToolError(f"unknown calcId {calcId!r}; call compute_amount first")
    calc = run["calcs"][0]
    if calc["rule"] != RULE_FOR_TYPE[type]:
        raise ToolError(f"calc {calcId} used rule {calc['rule']!r}, which cannot resolve a {type!r} finding")
    if calc["amountCents"] != 0:
        raise ToolError(f"calc {calcId} computed {calc['amount']}, not 0: update the finding with "
                        f"upsert_finding instead of resolving it")
    key, doc_type_ = _BASIS[calc["rule"]]
    basis = calc["inputs"].get(key)
    newest = _coll("documents").find_one({"claimId": ctx.claimId, "type": doc_type_},
                                         sort=[("receivedAt", -1), ("filename", -1)])
    if not newest or newest["filename"] != basis:
        raise ToolError(f"calc {calcId} was computed on {basis!r}, but the latest {doc_type_} on file is "
                        f"{newest['filename'] if newest else None!r}; recompute on that document")
    finding_id = f"finding_{ctx.claimId}_{type}"
    res = _coll("findings").update_one(
        {"_id": finding_id, "status": "open"},
        {"$set": db.prepare_doc("findings", {"status": "resolved", "resolvedAt": _now(),
                                             "resolvedByFilename": basis, "resolution": reason,
                                             "updatedAt": _now()})})
    if res.matched_count == 0:
        raise ToolError(f"no open {type} finding to resolve")
    return {"findingId": finding_id, "status": "resolved", "resolvedByFilename": basis}


# ---------- registry ----------

def _obj(properties=None, required=()):
    schema = {"type": "object", "properties": properties or {}, "additionalProperties": False}
    if required:
        schema["required"] = list(required)
    return schema


_STR = {"type": "string", "minLength": 1, "maxLength": 200}
_TEXT = {"type": "string", "minLength": 1, "maxLength": 2000}
_QUERY = {"type": "string", "minLength": 1, "maxLength": 500}
_TITLE = {"type": "string", "minLength": 1, "maxLength": 70}
_SUMMARY = {"type": "string", "minLength": 1, "maxLength": 140}
_POINT = {"type": "string", "maxLength": 110, "pattern": r"^(Policy|On file|Insurer): \S"}
_ASOF = {"type": "string", "description": "ISO-8601 datetime with UTC offset, e.g. 2026-07-18T00:00:00-05:00"}


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_schema: dict
    fn: callable


TOOLS = [
    Tool("get_claim_state", "Claim header, loss date, insurer, payments by category, open findings and "
         "documents on file.", _obj(), get_claim_state),
    Tool("get_rules", "The current rulebook version for this claim's insurer.", _obj(), get_rules),
    Tool("list_documents", "Documents on file, optionally only those received on or before asOf.",
         _obj({"asOf": _ASOF}), list_documents),
    Tool("read_document", "Full text of a document with line numbers. Line-number prefixes are not "
         "part of the text: never include them in quotes.",
         _obj({"filename": _STR}, ["filename"]), read_document),
    Tool("search_policy", "Semantic search over policy and endorsement clauses that were in effect on asOf.",
         _obj({"query": _QUERY, "asOf": _ASOF}, ["query", "asOf"]), search_policy),
    Tool("record_fact", "Record a dated fact. The quote must appear verbatim in the source document. "
         "row names the ledger row the fact belongs to; label is the fact's content.",
         _obj({"row": _STR, "label": _STR, "validFrom": _ASOF, "sourceFilename": _STR, "quote": _TEXT},
              ["row", "label", "validFrom", "sourceFilename", "quote"]), record_fact),
    Tool("supersede_fact", "Mark an older fact as replaced by a newer one; the old fact's validTo becomes "
         "the new fact's validFrom.", _obj({"oldFactId": _STR, "newFactId": _STR},
                                            ["oldFactId", "newFactId"]), supersede_fact),
    Tool("record_decision", "Record an insurer decision (a document) and the documents it relied on.",
         _obj({"filename": _STR, "madeAt": _ASOF, "citedFilenames": {"type": "array", "items": _STR, "maxItems": 50}},
              ["filename", "madeAt", "citedFilenames"]), record_decision),
    Tool("replay", "For a recorded decision: the documents received by then, split into cited and "
         "not_cited.", _obj({"decisionId": _STR}, ["decisionId"]), replay),
    Tool("compute_amount", "Compute a dollar amount exactly. Rules: labor_depreciation_refund {estimate}; "
         "missing_coverage {bid, estimate, endorsement}; unpaid_ale {promise, notice}. Inputs are "
         "filenames. Returns a calcId to cite in upsert_finding.",
         _obj({"rule": {"type": "string", "enum": list(COMPUTE_RULES)},
               "inputs": _obj({k: _STR for k in ("estimate", "bid", "endorsement", "promise", "notice")})},
              ["rule", "inputs"]), compute_amount),
    Tool("resolve_finding", "Close the open finding of this type because its amount is now 0. calcId must "
         "be a compute_amount result in this run, for that type's rule, with amount 0, computed on the "
         "latest estimate (or latest ALE notice).",
         _obj({"type": {"type": "string", "enum": list(FINDING_TYPES)}, "calcId": _STR, "reason": _TEXT},
              ["type", "calcId", "reason"]), resolve_finding),
    Tool("upsert_finding", "Create or update the finding of this type. The amount is taken from the "
         "calculation; you cannot set it. Every evidence quote must be verbatim. Write for the policyholder in "
         "plain English: title (max 70 chars) says what they lost; summary is one sentence (max 140); points are "
         "2-4 short bullets (max 110 each), each starting with \"Policy:\", \"On file:\" or \"Insurer:\".",
         _obj({"type": {"type": "string", "enum": list(FINDING_TYPES)}, "title": _TITLE, "summary": _SUMMARY,
               "points": {"type": "array", "minItems": 2, "maxItems": 4, "items": _POINT},
               "calcId": _STR,
               "evidence": {"type": "array", "minItems": 1, "maxItems": 20,
                            "items": _obj({"filename": _STR, "quote": _TEXT}, ["filename", "quote"])},
               "decisionId": _STR},
              ["type", "title", "summary", "points", "calcId", "evidence"]), upsert_finding),
]
_BY_NAME = {t.name: t for t in TOOLS}


def tool_definitions() -> list:
    """The tool list in the shape the Anthropic Messages API expects."""
    return [{"name": t.name, "description": t.description, "input_schema": t.input_schema} for t in TOOLS]


def call_tool(ctx: ToolContext, name: str, args: dict) -> dict:
    """Validate args against the tool's schema and run it. Always returns a dict; never raises."""
    tool = _BY_NAME.get(name) if isinstance(name, str) else None
    if tool is None:
        return {"error": f"unknown tool {name!r}"}
    try:
        jsonschema.validate(args, tool.input_schema, cls=jsonschema.Draft202012Validator)
    except jsonschema.ValidationError as e:
        return {"error": f"invalid arguments for {name}: {e.message}"}
    try:
        return tool.fn(ctx, **args)
    except ToolError as e:
        return {"error": str(e)}
    except PermissionError:
        return {"error": "access denied"}
    except Exception as e:
        log.exception("tool %s failed", name)
        return {"error": f"internal error in {name} ({type(e).__name__})"}
