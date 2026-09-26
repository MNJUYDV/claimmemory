"""Agent run bookkeeping. Harness code, not a tool."""
import uuid
from datetime import datetime, timezone

import db
from tools import ToolContext


def rulebook_version(claim_id: str):
    """The insurer's current rulebook version, recorded on each run so scores can be compared over time."""
    claim = db.get_collection("claims").find_one({"claimId": claim_id})
    versions = [r["version"] for r in db.get_collection("rules").find({"insurer": claim["insurer"]})] if claim else []
    return max(versions) if versions else None


def start_run(claim_id: str) -> ToolContext:
    run_id = f"run_{uuid.uuid4().hex[:12]}"
    db.insert_one("agent_runs", {"runId": run_id, "claimId": claim_id, "status": "running",
                                 "startedAt": datetime.now(timezone.utc), "calcs": [],
                                 "messages": [], "toolCalls": [], "stepCount": 0,
                                 "rulebookVersion": rulebook_version(claim_id)})
    return ToolContext(claimId=claim_id, runId=run_id)


def finish_run(ctx: ToolContext, status: str = "done") -> None:
    db.get_collection("agent_runs").update_one({"runId": ctx.runId, "claimId": ctx.claimId},
                                               {"$set": {"status": status}})
