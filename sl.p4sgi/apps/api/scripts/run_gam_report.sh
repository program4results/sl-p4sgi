#!/usr/bin/env bash
# Allowlisted GAM report runner for SL-P4SGI (Phase 3.1 / 4a / 0.4.4).
# Usage: run_gam_report.sh <report> <output_path> [domains_csv]
# domains_csv: comma-separated allowlisted-looking domains (e.g. sl.p4sgi.com).
# Empty / omitted / "all" = customer-wide (no domain filter).
# Read-only allowlist only — never passes unsanitized client strings to a shell.
set -euo pipefail

REPORT="${1:-}"
OUT="${2:-}"
DOMAINS_RAW="${3:-}"

usage() {
  echo "Usage: $0 <report> <output_path> [domains_csv]" >&2
  echo "Reports: users|enrolled|never_logged_in|wrong_password|data_storage|ous|admins|groups|suspended|security_2sv|last_login" >&2
  exit 2
}

[[ -n "$REPORT" && -n "$OUT" ]] || usage

find_gam() {
  if command -v gam >/dev/null 2>&1; then
    command -v gam
    return 0
  fi
  for candidate in \
    "${GAM_BIN:-}" \
    /opt/gam7/gam \
    /home/george/bin/gam7/gam \
    "$HOME/bin/gam7/gam"
  do
    if [[ -n "$candidate" && -x "$candidate" ]]; then
      echo "$candidate"
      return 0
    fi
  done
  return 1
}

# Strict domain token: labels + dots only (no spaces, slashes, shell metacharacters).
validate_domain() {
  local d="$1"
  [[ "$d" =~ ^[a-zA-Z0-9]([a-zA-Z0-9.-]*[a-zA-Z0-9])?\.[a-zA-Z]{2,}$ ]] || return 1
  case "$d" in
    *['\"`$\\\;\|\&\<\>\(\)\{\}\[\]\!\#\~\*\?']*) return 1 ;;
  esac
  return 0
}

parse_domains() {
  local raw="$1"
  DOMAINS=()
  raw="$(echo "$raw" | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]')"
  [[ -z "$raw" || "$raw" == "all" ]] && return 0
  local IFS=','
  local -a parts
  read -ra parts <<< "$raw"
  local d
  for d in "${parts[@]}"; do
    [[ -z "$d" ]] && continue
    if ! validate_domain "$d"; then
      echo "ERROR:invalid_domain: rejected '$d' (must look like host.tld)" >&2
      exit 2
    fi
    DOMAINS+=("$d")
  done
}

# Merge several CSVs (same header) into one; first file supplies header.
merge_csvs() {
  local dest="$1"
  shift
  local first=1 f
  : >"$dest"
  for f in "$@"; do
    [[ -f "$f" && -s "$f" ]] || continue
    if [[ $first -eq 1 ]]; then
      cat "$f" >>"$dest"
      first=0
    else
      # skip header line
      tail -n +2 "$f" >>"$dest"
    fi
  done
  # Ensure at least a header if everything empty — caller handles that.
}

# Post-filter a CSV keeping rows whose email-like column ends with @domain for selected domains.
# Args: infile outfile col_index_or_name domains...
filter_csv_email_domains() {
  local infile="$1" outfile="$2" colhint="$3"
  shift 3
  local doms=("$@")
  python3 - "$infile" "$outfile" "$colhint" "${doms[@]}" <<'PY'
import csv, sys
from pathlib import Path

infile, outfile, colhint = sys.argv[1], sys.argv[2], sys.argv[3]
domains = {d.lower() for d in sys.argv[4:]}
if not domains:
    Path(outfile).write_bytes(Path(infile).read_bytes())
    raise SystemExit(0)

text = Path(infile).read_text(encoding="utf-8", errors="replace")
# Drop leading comment/error lines that are not CSV
lines = text.splitlines()
start = 0
for i, line in enumerate(lines):
    if line.startswith("#"):
        continue
    if "," in line:
        start = i
        break
body = "\n".join(lines[start:])
reader = csv.reader(body.splitlines())
rows = list(reader)
if not rows:
    Path(outfile).write_text("", encoding="utf-8")
    raise SystemExit(0)
header = rows[0]
# Resolve column: prefer named hint, else first cell containing @ in data, else common names
col = None
lower_h = [h.lower() for h in header]
hint = colhint.lower()
for name in (hint, "primaryemail", "user", "email", "account", "adminemail", "group"):
    if name in lower_h:
        col = lower_h.index(name)
        break
if col is None:
    for r in rows[1:20]:
        for i, cell in enumerate(r):
            if "@" in cell:
                col = i
                break
        if col is not None:
            break
if col is None:
    # No email column — keep as-is (e.g. OUs)
    Path(outfile).write_text(body + ("\n" if body and not body.endswith("\n") else ""), encoding="utf-8")
    raise SystemExit(0)

def email_domain(cell: str) -> str | None:
    cell = (cell or "").strip()
    if "@" not in cell:
        return None
    return cell.rsplit("@", 1)[-1].strip().lower() or None

out_rows = [header]
for r in rows[1:]:
    if col >= len(r):
        continue
    d = email_domain(r[col])
    if d and d in domains:
        out_rows.append(r)

with open(outfile, "w", encoding="utf-8", newline="") as fh:
    w = csv.writer(fh)
    w.writerows(out_rows)
PY
}

