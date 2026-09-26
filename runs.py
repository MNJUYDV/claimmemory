"""Agent run bookkeeping. Harness code, not a tool."""
import uuid
from datetime import datetime, timezone

import db
from tools import ToolContext


def start_run(claim_id: str) -> ToolContext:
    run_id = f"run_{uuid.uuid4().hex[:12]}"
    db.insert_one("agent_runs", {"runId": run_id, "claimId": claim_id, "status": "running",
                                 "startedAt": datetime.now(timezone.utc), "calcs": []})
    return ToolContext(claimId=claim_id, runId=run_id)


def finish_run(ctx: ToolContext, status: str = "done") -> None:
    db.get_collection("agent_runs").update_one({"runId": ctx.runId, "claimId": ctx.claimId},
                                               {"$set": {"status": status}})
