"""
Drive export helpers for sl.p4sgi (0.4.18+).

Convention (Africa/Johannesburg date YYYYMMDD):
  Folder:  YYYYMMDD-sl.p4sgi-<section>
    Doc:   YYYYMMDD-sl.p4sgi-<section>       (Google Document, A4)
    Sheet: YYYYMMDD-sl.p4sgi-<section>-data  (Google Spreadsheet, full rows)

  Master pack ("all"):
  Folder:  YYYYMMDD-sl.p4sgi-all
    one subfolder per section (same Doc + Sheet naming inside).

Artifacts are built under DATA_DIR/exports/ then uploaded via GAM 7.x:
  gam user geb@p4sgi.com create drivefile localfile <path> \\
    drivefilename <name> mimetype gdoc|gsheet|gfolder \\
    parentid <folderId> returnidonly

Register future sections in main.EXPORT_SECTIONS (see export_section endpoint).
"""

from __future__ import annotations

import csv
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

JHB = ZoneInfo("Africa/Johannesburg")

# A4 in EMUs (python-docx / OOXML: 1 inch = 914400 EMUs)
A4_W = 11906627  # 210mm
A4_H = 16838813  # 297mm

GAM_EXPORT_USER = os.getenv("GAM_EXPORT_USER", "geb@p4sgi.com").strip() or "geb@p4sgi.com"
DRIVE_EXPORT_FOLDER_ID = os.getenv(
    "DRIVE_EXPORT_FOLDER_ID", "1r2lyU95j4BpdaLxLL-ayhl2Vsz7LnTYZ"
).strip()
GAM_BIN = os.getenv("GAM_BIN", "/home/george/bin/gam7/gam").strip() or "/home/george/bin/gam7/gam"
GAM_UPLOAD_TIMEOUT = int(os.getenv("GAM_UPLOAD_TIMEOUT_SEC", "180"))

# Soft cap for Doc tables (Sheet always gets every column).
DOC_MAX_COLS = 10
DOC_MAX_ROWS = 200


def johannesburg_stamp() -> str:
    return datetime.now(JHB).strftime("%Y%m%d")


def johannesburg_now_label() -> str:
    return datetime.now(JHB).strftime("%Y-%m-%d %H:%M:%S %Z")


def section_folder_name(stamp: str, section: str) -> str:
    return f"{stamp}-sl.p4sgi-{section}"


def section_doc_name(stamp: str, section: str) -> str:
    return f"{stamp}-sl.p4sgi-{section}"


def section_sheet_name(stamp: str, section: str) -> str:
    return f"{stamp}-sl.p4sgi-{section}-data"


def ensure_exports_dir(data_dir: Path) -> Path:
    out = Path(data_dir) / "exports"
    out.mkdir(parents=True, exist_ok=True)
    return out


def _safe_cell(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, (dict, list)):
        import json
        try:
            return json.dumps(v, ensure_ascii=False, default=str)
        except Exception:  # noqa: BLE001
            return str(v)
    return str(v)


def rows_to_columns(rows: list[dict[str, Any]], preferred: list[str] | None = None) -> list[str]:
    cols: list[str] = []
    if preferred:
        for c in preferred:
            if c not in cols:
                cols.append(c)
    for r in rows:
        for k in r:
            if k not in cols:
                cols.append(k)
    return cols


def write_xlsx(
    path: Path,
    *,
    title: str,
    columns: list[str],
    rows: list[dict[str, Any]],
    meta: dict[str, Any] | None = None,
) -> Path:
    from openpyxl import Workbook
    from openpyxl.styles import Font, Alignment, PatternFill, Border, Side
    from openpyxl.utils import get_column_letter

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    ws_meta = wb.active
    ws_meta.title = "Summary"
    ws_meta["A1"] = title
    ws_meta["A1"].font = Font(bold=True, size=14)
    ws_meta["A2"] = "Generated (Africa/Johannesburg)"
    ws_meta["B2"] = johannesburg_now_label()
    row_i = 3
    for k, v in (meta or {}).items():
        ws_meta.cell(row=row_i, column=1, value=str(k))
        ws_meta.cell(row=row_i, column=2, value=_safe_cell(v))
        row_i += 1
    ws_meta.column_dimensions["A"].width = 28
    ws_meta.column_dimensions["B"].width = 60

    ws = wb.create_sheet("Data", 1)
    header_fill = PatternFill("solid", fgColor="1E293B")
    header_font = Font(bold=True, color="F8FAFC")
    thin = Border(
        left=Side(style="thin", color="CBD5E1"),
        right=Side(style="thin", color="CBD5E1"),
        top=Side(style="thin", color="CBD5E1"),
        bottom=Side(style="thin", color="CBD5E1"),
    )
    for ci, col in enumerate(columns, start=1):
        cell = ws.cell(row=1, column=ci, value=col)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(wrap_text=True, vertical="center")
        cell.border = thin
    for ri, r in enumerate(rows, start=2):
        for ci, col in enumerate(columns, start=1):
            cell = ws.cell(row=ri, column=ci, value=_safe_cell(r.get(col)))
            cell.border = thin
            cell.alignment = Alignment(wrap_text=False, vertical="center")
    for ci, col in enumerate(columns, start=1):
        width = min(42, max(10, len(col) + 2))
        if rows:
            sample = max((_safe_cell(r.get(col)) for r in rows[:50]), key=len, default="")
            width = min(48, max(width, min(len(sample) + 2, 36)))
        ws.column_dimensions[get_column_letter(ci)].width = width
    ws.auto_filter.ref = f"A1:{get_column_letter(max(len(columns), 1))}{max(len(rows) + 1, 1)}"
    ws.freeze_panes = "A2"
    wb.save(path)
    return path