if ! GAM_CMD="$(find_gam)"; then
  echo "ERROR:gam_not_in_path: gam binary not found (checked PATH, GAM_BIN, /opt/gam7/gam, ~/bin/gam7/gam)" >&2
  exit 127
fi

parse_domains "$DOMAINS_RAW"
mkdir -p "$(dirname "$OUT")"

TMPDIR_RUN=$(mktemp -d)
trap 'rm -rf "$TMPDIR_RUN"' EXIT

# Run gam argv; capture to a file. Returns gam exit code.
run_gam_to() {
  local dest="$1"
  shift
  set +e
  "$@" >"$dest" 2>"${dest}.stderr"
  local rc=$?
  set -e
  if [[ $rc -ne 0 ]]; then
    {
      echo "# gam exit=$rc report=$REPORT"
      if [[ -s "${dest}.stderr" ]]; then
        cat "${dest}.stderr"
      fi
    } >>"$dest"
  fi
  return "$rc"
}

# Append domain X to a print-users style command when filtering.
# For multiple domains: run once per domain and merge.
run_print_users_filtered() {
  # remaining args are the gam argv AFTER gam binary (e.g. print users query ... fields ...)
  local -a base=("$@")
  local rc=0
  if [[ ${#DOMAINS[@]} -eq 0 ]]; then
    run_gam_to "$OUT" "$GAM_CMD" "${base[@]}" || rc=$?
    return "$rc"
  fi
  local -a parts=()
  local d part rc_one=0
  for d in "${DOMAINS[@]}"; do
    part="$TMPDIR_RUN/${d}.csv"
    # Insert `domain <d>` after `print users` (base[0]=print base[1]=users)
    if [[ ${#base[@]} -ge 2 && "${base[0]}" == "print" && "${base[1]}" == "users" ]]; then
      run_gam_to "$part" "$GAM_CMD" print users domain "$d" "${base[@]:2}" || rc_one=$?
    else
      run_gam_to "$part" "$GAM_CMD" "${base[@]}" domain "$d" || rc_one=$?
    fi
    if [[ $rc_one -ne 0 ]]; then
      rc=$rc_one
    fi
    parts+=("$part")
  done
  merge_csvs "$OUT" "${parts[@]}"
  # If merge produced empty (all failed), leave error content from last part
  if [[ ! -s "$OUT" && ${#parts[@]} -gt 0 ]]; then
    cat "${parts[-1]}" >"$OUT" || true
  fi
  return "$rc"
}


# Keep rows where lastLoginTime is empty or "Never" (Directory never-signed-in).
filter_csv_never_logged_in() {
  local infile="$1" outfile="$2"
  python3 - "$infile" "$outfile" <<'NEVERPY'
import csv, sys
from pathlib import Path
infile, outfile = sys.argv[1], sys.argv[2]
text = Path(infile).read_text(encoding="utf-8", errors="replace")
lines = text.splitlines()
start = 0
for i, line in enumerate(lines):
    if line.startswith("#"):
        continue
    if "," in line:
        start = i
        break
body = "\n".join(lines[start:])
reader = csv.reader(body.splitlines())
rows = list(reader)
if not rows:
    Path(outfile).write_text("", encoding="utf-8")
    raise SystemExit(0)
header = rows[0]
lower_h = [h.lower() for h in header]
col = None
for name in ("lastlogintime", "lastlogin", "last_login_time"):
    if name in lower_h:
        col = lower_h.index(name)
        break
out_rows = [header]
if col is None:
    with open(outfile, "w", encoding="utf-8", newline="") as fh:
        csv.writer(fh).writerows(out_rows)
    raise SystemExit(0)
for r in rows[1:]:
    if col >= len(r):
        continue
    val = (r[col] or "").strip()
    if val == "" or val.lower() == "never":
        out_rows.append(r)
with open(outfile, "w", encoding="utf-8", newline="") as fh:
    csv.writer(fh).writerows(out_rows)
NEVERPY
}

rc=0
case "$REPORT" in
  users)
    # All Workspace users (optionally scoped to domain=).
    run_print_users_filtered print users fields primaryEmail,name,orgUnitPath,suspended,creationTime,lastLoginTime,recoveryEmail || rc=$?
    ;;
  enrolled)
    # Users enrolled = Workspace users with creationTime (NOT ChromeOS devices).
    run_print_users_filtered print users fields primaryEmail,name,orgUnitPath,creationTime,lastLoginTime,suspended,recoveryEmail || rc=$?
    ;;
  wrong_password)
    # Force-change-password flag (ops label: wrong_password).
    run_print_users_filtered print users query "changePasswordAtNextLogin=true" fields primaryEmail,name,orgUnitPath,changePasswordAtNextLogin,suspended,recoveryEmail || rc=$?
    ;;
  data_storage)
    # Usage report has no domain=; run customer-wide then post-filter by email domain.
    run_gam_to "$TMPDIR_RUN/raw.csv" "$GAM_CMD" report usage user \
      parameters accounts:used_quota_in_mb,accounts:drive_used_quota_in_mb,accounts:gmail_used_quota_in_mb \
      startdate -3d enddate -2d || rc=$?
    if [[ ${#DOMAINS[@]} -eq 0 ]]; then
      cp "$TMPDIR_RUN/raw.csv" "$OUT"
    else
      filter_csv_email_domains "$TMPDIR_RUN/raw.csv" "$OUT" user "${DOMAINS[@]}"
    fi
    ;;
  ous)
    # OU tree is customer-wide; domain filter does not apply at GAM level.
    run_gam_to "$OUT" "$GAM_CMD" print ous || rc=$?
    ;;
  admins)
    run_gam_to "$TMPDIR_RUN/raw.csv" "$GAM_CMD" print admins || rc=$?
    if [[ ${#DOMAINS[@]} -eq 0 ]]; then
      cp "$TMPDIR_RUN/raw.csv" "$OUT"
    else
      filter_csv_email_domains "$TMPDIR_RUN/raw.csv" "$OUT" "" "${DOMAINS[@]}"
    fi
    ;;
  groups)
    run_gam_to "$TMPDIR_RUN/raw.csv" "$GAM_CMD" print groups fields email,name,id,adminCreated,directMembersCount || rc=$?
    if [[ ${#DOMAINS[@]} -eq 0 ]]; then
      cp "$TMPDIR_RUN/raw.csv" "$OUT"
    else
      filter_csv_email_domains "$TMPDIR_RUN/raw.csv" "$OUT" email "${DOMAINS[@]}"
    fi
    ;;
  suspended)
    run_print_users_filtered print users query "isSuspended=true" fields primaryEmail,name,orgUnitPath,suspended,recoveryEmail,lastLoginTime || rc=$?
    ;;
  security_2sv)
    run_print_users_filtered print users fields primaryEmail,name,orgUnitPath,isEnrolledIn2Sv,isEnforcedIn2Sv,suspended,recoveryEmail || rc=$?
    ;;
  never_logged_in)
    # Directory users with empty/Never lastLoginTime (never signed into Workspace).
    run_print_users_filtered print users fields primaryEmail,name,orgUnitPath,creationTime,lastLoginTime,suspended,recoveryEmail || rc=$?
    if [[ $rc -eq 0 && -s "$OUT" ]]; then
      filter_csv_never_logged_in "$OUT" "$TMPDIR_RUN/never.csv"
      mv "$TMPDIR_RUN/never.csv" "$OUT"
    fi
    ;;
  last_login)
    run_print_users_filtered print users fields primaryEmail,name,orgUnitPath,lastLoginTime,creationTime,suspended,recoveryEmail || rc=$?
    ;;
  *)
    echo "ERROR:unknown_report: allowlisted reports are users|enrolled|never_logged_in|wrong_password|data_storage|ous|admins|groups|suspended|security_2sv|last_login" >&2
    exit 2
    ;;
esac

if [[ $rc -ne 0 ]]; then
  exit "$rc"
fi

rm -f "${OUT}.stderr" 2>/dev/null || true
exit 0
