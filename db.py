"""MongoDB connection. The client is created lazily so a bad URI never breaks import."""
from datetime import datetime
from functools import lru_cache

import certifi
from pymongo import MongoClient
from pymongo.collection import Collection
from pymongo.database import Database

import config
from constants import OPEN_ENDED

COLLECTION_NAMES = (
    "claims",
    "documents",
    "policy_clauses",
    "facts",
    "decisions",
    "findings",
    "payments",
    "rules",
    "agent_runs",
    "labels",
    "scores",
)


@lru_cache(maxsize=1)
def get_client() -> MongoClient:
    return MongoClient(config.MONGODB_URI, serverSelectionTimeoutMS=5000, tlsCAFile=certifi.where(),
                       tz_aware=True)


def get_db() -> Database:
    return get_client()[config.DB_NAME]


def get_collection(name: str) -> Collection:
    if name not in COLLECTION_NAMES:
        raise KeyError(name)
    return get_db()[name]


def __getattr__(name: str) -> Collection:
    """Allow `db.claims`, `db.documents`, ... for each of the 11 collections."""
    if name in COLLECTION_NAMES:
        return get_collection(name)
    raise AttributeError(name)


# Date fields we store, and the open-ended ones that default to OPEN_ENDED.
DATE_FIELDS = ("receivedAt", "validFrom", "validTo", "effectiveFrom", "effectiveTo",
               "lossDate", "paidAt", "createdAt", "updatedAt", "ingestedAt", "madeAt", "startedAt", "finishedAt", "scoredAt")
OPEN_ENDED_FIELDS = {"policy_clauses": "effectiveTo", "facts": "validTo"}


def prepare_doc(collection: str, doc: dict) -> dict:
    """Return a copy of doc that is safe to write: dates must be tz-aware datetimes.

    Raises TypeError for string/naive dates. A missing or None open-ended field
    (policy_clauses.effectiveTo, facts.validTo) becomes OPEN_ENDED.
    """
    out = dict(doc)
    open_field = OPEN_ENDED_FIELDS.get(collection)
    if open_field and out.get(open_field) is None:
        out[open_field] = OPEN_ENDED
    for field in DATE_FIELDS:
        if field not in out:
            continue
        v = out[field]
        if not isinstance(v, datetime) or v.tzinfo is None or v.utcoffset() is None:
            raise TypeError(f"{collection}.{field} must be a timezone-aware datetime, got {v!r}")
    return out


def insert_one(collection: str, doc: dict):
    prepared = prepare_doc(collection, doc)  # validate before touching the driver
    return get_collection(collection).insert_one(prepared)


def upsert_one(collection: str, filter: dict, doc: dict):
    """Replace-or-insert the document matching filter. Idempotent; validates dates first."""
    prepared = prepare_doc(collection, doc)
    return get_collection(collection).replace_one(filter, prepared, upsert=True)


# Ground truth. Only the scorer may read these; agent code must use get_agent_collection().
SCORER_ONLY = frozenset({"labels", "scores"})


def get_agent_collection(name: str) -> Collection:
    if name in SCORER_ONLY:
        raise PermissionError(f"collection {name!r} is scorer-only and not readable by the agent")
    return get_collection(name)