def write_docx(
    path: Path,
    *,
    title: str,
    columns: list[str],
    rows: list[dict[str, Any]],
    summary: dict[str, Any] | None = None,
    meta_lines: list[str] | None = None,
    orientation: str = "portrait",
    sheet_note: str | None = None,
) -> Path:
    from docx import Document
    from docx.enum.section import WD_ORIENT
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml.ns import qn
    from docx.oxml import OxmlElement
    from docx.shared import Pt, Cm, RGBColor

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = Document()
    section = doc.sections[0]
    if orientation == "landscape":
        section.orientation = WD_ORIENT.LANDSCAPE
        section.page_width = A4_H
        section.page_height = A4_W
    else:
        section.orientation = WD_ORIENT.PORTRAIT
        section.page_width = A4_W
        section.page_height = A4_H
    for attr in ("top_margin", "bottom_margin", "left_margin", "right_margin"):
        setattr(section, attr, Cm(1.5))

    h = doc.add_heading(title, level=0)
    h.alignment = WD_ALIGN_PARAGRAPH.LEFT
    for line in meta_lines or []:
        p = doc.add_paragraph(line)
        p.style = doc.styles["Normal"]
        for run in p.runs:
            run.font.size = Pt(9)
            run.font.color.rgb = RGBColor(0x47, 0x55, 0x69)

    if summary:
        doc.add_heading("Summary", level=2)
        chips = []
        for k, v in summary.items():
            if isinstance(v, dict):
                chips.append(f"{k}: " + ", ".join(f"{sk}={sv}" for sk, sv in list(v.items())[:8]))
            else:
                chips.append(f"{k}: {_safe_cell(v)}")
        for c in chips[:24]:
            doc.add_paragraph(c, style="List Bullet")

    doc_cols = columns[:DOC_MAX_COLS]
    truncated_cols = len(columns) > len(doc_cols)
    show_rows = rows[:DOC_MAX_ROWS]
    truncated_rows = len(rows) > len(show_rows)

    doc.add_heading("Data", level=2)
    note_bits = []
    if truncated_cols:
        note_bits.append(f"showing {len(doc_cols)} of {len(columns)} columns")
    if truncated_rows:
        note_bits.append(f"showing {len(show_rows)} of {len(rows)} rows")
    if sheet_note:
        note_bits.append(sheet_note)
    elif truncated_cols or truncated_rows:
        note_bits.append("full rows+columns are in the sibling Google Sheet (-data)")
    if note_bits:
        np = doc.add_paragraph("; ".join(note_bits))
        for run in np.runs:
            run.font.size = Pt(8)
            run.font.italic = True
            run.font.color.rgb = RGBColor(0x64, 0x74, 0x8B)

    if not doc_cols:
        doc.add_paragraph("(no columns / empty dataset)")
    else:
        table = doc.add_table(rows=1 + len(show_rows), cols=len(doc_cols))
        table.style = "Table Grid"
        hdr = table.rows[0].cells
        for i, col in enumerate(doc_cols):
            hdr[i].text = col
            for p in hdr[i].paragraphs:
                for run in p.runs:
                    run.bold = True
                    run.font.size = Pt(8)
        for ri, r in enumerate(show_rows):
            cells = table.rows[ri + 1].cells
            for ci, col in enumerate(doc_cols):
                cells[ci].text = _safe_cell(r.get(col))[:200]
                for p in cells[ci].paragraphs:
                    for run in p.runs:
                        run.font.size = Pt(7)

        # Shade header row lightly
        for cell in table.rows[0].cells:
            tc = cell._tc
            tcPr = tc.get_or_add_tcPr()
            shd = OxmlElement("w:shd")
            shd.set(qn("w:fill"), "E2E8F0")
            shd.set(qn("w:val"), "clear")
            tcPr.append(shd)

    footer = doc.add_paragraph(
        f"sl.p4sgi export · convention: folder YYYYMMDD-sl.p4sgi-<section> "
        f"contains Doc + Sheet (-data) · generated {johannesburg_now_label()}"
    )
    for run in footer.runs:
        run.font.size = Pt(7)
        run.font.color.rgb = RGBColor(0x94, 0xA3, 0xB8)

    doc.save(path)
    return path


