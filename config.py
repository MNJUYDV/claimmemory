"""Environment configuration. Exits immediately if a required variable is missing."""
import os
import sys

from dotenv import load_dotenv

load_dotenv()

REQUIRED = ("MONGODB_URI", "VOYAGE_API_KEY", "ANTHROPIC_API_KEY")

_missing = [name for name in REQUIRED if not os.environ.get(name, "").strip()]
if _missing:
    sys.exit(f"ERROR: missing required environment variable(s): {', '.join(_missing)}")

MONGODB_URI = os.environ["MONGODB_URI"]
VOYAGE_API_KEY = os.environ["VOYAGE_API_KEY"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
DB_NAME = os.environ.get("DB_NAME", "").strip() or "claimmemory"
VOYAGE_MODEL = os.environ.get("VOYAGE_MODEL", "").strip() or "voyage-3-large"
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "").strip() or "claude-sonnet-5"
