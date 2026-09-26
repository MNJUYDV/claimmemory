"""Change-stream listener: a new document in the database starts a review of its claim.

One run per claim at a time; documents that arrive during a run queue exactly one follow-up run.
Inserts flagged source "seed" or "bulk" (harness ingestion, seeding) never trigger anything, and
documents a run already reviewed are skipped.
"""
import logging
import threading

import agent
import db

log = logging.getLogger("claimmemory.listener")
IGNORED_SOURCES = ("seed", "bulk")


class ClaimRunner:
    """Runs reviews in background threads, serialised per claim, with a single queued follow-up."""

    def __init__(self, run_fn=None):
        self.run_fn = run_fn or (lambda claim_id, triggered=None: agent.run_agent(claim_id, triggered=triggered))
        self._triggers = {}  # claim -> documents that arrived since the next run was decided
        self._lock = threading.Lock()
        self._state = {}  # claim -> {"running": bool, "pending": bool}
        self._idle = threading.Condition(self._lock)

    def trigger(self, claim_id: str, document: dict = None) -> str:
        """Start (or queue) a review. `document` is the arrival that caused it: {filename, receivedAt}."""
        with self._lock:
            if document:
                self._triggers.setdefault(claim_id, []).append(document)
            st = self._state.setdefault(claim_id, {"running": False, "pending": False})
            if st["running"]:
                st["pending"] = True  # any number of arrivals during a run collapse into one follow-up
                return "queued"
            st["running"] = True
        threading.Thread(target=self._work, args=(claim_id,), daemon=True, name=f"review-{claim_id}").start()
        return "started"

    def _work(self, claim_id: str) -> None:
        while True:
            with self._lock:
                triggered = self._triggers.pop(claim_id, [])
            try:
                self.run_fn(claim_id, triggered=triggered) if triggered else self.run_fn(claim_id)
            except Exception:
                log.exception("review of %s crashed", claim_id)
            with self._lock:
                st = self._state[claim_id]
                if st["pending"]:
                    st["pending"] = False
                    continue
                st["running"] = False
                self._idle.notify_all()
                return

    def busy(self, claim_id: str) -> bool:
        with self._lock:
            return self._state.get(claim_id, {}).get("running", False)

    def wait_idle(self, timeout: float = 60.0) -> bool:
        with self._idle:
            return self._idle.wait_for(lambda: not any(s["running"] for s in self._state.values()), timeout)


def already_reviewed(claim_id: str, filename: str) -> bool:
    return db.get_collection("agent_runs").find_one({
        "claimId": claim_id, "documentFilenames": filename,
        "status": {"$in": ["running", "completed"]}}, {"_id": 1}) is not None


def handle_insert(doc: dict, runner: ClaimRunner) -> str:
    """Decide what a newly inserted document means. Returns started | queued | ignored | reviewed."""
    if doc.get("source") in IGNORED_SOURCES:
        return "ignored"
    if already_reviewed(doc["claimId"], doc["filename"]):
        return "reviewed"
    return runner.trigger(doc["claimId"], {"filename": doc["filename"], "receivedAt": doc.get("receivedAt")})


class Listener:
    def __init__(self, runner: ClaimRunner):
        self.runner = runner
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._thread = None

    def start(self, wait: float = 10.0):
        self._thread = threading.Thread(target=self._loop, daemon=True, name="change-stream")
        self._thread.start()
        self._ready.wait(wait)  # don't return until the stream is open, or inserts could be missed
        return self

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(5)

    def _loop(self):
        pipeline = [{"$match": {"operationType": "insert",
                                "fullDocument.source": {"$nin": list(IGNORED_SOURCES)}}}]
        while not self._stop.is_set():
            try:
                with db.get_collection("documents").watch(pipeline, max_await_time_ms=500) as stream:
                    self._ready.set()
                    while not self._stop.is_set():
                        change = stream.try_next()
                        if change:
                            outcome = handle_insert(change["fullDocument"], self.runner)
                            log.info("new document %s -> %s", change["fullDocument"].get("filename"), outcome)
            except Exception:
                log.exception("change stream failed; retrying")
                self._ready.set()
                self._stop.wait(2)