def write_csv(path: Path, columns: list[str], rows: list[dict[str, Any]]) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=columns or ["empty"], extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: _safe_cell(r.get(k)) for k in (columns or ["empty"])})
    return path


class GamUploadError(RuntimeError):
    def __init__(self, message: str, *, hint: str | None = None, stderr: str = "") -> None:
        super().__init__(message)
        self.hint = hint
        self.stderr = stderr


_SCOPE_HINT = (
    "Drive write may need a one-time OAuth scope for geb@p4sgi.com. On the gbu host run:\n"
    "  gam oauth create\n"
    "  # or add Drive scopes if prompted, then confirm:\n"
    "  gam user geb@p4sgi.com show fileinfo "
    f"{DRIVE_EXPORT_FOLDER_ID} fields id,name,capabilities\n"
    "  gam user geb@p4sgi.com create drivefile drivefilename test-folder "
    f"mimetype gfolder parentid {DRIVE_EXPORT_FOLDER_ID} returnidonly\n"
    "Local DOCX/XLSX were still written under docker-data/sl.p4sgi/exports/."
)


def _gam_env() -> dict[str, str]:
    env = os.environ.copy()
    extra = [
        "/home/george/bin/gam7",
        str(Path(GAM_BIN).parent),
    ]
    env["PATH"] = ":".join(extra + [env.get("PATH", "")])
    if os.getenv("GAMCFGDIR"):
        env["GAMCFGDIR"] = os.getenv("GAMCFGDIR", "")
    if os.getenv("HOME"):
        env["HOME"] = os.getenv("HOME", "")
    return env


def _run_gam(args: list[str]) -> str:
    cmd = [GAM_BIN, *args]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=GAM_UPLOAD_TIMEOUT,
            env=_gam_env(),
            check=False,
        )
    except FileNotFoundError as exc:
        raise GamUploadError(f"gam_not_found: {GAM_BIN}", hint=_SCOPE_HINT) from exc
    except subprocess.TimeoutExpired as exc:
        raise GamUploadError(f"gam_timeout after {GAM_UPLOAD_TIMEOUT}s", hint=_SCOPE_HINT) from exc
    out = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
    if proc.returncode != 0:
        low = out.lower()
        scope_ish = any(
            k in low
            for k in (
                "insufficient permission",
                "access not configured",
                "invalid_grant",
                "unauthorized",
                "scope",
                "oauth",
                "forbidden",
                "login_required",
            )
        )
        raise GamUploadError(
            f"gam exited {proc.returncode}: {out[-1500:]}",
            hint=_SCOPE_HINT if scope_ish else None,
            stderr=out[-2000:],
        )
    return out


_ID_RE = re.compile(r"[-\w]{25,}")


def _extract_id(text: str) -> str:
    # returnidonly prints a bare id; fall back to first Drive-like token
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    for ln in reversed(lines):
        if _ID_RE.fullmatch(ln):
            return ln
        m = _ID_RE.search(ln)
        if m and "http" not in ln.lower():
            return m.group(0)
    raise GamUploadError(f"could not parse drive id from gam output: {text[-500:]}", hint=_SCOPE_HINT)


def gam_create_folder(name: str, parent_id: str) -> dict[str, str]:
    out = _run_gam([
        "user", GAM_EXPORT_USER, "create", "drivefile",
        "drivefilename", name,
        "mimetype", "gfolder",
        "parentid", parent_id,
        "returnidonly",
    ])
    fid = _extract_id(out)
    return {
        "id": fid,
        "name": name,
        "mimeType": "application/vnd.google-apps.folder",
        "webViewLink": f"https://drive.google.com/drive/folders/{fid}",
    }


