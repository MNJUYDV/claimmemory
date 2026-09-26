import logging

from fastapi import FastAPI
from fastapi.responses import JSONResponse

import config
import db

log = logging.getLogger("claimmemory")
app = FastAPI(title="ClaimMemory")


@app.get("/health")
def health():
    try:
        db.get_client().admin.command("ping")
    except Exception:
        # Never leak the URI or a stack trace to the caller.
        log.error("health check: database unreachable")
        return JSONResponse(status_code=503, content={"ok": False, "error": "database unreachable"})
    return {"ok": True, "db": config.DB_NAME, "collections": list(db.COLLECTION_NAMES)}
