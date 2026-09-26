"""Create regular and Atlas vector indexes. Safe to run repeatedly.

Usage: python scripts/create_indexes.py --db claimmemory
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

READY_TIMEOUT_S = 180
POLL_S = 5

CLAIM_ID_COLLECTIONS = (
    "documents", "policy_clauses", "facts", "decisions",
    "findings", "payments", "agent_runs", "labels", "scores",
)
EXTRA_INDEXES = (
    ("documents", "receivedAt"),
    ("facts", "validFrom"),
    ("agent_runs", "status"),
    ("rules", "insurer"),
)


def vector_definition(filter_paths):
    return {
        "fields": [
            {"type": "vector", "path": "embedding", "numDimensions": 1024, "similarity": "cosine"},
            *({"type": "filter", "path": p} for p in filter_paths),
        ]
    }


VECTOR_INDEXES = (
    ("documents", "doc_vectors", vector_definition(["claimId", "type"])),
    ("policy_clauses", "clause_vectors", vector_definition(["claimId", "effectiveFrom", "effectiveTo"])),
)


def print_manual_instructions(reason):
    print(f"\nCould not create vector indexes from the driver: {reason}", file=sys.stderr)
    print("Create them in the Atlas UI (Search & Vector Search > Create Index > "
          "Vector Search > JSON editor), on database "
          f"'{os.environ['DB_NAME']}':\n", file=sys.stderr)
    for coll, name, definition in VECTOR_INDEXES:
        print(f"Collection: {coll}   Index name: {name}", file=sys.stderr)
        print(json.dumps(definition, indent=2), file=sys.stderr)
        print(file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="database name")
    args = parser.parse_args()
    os.environ["DB_NAME"] = args.db  # must precede importing config

    import db
    from pymongo.errors import PyMongoError
    from pymongo.operations import SearchIndexModel

    database = db.get_db()
    existing = set(database.list_collection_names())
    for name in db.COLLECTION_NAMES:
        if name not in existing:
            database.create_collection(name)

    for name in CLAIM_ID_COLLECTIONS:
        database[name].create_index("claimId")
    for name, field in EXTRA_INDEXES:
        database[name].create_index(field)
    print("Regular indexes OK")

    try:
        for coll, name, definition in VECTOR_INDEXES:
            have = {i["name"] for i in database[coll].list_search_indexes()}
            if name in have:
                print(f"Vector index {coll}.{name} already exists")
                continue
            database[coll].create_search_index(
                SearchIndexModel(definition=definition, name=name, type="vectorSearch")
            )
            print(f"Vector index {coll}.{name} created")
    except PyMongoError as e:
        print_manual_instructions(type(e).__name__)
        sys.exit(1)

    deadline = time.monotonic() + READY_TIMEOUT_S
    pending = {name: coll for coll, name, _ in VECTOR_INDEXES}
    while pending and time.monotonic() < deadline:
        for name, coll in list(pending.items()):
            for idx in database[coll].list_search_indexes(name):
                if idx.get("status") == "READY" and idx.get("queryable", True):
                    print(f"Vector index {coll}.{name} READY")
                    del pending[name]
        if pending:
            time.sleep(POLL_S)
    if pending:
        print(f"ERROR: not READY after {READY_TIMEOUT_S}s: {', '.join(pending)}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
