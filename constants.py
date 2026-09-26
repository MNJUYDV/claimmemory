"""Shared constants."""
from datetime import datetime, timezone

UTC = timezone.utc

# Sentinel for open-ended validity: policy_clauses.effectiveTo, facts.validTo.
# (Vector-index filters can't match missing/null, so open-ended must be a real date.)
OPEN_ENDED = datetime(9999, 12, 31, tzinfo=UTC)
