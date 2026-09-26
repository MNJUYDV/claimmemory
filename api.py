"""HTTP API: upload, live event stream, read models. No auth (local demo). Labels are never exposed."""
import json
import queue
import time

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import StreamingResponse

import db
import ingest
import workspace
from events import bus
from tools import ToolContext, replay

router = APIRouter(prefix="/api")
HEARTBEAT_SECONDS = 15


@router.post("/claims/{claim_id}/documents")
def upload_document(claim_id: str, file: UploadFile = File(...)):
    """Save, register and ingest a document. The change-stream listener (not this call) starts the review."""
    try:
        return ingest.save_upload(claim_id, file.filename or "", file.file.read(ingest.MAX_UPLOAD_BYTES + 1))
    except ingest.UploadError as e:
        raise HTTPException(e.status, str(e)) from None


def sse_stream(claim_id: str, heartbeat: float = None):
    """Server-sent events for one claim: every bus event, plus a comment heartbeat when idle."""
    heartbeat = HEARTBEAT_SECONDS if heartbeat is None else heartbeat
    q = queue.Queue()
    unsubscribe = bus.subscribe(lambda e: q.put(e) if e.get("claimId") == claim_id else None)
    try:
        yield ": connected\n\n"
        while True:
            try:
                event = q.get(timeout=heartbeat)
            except queue.Empty:
                yield f": heartbeat {int(time.time())}\n\n"
                continue
            if event["type"] == "tool_call":  # the step list needs the summary, not the full tool output
                event = {k: v for k, v in event.items() if k != "output"}
            yield f"event: {event['type']}\ndata: {json.dumps(event, default=str)}\n\n"
    finally:
        unsubscribe()


@router.get("/claims/{claim_id}/events")
def claim_events(claim_id: str):
    return StreamingResponse(sse_stream(claim_id), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/claims/{claim_id}/workspace")
def claim_workspace(claim_id: str):
    ws = workspace.workspace(claim_id)
    if ws is None:
        raise HTTPException(404, "unknown claim")
    return ws


@router.get("/decisions/{decision_id}/replay")
def decision_replay(decision_id: str):
    decision = db.get_agent_collection("decisions").find_one({"_id": decision_id})
    if not decision:
        raise HTTPException(404, "unknown decision")
    r = replay(ToolContext(decision["claimId"], ""), decision_id)
    pick = lambda rows: [{"filename": d["filename"], "receivedAt": d["receivedAt"]} for d in rows]
    return {"filename": r["filename"], "madeAt": r["madeAt"], "cited": pick(r["cited"]),
            "notCited": pick(r["not_cited"])}


@router.get("/claims/{claim_id}/rulebook")
def claim_rulebook(claim_id: str):
    history = workspace.rulebook_history(claim_id)
    if history is None:
        raise HTTPException(404, "unknown claim")
    return history
