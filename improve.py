"""Self-improving rulebook: turn a run's misses into a candidate rule, test it, promote it only if it helps.

Harness code, not a tool. The proposer (a separate Claude call) sees only the scorer's miss report for
the training claim: never raw labels, never another claim's data. A candidate is promoted only when a
re-run of the training claim gets strictly better without losing anything.
"""
import json
import logging
import re
from datetime import datetime, timezone

import anthropic

import agent
import config
import db
import rulebook
import scorer
import tools
from events import bus

log = logging.getLogger("claimmemory.improve")

ALLOWED_TYPES = tools.FINDING_TYPES
COMPUTE_RULES = tools.COMPUTE_RULES
RULE_FOR_TYPE = tools.RULE_FOR_TYPE
MIN_INSTRUCTION, MAX_INSTRUCTION = 20, 500

COMPUTE_RULE_HELP = {
    "labor_depreciation_refund": "inputs {estimate}: sums the labor_depreciation column of an estimate's line items",
    "missing_coverage": "inputs {bid, estimate, endorsement}: code-upgrade items in the contractor bid that appear "
                        "in no estimate line, capped at the endorsement limit",
    "unpaid_ale": "inputs {promise, notice}: (promised months - months actually paid) x monthly rate",
}

PROPOSER_SYSTEM = """You improve the rulebook of an insurance-claim review agent. The agent files a finding only for a type that has a rule in its rulebook. You are given a miss report: finding types the agent failed to file on a claim it reviewed, each with the expected amount and the evidence (document quotes) that showed the error.

Write ONE new rule that would let the agent catch that kind of error on any claim of this insurer. The rule's instruction is read by the agent, so it must say what to compare or look for, in which documents, and when a finding applies. Keep it general: no claim ids, names, addresses or dollar figures. Respond with JSON only: {"type", "instruction", "computeRule"}. The instruction must be 20 to 500 characters. computeRule must be the calculation that produces the amount for that type."""

PROPOSAL_SCHEMA = {
    "type": "object",
    "properties": {"type": {"type": "string", "enum": list(ALLOWED_TYPES)},
                   "instruction": {"type": "string"},
                   "computeRule": {"type": "string", "enum": list(COMPUTE_RULES)}},
    "required": ["type", "instruction", "computeRule"],
    "additionalProperties": False,
}


def _now():
    return datetime.now(timezone.utc)


def _emit(run_id, claim_id, etype, **fields):
    bus.publish({"type": etype, "runId": run_id, "claimId": claim_id, "ts": _now().isoformat(), **fields})


# ---------- proposer ----------

def claude_proposer(client=None):
    """The default proposer: one Claude call returning strict JSON text."""
    client = client or anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

    def propose(miss_report: list, context: dict) -> str:
        prompt = json.dumps({"missReport": miss_report, **context}, indent=2)
        response = client.messages.create(
            model=config.CLAUDE_MODEL, max_tokens=1500, system=PROPOSER_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
            output_config={"format": {"type": "json_schema", "schema": PROPOSAL_SCHEMA}})
        return "".join(b.text for b in response.content if b.type == "text")
    return propose


