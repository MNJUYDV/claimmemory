"""The agent loop: Claude tool use over the tools in tools.py, with a durable run log.

Every step is persisted to agent_runs (messages, tool calls with inputs/outputs/timing, status), so a
run can be resumed after a failure, audited afterwards, and streamed live through events.bus.
"""
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import anthropic

import config
import db
import ingest
import runs
import tools
import dataparse
import workspace
from events import bus
from tools import ToolContext, call_tool

log = logging.getLogger("claimmemory.agent")

MAX_STEPS = 40
MAX_TOKENS = 16000
STATUSES = ("running", "completed", "needs_review", "failed")

SYSTEM_PROMPT = """You work for the policyholder's public adjuster. You review an insurance claim to find money the insurer owes the policyholder. You act only through the tools provided; you never supply a claim id or run id.

Work in this order:
1. Call get_claim_state, then get_rules. The rulebook lists the only finding types you may file.
2. Read every document with read_document. Line numbers are display only: never include them in a quote.
3. Record facts (record_fact) for the timeline, and only these four rows: "policy" (only the policy terms that matter to a finding, such as the labor-depreciation clause, the ALE limit, the ordinance-or-law endorsement), "estimates" (one fact per estimate version, quoting its "Total:" line, validFrom set to that estimate's receivedAt from list_documents), "promises" (each promise made to the policyholder), and "living_expenses" (ALE payments and cutoffs, including each ALE notice). Do not record facts for subtotals, line items, bids, or procedural rules. Every label is short plain English, at most 40 characters, with no jargon: "Living expenses promised to month 12", not "ALE through month 12"; "Estimate v2 written", not "Estimate v2 total". When a newer estimate or ALE notice arrives, supersede the older one's fact with supersede_fact (re-record the older facts first if you need their ids: recording an identical fact again is harmless). Copy timestamps exactly, including their UTC offset.
4. Record a decision (record_decision) for each estimate: its filename, madeAt set to its receivedAt, and citedFilenames set to exactly the documents in its "Relied on" header.
5. Use replay on each decision to see which documents the insurer had already received but did not cite.
6. Then apply each rule in the rulebook: use search_policy to find the governing clause, call compute_amount with the rule's computeRule and the right documents, then call upsert_finding with the calcId and verbatim evidence quotes (each at least 10 characters, copied exactly from the document text). Cite the evidence that shows why the finding applies and where the numbers come from.
7. Always compute on the latest non-superseded estimate and the latest ALE notice (the newest of each by receivedAt): newer documents replace older ones. After recomputing each rule, act on the result: if the amount is above 0, call upsert_finding (it updates an existing finding whose amount changed); if the amount is 0, call resolve_finding with that calcId and a one-sentence reason, so an earlier finding the new document fixed is closed. If it is 0 and no finding is open for that rule, file nothing.

Write every finding for the policyholder, in plain English (say "living expenses", never "ALE"): a title of at most 70 characters that says what they lost; a one-sentence summary of at most 140 characters; and 2 to 4 short points of at most 110 characters each, every one starting with "Policy:" (what the policy says), "On file:" (what the documents show) or "Insurer:" (what the insurer did). Example of the format (write yours from this claim's own documents):
  title: "Living expenses stopped six months early"
  summary: "Your insurer promised living expenses through month 12 but stopped paying after month 6."
  points: ["Policy: Living expenses are payable for up to 12 months while the home is unlivable.", "On file: The adjuster promised $1,400 a month through month 12.", "Insurer: A notice ended living-expense payments after month 6."]

Never state a dollar amount that compute_amount did not return; the amount on a finding is set by the system from the calculation. File a finding only for a type in the rulebook, and only when its calculation shows an amount above 0. When you are finished, reply with a one-paragraph summary and make no further tool calls."""


@dataclass
class RunResult:
    runId: str
    claimId: str
    status: str
    steps: int
    summary: str = ""
    error: str = ""


