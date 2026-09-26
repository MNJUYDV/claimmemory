# ClaimMemory backend

Python 3.11, FastAPI, MongoDB Atlas.

## Setup

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then fill in MONGODB_URI, VOYAGE_API_KEY, ANTHROPIC_API_KEY
```

Optional: `DB_NAME` (default `claimmemory`), `VOYAGE_MODEL` (default `voyage-3-large`), `CLAUDE_MODEL` (default `claude-sonnet-5`).
The app exits at startup if a required variable is missing.

## Create indexes

```bash
python scripts/create_indexes.py --db claimmemory
```

Safe to re-run. Waits up to 3 minutes for the vector indexes to become READY. If your
Atlas tier doesn't allow creating search indexes from the driver, it prints the index
JSON to paste into the Atlas UI and exits non-zero.

## Run

```bash
uvicorn main:app --reload
curl localhost:8000/health
```

## Test

```bash
pytest                    # everything (uses DB claimmemory_test)
pytest -m "not integration"   # no network
```

Integration tests hit Atlas, Voyage and Anthropic and create vector indexes on `claimmemory_test`.
