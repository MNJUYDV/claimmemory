"""Versioned rulebooks. Harness code (the agent reads rules only through tools.get_rules).

Storage is the `rules` collection: one document per rule (docType absent or "rule"), plus one optional
header per version (docType "version") holding status and provenance. A version without a header is
active (that is how seeded v1 exists). Statuses: active | candidate | rejected | inactive.
A run pins the version that was active when it started (agent_runs.rulebookVersion).
"""
from datetime import datetime, timezone

import db

VERSION_DOC = {"docType": "version"}
NOT_A_HEADER = {"docType": {"$ne": "version"}}


def _rules():
    return db.get_collection("rules")


def insurer_of(claim_id: str):
    claim = db.get_collection("claims").find_one({"claimId": claim_id})
    return claim["insurer"] if claim else None


def headers(insurer: str) -> dict:
    return {h["version"]: h for h in _rules().find({"insurer": insurer, **VERSION_DOC})}


def versions(insurer: str) -> list:
    return sorted(_rules().distinct("version", {"insurer": insurer}))


def status(insurer: str, version: int, hdrs: dict = None) -> str:
    hdrs = headers(insurer) if hdrs is None else hdrs
    return hdrs.get(version, {}).get("status", "active")


def active_version(insurer: str):
    hdrs = headers(insurer)
    live = [v for v in versions(insurer) if status(insurer, v, hdrs) == "active"]
    return max(live) if live else None


def active_version_for_claim(claim_id: str):
    insurer = insurer_of(claim_id)
    return active_version(insurer) if insurer else None


def rules_for(insurer: str, version: int) -> list:
    """The active rules of one version (any status: a run pinned to a candidate reads its rules)."""
    return [r for r in _rules().find({"insurer": insurer, "version": version, **NOT_A_HEADER})
            if r.get("active", True)]


def added_in_version(insurer: str, rule_id: str) -> int:
    """The first rulebook version a rule appeared in. Rules that predate the stored field fall back to
    the lowest version holding a rule with that id (copies keep their id across versions)."""
    docs = list(_rules().find({"insurer": insurer, "id": rule_id, **NOT_A_HEADER}))
    stored = [d["addedInVersion"] for d in docs if "addedInVersion" in d]
    return min(stored) if stored else min((d["version"] for d in docs), default=1)


def backfill_added_in_version(insurer: str) -> int:
    """Stamp addedInVersion on rule documents that lack it. Idempotent; returns how many were updated."""
    n = 0
    for d in list(_rules().find({"insurer": insurer, "addedInVersion": {"$exists": False}, **NOT_A_HEADER})):
        _rules().update_one({"_id": d["_id"]}, {"$set": {"addedInVersion": added_in_version(insurer, d["id"])}})
        n += 1
    return n


def header(insurer: str, version: int):
    return _rules().find_one({"insurer": insurer, "version": version, **VERSION_DOC})


def _now():
    return datetime.now(timezone.utc)


def create_candidate(insurer: str, base_version: int, new_rule: dict, provenance: dict) -> int:
    """New version = the base version's rules + new_rule, status candidate. Returns the version number."""
    hdrs = headers(insurer)
    for v, h in hdrs.items():  # a candidate left over from a crashed attempt is abandoned
        if h["status"] == "candidate":
            set_status(insurer, v, "rejected", reason="abandoned: a newer candidate was created",
                       decidedAt=_now())
    version = max(versions(insurer)) + 1
    for r in rules_for(insurer, base_version):
        copy = {k: v for k, v in r.items() if k != "_id"}
        copy["addedInVersion"] = r.get("addedInVersion", added_in_version(insurer, r["id"]))  # never re-stamped
        db.upsert_one("rules", {"id": copy["id"], "version": version}, {**copy, "version": version})
    rule = {**new_rule, "id": f"{new_rule['type']}-v{version}", "insurer": insurer, "version": version,
            "active": True, "docType": "rule", "addedInVersion": version, "createdAt": _now()}
    db.upsert_one("rules", {"id": rule["id"], "version": version}, rule)
    db.upsert_one("rules", {"insurer": insurer, "version": version, **VERSION_DOC}, {
        **VERSION_DOC, "insurer": insurer, "version": version, "status": "candidate",
        "baseVersion": base_version, "proposal": new_rule, "createdAt": _now(), **provenance})
    return version


def set_status(insurer: str, version: int, new_status: str, **fields) -> None:
    key = {"insurer": insurer, "version": version, **VERSION_DOC}
    db.get_collection("rules").update_one(
        key, {"$set": db.prepare_doc("rules", {"status": new_status, **fields})}, upsert=True)


def promote(insurer: str, version: int, **fields) -> None:
    """Candidate becomes active; every other active version becomes inactive history."""
    hdrs = headers(insurer)
    for v in versions(insurer):
        if v != version and status(insurer, v, hdrs) == "active":
            set_status(insurer, v, "inactive", supersededByVersion=version, retiredAt=_now())
    set_status(insurer, version, "active", **fields)
