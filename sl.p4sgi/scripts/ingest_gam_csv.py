#!/usr/bin/env python3
"""Ingest GAM CSV extracts from docker-data/sl.p4sgi/gam-out into provisioning_jobs.

Does **not** call live GAM / Workspace APIs. Drop CSV files produced by GAM
(e.g. `gam print users …`) into gam-out/, then run this script.

Expected columns (any subset; extras kept in payload):
  Required-ish for identity:
    primaryEmail | email | schoolEmail
    orgUnitPath  | ou | ou_path
    emis         | (or inferred from orgUnitPath school-NNNNN / email)

  Optional school-config fields (used on Approve):
    schoolName | name | name.fullName
    attendanceDocId | attendance_doc_id
    websitePackDocId | googleSiteId | publishExecUrl
    siteAttendanceUrl | driveFolderId | configEndpoint

GAM print users example:
  gam print users fields primaryEmail,name,orgUnitPath ou /sl/pilot \\
    > /home/george/drive_14tb/docker-data/sl.p4sgi/gam-out/users.csv

Then enrich with emis / attendanceDocId columns (or use sample_ous.csv which
already includes Test Primary fields), and:

  python3 scripts/ingest_gam_csv.py
  # UI → Provisioning jobs → Approve

Usage:
  python3 scripts/ingest_gam_csv.py
  python3 scripts/ingest_gam_csv.py --dir /path/to/gam-out
  python3 scripts/ingest_gam_csv.py --file users.csv
  python3 scripts/ingest_gam_csv.py --approve
  python3 scripts/ingest_gam_csv.py --base-url http://127.0.0.1:8088
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
DEFAULT_GAM_OUT = DEFAULT_DATA / "gam-out"

# Written if gam-out has no CSVs
SAMPLE_NAME = "sample_gam_users.csv"
SAMPLE_CSV = """primaryEmail,name.fullName,orgUnitPath,emis,schoolName,attendanceDocId,websitePackDocId,googleSiteId
sl-test@sl.p4sgi.com,Test Primary School,/sl/pilot/school-110101,110101,Test Primary School,1DKQmr6GNGnx1i-f19Tw1w1QGRfUmJr7-l3JlDWv_Fck,12Gx23QvwXUBGZLz9osggOOCuHFKHRSdtV41SVQ0k9oU,17Tr6txpL2F0WfNAklhtcsWfQHKVYBl2v
"""


def ensure_sample(gam_out: Path) -> Path | None:
    """If no CSVs present, write a sample and return its path."""
    gam_out.mkdir(parents=True, exist_ok=True)
    existing = sorted(gam_out.glob("*.csv"))
    if existing:
        return None
    path = gam_out / SAMPLE_NAME
    path.write_text(SAMPLE_CSV, encoding="utf-8")
    print(f"No CSVs found; wrote sample {path}")
    return path


def post_file(base_url: str, path: Path) -> dict:
    boundary = "----slp4sgiGamCsvBoundary"
    data = path.read_bytes()
    filename = path.name
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: text/csv\r\n\r\n"
    ).encode() + data + f"\r\n--{boundary}--\r\n".encode()

    url = base_url.rstrip("/") + "/api/v1/provisioning/jobs"
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode())


def approve(base_url: str, job_id: str) -> dict:
    url = f"{base_url.rstrip('/')}/api/v1/provisioning/jobs/{job_id}/approve"
    req = urllib.request.Request(url, method="POST", data=b"")
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode())


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--base-url", default="http://127.0.0.1:8088")
    p.add_argument("--dir", type=Path, default=DEFAULT_GAM_OUT, help="Directory of *.csv extracts")
    p.add_argument("--file", type=Path, help="Single CSV (relative to --dir or absolute)")
    p.add_argument("--approve", action="store_true", help="Approve each created job")
    p.add_argument("--all", action="store_true", help="Ingest every *.csv (default: newest only unless --file)")
    args = p.parse_args()

    gam_out = args.dir.resolve()
    ensure_sample(gam_out)

    if args.file:
        path = args.file if args.file.is_absolute() else (gam_out / args.file)
        files = [path]
    else:
        files = sorted(gam_out.glob("*.csv"), key=lambda x: x.stat().st_mtime)
        if not files:
            print(f"No CSV files in {gam_out}", file=sys.stderr)
            return 1
        if not args.all:
            files = [files[-1]]  # newest only
            print(f"Ingesting newest CSV only (pass --all for every file): {files[0].name}")

    created = []
    for path in files:
        if not path.is_file():
            print(f"Missing: {path}", file=sys.stderr)
            return 1
        try:
            payload = post_file(args.base_url, path)
        except urllib.error.URLError as exc:
            print(f"Request failed for {path.name}: {exc}", file=sys.stderr)
            print("Is the stack up?  docker compose up -d --build", file=sys.stderr)
            return 2
        job = payload.get("job") or payload
        job_id = job.get("id")
        print(f"Created job id={job_id} status={job.get('status')} emis={job.get('emis')} from {path.name}")
        created.append(job)
        if args.approve and job_id:
            try:
                result = approve(args.base_url, job_id)
                print(f"  Approved → {result.get('config_path')} v{result.get('config_version')}")
            except urllib.error.URLError as exc:
                print(f"  Approve failed: {exc}", file=sys.stderr)
                return 3

    print(json.dumps({"jobs": created}, indent=2, default=str))
    print(f"\n{len(created)} job(s). Open UI → Provisioning jobs → Approve if not --approve.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
