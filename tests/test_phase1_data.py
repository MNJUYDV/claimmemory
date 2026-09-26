import json
import subprocess
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

import dataparse
import db
from constants import parse_dt

ROOT = Path(__file__).resolve().parent.parent
DATA = dataparse.DATA_DIR
MARIA, PARK = DATA / "HO-48213", DATA / "PK-20719"
CLAIM_DIRS = [MARIA, PARK]

MANIFEST = json.loads((DATA / "manifest.json").read_text())
LABELS = json.loads((DATA / "labels.json").read_text())
RULES = json.loads((DATA / "seed_rules.json").read_text())

# Distinctive phrases of code-upgrade work that must not appear in any estimate.
CODE_KEYWORDS = ["service panel", "afci", "arc-fault", "type x", "interconnected", "makeup-air", "gfci"]


def est(claim_dir, v):
    return dataparse.parse_estimate(claim_dir / f"estimate_{v}.txt")


def test_manifest_files_exist_and_dates_aware():  # T1.1
    assert MANIFEST
    for e in MANIFEST:
        assert (DATA / e["claimId"] / e["filename"]).is_file(), e
        assert parse_dt(e["receivedAt"]).utcoffset() is not None
    on_disk = {(d.name, f.name) for d in CLAIM_DIRS for f in d.iterdir()}
    assert on_disk == {(e["claimId"], e["filename"]) for e in MANIFEST}, "manifest must list every file"
    holds = [(e["claimId"], e["filename"]) for e in MANIFEST if e["hold"]]
    assert holds == [("HO-48213", "estimate_v3.txt")]


def test_estimate_tables_parse():  # T1.2
    for d in CLAIM_DIRS:
        for f in sorted(d.glob("estimate_v*.txt")):
            e = dataparse.parse_estimate(f)
            assert e.lines, f
            for l in e.lines:  # parse_estimate rejects rows without 5 columns / non-numeric amounts
                assert l.material >= 0 and l.labor >= 0 and 0 <= l.labor_depreciation <= l.labor
            assert dataparse.parse_money(e.header["Total"]) == e.total, f


def test_maria_estimate_totals():
    assert [est(MARIA, v).total for v in ("v1", "v2", "v3")] == [Decimal(54000), Decimal(61200), Decimal(61200)]
    assert est(MARIA, "v1").labor_depreciation_total == 0


@pytest.mark.parametrize("v", ["v2", "v3"])
def test_maria_labor_depreciation(v):  # T1.3
    e = est(MARIA, v)
    assert len(e.lines) == 52
    assert len(e.labor_depreciation_lines) == 41
    assert e.labor_depreciation_total == Decimal(11300)


def test_maria_code_upgrades_missing_from_estimates():  # T1.4
    items = dataparse.parse_code_upgrades(MARIA / "contractor_bid.txt")
    assert sum(i.amount for i in items) == Decimal(18700)
    for v in ("v2", "v3"):
        text = (MARIA / f"estimate_{v}.txt").read_text().lower()
        for i in items:
            assert i.description.lower() not in text
        for k in CODE_KEYWORDS:
            assert k not in text, (v, k)


def test_maria_v3_differs_from_v2_only_in_date_and_descriptions():
    v2, v3 = est(MARIA, "v2"), est(MARIA, "v3")
    assert v3.header["Date written"] == "2026-09-25" and v3.total == v2.total
    assert [(l.line_id, l.material, l.labor, l.labor_depreciation) for l in v2.lines] == \
           [(l.line_id, l.material, l.labor, l.labor_depreciation) for l in v3.lines]
    assert 1 <= sum(a.description != b.description for a, b in zip(v2.lines, v3.lines)) <= 2


def test_endorsement_vs_estimate_v2():  # T1.5
    eff = dataparse.parse_endorsement_effective_from(MARIA / "endorsement_code_upgrade.txt")
    assert eff == date(2026, 4, 1)  # policy renewal date
    rec = {e["filename"]: parse_dt(e["receivedAt"]) for e in MANIFEST if e["claimId"] == "HO-48213"}
    assert rec["endorsement_code_upgrade.txt"].date() == date(2026, 6, 27)
    v2 = est(MARIA, "v2")
    assert v2.header["Date written"] == "2026-07-18"
    assert rec["endorsement_code_upgrade.txt"].date() < date.fromisoformat(v2.header["Date written"])
    assert "endorsement_code_upgrade.txt" not in v2.relied_on
    assert "endorsement_code_upgrade.txt" not in est(MARIA, "v3").relied_on


