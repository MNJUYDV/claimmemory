import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def test_missing_anthropic_key_exits(tmp_path):  # T0.1
    # Run a copy of config.py somewhere with no .env, so dotenv can't fill the gap.
    shutil.copy(ROOT / "config.py", tmp_path / "config.py")
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    env.update(MONGODB_URI="mongodb://x", VOYAGE_API_KEY="x")
    r = subprocess.run([sys.executable, "-c", "import config"],
                       env=env, cwd=tmp_path, capture_output=True, text=True)
    assert r.returncode != 0
    assert "ANTHROPIC_API_KEY" in r.stderr


def test_env_example_has_names_only():  # T0.2
    for line in (ROOT / ".env.example").read_text().splitlines():
        if line.strip():
            name, _, value = line.partition("=")
            assert name and value == ""


def test_env_is_gitignored():  # T0.2
    r = subprocess.run(["git", "check-ignore", ".env"], cwd=ROOT, capture_output=True, text=True)
    assert r.returncode == 0


def test_no_secrets_in_tracked_files():  # T0.3
    patterns = [
        r"sk-ant-[A-Za-z0-9_-]{10,}",
        r"pa-[A-Za-z0-9_-]{20,}",
        r"mongodb(\+srv)?://[^\s/:@]+:[^\s/@]+@",
    ]
    files = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout.split()
    for f in files:
        if f == "tests/test_config.py":
            continue
        text = (ROOT / f).read_text(errors="ignore")
        for p in patterns:
            assert not re.search(p, text), f"possible secret in {f}"
