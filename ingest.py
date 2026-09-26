"""Document ingestion. Plain harness code, called by the harness; NOT a tool the agent sees."""
import hashlib
import json
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone

import db
import embeddings
from constants import OPEN_ENDED, parse_dt
import dataparse
from dataparse import parse_effective_from_text

CLAUSE_TYPES = ("policy", "endorsement")


def doc_type(filename: str) -> str:
    if filename == "policy.txt":
        return "policy"
    if filename.startswith("endorsement"):
        return "endorsement"
    if filename.startswith("estimate_"):
        return "estimate"
    if filename.startswith("contractor_bid"):
        return "bid"
    if filename.startswith("adjuster_email"):
        return "email"
    if filename == "ale_notice.txt":
        return "notice"
    if filename == "payments.json":
        return "payments"
    return "other"


@dataclass(frozen=True)
class Clause:
    section: str   # "4.2" for numbered clauses, "P1", "P2", ... for paragraph-split documents
    heading: str   # enclosing section heading, or the document title
    text: str


def split_clauses(text: str) -> list:
    """Split a policy or endorsement into clauses.

    Documents with numbered clauses ("4.2 ...") split per clause, each tagged with its
    "SECTION n. HEADING". Documents without them split into blank-line-separated paragraphs.
    """
    lines = text.splitlines()
    numbered = re.compile(r"^(\d+\.\d+)\s+(.*)$")
    heading_re = re.compile(r"^SECTION \d+\.\s*(.+)$")
    if any(numbered.match(l) for l in lines):
        clauses, heading, cur = [], "", None
        for line in lines + [""]:
            h, n = heading_re.match(line), numbered.match(line)
            if h or n or not line.strip():
                if cur:
                    clauses.append(Clause(cur[0], cur[1], " ".join(cur[2])))
                    cur = None
            if h:
                heading = h.group(1).strip()
            elif n:
                cur = (n.group(1), heading, [n.group(2).strip()])
            elif cur and line.strip():
                cur[2].append(line.strip())
        return clauses
    title = next((l.strip() for l in lines if l.strip()), "")
    blocks = [b.strip() for b in re.split(r"\n\s*\n", text) if b.strip()]
    return [Clause(f"P{i}", title, " ".join(b.split())) for i, b in enumerate(blocks, 1)]


def _manifest_entry(claim_id: str, filename: str) -> dict:
    for e in json.loads((dataparse.DATA_DIR / "manifest.json").read_text()):
        if e["claimId"] == claim_id and e["filename"] == filename:
            return e
    raise KeyError(f"{claim_id}/{filename} is not in the manifest")


def ingest_document(claim_id: str, filename: str, source: str = "upload") -> dict:
    """Store one document (text, receivedAt, embedding) and, for policy/endorsement, its clauses.

    Idempotent by (claimId, filename): re-running replaces in place and reuses the stored
    embedding when the text is unchanged. `source` tags the insert for the change-stream listener:
    "seed" and "bulk" inserts never trigger a review; "upload" (a new document arriving) does.
    """
    entry = _manifest_entry(claim_id, filename)
    text = (dataparse.DATA_DIR / claim_id / filename).read_text()
    sha = hashlib.sha256(text.encode()).hexdigest()
    dtype = doc_type(filename)
    key = {"claimId": claim_id, "filename": filename}

    existing = db.get_collection("documents").find_one({**key, "textSha256": sha})
    embedding = existing["embedding"] if existing and existing.get("embedding") else \
        embeddings.embed([text], "document")[0]
    db.upsert_one("documents", key, {
        **key, "type": dtype, "text": text, "textSha256": sha, "embedding": embedding,
        "receivedAt": parse_dt(entry["receivedAt"]), "hold": bool(entry.get("hold")), "source": source,
        "ingestedAt": datetime.now(timezone.utc)})

    n_clauses = 0
    if dtype in CLAUSE_TYPES:
        n_clauses = _ingest_clauses(claim_id, filename, text)
    return {"claimId": claim_id, "filename": filename, "type": dtype, "clauses": n_clauses}


def _ingest_clauses(claim_id: str, filename: str, text: str) -> int:
    clauses = split_clauses(text)
    eff = parse_effective_from_text(text)
    effective_from = datetime(eff.year, eff.month, eff.day, tzinfo=timezone.utc)
    vectors = embeddings.embed([f"{c.heading}: {c.text}" for c in clauses], "document") if clauses else []
    for c, vec in zip(clauses, vectors):
        db.upsert_one("policy_clauses", {"claimId": claim_id, "filename": filename, "section": c.section}, {
            "claimId": claim_id, "filename": filename, "section": c.section, "heading": c.heading,
            "text": c.text, "embedding": vec, "effectiveFrom": effective_from, "effectiveTo": OPEN_ENDED})
    db.get_collection("policy_clauses").delete_many(
        {"claimId": claim_id, "filename": filename, "section": {"$nin": [c.section for c in clauses]}})
    return len(clauses)


def ingest_claim(claim_id: str, exclude=()) -> list:
    """Ingest every manifest file of a claim except those in exclude."""
    return [ingest_document(claim_id, e["filename"], source="bulk")
            for e in json.loads((dataparse.DATA_DIR / "manifest.json").read_text())
            if e["claimId"] == claim_id and e["filename"] not in exclude]


_MANIFEST_LOCK = threading.Lock()
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
MAX_UPLOAD_BYTES = 1_000_000


class UploadError(ValueError):
    """A rejected upload; .status is the HTTP status to answer with."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def save_upload(claim_id: str, filename: str, data: bytes) -> dict:
    """Store an uploaded document: file under data/<claimId>/, manifest entry (receivedAt = now, hold
    cleared), then ingest it. Does NOT start the agent: the change-stream listener does that.
    Uploading identical content again is a no-op ({"duplicate": True})."""
    if not db.get_collection("claims").find_one({"claimId": claim_id}):
        raise UploadError(f"unknown claim {claim_id!r}", 404)
    if not _SAFE_NAME.match(filename or "") or not filename.endswith((".txt", ".json")):
        raise UploadError("filename must be a plain .txt or .json name")
    if len(data) > MAX_UPLOAD_BYTES:
        raise UploadError("file too large", 413)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise UploadError("file must be UTF-8 text") from None
    sha = hashlib.sha256(text.encode()).hexdigest()
    if db.get_collection("documents").find_one({"claimId": claim_id, "filename": filename, "textSha256": sha}):
        return {"claimId": claim_id, "filename": filename, "duplicate": True}

    with _MANIFEST_LOCK:
        folder = dataparse.DATA_DIR / claim_id
        folder.mkdir(parents=True, exist_ok=True)
        (folder / filename).write_text(text)
        path = dataparse.DATA_DIR / "manifest.json"
        entries = json.loads(path.read_text())
        received = datetime.now(timezone.utc).isoformat(timespec="seconds")
        entry = next((e for e in entries if e["claimId"] == claim_id and e["filename"] == filename), None)
        if entry:
            entry.update(receivedAt=received, hold=False)
        else:
            entries.append({"claimId": claim_id, "filename": filename, "receivedAt": received, "hold": False})
        path.write_text(json.dumps(entries, indent=2) + "\n")
    return {**ingest_document(claim_id, filename, source="upload"), "receivedAt": received, "duplicate": False}
