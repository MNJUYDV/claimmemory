"""Parsers for the synthetic claim files.

The single source of truth for recomputing every planted-error amount in code.
"""
import json
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "data"
ESTIMATE_COLUMNS = ("line_id", "description", "material", "labor", "labor_depreciation")
CODE_UPGRADE_COLUMNS = ("item_id", "description", "amount")


def parse_money(text: str) -> Decimal:
    try:
        return Decimal(text.replace("$", "").replace(",", "").strip())
    except InvalidOperation:
        raise ValueError(f"not a number: {text!r}") from None


def _table_rows(lines: list[str], header: tuple[str, ...]) -> list[list[str]]:
    """Rows of the pipe-delimited table whose header line matches, up to the next blank line."""
    want = " | ".join(header)
    start = next((i for i, l in enumerate(lines) if l.strip() == want), None)
    if start is None:
        raise ValueError(f"table header not found: {want}")
    rows = []
    for line in lines[start + 1:]:
        if not line.strip():
            break
        cells = [c.strip() for c in line.split("|")]
        if len(cells) != len(header):
            raise ValueError(f"expected {len(header)} columns, got {len(cells)}: {line!r}")
        rows.append(cells)
    return rows


@dataclass(frozen=True)
class LineItem:
    line_id: str
    description: str
    material: Decimal
    labor: Decimal
    labor_depreciation: Decimal


@dataclass(frozen=True)
class Estimate:
    header: dict
    lines: list

    @property
    def relied_on(self) -> list:
        return [n.strip() for n in self.header["Relied on"].split(";") if n.strip()]

    @property
    def total(self) -> Decimal:
        return sum((l.material + l.labor for l in self.lines), Decimal(0))

    @property
    def labor_depreciation_lines(self) -> list:
        return [l for l in self.lines if l.labor_depreciation > 0]

    @property
    def labor_depreciation_total(self) -> Decimal:
        return sum((l.labor_depreciation for l in self.lines), Decimal(0))


def parse_estimate(path) -> Estimate:
    lines = Path(path).read_text().splitlines()
    header = {}
    for line in lines:
        if line.strip() == " | ".join(ESTIMATE_COLUMNS):
            break
        key, sep, value = line.partition(": ")
        if sep:
            header[key.strip()] = value.strip()
    items = [
        LineItem(r[0], r[1], parse_money(r[2]), parse_money(r[3]), parse_money(r[4]))
        for r in _table_rows(lines, ESTIMATE_COLUMNS)
    ]
    return Estimate(header, items)


@dataclass(frozen=True)
class CodeUpgrade:
    item_id: str
    description: str
    amount: Decimal


def parse_code_upgrades(bid_path) -> list:
    lines = Path(bid_path).read_text().splitlines()
    at = next((i for i, l in enumerate(lines) if l.strip() == "Code upgrade items"), None)
    if at is None:
        raise ValueError("no 'Code upgrade items' section")
    return [CodeUpgrade(r[0], r[1], parse_money(r[2]))
            for r in _table_rows(lines[at + 1:], CODE_UPGRADE_COLUMNS)]


@dataclass(frozen=True)
class AleTerms:
    promised_months: int
    cutoff_months: int
    monthly_rate: Decimal

    @property
    def unpaid(self) -> Decimal:
        return (self.promised_months - self.cutoff_months) * self.monthly_rate


def parse_ale(claim_dir) -> AleTerms:
    claim_dir = Path(claim_dir)
    promise = None
    for email in sorted(claim_dir.glob("adjuster_email_*.txt")):
        promise = re.search(r"through month (\d+) at \$([\d,]+) per month", email.read_text())
        if promise:
            break
    if not promise:
        raise ValueError("no ALE promise found in adjuster emails")
    notice = re.search(r"ALE payments end after month (\d+)", (claim_dir / "ale_notice.txt").read_text())
    if not notice:
        raise ValueError("no ALE cutoff found in ale_notice.txt")
    return AleTerms(int(promise.group(1)), int(notice.group(1)), parse_money(promise.group(2)))


def load_payments(path) -> list:
    return json.loads(Path(path).read_text(), parse_float=Decimal)


def sum_payments(path, category=None) -> Decimal:
    """Total of payments.json, optionally only one category ('dwelling' or 'ale')."""
    return sum((Decimal(str(p["amount"])) for p in load_payments(path)
                if category is None or p["category"] == category), Decimal(0))


def parse_endorsement_effective_from(path) -> date:
    m = re.search(r"Effective from: (\d{4}-\d{2}-\d{2})", Path(path).read_text())
    if not m:
        raise ValueError("no 'Effective from' date in endorsement")
    return date.fromisoformat(m.group(1))
