"""Change-stream listener: a new document in the database starts a review of its claim.

One run per claim at a time; documents that arrive during a run queue exactly one follow-up run.
Inserts flagged source "seed" or "bulk" (harness ingestion, seeding) never trigger anything, and
documents a run already reviewed are skipped.
"""
import logging
import threading

import agent
import db
import improve

log = logging.getLogger("claimmemory.listener")
IGNORED_SOURCES = ("seed", "bulk")


class ClaimRunner:
    """Runs jobs in background threads, serialised per claim. A job is a review or an improvement;
    any number of arrivals of one job during a run collapse into a single follow-up."""

    def __init__(self, run_fn=None, improve_fn=None):
        self.run_fn = run_fn or (lambda claim_id, triggered=None: agent.run_agent(claim_id, triggered=triggered))
        self.improve_fn = improve_fn or improve.improve_from_settlement
        self._lock = threading.Lock()
        self._jobs = {}      # claim -> job names waiting, in arrival order
        self._triggers = {}  # claim -> documents that arrived since the next review was decided
        self._running = set()
        self._idle = threading.Condition(self._lock)

    def trigger(self, claim_id: str, document: dict = None, job: str = "review") -> str:
        """Start (or queue) a job. `document` is the arrival that caused it: {filename, receivedAt}."""
        with self._lock:
            if document and job == "review":
                self._triggers.setdefault(claim_id, []).append(document)
            jobs = self._jobs.setdefault(claim_id, [])
            if job not in jobs:
                jobs.append(job)
            if claim_id in self._running:
                return "queued"
            self._running.add(claim_id)
        threading.Thread(target=self._work, args=(claim_id,), daemon=True, name=f"review-{claim_id}").start()
        return "started"

    def _work(self, claim_id: str) -> None:
        while True:
            with self._lock:
                jobs = self._jobs.get(claim_id, [])
                if not jobs:
                    self._running.discard(claim_id)
                    self._idle.notify_all()
                    return
                job = jobs.pop(0)
                triggered = self._triggers.pop(claim_id, []) if job == "review" else []
            try:
                if job == "improve":
                    self.improve_fn(claim_id)
                elif triggered:
                    self.run_fn(claim_id, triggered=triggered)
                else:
                    self.run_fn(claim_id)
            except Exception:
                log.exception("%s of %s crashed", job, claim_id)

    def busy(self, claim_id: str) -> bool:
        with self._lock:
            return claim_id in self._running

    def wait_idle(self, timeout: float = 60.0) -> bool:
        with self._idle:
            return self._idle.wait_for(lambda: not self._running, timeout)


def already_reviewed(claim_id: str, filename: str) -> bool:
    return db.get_collection("agent_runs").find_one({
        "claimId": claim_id, "documentFilenames": filename,
        "status": {"$in": ["running", "completed"]}}, {"_id": 1}) is not None


def handle_insert(doc: dict, runner: ClaimRunner) -> str:
    """Decide what a newly inserted document means. Returns started | queued | ignored | reviewed."""
    if doc.get("source") in IGNORED_SOURCES:
        return "ignored"
    if doc.get("type") == "settlement":  # the claim's real outcome: learn from it, if we have labels for it
        if db.get_collection("labels").find_one({"claimId": doc["claimId"]}):
            return runner.trigger(doc["claimId"], job="improve")
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