# ---------- persistence and events ----------

def _now():
    return datetime.now(timezone.utc)


def _runs():
    return db.get_collection("agent_runs")


def _emit(ctx: ToolContext, etype: str, **fields):
    bus.publish({"type": etype, "runId": ctx.runId, "claimId": ctx.claimId, "ts": _now().isoformat(), **fields})


def _set(ctx: ToolContext, **fields):
    _runs().update_one({"runId": ctx.runId, "claimId": ctx.claimId}, {"$set": db.prepare_doc("agent_runs", fields)})


def _block_to_param(block) -> dict:
    """An API response block as the request-side param, so history replays exactly."""
    t = block.type
    if t == "text":
        return {"type": "text", "text": block.text}
    if t == "tool_use":
        return {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
    if t == "thinking":
        return {"type": "thinking", "thinking": block.thinking, "signature": block.signature}
    return block.model_dump(mode="json", exclude_none=True)


# ---------- tool execution ----------

def _execute_tools(ctx: ToolContext, step: int, response_blocks: list) -> list:
    """Run every tool_use block in order; log each call; return the tool_result blocks."""
    results = []
    for block in (b for b in response_blocks if b["type"] == "tool_use"):
        started = _now()
        t0 = time.perf_counter()
        output = call_tool(ctx, block["name"], block["input"])
        ms = round((time.perf_counter() - t0) * 1000)
        is_error = "error" in output
        seq = len(_runs().find_one({"runId": ctx.runId}, {"toolCalls": 1})["toolCalls"]) + 1
        record = {"seq": seq, "step": step, "toolUseId": block["id"], "name": block["name"],
                  "input": block["input"], "output": output, "isError": is_error,
                  "startedAt": started, "durationMs": ms}
        _runs().update_one({"runId": ctx.runId}, {"$push": {"toolCalls": record}})
        _emit(ctx, "tool_call", step=step, seq=seq, name=block["name"], input=block["input"],
              output=output, isError=is_error, durationMs=ms,
              summary=workspace.summarize_call(block["name"], block["input"], output))
        if block["name"] == "upsert_finding" and not is_error:
            _emit(ctx, f"finding_{output['status']}", findingType=block["input"]["type"], amount=output["amount"])
            _emit(ctx, "totals_changed", totals=workspace.totals(ctx.claimId))
        if block["name"] == "resolve_finding" and not is_error:
            _emit(ctx, "finding_resolved", findingType=block["input"]["type"])
            _emit(ctx, "totals_changed", totals=workspace.totals(ctx.claimId))
        results.append({"type": "tool_result", "tool_use_id": block["id"],
                        "content": json.dumps(output), "is_error": is_error})
    return results


# ---------- the loop ----------

def _drive(ctx, client, model, max_steps, after_step) -> RunResult:
    run = _runs().find_one({"runId": ctx.runId, "claimId": ctx.claimId})
    messages, step = run["messages"], run["stepCount"]
    usage = dict(run.get("usage") or {})
    summary, status, note = "", "running", ""
    try:
        while True:
            if step >= max_steps:
                status, note = "needs_review", f"step limit ({max_steps}) reached"
                break
            response = client.messages.create(
                model=model, max_tokens=MAX_TOKENS, system=SYSTEM_PROMPT, messages=messages,
                tools=tools.tool_definitions(), cache_control={"type": "ephemeral"})
            step += 1
            blocks = [_block_to_param(b) for b in response.content]
            messages.append({"role": "assistant", "content": blocks})
            text = "\n".join(b["text"] for b in blocks if b["type"] == "text")
            for k, v in (("inputTokens", "input_tokens"), ("outputTokens", "output_tokens"),
                         ("cacheReadTokens", "cache_read_input_tokens"),
                         ("cacheWriteTokens", "cache_creation_input_tokens")):
                usage[k] = usage.get(k, 0) + (getattr(response.usage, v, 0) or 0)
            _emit(ctx, "assistant_message", step=step, text=text, stopReason=response.stop_reason)

            done = False
            if response.stop_reason == "tool_use":
                messages.append({"role": "user", "content": _execute_tools(ctx, step, blocks)})
            elif response.stop_reason == "end_turn":
                status, summary, done = "completed", text, True
            elif response.stop_reason == "pause_turn":
                pass  # the model paused; send the history back as is
            else:  # max_tokens, refusal, ...
                status, note, done = "needs_review", f"model stopped: {response.stop_reason}", True

            fields = {"messages": messages, "stepCount": step, "usage": usage}
            if done:
                fields.update(status=status, summary=summary, note=note, finishedAt=_now())
            _set(ctx, **fields)  # one write: the step is durable only once messages and status agree
            if done:
                break
            if after_step:
                after_step(step)  # test hook: may raise to simulate a crash after a durable step
    except Exception as e:
        log.exception("run %s failed at step %s", ctx.runId, step)
        err = f"{type(e).__name__}: {e}"[:500]
        _set(ctx, status="failed", error=err, finishedAt=_now())
        _emit(ctx, "run_finished", status="failed", steps=step, error=err)
        return RunResult(ctx.runId, ctx.claimId, "failed", step, error=err)

    if status == "needs_review" and not summary:
        _set(ctx, status=status, note=note, finishedAt=_now())
    _emit(ctx, "run_finished", status=status, steps=step, note=note)
    return RunResult(ctx.runId, ctx.claimId, status, step, summary=summary, error=note)


def _client(client):
    return client or anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)