def gam_upload_convert(
    local_path: Path,
    *,
    drive_name: str,
    mime_shortcut: str,
    parent_id: str,
) -> dict[str, str]:
    """Upload local file and convert: mime_shortcut = gdoc | gsheet."""
    local_path = Path(local_path)
    if not local_path.is_file():
        raise GamUploadError(f"local file missing: {local_path}")
    out = _run_gam([
        "user", GAM_EXPORT_USER, "create", "drivefile",
        "localfile", str(local_path),
        "drivefilename", drive_name,
        "mimetype", mime_shortcut,
        "parentid", parent_id,
        "returnidonly",
    ])
    fid = _extract_id(out)
    if mime_shortcut in ("gdoc", "gdocument"):
        link = f"https://docs.google.com/document/d/{fid}/edit"
        mime = "application/vnd.google-apps.document"
    else:
        link = f"https://docs.google.com/spreadsheets/d/{fid}/edit"
        mime = "application/vnd.google-apps.spreadsheet"
    return {"id": fid, "name": drive_name, "mimeType": mime, "webViewLink": link}


def build_and_upload_section(
    *,
    data_dir: Path,
    section: str,
    title: str,
    columns: list[str],
    rows: list[dict[str, Any]],
    summary: dict[str, Any] | None,
    meta_lines: list[str],
    orientation: str,
    formats: set[str],
    parent_folder_id: str | None = None,
    stamp: str | None = None,
    upload: bool = True,
) -> dict[str, Any]:
    """
    Write local DOCX/XLSX under exports/<stamp>-<section>/, optionally create Drive
    folder + upload/convert. Always returns local paths; Drive fields present when upload ok.
    """
    stamp = stamp or johannesburg_stamp()
    folder_name = section_folder_name(stamp, section)
    work = ensure_exports_dir(data_dir) / folder_name
    work.mkdir(parents=True, exist_ok=True)

    result: dict[str, Any] = {
        "section": section,
        "stamp": stamp,
        "folder_name": folder_name,
        "convention": (
            "folder YYYYMMDD-sl.p4sgi-<section> contains Doc "
            "YYYYMMDD-sl.p4sgi-<section> + Sheet YYYYMMDD-sl.p4sgi-<section>-data"
        ),
        "local": {},
        "drive": {},
        "upload_ok": False,
        "upload_error": None,
        "upload_hint": None,
        "row_count": len(rows),
        "column_count": len(columns),
        "orientation": orientation,
        "scoped_as": GAM_EXPORT_USER,
    }

    meta = {
        "section": section,
        "title": title,
        "generated_at_jhb": johannesburg_now_label(),
        "row_count": len(rows),
        "column_count": len(columns),
        "orientation": orientation,
    }
    for line in meta_lines:
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip()

    if "sheet" in formats or "xlsx" in formats:
        xlsx = work / f"{section_sheet_name(stamp, section)}.xlsx"
        write_xlsx(xlsx, title=title, columns=columns, rows=rows, meta=meta)
        result["local"]["xlsx"] = str(xlsx)

    if "doc" in formats or "docx" in formats:
        docx = work / f"{section_doc_name(stamp, section)}.docx"
        write_docx(
            docx,
            title=title,
            columns=columns,
            rows=rows,
            summary=summary,
            meta_lines=meta_lines,
            orientation=orientation,
            sheet_note=(
                f"Full data Sheet sibling name: {section_sheet_name(stamp, section)}"
                if ("sheet" in formats or "xlsx" in formats)
                else None
            ),
        )
        result["local"]["docx"] = str(docx)

    # Also keep a CSV for debugging / alternate upload paths
    if columns:
        csv_path = work / f"{section_sheet_name(stamp, section)}.csv"
        write_csv(csv_path, columns, rows)
        result["local"]["csv"] = str(csv_path)

    if not upload:
        return result

    parent = (parent_folder_id or DRIVE_EXPORT_FOLDER_ID).strip()
    if not parent:
        result["upload_error"] = "DRIVE_EXPORT_FOLDER_ID not set"
        result["upload_hint"] = _SCOPE_HINT
        return result

    try:
        folder = gam_create_folder(folder_name, parent)
        result["drive"]["folder"] = folder
        if "doc" in formats and result["local"].get("docx"):
            result["drive"]["doc"] = gam_upload_convert(
                Path(result["local"]["docx"]),
                drive_name=section_doc_name(stamp, section),
                mime_shortcut="gdoc",
                parent_id=folder["id"],
            )
        if "sheet" in formats and result["local"].get("xlsx"):
            # Prefer CSV→gsheet (cleaner cells) when csv present; else xlsx→gsheet
            local_for_sheet = Path(result["local"].get("csv") or result["local"]["xlsx"])
            result["drive"]["sheet"] = gam_upload_convert(
                local_for_sheet,
                drive_name=section_sheet_name(stamp, section),
                mime_shortcut="gsheet",
                parent_id=folder["id"],
            )
        result["upload_ok"] = True
    except GamUploadError as exc:
        result["upload_error"] = str(exc)
        result["upload_hint"] = exc.hint or _SCOPE_HINT
        result["upload_ok"] = False

    return result
