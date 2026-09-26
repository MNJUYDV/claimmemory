"""Generate the synthetic Phase 1 data under data/. Deterministic: re-running rewrites identical files.

Every planted-error amount is recomputed from the written files with dataparse and
asserted against the spec, so the labels can never drift from the documents.

Usage: python scripts/generate_data.py
All people, insurers and addresses are fictional.
"""
import json
import os
import random
import sys
from datetime import date
from decimal import Decimal

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import dataparse  # noqa: E402

DATA = dataparse.DATA_DIR

# Words that identify code-upgrade work; they must not appear in any estimate line.
CODE_KEYWORDS = ("panel", "afci", "type x", "interconnected", "makeup", "alarm", "gfci", "fire-rated")

DESCRIPTIONS = [
    "Demolition and debris removal", "Haul-away and dumpster rental", "Contents manipulation and pack-out",
    "Water extraction from suppression", "Structural drying equipment, per day",
    "Soot and smoke residue cleaning, walls", "Soot and smoke residue cleaning, ceiling",
    "Seal and prime, smoke-damaged surfaces", "Drywall 1/2 inch, hang and finish, walls",
    "Drywall 1/2 inch, hang and finish, ceiling", "Insulation, batt, exterior wall",
    "Framing repair, wall studs", "Framing repair, ceiling joists", "Subfloor replacement, plywood",
    "Vinyl plank flooring", "Flooring underlayment", "Baseboard trim, 5 inch", "Crown molding, paint grade",
    "Interior door, pre-hung, replace", "Door casing and hardware", "Window, vinyl double-hung, replace",
    "Window trim and sill", "Base cabinets, replace", "Wall cabinets, replace", "Cabinet hardware",
    "Countertop, quartz, fabricate and install", "Backsplash tile, ceramic", "Sink, stainless, reset",
    "Faucet, replace", "Range hood, replace", "Dishwasher, reset and reconnect", "Plumbing supply lines, replace",
    "Drain line repair", "Outlets and switches, replace", "Light fixtures, replace", "Recessed lighting, replace",
    "Wiring repair, damaged runs", "Paint, walls, two coats", "Paint, ceiling, two coats",
    "Paint, trim and doors", "Texture, ceiling, match existing", "HVAC duct cleaning",
    "HVAC register and grille replacement", "Attic insulation, blown-in, repair", "Roof decking repair, section",
    "Roofing, asphalt shingle, repair", "Flashing and vent boots", "Gutter and downspout repair",
    "Exterior siding repair, vinyl", "Exterior paint, touch-up", "Final cleaning, post-construction",
    "Project supervision and general conditions",
]
assert len(DESCRIPTIONS) == 52
for _d in DESCRIPTIONS:
    assert "|" not in _d and not any(k in _d.lower() for k in CODE_KEYWORDS), _d

