"""In-process event bus. Every agent step is published here (an SSE endpoint will subscribe later)."""
import logging
import queue
import threading

log = logging.getLogger("claimmemory.events")


class EventBus:
    def __init__(self):
        self._lock = threading.Lock()
        self._subs = []  # (run_id or None, callback)

    def subscribe(self, callback, run_id=None):
        """Call callback(event) for every event (or only this run's). Returns an unsubscribe function."""
        entry = (run_id, callback)
        with self._lock:
            self._subs.append(entry)

        def unsubscribe():
            with self._lock:
                if entry in self._subs:
                    self._subs.remove(entry)
        return unsubscribe

    def queue(self, run_id=None):
        """A queue.Queue that receives events: for a consumer running on another thread."""
        q = queue.Queue()
        self.subscribe(q.put, run_id)
        return q

    def publish(self, event: dict) -> None:
        with self._lock:
            subs = list(self._subs)
        for run_id, callback in subs:
            if run_id is None or run_id == event.get("runId"):
                try:
                    callback(event)
                except Exception:  # a broken subscriber must never break a run
                    log.exception("event subscriber failed")


bus = EventBus()
