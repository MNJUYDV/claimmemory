"""Agent run bookkeeping. Harness code, not a tool."""
import uuid
from datetime import datetime, timezone

import db
import rulebook
from tools import ToolContext


def start_run(claim_id: str, rulebook_version: int = None) -> ToolContext:
    """Start a run pinned to a rulebook version (default: the version active right now)."""
    run_id = f"run_{uuid.uuid4().hex[:12]}"
    db.insert_one("agent_runs", {"runId": run_id, "claimId": claim_id, "status": "running",
                                 "startedAt": datetime.now(timezone.utc), "calcs": [],
                                 "messages": [], "toolCalls": [], "stepCount": 0,
                                 "rulebookVersion": rulebook_version if rulebook_version is not None
                                 else rulebook.active_version_for_claim(claim_id)})
    return ToolContext(claimId=claim_id, runId=run_id)


def finish_run(ctx: ToolContext, status: str = "done") -> None:
    db.get_collection("agent_runs").update_one({"runId": ctx.runId, "claimId": ctx.claimId},
                                               {"$set": {"status": status}})