CLAIMS = {
    "HO-48213": dict(
        name="Maria Alvarez", role="live", status="open", loss="2026-05-30", tz="-05:00",
        address="77 Copperleaf Lane, Alderbrook, ST 00000", policy_no="HM-HO-0071843",
        adjuster="Priya Nandakumar", contractor="Ridgeway Restoration LLC", seed=48213,
        endorsement_effective="2026-04-01", policy_effective="2025-04-01",
        est=dict(  # version: (date, total $, n lines, n labor-dep lines, labor-dep $, lines w/o labor)
            v1=("2026-06-10", 54000, 44, 0, 0, 5),
            v2=("2026-07-18", 61200, 52, 41, 11300, 6),
        ),
        v3_date="2026-09-25",
        bid_date="2026-06-20",
        code_items=[
            ("Electrical service panel upgrade to current code", 6400),
            ("Arc-fault (AFCI) breaker retrofit, kitchen and adjoining circuits", 3900),
            ("Type X fire-rated gypsum board, kitchen walls and ceiling", 4300),
            ("Interconnected smoke and CO alarm system", 1150),
            ("Kitchen makeup-air ventilation for range hood", 2950),
        ],
        ale=dict(promised=12, cutoff=6, rate=1400, email_date="2026-06-03", notice_date="2026-08-03"),
        payments=[("2026-06-12T10:00:00", 30000, "dwelling", "Initial dwelling advance"),
                  ("2026-07-25T10:00:00", 31200, "dwelling", "Dwelling payment on estimate v2"),
                  ("2026-08-01T10:00:00", 8400, "ale", "ALE months 1-6 (prepaid)")],
        received={
            "policy.txt": "2026-06-01T09:15:00", "adjuster_email_1.txt": "2026-06-03T14:20:00",
            "estimate_v1.txt": "2026-06-10T17:30:00", "contractor_bid.txt": "2026-06-22T10:05:00",
            "endorsement_code_upgrade.txt": "2026-06-27T11:00:00", "estimate_v2.txt": "2026-07-18T15:45:00",
            "adjuster_email_2.txt": "2026-07-18T16:10:00", "ale_notice.txt": "2026-08-03T09:00:00",
            "payments.json": "2026-08-03T10:00:00", "estimate_v3.txt": "2026-09-25T13:30:00",
        },
        hold="estimate_v3.txt",
    ),
    "PK-20719": dict(
        name="Daniel and Grace Park", role="past", status="closed", loss="2026-02-14", tz="-06:00",
        address="1184 Marlow Terrace, Alderbrook, ST 00000", policy_no="HM-HO-0059172",
        adjuster="Tomas Reinholt", contractor="Cobalt Creek Builders", seed=20719,
        endorsement_effective="2025-12-01", policy_effective="2024-12-01",
        est=dict(
            v1=("2026-03-02", 29800, 26, 0, 0, 4),
            v2=("2026-04-09", 34500, 30, 18, 4200, 5),
        ),
        v3_date=None,
        bid_date="2026-03-10",
        code_items=[
            ("Electrical service panel upgrade to current code", 3200),
            ("Type X fire-rated gypsum board, garage-house separation wall", 2100),
            ("Interconnected smoke and CO alarm system", 1600),
        ],
        ale=dict(promised=6, cutoff=4, rate=1400, email_date="2026-02-18", notice_date="2026-05-18"),
        payments=[("2026-03-05T10:00:00", 15000, "dwelling", "Initial dwelling advance"),
                  ("2026-04-15T10:00:00", 19500, "dwelling", "Dwelling payment on estimate v2"),
                  ("2026-05-15T10:00:00", 5600, "ale", "ALE months 1-4 (prepaid)")],
        received={
            "policy.txt": "2026-02-16T10:00:00", "adjuster_email_1.txt": "2026-02-18T13:00:00",
            "estimate_v1.txt": "2026-03-02T16:00:00", "contractor_bid.txt": "2026-03-10T11:30:00",
            "endorsement_code_upgrade.txt": "2026-03-12T09:45:00", "estimate_v2.txt": "2026-04-09T15:00:00",
            "adjuster_email_2.txt": "2026-04-09T15:20:00", "ale_notice.txt": "2026-05-18T09:00:00",
            "payments.json": "2026-05-18T10:00:00",
        },
        hold=None,
    ),
}


# ---------- money helpers (integer cents) ----------

def money(cents: int) -> str:
    return f"{cents // 100}.{cents % 100:02d}"


def usd(cents: int) -> str:
    return f"${cents // 100:,}.{cents % 100:02d}"