def held_filenames(claim_id: str) -> list:
    entries = json.loads((dataparse.DATA_DIR / "manifest.json").read_text())
    return [e["filename"] for e in entries if e["claimId"] == claim_id and e.get("hold")]


def run_agent(claim_id: str, task: str = "review_claim", *, max_steps: int = MAX_STEPS,
              client=None, model: str = None, after_step=None, rulebook_version: int = None) -> RunResult:
    """Ingest the claim's documents (skipping held ones), then run the tool-use loop to completion.

    The run reads the rulebook version active at start (or rulebook_version, for a candidate re-run)
    and records it on the run."""
    if not db.get_collection("claims").find_one({"claimId": claim_id}):
        raise ValueError(f"unknown claim {claim_id!r}; seed the database first")
    if task != "review_claim":
        raise ValueError(f"unknown task {task!r}")
    ingest.ingest_claim(claim_id, exclude=held_filenames(claim_id))
    ctx = runs.start_run(claim_id, rulebook_version)
    _set(ctx, documentFilenames=[d["filename"] for d in db.get_collection("documents").find(
        {"claimId": claim_id}, {"filename": 1})])  # what this run reviews (the listener skips these)
    _set(ctx, task=task, model=model or config.CLAUDE_MODEL,
         messages=[{"role": "user", "content": f"Review this claim ({task}) and file any findings the "
                                               f"rulebook allows."}])
    _emit(ctx, "run_started", task=task)
    return _drive(ctx, _client(client), model or config.CLAUDE_MODEL, max_steps, after_step)


def resume_run(run_id: str, *, max_steps: int = MAX_STEPS, client=None, model: str = None,
               after_step=None) -> RunResult:
    """Continue a failed, needs_review or interrupted run from its last durable step."""
    run = _runs().find_one({"runId": run_id})
    if not run:
        raise KeyError(f"unknown run {run_id!r}")
    ctx = ToolContext(run["claimId"], run_id)
    if run["status"] == "completed":
        return RunResult(run_id, run["claimId"], "completed", run["stepCount"], summary=run.get("summary", ""))
    _set(ctx, status="running", error="", note="")
    _emit(ctx, "run_resumed", fromStep=run["stepCount"])
    return _drive(ctx, _client(client), model or run.get("model") or config.CLAUDE_MODEL,
                  max_steps, after_step)