def test_endorsements_effective_before_loss():
    for c in json.loads((DATA / "claims.json").read_text()):
        loss = parse_dt(c["lossDate"]).date()
        path = DATA / c["claimId"] / "endorsement_code_upgrade.txt"
        assert dataparse.parse_endorsement_effective_from(path) < loss, c["claimId"]
        assert "on or after" not in path.read_text()


def test_maria_ale():  # T1.6
    a = dataparse.parse_ale(MARIA)
    assert (a.promised_months, a.cutoff_months, a.monthly_rate) == (12, 6, Decimal(1400))
    assert a.unpaid == Decimal(8400)


def test_maria_payments_total():  # T1.7
    p = MARIA / "payments.json"
    assert dataparse.sum_payments(p, "dwelling") == Decimal(61200)
    assert dataparse.sum_payments(p, "ale") == Decimal(8400)
    assert {x["category"] for x in dataparse.load_payments(p)} == {"dwelling", "ale"}


def latest_estimate_total(claim_dir):
    latest = sorted(claim_dir.glob("estimate_v*.txt"), key=lambda f: int(f.stem.split("_v")[1]))[-1]
    return dataparse.parse_estimate(latest).total


@pytest.mark.parametrize("claim_dir", CLAIM_DIRS, ids=lambda d: d.name)
def test_dwelling_payments_equal_latest_estimate(claim_dir):  # no unplanted gap
    assert dataparse.sum_payments(claim_dir / "payments.json", "dwelling") == latest_estimate_total(claim_dir)


def test_park_ale_payments():
    assert dataparse.sum_payments(PARK / "payments.json", "ale") == Decimal(5600)


def test_maria_labels():  # T1.8
    f = LABELS["HO-48213"]["findings"]
    assert sorted(x["type"] for x in f) == ["labor_depreciation", "missing_coverage", "unpaid_ale"]
    assert sum(x["amount"] for x in f) == 38400
    assert {x["type"]: x["amount"] for x in f} == {
        "labor_depreciation": 11300, "missing_coverage": 18700, "unpaid_ale": 8400}


def test_park_labels_recomputed_from_files():  # T1.9
    e = est(PARK, "v2")
    assert len(e.labor_depreciation_lines) == 18
    assert est(PARK, "v1").labor_depreciation_total == 0
    got = {
        "labor_depreciation": e.labor_depreciation_total,
        "missing_coverage": sum(i.amount for i in dataparse.parse_code_upgrades(PARK / "contractor_bid.txt")),
        "unpaid_ale": dataparse.parse_ale(PARK).unpaid,
    }
    assert got == {"labor_depreciation": 4200, "missing_coverage": 6900, "unpaid_ale": 2800}
    labelled = {x["type"]: x["amount"] for x in LABELS["PK-20719"]["findings"]}
    assert labelled == got
    assert "endorsement_code_upgrade.txt" not in e.relied_on


def test_rulebook_v1():  # T1.10
    types = {r["type"] for r in RULES["rules"]}
    assert RULES["version"] == 1
    assert types == {"missing_coverage", "unpaid_ale"}
    assert "labor_depreciation" not in types
    for r in RULES["rules"]:
        assert set(r) >= {"id", "insurer", "type", "instruction", "computeRule"}
        assert r["insurer"] == "Harborline Mutual"


def test_label_evidence_quotes_verbatim():  # T1.12
    n = 0
    for claim_id, entry in LABELS.items():
        for finding in entry["findings"]:
            assert finding["evidence"]
            for ev in finding["evidence"]:
                assert ev["quote"] in (DATA / claim_id / ev["filename"]).read_text(), ev
                n += 1
    assert n > 0


def test_labels_not_reachable_by_agent():
    assert not any((d / "labels.json").exists() for d in CLAIM_DIRS)
    assert all(e["filename"] != "labels.json" for e in MANIFEST)
    for name in ("labels", "scores"):
        with pytest.raises(PermissionError):
            db.get_agent_collection(name)


@pytest.mark.integration
def test_seed_idempotent_and_dates_are_datetimes():  # T1.11
    def run():
        r = subprocess.run([sys.executable, "scripts/seed.py", "--db", "claimmemory_test"],
                           cwd=ROOT, capture_output=True, text=True)
        assert r.returncode == 0, r.stdout + r.stderr
        return {n: db.get_collection(n).count_documents({}) for n in db.COLLECTION_NAMES}

    first, second = run(), run()
    assert first == second
    assert first["claims"] == 2 and first["payments"] == 6 and first["labels"] == 6 and first["rules"] == 2
    assert first["documents"] == 0
    for coll, field in (("claims", "lossDate"), ("payments", "paidAt"), ("rules", "createdAt")):
        for doc in db.get_collection(coll).find():
            assert isinstance(doc[field], datetime) and doc[field].tzinfo is not None, (coll, field)
