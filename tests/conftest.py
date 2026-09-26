import os

# Must be set before config is imported anywhere (overrides .env).
os.environ["DB_NAME"] = "claimmemory_test"

import pytest
from dotenv import load_dotenv

# Real values (from .env) win; placeholders only let offline tests import config.
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
for _k in ("MONGODB_URI", "VOYAGE_API_KEY", "ANTHROPIC_API_KEY"):
    if not os.environ.get(_k, "").strip():  # unset, or blank in .env
        os.environ[_k] = "placeholder"

PROD_DB = "claimmemory"
PLACEHOLDER = "placeholder"


def _unusable(var):
    v = os.environ.get(var, "").strip()
    return not v or v == PLACEHOLDER


def _needed_var(item):
    name = item.name.lower()
    if "voyage" in name:
        return "VOYAGE_API_KEY"
    if "claude" in name:
        return "ANTHROPIC_API_KEY"
    return "MONGODB_URI"


def pytest_collection_modifyitems(config, items):
    for item in items:
        if item.get_closest_marker("integration"):
            var = _needed_var(item)
            if _unusable(var):
                item.add_marker(pytest.mark.skip(
                    reason=f"{var} is missing or a placeholder; set it in .env"))


def _snapshot(client):
    d = client[PROD_DB]
    return {n: d[n].count_documents({}) for n in sorted(d.list_collection_names())}


@pytest.fixture(scope="session", autouse=True)
def prod_db_untouched(request):
    """T0.13: the real claimmemory database must be unchanged after the suite."""
    if _unusable("MONGODB_URI") or not any(
            i.get_closest_marker("integration") for i in request.session.items):
        yield
        return
    import db
    client = db.get_client()
    before = _snapshot(client)
    yield
    assert _snapshot(client) == before, f"{PROD_DB} database was modified by tests"


@pytest.fixture(autouse=True)
def clean_test_data(request):
    yield
    if not request.node.get_closest_marker("integration"):
        return
    import config
    import db
    assert config.DB_NAME == "claimmemory_test"
    for name in db.COLLECTION_NAMES:
        db.get_collection(name).delete_many({})


@pytest.fixture(scope="session")
def vector_indexes():
    """Create indexes on the test DB once and wait for READY."""
    import subprocess, sys
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    r = subprocess.run(
        [sys.executable, "scripts/create_indexes.py", "--db", "claimmemory_test"],
        cwd=root, capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stdout + r.stderr
