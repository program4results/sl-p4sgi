#!/usr/bin/env bash
# Stub outline — Site template + school-config assist for ~100 schools (sl.p4sgi 0.4.10)
#
# PURPOSE
#   Help HQ batch-write school-configs/{emis}.json from a GAM-derived list and print
#   the Embed URL checklist. Does NOT call Sites API to insert iframes (Google cannot
#   reliably automate new Sites page-body embeds). Does NOT invent SEMIS/EMIS rows.
#   Does NOT require Cloudflare.
#
# ONE-TIME (manual, outside this script)
#   1. Deploy shared Apps Script Web app (docs/apps-script-radar-html.gs) → Anyone
#   2. Set SHARED_EXEC below to that /exec URL (no query string)
#   3. Build Site template with iframe: ${SHARED_EXEC}?emis={{EMIS}}&embed=1
#
# PER SCHOOL (this stub + HQ clicks)
#   - Input CSV/JSON must already contain real emis / schoolName / schoolEmail
#     (from GAM Directory / dash /api/v1/emis — never invent)
#   - Script writes school-config JSON with shared googleRadarExecUrl
#   - HQ copies Site from template, replaces {{EMIS}}, Publishes, pastes siteAttendanceUrl
#
# APPS SCRIPT NOTES (optional future)
#   - DriveApp can copy Attendance Docs; store new file id → attendanceDocId
#   - UrlFetchApp / ScriptApp cannot Deploy Web apps or edit Sites embeds for you
#   - doPost on the shared /exec already keys storage by emis — one project is enough
#
# USAGE (when fleshed out — currently dry-run outline)
#   export SHARED_EXEC='https://script.google.com/macros/s/DEPLOYMENT_ID/exec'
#   bash scripts/provision_school_site_template.stub.sh \
#     /path/to/known-schools.csv \
#     /home/george/drive_14tb/docker-data/sl.p4sgi/school-configs
#
# Expected CSV columns (minimal): emis,schoolName,schoolEmail[,attendanceDocId,siteAttendanceUrl]
#
set -euo pipefail

SHARED_EXEC="${SHARED_EXEC:-}"
INPUT="${1:-}"
OUT_DIR="${2:-/home/george/drive_14tb/docker-data/sl.p4sgi/school-configs}"
DASH_BASE="${DASH_BASE:-http://127.0.0.1:8088}"

usage() {
  sed -n '2,35p' "$0" | sed 's/^# \?//'
  exit 1
}

[[ -n "$INPUT" ]] || usage
[[ -f "$INPUT" ]] || { echo "Input not found: $INPUT" >&2; exit 1; }
[[ -n "$SHARED_EXEC" ]] || {
  echo "Set SHARED_EXEC to the shared HtmlService /exec (no ?emis). Cloudflare not required." >&2
  exit 1
}
case "$SHARED_EXEC" in
  *'?'*) echo "SHARED_EXEC must be the bare /exec URL (strip ?emis=&embed=)." >&2; exit 1 ;;
esac

mkdir -p "$OUT_DIR"

echo "== sl.p4sgi site-template provision stub (dry-run outline) =="
echo "SHARED_EXEC=$SHARED_EXEC"
echo "INPUT=$INPUT"
echo "OUT_DIR=$OUT_DIR"
echo
echo "For each known EMIS row you would:"
echo "  1. Write $OUT_DIR/{emis}.json with googleRadarExecUrl=\$SHARED_EXEC"
echo "  2. Print embed: \${SHARED_EXEC}?emis={emis}&embed=1"
echo "  3. Print HQ checklist: copy Site template → replace EMIS → Publish → set siteAttendanceUrl"
echo "  4. Never create EMIS codes not present in INPUT"
echo
echo "Example JSON body (illustrative — not written by this stub yet):"
cat << JSON
{
  "emis": "<from INPUT>",
  "schoolName": "<from INPUT>",
  "schoolEmail": "<from INPUT>",
  "googleRadarExecUrl": "${SHARED_EXEC}",
  "siteAttendanceUrl": "",
  "attendanceDocId": "",
  "publishExecUrl": "",
  "websitePackDocId": "",
  "googleSiteId": "",
  "attendancePageUrl": "${DASH_BASE}/school/<emis>/attendance?embed=1",
  "radarPageUrl": "${DASH_BASE}/radar/?emis=<emis>&embed=1",
  "driveFolderId": "",
  "configEndpoint": "",
  "_notes": "googleRadarExecUrl is shared fleet-wide. Fill siteAttendanceUrl after Sites copy+Publish. See docs/SCHOOL_SITE_TEMPLATE_PROVISION.md"
}
JSON

# --- Implementation hook (intentionally incomplete) ---
# When implementing: parse CSV, skip blank emis, refuse rows that look invented
# (e.g. require emis to match /^[0-9]{4,}$/ or warehouse allowlist from
# curl -s $DASH_BASE/api/v1/emis), then write JSON with python3 -c / jq.
# Do not call Cloudflare. Do not POST to Sites embed APIs.

echo
echo "Stub complete — no school-config files written. See docs/SCHOOL_SITE_TEMPLATE_PROVISION.md"
exit 0
