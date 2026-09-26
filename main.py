import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

import api
import config
import db
from listener import ClaimRunner, Listener

log = logging.getLogger("claimmemory")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start the change-stream listener with the app (set CLAIMMEMORY_LISTENER=off to disable)."""
    listener = None
    if os.environ.get("CLAIMMEMORY_LISTENER", "on").lower() != "off":
        app.state.runner = ClaimRunner()
        listener = Listener(app.state.runner).start()
    yield
    if listener:
        listener.stop()


app = FastAPI(title="ClaimMemory", lifespan=lifespan)
# Local Vite dev server, plus any deployed frontend listed in CORS_ORIGINS (comma-separated).
ORIGINS = ["http://localhost:5173"] + [o.strip().rstrip("/") for o in os.environ.get("CORS_ORIGINS", "").split(",")
                                        if o.strip()]
app.add_middleware(CORSMiddleware, allow_origins=ORIGINS, allow_methods=["*"], allow_headers=["*"])
app.include_router(api.router)


@app.get("/health")
def health():
    try:
        db.get_client().admin.command("ping")
    except Exception:
        # Never leak the URI or a stack trace to the caller.
        log.error("health check: database unreachable")
        return JSONResponse(status_code=503, content={"ok": False, "error": "database unreachable"})
    return {"ok": True, "db": config.DB_NAME, "collections": list(db.COLLECTION_NAMES)}
