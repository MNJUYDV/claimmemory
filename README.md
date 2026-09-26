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

## Synthetic data (Phase 1)

```bash
python scripts/generate_data.py          # regenerate data/ (deterministic; asserts every planted amount)
python scripts/seed.py --db claimmemory  # claims, payments, labels, rulebook v1 (idempotent; no documents)
```

Claims: `PK-20719` (past, labeled, training) and `HO-48213` (live). `data/labels.json` is ground
truth for the scorer only; agent code must read collections via `db.get_agent_collection`, which
refuses `labels` and `scores`. `dataparse.py` recomputes every amount from the files.

## Tools and guardrails (Phase 2, no LLM)

- `ingest.py`: `ingest_document(claimId, filename)` / `ingest_claim(claimId)`. Harness-only, idempotent.
- `tools.py`: 11 tools with JSON schemas (`tool_definitions()`), dispatched with `call_tool(ctx, name, args)`.
  `ToolContext(claimId, runId)` is injected by the harness (`runs.start_run(claimId)`); the model never
  supplies either. Amounts come only from `compute_amount`; quotes must be verbatim; errors are JSON.
- Tools read data only through `db.get_agent_collection()`, which refuses `labels` and `scores`.

## Agent, run log and scorer (Phase 3)

```bash
python scripts/seed.py --db claimmemory
python scripts/run_review.py --claim PK-20719 --db claimmemory   # review, then score
```

- `agent.py`: `run_agent(claimId)` ingests the claim (skipping `hold: true` files), then runs the Claude
  tool-use loop (max 40 steps). Every step is saved to `agent_runs` (messages, tool calls with
  inputs/outputs/timing, status). `resume_run(runId)` continues a failed or interrupted run.
  Statuses: `running`, `completed`, `needs_review`, `failed`. Steps are published on `events.bus`.
- Guardrail: `upsert_finding` refuses a finding type unless the insurer's current rulebook has an active
  rule for it.
- `scorer.py`: `score_run(runId)` compares a run's findings with the labels and stores the result in
  `scores`. It is not a tool and the agent cannot reach it.

## Run

```bash
uvicorn main:app --reload
curl localhost:8000/health
```

## Test

```bash
pytest                        # offline + integration tests (DB claimmemory_test); skips llm tests
pytest -m "not integration and not llm"   # no network
pytest -m llm                 # real Claude reviews (slow, costs money)
pytest -m "llm or not llm"    # everything
```

Integration tests hit Atlas, Voyage and Anthropic and create vector indexes on `claimmemory_test`.
