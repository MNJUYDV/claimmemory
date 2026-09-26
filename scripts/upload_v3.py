"""Upload a claim's held estimate_v3 through the API (the on-stage fallback for the live demo).

Usage: python scripts/upload_v3.py --claim HO-48213 [--api http://localhost:8000]
The server must be running (uvicorn main:app). Uploading triggers the review by itself.
"""
import argparse
import os
import sys

import httpx

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--claim", required=True, help="claim id, e.g. HO-48213")
    parser.add_argument("--api", default="http://localhost:8000")
    args = parser.parse_args()
    path = os.path.join(os.environ.get("CLAIMMEMORY_DATA_DIR") or os.path.join(ROOT, "data"),
                        args.claim, "estimate_v3.txt")
    if not os.path.exists(path):
        sys.exit(f"ERROR: {path} not found")
    with open(path, "rb") as f:
        try:
            r = httpx.post(f"{args.api}/api/claims/{args.claim}/documents",
                           files={"file": ("estimate_v3.txt", f, "text/plain")}, timeout=120)
        except httpx.ConnectError:
            sys.exit(f"ERROR: cannot reach the API at {args.api}; start it with: uvicorn main:app")
    if r.status_code != 200:
        sys.exit(f"ERROR {r.status_code}: {r.text}")
    body = r.json()
    print("Already uploaded; nothing to do." if body.get("duplicate") else
          f"Uploaded {body['filename']} (receivedAt {body['receivedAt']}). The review starts on its own.")


if __name__ == "__main__":
    main()
