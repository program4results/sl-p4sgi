#!/usr/bin/env python3
"""Create a provisioning job from the local gam-out sample (no live GAM calls).

Usage:
  python3 scripts/ingest_gam_sample.py
  python3 scripts/ingest_gam_sample.py --json
  python3 scripts/ingest_gam_sample.py --file /path/to/extract.csv
  python3 scripts/ingest_gam_sample.py --base-url http://127.0.0.1:8088
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

STACK = Path(__file__).resolve().parents[1]
DEFAULT_DATA = (STACK / "../../docker-data/sl.p4sgi").resolve()
DEFAULT_CSV = DEFAULT_DATA / "gam-out" / "sample_ous.csv"
DEFAULT_JSON = DEFAULT_DATA / "gam-out" / "sample_ous.json"


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--base-url", default="http://127.0.0.1:8088")
    p.add_argument("--json", action="store_true", help="Use sample_ous.json instead of CSV")
    p.add_argument("--file", type=Path, help="Explicit sample file (.csv or .json)")
    p.add_argument("--approve", action="store_true", help="Also approve the created job")
    args = p.parse_args()

    path = args.file
    if path is None:
        path = DEFAULT_JSON if args.json else DEFAULT_CSV
    if not path.is_file():
        print(f"Sample not found: {path}", file=sys.stderr)
        return 1

    boundary = "----slp4sgiBoundary7d8f"
    data = path.read_bytes()
    filename = path.name
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: application/octet-stream\r\n\r\n"
    ).encode() + data + f"\r\n--{boundary}--\r\n".encode()

    url = args.base_url.rstrip("/") + "/api/v1/provisioning/jobs"
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode())
    except urllib.error.URLError as exc:
        print(f"Request failed: {exc}", file=sys.stderr)
        print("Is the stack up?  docker compose up -d --build", file=sys.stderr)
        return 2

    job = payload.get("job") or payload
    job_id = job.get("id")
    print(json.dumps(payload, indent=2))
    print(f"\nCreated job id={job_id} status={job.get('status')} emis={job.get('emis')}")

    if args.approve and job_id:
        approve_url = f"{args.base_url.rstrip('/')}/api/v1/provisioning/jobs/{job_id}/approve"
        req2 = urllib.request.Request(approve_url, method="POST", data=b"")
        with urllib.request.urlopen(req2, timeout=30) as resp:
            result = json.loads(resp.read().decode())
        print(json.dumps(result, indent=2))
        print(f"\nWrote {result.get('config_path')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
