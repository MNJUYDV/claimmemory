"""Shared constants."""
from datetime import datetime, timezone

UTC = timezone.utc

# Sentinel for open-ended validity: policy_clauses.effectiveTo, facts.validTo.
# (Vector-index filters can't match missing/null, so open-ended must be a real date.)
OPEN_ENDED = datetime(9999, 12, 31, tzinfo=UTC)


def parse_dt(value: str) -> datetime:
    """Parse an ISO-8601 string that carries a UTC offset. Naive strings are rejected."""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError(f"datetime must be timezone-aware: {value!r}")
    return dt