def split(total: int, weights: list) -> list:
    """Split total across weights so the parts are proportional and sum exactly to total."""
    s = sum(weights)
    parts = [total * w // s for w in weights]
    for i in range(total - sum(parts)):
        parts[i % len(parts)] += 1
    assert sum(parts) == total
    return parts


def build_lines(rng, n, total, n_dep, dep_total, n_no_labor, labor_pct=55):
    """Return rows [(description, material_c, labor_c, dep_c)] with exact totals (all in cents)."""
    idx = list(range(n))
    rng.shuffle(idx)
    no_labor = set(idx[:n_no_labor])
    labor_idx = [i for i in range(n) if i not in no_labor]
    dep_idx = sorted(rng.sample(labor_idx, n_dep))

    labor_total = total * labor_pct // 100
    labor = dict.fromkeys(range(n), 0)
    labor.update(zip(labor_idx, split(labor_total, [rng.randint(60, 200) for _ in labor_idx])))
    material = split(total - labor_total, [rng.randint(30, 200) for _ in range(n)])
    dep = dict.fromkeys(range(n), 0)
    if n_dep:
        dep.update(zip(dep_idx, split(dep_total, [labor[i] for i in dep_idx])))
        assert all(0 < dep[i] <= labor[i] for i in dep_idx)
    return [(DESCRIPTIONS[i], material[i], labor[i], dep[i]) for i in range(n)]


def estimate_text(claim_id, c, version, date, relied, method, rows):
    total = sum(m + l for _, m, l, _ in rows)
    out = [
        "ESTIMATE",
        f"Claim: {claim_id}",
        f"Insured: {c['name']}",
        f"Version: {version}",
        f"Date written: {date}",
        f"Relied on: {relied}",
        f"Depreciation method: actual cash value; depreciation applied to {method}",
        f"Total: {usd(total)}",
        "",
        " | ".join(dataparse.ESTIMATE_COLUMNS),
    ]
    for i, (desc, m, l, d) in enumerate(rows, 1):
        out.append(f"L{i:03d} | {desc} | {money(m)} | {money(l)} | {money(d)}")
    return "\n".join(out) + "\n"


# ---------- per-file text ----------

def policy_text(claim_id, c):
    return f"""HARBORLINE MUTUAL - HOMEOWNERS POLICY
Policy number: {c['policy_no']}
Insured: {c['name']}
Property: {c['address']}
Claim: {claim_id}
Effective from: {c['policy_effective']}

SECTION 1. DEFINITIONS
1.1 "Covered loss" means direct physical loss by fire to the insured dwelling.
1.2 "Actual cash value" means replacement cost less applicable depreciation.

SECTION 4. LOSS SETTLEMENT
4.1 Dwelling losses are settled at actual cash value until repairs are completed.
4.2 Labor is not subject to depreciation. Depreciation, when applied, is limited to the cost of materials.
4.3 Estimates must list every document relied on in their preparation.

SECTION 6. ADDITIONAL LIVING EXPENSE
6.1 Additional living expense (ALE) is payable for up to 12 months while the residence is uninhabitable.
6.2 ALE is paid at the monthly rate agreed in writing with the insured.
"""


def endorsement_text(c):
    return f"""ORDINANCE OR LAW COVERAGE ENDORSEMENT
Policy number: {c['policy_no']}
Insured: {c['name']}
Effective from: {c['endorsement_effective']} (policy renewal date)
Limit of liability: $25,000

This endorsement covers the increased cost to repair or rebuild covered property that results from
enforcement of a building, zoning or fire code. It applies to the loss. All other terms of the policy
remain unchanged.
"""


def bid_text(claim_id, c, base_cents):
    items = [(f"CU{i}", d, a * 100) for i, (d, a) in enumerate(c["code_items"], 1)]
    sub = sum(a for _, _, a in items)
    out = [
        "CONTRACTOR BID",
        f"Contractor: {c['contractor']}",
        f"Claim: {claim_id}",
        f"Date: {c['bid_date']}",
        "",
        "BASE REPAIR SCOPE",
        f"Base scope subtotal: {usd(base_cents)} (matches estimate v1 line items)",
        "",
        "Code upgrade items",
        " | ".join(dataparse.CODE_UPGRADE_COLUMNS),
    ]
    out += [f"{i} | {d} | {money(a)}" for i, d, a in items]
    out += ["", f"Code upgrade subtotal: {usd(sub)}", "", f"Total bid: {usd(base_cents + sub)}"]
    return "\n".join(out) + "\n"


def email1_text(claim_id, c):
    a = c["ale"]
    return f"""From: {c['adjuster']}, Claims Adjuster, Harborline Mutual
To: {c['name']}
Date: {a['email_date']}
Subject: Claim {claim_id} - additional living expenses

Dear {c['name']},

I am sorry about the fire at your home. While the residence is being repaired we will cover your
additional living expenses (ALE) through month {a['promised']} at ${a['rate']:,} per month.
Please keep receipts for any costs above your normal expenses.

{c['adjuster']}
Harborline Mutual Claims
"""


def email2_text(claim_id, c):
    return f"""From: {c['adjuster']}, Claims Adjuster, Harborline Mutual
To: {c['name']}
Date: {c['est']['v2'][0]}
Subject: Claim {claim_id} - updated estimate

Attached is the updated repair estimate (version v2). Please review it with your contractor and
contact me with any questions.

{c['adjuster']}
Harborline Mutual Claims
"""


def notice_text(claim_id, c):
    a = c["ale"]
    return f"""HARBORLINE MUTUAL - NOTICE OF ALE STATUS
Claim: {claim_id}
Insured: {c['name']}
Date: {a['notice_date']}

Please be advised that ALE payments end after month {a['cutoff']}. The final ALE payment has been
issued. No further ALE payments will be made on this claim.
"""


def payments_json(claim_id, c):
    rows = [
        {"paymentId": f"{claim_id}-P{i}", "claimId": claim_id, "paidAt": f"{ts}{c['tz']}",
         "amount": amt, "category": kind, "payee": c["name"], "memo": memo}
        for i, (ts, amt, kind, memo) in enumerate(c["payments"], 1)
    ]
    return json.dumps(rows, indent=2) + "\n"


# ---------- driver ----------

def write(path, text):
    with open(path, "w") as f:
        f.write(text)


def generate_claim(claim_id, c):
    d = DATA / claim_id
    d.mkdir(parents=True, exist_ok=True)
    rng = random.Random(c["seed"])

    v1d, v1t, v1n, _, _, v1nl = c["est"]["v1"]
    v2d, v2t, v2n, v2dn, v2dt, v2nl = c["est"]["v2"]
    v1 = build_lines(rng, v1n, v1t * 100, 0, 0, v1nl)
    v2 = build_lines(rng, v2n, v2t * 100, v2dn, v2dt * 100, v2nl)

    write(d / "policy.txt", policy_text(claim_id, c))
    write(d / "endorsement_code_upgrade.txt", endorsement_text(c))
    write(d / "estimate_v1.txt", estimate_text(claim_id, c, "v1", v1d, "policy.txt", "material only", v1))
    both = "material and labor"
    # v2/v3 rely on the bid but, crucially, not on the endorsement.
    write(d / "estimate_v2.txt", estimate_text(
        claim_id, c, "v2", v2d, "policy.txt; estimate_v1.txt; contractor_bid.txt", both, v2))
    if c["v3_date"]:
        v3 = list(v2)  # identical amounts to v2; only the date and two descriptions change
        for k in (0, 1):
            desc, m, l, dep = v3[k]
            v3[k] = (f"{desc}, per re-inspection", m, l, dep)
        write(d / "estimate_v3.txt", estimate_text(
            claim_id, c, "v3", c["v3_date"], "policy.txt; estimate_v2.txt; contractor_bid.txt", both, v3))
    write(d / "contractor_bid.txt", bid_text(claim_id, c, v1t * 100))
    write(d / "adjuster_email_1.txt", email1_text(claim_id, c))
    write(d / "adjuster_email_2.txt", email2_text(claim_id, c))
    write(d / "ale_notice.txt", notice_text(claim_id, c))
    write(d / "payments.json", payments_json(claim_id, c))


def recompute(claim_id, c):
    """Recompute the three planted amounts from the files just written, and check the spec."""
    d = DATA / claim_id
    versions = ["v2"] + (["v3"] if c["v3_date"] else [])
    want_dep = Decimal(c["est"]["v2"][4])
    want_n = c["est"]["v2"][3]
    for v in versions:
        est = dataparse.parse_estimate(d / f"estimate_{v}.txt")
        assert len(est.labor_depreciation_lines) == want_n, (claim_id, v)
        assert est.labor_depreciation_total == want_dep, (claim_id, v)
        assert "endorsement_code_upgrade.txt" not in est.relied_on
    code = sum(i.amount for i in dataparse.parse_code_upgrades(d / "contractor_bid.txt"))
    ale = dataparse.parse_ale(d)
    latest = dataparse.parse_estimate(d / f"estimate_{versions[-1]}.txt").total
    dwelling = dataparse.sum_payments(d / "payments.json", "dwelling")
    assert dwelling == latest, (claim_id, dwelling, latest)  # no unplanted gap
    assert dataparse.sum_payments(d / "payments.json", "ale") == ale.cutoff_months * ale.monthly_rate
    eff = dataparse.parse_endorsement_effective_from(d / "endorsement_code_upgrade.txt")
    assert eff.isoformat() == c["endorsement_effective"] and eff < date.fromisoformat(c["loss"])
    return want_dep, code, ale.unpaid


def quote_in(claim_id, filename, quote):
    assert quote in (DATA / claim_id / filename).read_text(), (claim_id, filename, quote)
    return {"filename": filename, "quote": quote}


def labels_for(claim_id, c, amounts):
    dep, code, ale = amounts
    a = c["ale"]
    method = "Depreciation method: actual cash value; depreciation applied to material and labor"
    est_files = ["estimate_v2.txt"] + (["estimate_v3.txt"] if c["v3_date"] else [])
    relied = {"estimate_v2.txt": "Relied on: policy.txt; estimate_v1.txt; contractor_bid.txt",
              "estimate_v3.txt": "Relied on: policy.txt; estimate_v2.txt; contractor_bid.txt"}
    code_total = int(code)
    return [
        {"type": "labor_depreciation", "amount": int(dep), "evidence": [
            quote_in(claim_id, "policy.txt", "Labor is not subject to depreciation."),
            *(quote_in(claim_id, f, method) for f in est_files)]},
        {"type": "missing_coverage", "amount": code_total, "evidence": [
            quote_in(claim_id, "endorsement_code_upgrade.txt", "Limit of liability: $25,000"),
            quote_in(claim_id, "contractor_bid.txt", f"Code upgrade subtotal: ${code_total:,}.00"),
            *(quote_in(claim_id, f, relied[f]) for f in est_files)]},
        {"type": "unpaid_ale", "amount": int(ale), "evidence": [
            quote_in(claim_id, "adjuster_email_1.txt",
                     f"through month {a['promised']} at ${a['rate']:,} per month"),
            quote_in(claim_id, "ale_notice.txt", f"ALE payments end after month {a['cutoff']}")]},
    ]


def main():
    DATA.mkdir(exist_ok=True)
    manifest, labels, claims = [], {}, []
    for claim_id, c in CLAIMS.items():
        generate_claim(claim_id, c)
        amounts = recompute(claim_id, c)
        labels[claim_id] = {"findings": labels_for(claim_id, c, amounts)}
        for filename, ts in c["received"].items():
            assert (DATA / claim_id / filename).exists(), filename
            manifest.append({"claimId": claim_id, "filename": filename,
                             "receivedAt": f"{ts}{c['tz']}", "hold": filename == c["hold"]})
        claims.append({
            "claimId": claim_id, "insurer": "Harborline Mutual", "insuredName": c["name"],
            "propertyAddress": c["address"], "policyNumber": c["policy_no"],
            "lossDate": f"{c['loss']}T00:00:00{c['tz']}", "lossType": "fire",
            "status": c["status"], "role": c["role"]})

    write(DATA / "manifest.json", json.dumps(manifest, indent=2) + "\n")
    write(DATA / "labels.json", json.dumps(labels, indent=2) + "\n")
    write(DATA / "claims.json", json.dumps(claims, indent=2) + "\n")
    write(DATA / "seed_rules.json", json.dumps(seed_rules(), indent=2) + "\n")
    for cid, l in labels.items():
        print(cid, {f["type"]: f["amount"] for f in l["findings"]})


def seed_rules():
    created = "2026-01-01T00:00:00+00:00"
    return {
        "insurer": "Harborline Mutual", "version": 1,
        "rules": [
            {"id": "HM-MC-001", "insurer": "Harborline Mutual", "type": "missing_coverage", "version": 1,
             "createdAt": created, "active": True, "computeRule": "missing_coverage",
             "instruction": "If an ordinance-or-law endorsement was in effect and received before an estimate "
                            "was written but the estimate's 'Relied on' list omits it, sum the contractor's code "
                            "upgrade items that appear in no estimate line. Report the sum, capped at the "
                            "endorsement limit."},
            {"id": "HM-ALE-001", "insurer": "Harborline Mutual", "type": "unpaid_ale", "version": 1,
             "createdAt": created, "active": True, "computeRule": "unpaid_ale",
             "instruction": "Compare the adjuster's written ALE promise (months and monthly rate) with the "
                            "cutoff stated in the ALE notice. Unpaid ALE is (promised months - cutoff month) "
                            "x monthly rate."},
        ],
    }


if __name__ == "__main__":
    main()