def validate_proposal(raw, missed_types) -> tuple:
    """Return (rule, None) if the proposal is acceptable, else (None, reason)."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return None, "proposal is not valid JSON"
    if not isinstance(raw, dict):
        return None, "proposal is not a JSON object"
    if set(raw) != {"type", "instruction", "computeRule"}:
        return None, f"proposal must have exactly type, instruction, computeRule (got {sorted(raw)})"
    rtype, instruction, compute = raw["type"], raw["instruction"], raw["computeRule"]
    if rtype not in ALLOWED_TYPES:
        return None, f"unknown finding type {rtype!r}"
    if compute not in COMPUTE_RULES:
        return None, f"unknown computeRule {compute!r}"
    if RULE_FOR_TYPE[rtype] != compute:
        return None, f"computeRule {compute!r} does not compute {rtype!r} findings"
    if rtype not in missed_types:
        return None, f"{rtype!r} was not a missed type"
    if not isinstance(instruction, str) or not MIN_INSTRUCTION <= len(instruction.strip()) <= MAX_INSTRUCTION:
        return None, f"instruction must be {MIN_INSTRUCTION}-{MAX_INSTRUCTION} characters"
    if re.search(r"\$\s?\d|\b[A-Z]{2}-\d{4,}\b", instruction):
        return None, "instruction contains a dollar figure or claim id; rules must be general"
    return {"type": rtype, "instruction": instruction.strip(), "computeRule": compute}, None


# ---------- promotion decision ----------

def summarize(score: dict) -> dict:
    return {"runId": score["runId"], "rulebookVersion": score.get("rulebookVersion"),
            "caughtCount": score["caughtCount"], "expectedCount": score["expectedCount"],
            "caught": score["caught"], "missed": score["missed"], "falsePositives": score["falsePositives"],
            "exactAmountCount": score["exactAmountCount"],
            "amounts": {t: {"found": a["found"], "exact": a["exact"]} for t, a in score["amounts"].items()},
            "scoredAt": score["scoredAt"]}


def decide(before: dict, after: dict) -> tuple:
    """Promote only if strictly more types are caught, none are lost, false positives don't grow and
    every caught amount is exact. Returns (promote, reason)."""
    problems = []
    if after["caughtCount"] <= before["caughtCount"]:
        problems.append(f"caught count did not increase ({before['caughtCount']} -> {after['caughtCount']})")
    lost = sorted(set(before["caught"]) - set(after["caught"]))
    if lost:
        problems.append(f"lost previously caught types: {', '.join(lost)}")
    if len(after["falsePositives"]) > len(before["falsePositives"]):
        problems.append(f"false positives increased ({len(before['falsePositives'])} -> "
                        f"{len(after['falsePositives'])}: {', '.join(after['falsePositives'])})")
    inexact = sorted(t for t in after["caught"] if not after["amounts"][t]["exact"])
    if inexact:
        problems.append(f"amounts not exact for: {', '.join(inexact)}")
    if problems:
        return False, "; ".join(problems)
    gained = sorted(set(after["caught"]) - set(before["caught"]))
    return True, (f"caught {before['caughtCount']} -> {after['caughtCount']} of {after['expectedCount']} "
                  f"(gained {', '.join(gained)}); nothing lost, no new false positives, amounts exact")


# ---------- the improvement loop ----------

def improve_rulebook(training_claim_id: str, run_id: str, *, proposer=None, client=None, agent_client=None,
                     max_steps: int = agent.MAX_STEPS) -> dict:
    """Propose, test and (maybe) promote one new rule from the misses of `run_id` on the training claim."""
    run = db.get_collection("agent_runs").find_one({"runId": run_id})
    if not run or run["claimId"] != training_claim_id:
        raise ValueError(f"run {run_id!r} is not a run of claim {training_claim_id!r}")
    insurer = rulebook.insurer_of(training_claim_id)
    base_version = run["rulebookVersion"]
    emit = lambda etype, **f: _emit(run_id, training_claim_id, etype, **f)

    before = db.get_collection("scores").find_one({"runId": run_id}) or scorer.score_run(run_id)
    if not before["missed"]:
        emit("no_change", reason="nothing was missed")
        return {"decision": "no_change", "reason": "no change: nothing was missed", "version": base_version}

    miss_report = before["missDetails"]  # scorer output only: never raw labels or other claims
    emit("miss_detected", missReport=miss_report, scoreBefore=summarize(before))

    context = {"allowedTypes": list(ALLOWED_TYPES), "computeRules": COMPUTE_RULE_HELP,
               "currentRules": [{"type": r["type"], "instruction": r["instruction"],
                                 "computeRule": r["computeRule"]}
                                for r in rulebook.rules_for(insurer, base_version)]}
    try:
        raw = (proposer or claude_proposer(client))(miss_report, context)
    except Exception as e:
        log.exception("proposer failed")
        reason = f"proposer failed ({type(e).__name__})"
        emit("rule_rejected", stage="proposal", reason=reason)
        return {"decision": "rejected", "stage": "proposal", "reason": reason, "version": base_version}
    rule, problem = validate_proposal(raw, {m["type"] for m in miss_report})
    if problem:  # invalid output: rejected, and nothing has touched the database
        emit("rule_rejected", stage="proposal", reason=problem, proposal=raw if isinstance(raw, (dict, str)) else None)
        return {"decision": "rejected", "stage": "proposal", "reason": problem, "version": base_version}
    emit("rule_proposed", proposal=rule)

    provenance = {"proposedFromRunId": run_id, "trainingClaimId": training_claim_id,
                  "scoreBefore": summarize(before)}
    version = rulebook.create_candidate(insurer, base_version, rule, provenance)
    try:
        result = agent.run_agent(training_claim_id, client=agent_client, max_steps=max_steps,
                                 rulebook_version=version)
        if result.status != "completed":
            raise RuntimeError(f"candidate run ended {result.status}: {result.error}")
        after = scorer.score_run(result.runId)
    except Exception as e:
        log.exception("candidate run failed")
        reason = f"candidate run failed: {e}"[:300]
        rulebook.set_status(insurer, version, "rejected", decision="rejected", reason=reason, decidedAt=_now())
        emit("rule_rejected", stage="candidate_run", version=version, reason=reason)
        return {"decision": "rejected", "stage": "candidate_run", "reason": reason, "version": base_version,
                "candidateVersion": version}

    emit("candidate_scored", version=version, candidateRunId=result.runId, scoreAfter=summarize(after))
    promote, reason = decide(before, after)
    decided = {"candidateRunId": result.runId, "scoreAfter": summarize(after), "scoredAt": after["scoredAt"],
               "decision": "promoted" if promote else "rejected", "reason": reason, "decidedAt": _now()}
    if promote:
        rulebook.promote(insurer, version, **decided)
        emit("rule_promoted", version=version, previousVersion=base_version, reason=reason)
    else:
        rulebook.set_status(insurer, version, "rejected", **decided)
        emit("rule_rejected", stage="candidate_score", version=version, reason=reason)
    return {"decision": decided["decision"], "reason": reason, "proposal": rule, "candidateVersion": version,
            "candidateRunId": result.runId, "version": version if promote else base_version,
            "scoreBefore": summarize(before), "scoreAfter": summarize(after)}


def improve_from_settlement(claim_id: str, **kwargs) -> dict:
    """A settlement letter is the claim's real outcome. Score the latest review run against the labels,
    then try to learn a rule from what it missed. Runs on the rulebook version active now."""
    if not db.get_collection("labels").find_one({"claimId": claim_id}):
        return {"decision": "skipped", "reason": "claim has no labeled outcomes"}
    active = rulebook.active_version_for_claim(claim_id)
    run = db.get_collection("agent_runs").find_one(
        {"claimId": claim_id, "status": "completed", "rulebookVersion": active}, sort=[("startedAt", -1)])
    if run:
        run_id = run["runId"]
    else:
        result = agent.run_agent(claim_id)
        if result.status != "completed":
            raise RuntimeError(f"review ended {result.status}: {result.error}")
        run_id = result.runId
    _emit(run_id, claim_id, "improvement_started", trigger="settlement", rulebookVersion=active)
    scorer.score_run(run_id)
    return improve_rulebook(claim_id, run_id, **kwargs)
