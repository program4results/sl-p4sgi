/**
 * SL-P4SGI — Apps Script: Attendance Doc publish (+ optional HtmlService charts)
 *
 * For **parent Site spiders (preferred pilot path)** use the dedicated charts project:
 *   docs/apps-script-radar-html.gs  → Deploy Web app Anyone → googleRadarExecUrl
 *   Runbook: docs/SITE_ATTENDANCE_EMBED.md
 *
 * This file remains the Attendance Doc body-replace webhook (tablet POST → Doc).
 * It also includes doGet HtmlService if HQ prefers a single combined project instead.
 *
var SUPPORTED_KINDS = {
  daily_attendance_parents: true,
  radar_parents: true,
  skill_result: true,
};

var PROP_RADAR_PREFIX = "RADAR_JSON_";
var PROP_DAILY_PREFIX = "DAILY_JSON_";
var PROP_MAX_CHARS = 8500; // Script Properties ~9KB soft cap per value

// ---------------------------------------------------------------------------
// GET — Google-hosted parent page (Sites embed target)
// ---------------------------------------------------------------------------

function doGet(e) {
  e = e || { parameter: {} };
  var params = e.parameter || {};
  var emis = String(params.emis || defaultEmis_() || "110101").trim();
  var format = String(params.format || "").toLowerCase();

  if (format === "json") {
    return jsonOut_({
      ok: true,
      emis: emis,
      radar: loadRadar_(emis),
      daily: loadDaily_(emis),
      googleRadarExecUrlHint: "Embed this Web app /exec URL on the Site (Insert → Embed → By URL).",
    });
  }

  var radar = loadRadar_(emis);
  var daily = loadDaily_(emis);
  if (!radar) {
    radar = demoRadar_(emis);
  }
  if (!daily) {
    daily = demoDaily_(emis, radar);
  }
  var period = String(params.period || "Weekly");
  var embed = String(params.embed || "") === "1";
  var html = buildChartsHtml_(emis, radar, daily, period, embed);
  return HtmlService.createHtmlOutput(html)
    .setTitle((radar.schoolName || daily.schoolName || "School") + " · attendance")
    .setXFrameOptionsMode(HtmlService.XFrameOptionsMode.ALLOWALL)
    .addMetaTag("viewport", "width=device-width, initial-scale=1");
}

// ---------------------------------------------------------------------------
// POST — tablet publish: Doc + store tiny JSON for GET charts
// ---------------------------------------------------------------------------

function doPost(e) {
  try {
    var data = JSON.parse((e && e.postData && e.postData.contents) || "{}");
    var kind = (data.kind || "daily_attendance_parents").toString();
    if (!SUPPORTED_KINDS[kind]) {
      return jsonOut_({ ok: false, error: "unsupported kind: " + kind });
    }

    var emis = String(data.emis || defaultEmis_() || "").trim();
    var radarDoc = null;
    if (data.radar && typeof data.radar === "object") {
      radarDoc = data.radar;
    } else if (kind === "radar_parents" && data.periods) {
      radarDoc = data; // body itself is radar_parents
    }

    // Reject PNG / base64 early (parents get SVG from HtmlService, never images).
    var blob = JSON.stringify(data);
    if (/data:image|base64/i.test(blob)) {
      return jsonOut_({ ok: false, error: "PNG/base64 not accepted — send radar_parents JSON series only" });
    }

    var storedRadar = null;
    var storedDaily = null;
    if (radarDoc) {
      if (!emis) emis = String(radarDoc.emis || "").trim();
      storedRadar = storeRadar_(emis, radarDoc, data);
    }
    if (kind === "daily_attendance_parents" || data.snap != null || data.confirmed != null) {
      if (!emis) emis = String(data.emis || "").trim();
      storedDaily = storeDaily_(emis, data, radarDoc);
    }

    var docId = (data.targetDocId || PropertiesService.getScriptProperties().getProperty("ATTENDANCE_DOC_ID") || "").toString().trim();
    var docResult = null;
    if (docId) {
      var md = (data.reportMarkdown || "").toString();
      if (!md && radarDoc) md = radarMarkdown_(data);
      if (!md && storedDaily) md = dailyMarkdown_(storedDaily);
      if (md) {
        docResult = writeAttendanceDoc_(docId, md, kind, data, storedRadar, storedDaily);
      }
    } else if (!storedRadar && !storedDaily) {
      return jsonOut_({ ok: false, error: "missing targetDocId (and no radar/daily JSON to store)" });
    }

    var chartsUrl = chartsPageUrl_(data, radarDoc);
    var ingest = ingestRadar_(data, radarDoc, chartsUrl);
    var dailyIngest = ingestDaily_(data, chartsUrl);

    return jsonOut_({
      ok: true,
      emis: emis,
      kind: kind,
      docId: docId || null,
      docUpdated: !!(docResult && docResult.ok),
      storedRadar: !!storedRadar,
      storedDaily: !!storedDaily,
      googleRadarExecUrl: chartsUrl || webAppExecUrl_(),
      radarPageUrl: (data.radarPageUrl || "").toString(),
      attendancePageUrl: (data.attendancePageUrl || "").toString(),
      chartsPageUrl: chartsUrl || webAppExecUrl_(),
      radarIngest: ingest,
      dailyIngest: dailyIngest,
    });
  } catch (err) {
    return jsonOut_({ ok: false, error: String(err) });
  }
}

// ---------------------------------------------------------------------------
// Storage (Script Properties — tiny JSON only)
// ---------------------------------------------------------------------------

function defaultEmis_() {
  return (PropertiesService.getScriptProperties().getProperty("DEFAULT_EMIS") || "110101").toString().trim();
}

function storeRadar_(emis, radarDoc, data) {
  if (!emis) throw new Error("missing emis for radar store");
  var clean = normalizeRadarForStore_(radarDoc, emis, data);
  var text = JSON.stringify(clean);
  if (text.length > PROP_MAX_CHARS) {
    throw new Error("radar JSON too large for Script Properties (" + text.length + " chars; max " + PROP_MAX_CHARS + ")");
  }
  PropertiesService.getScriptProperties().setProperty(PROP_RADAR_PREFIX + emis, text);
  return clean;
}

function storeDaily_(emis, data, radarDoc) {
  if (!emis) throw new Error("missing emis for daily store");
  var clean = normalizeDailyForStore_(data, emis, radarDoc);
  var text = JSON.stringify(clean);
  if (text.length > PROP_MAX_CHARS) {
    throw new Error("daily JSON too large for Script Properties (" + text.length + " chars)");
  }
  PropertiesService.getScriptProperties().setProperty(PROP_DAILY_PREFIX + emis, text);
  return clean;
}

function loadRadar_(emis) {
  var raw = PropertiesService.getScriptProperties().getProperty(PROP_RADAR_PREFIX + emis);
  if (!raw) return null;
  try { return JSON.parse(raw); } catch (err) { return null; }
}

function loadDaily_(emis) {
  var raw = PropertiesService.getScriptProperties().getProperty(PROP_DAILY_PREFIX + emis);
  if (!raw) return null;
  try { return JSON.parse(raw); } catch (err) { return null; }
}

function normalizeRadarForStore_(radar, emis, data) {
  var out = {
    schemaVersion: "1",
    kind: "radar_parents",
    emis: emis,
    schoolName: radar.schoolName || data.schoolName || "",
    schoolEmail: radar.schoolEmail || data.schoolEmail || "",
    domain: radar.domain || "",
    classLabel: radar.classLabel || data.classLabel || "",
    asOf: radar.asOf || data.attendanceDate || "",
    source: radar.source || "apps_script",
    demo: !!radar.demo,
    headline: radar.headline || null,
    valuesNote: radar.valuesNote || "",
    series: radar.series || [
      { id: "A", key: "snap", label: "Snap (face count)", color: "#1E88E5" },
      { id: "B", key: "confirmed", label: "Confirmed", color: "#FDD835" },
      { id: "C", key: "semis", label: "SEMIS enrollment", color: "#43A047" },
    ],
    periods: radar.periods || {},
  };
  // Drop image-ish keys if present
  delete out.png; delete out.image; delete out.base64;
  return out;
}

function normalizeDailyForStore_(data, emis, radarDoc) {
  var h = (radarDoc && radarDoc.headline) || {};
  var snap = data.snap != null ? data.snap : (h.A != null ? h.A : null);
  var confirmed = data.confirmed != null ? data.confirmed : (h.B != null ? h.B : null);
  var semis = data.semis != null ? data.semis : (h.C != null ? h.C : null);
  var gap = data.gapPct;
  if (gap == null && semis != null && semis > 0 && (confirmed != null || snap != null)) {
    var present = confirmed != null ? confirmed : snap;
    gap = Math.round(((semis - present) / semis) * 1000) / 10;
  }
  var siteRef = data.siteRef || data.site_ref || "";
  // Never confuse Site/legacy ref with warehouse EMIS
  if (siteRef && String(siteRef) === String(emis)) siteRef = "";
  return {
    schemaVersion: "1",
    kind: "daily_attendance_parents",
    emis: emis,
    schoolName: data.schoolName || (radarDoc && radarDoc.schoolName) || "",
    schoolEmail: data.schoolEmail || "",
    classLabel: data.classLabel || (radarDoc && radarDoc.classLabel) || "",
    attendanceDate: data.attendanceDate || data.asOf || (radarDoc && radarDoc.asOf) || "",
    asOf: data.asOf || data.attendanceDate || (radarDoc && radarDoc.asOf) || "",
    source: data.source || "apps_script",
    demo: !!data.demo,
    snap: snap,
    confirmed: confirmed,
    semis: semis,
    gapPct: gap,
    siteRef: siteRef || null,
    siteRefNote: siteRef ? "Optional Site/legacy ref — not warehouse EMIS" : null,
    headline: data.headline || null,
    reportMarkdown: data.reportMarkdown || "",
    bullets: data.bullets || null,
  };
}

// ---------------------------------------------------------------------------
// Doc writer
// ---------------------------------------------------------------------------

function writeAttendanceDoc_(docId, md, kind, data, storedRadar, storedDaily) {
  var doc = DocumentApp.openById(docId);
  var body = doc.getBody();
  body.clear();
  var lines = md.split(/\r?\n/);
  for (var i = 0; i < lines.length; i++) {
    var line = lines[i];
    var para;
    if (line.indexOf("# ") === 0) {
      para = body.appendParagraph(stripMdInline_(line.substring(2)));
      para.setHeading(DocumentApp.ParagraphHeading.HEADING1);
      applyInlineBoldItalic_(para);
    } else if (line.indexOf("## ") === 0) {
      para = body.appendParagraph(stripMdInline_(line.substring(3)));
      para.setHeading(DocumentApp.ParagraphHeading.HEADING2);
      applyInlineBoldItalic_(para);
    } else if (line.indexOf("- ") === 0) {
      para = body.appendListItem(stripMdInline_(line.substring(2)));
      applyInlineBoldItalic_(para);
    } else if (line.trim() === "") {
      body.appendParagraph("");
    } else {
      para = body.appendParagraph(stripMdInline_(line));
      applyInlineBoldItalic_(para);
    }
  }
  body.appendParagraph("");
  body.appendParagraph(
    "Updated " + new Date().toISOString() +
      " · kind " + kind +
      " · EMIS " + (data.emis || "") +
      " · " + (data.schoolName || data.schoolEmail || "") +
      " · " + (data.classLabel || "") +
      " · " + (data.attendanceDate || "")
  ).setForegroundColor("#666666");

  var chartsUrl = chartsPageUrl_(data, storedRadar);
  if (chartsUrl) {
    var linkPara = body.appendParagraph("Progressive radar / attendance charts (Google-hosted): ");
    linkPara.appendText(chartsUrl).setLinkUrl(chartsUrl);
    body.appendParagraph(
      "HQ Site tip: Insert → Embed → By URL → paste googleRadarExecUrl (this /exec URL). " +
      "Do NOT embed http://127.0.0.1 — Sites blocks it. Cloudflare / PUBLIC_BASE_URL not required for this pilot."
    ).setForegroundColor("#666666");
  }
  return { ok: true, docId: docId };
}

function stripMdInline_(s) {
  return String(s || "");
}

function applyInlineBoldItalic_(para) {
  if (!para) return;
  var text = para.getText();
  if (!text || text.indexOf("*") < 0) return;
  var parts = [];
  var re = /\*\*([^*]+)\*\*|\*([^*]+)\*|[^*]+|\*/g;
  var m;
  while ((m = re.exec(text)) !== null) {
    if (m[1] !== undefined) parts.push({ t: m[1], bold: true, italic: false });
    else if (m[2] !== undefined) parts.push({ t: m[2], bold: false, italic: true });
    else if (m[0] === "*") parts.push({ t: "*", bold: false, italic: false });
    else parts.push({ t: m[0], bold: false, italic: false });
  }
  var plain = parts.map(function (p) { return p.t; }).join("");
  para.setText(plain);
  var cursor = 0;
  for (var i = 0; i < parts.length; i++) {
    var p = parts[i];
    var len = p.t.length;
    if (len > 0 && (p.bold || p.italic)) {
      var style = {};
      if (p.bold) style[DocumentApp.Attribute.BOLD] = true;
      if (p.italic) style[DocumentApp.Attribute.ITALIC] = true;
      para.setAttributes(cursor, cursor + len - 1, style);
    }
    cursor += len;
  }
}

// ---------------------------------------------------------------------------
// Charts URL preference: Google /exec first (pilot), then dash URLs
// ---------------------------------------------------------------------------

/**
 * 0.4.8 — prefer Google-hosted HtmlService /exec (googleRadarExecUrl).
 * 0.4.9 — radar/attendance layout polish (see apps-script-radar-html.gs).
 * 0.4.11 — compact single-viewport embed (~650–720px); smaller chips/SVG; hide table in embed=1.
 * Dash localhost URLs are HQ preview only — Sites cannot iframe them.
 * Cloudflare / PUBLIC_BASE_URL abandoned for this pilot.
 */
function chartsPageUrl_(data, radarDoc) {
  var url = (
    data.googleRadarExecUrl ||
    (radarDoc && radarDoc.googleRadarExecUrl) ||
    webAppExecUrl_() ||
    data.attendancePageUrl ||
    (radarDoc && radarDoc.attendancePageUrl) ||
    data.radarPageUrl ||
    (radarDoc && radarDoc.radarPageUrl) ||
    ""
  ).toString().trim();
  // Never advertise localhost as the parent charts URL
  if (/127\.0\.0\.1|localhost/i.test(url)) {
    var g = data.googleRadarExecUrl || webAppExecUrl_() || "";
    if (g) return g;
  }
  return url;
}

function webAppExecUrl_() {
  try {
    return ScriptApp.getService().getUrl() || "";
  } catch (err) {
    return "";
  }
}

function radarMarkdown_(data) {
  var radar = data.radar || data;
  var lines = ["# Radar summary", ""];
  var headline = radar.headline || {};
  if (headline.A != null) {
    lines.push("- **Snapshot:** Snap " + headline.A + " / Confirmed " + headline.B + " / SEMIS " + headline.C);
    lines.push("");
  }
  var order = ["Daily", "Weekly", "Monthly", "Termly", "Annual"];
  var periods = radar.periods || {};
  for (var i = 0; i < order.length; i++) {
    var name = order[i];
    var block = periods[name];
    lines.push("## " + name);
    lines.push("");
    var axes = (block && block.axes) || [];
    if (!axes.length) {
      lines.push("_No axes._");
      lines.push("");
      continue;
    }
    for (var a = 0; a < axes.length; a++) {
      var ax = axes[a];
      var A = ax.A != null ? ax.A : ax.snap;
      var B = ax.B != null ? ax.B : ax.confirmed;
      var C = ax.C != null ? ax.C : ax.semis;
      lines.push("- **" + (ax.label || "") + ":** Snap " + A + " / Confirmed " + B + " / SEMIS " + C);
    }
    lines.push("");
  }
  lines.push("_Spider chart is the Google-hosted HTML page (/exec), not an image in this Doc._");
  return lines.join("\n");
}

function dailyMarkdown_(daily) {
  var lines = ["# Daily attendance" + (daily.classLabel ? " — " + daily.classLabel : ""), ""];
  if (daily.attendanceDate || daily.asOf) lines.push("- **Date:** " + (daily.attendanceDate || daily.asOf));
  if (daily.classLabel) lines.push("- **Class:** " + daily.classLabel);
  if (daily.snap != null) lines.push("- **Present (snap):** " + daily.snap);
  if (daily.confirmed != null) lines.push("- **Confirmed:** " + daily.confirmed);
  if (daily.semis != null) lines.push("- **SEMIS enrollment:** " + daily.semis);
  if (daily.gapPct != null) lines.push("- **Gap vs SEMIS:** " + daily.gapPct + "%");
  if (daily.siteRef) {
    lines.push("- **Site/legacy ref:** " + daily.siteRef + " (not EMIS; warehouse EMIS is " + daily.emis + ")");
  }
  return lines.join("\n");
}

// Optional dash mirror (not required for parents)
function ingestRadar_(data, radarDoc, radarPageUrl) {
  if (!radarDoc) return { skipped: true };
  var base = (data.radarIngestUrl || PropertiesService.getScriptProperties().getProperty("RADAR_INGEST_BASE") || "").toString().replace(/\/$/, "");
  if (!base) return { skipped: true, reason: "no radarIngestUrl (OK — Google /exec does not need dash)" };
  var emis = (radarDoc.emis || data.emis || "").toString().trim();
  if (!emis) return { ok: false, error: "missing emis" };
  var payload = radarDoc;
  if (radarPageUrl && !payload.radarPageUrl) payload.radarPageUrl = radarPageUrl;
  var headers = { "Content-Type": "application/json" };
  var token = PropertiesService.getScriptProperties().getProperty("PUBLISH_AUDIT_TOKEN");
  if (token) headers["X-Publish-Audit-Token"] = token;
  try {
    var resp = UrlFetchApp.fetch(base + "/api/v1/radar/" + encodeURIComponent(emis), {
      method: "post",
      contentType: "application/json",
      headers: headers,
      payload: JSON.stringify(payload),
      muteHttpExceptions: true,
    });
    return { code: resp.getResponseCode(), body: resp.getContentText().slice(0, 500) };
  } catch (err) {
    return { ok: false, error: String(err) };
  }
}

function ingestDaily_(data, chartsUrl) {
  var kind = (data.kind || "daily_attendance_parents").toString();
  if (kind !== "daily_attendance_parents") return { skipped: true, reason: "not daily" };
  var base = (
    data.attendanceIngestUrl ||
    data.radarIngestUrl ||
    PropertiesService.getScriptProperties().getProperty("ATTENDANCE_INGEST_BASE") ||
    PropertiesService.getScriptProperties().getProperty("RADAR_INGEST_BASE") ||
    ""
  ).toString().replace(/\/$/, "");
  if (!base) return { skipped: true, reason: "no attendanceIngestUrl (OK for Google path)" };
  var emis = (data.emis || "").toString().trim();
  if (!emis) return { ok: false, error: "missing emis" };
  var payload = {
    schemaVersion: "1",
    kind: "daily_attendance_parents",
    emis: emis,
    schoolName: data.schoolName || "",
    schoolEmail: data.schoolEmail || "",
    classLabel: data.classLabel || "",
    attendanceDate: data.attendanceDate || "",
    asOf: data.attendanceDate || "",
    source: "apps_script",
    snap: data.snap,
    confirmed: data.confirmed,
    semis: data.semis,
    gapPct: data.gapPct,
    siteRef: data.siteRef || null,
    headline: data.headline || ((data.classLabel || "Class") + (data.attendanceDate ? " — " + data.attendanceDate : "")),
    reportMarkdown: data.reportMarkdown || "",
    bullets: data.bullets || null,
    attendancePageUrl: data.attendancePageUrl || "",
    radarPageUrl: data.radarPageUrl || "",
    googleRadarExecUrl: chartsUrl || "",
  };
  var headers = { "Content-Type": "application/json" };
  var token = PropertiesService.getScriptProperties().getProperty("PUBLISH_AUDIT_TOKEN");
  if (token) headers["X-Publish-Audit-Token"] = token;
  try {
    var resp = UrlFetchApp.fetch(base + "/api/v1/attendance/" + encodeURIComponent(emis), {
      method: "post",
      contentType: "application/json",
      headers: headers,
      payload: JSON.stringify(payload),
      muteHttpExceptions: true,
    });
    return { code: resp.getResponseCode(), body: resp.getContentText().slice(0, 500) };
  } catch (err) {
    return { ok: false, error: String(err) };
  }
}

function jsonOut_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}

// ---------------------------------------------------------------------------
// Demo seed (Test Primary Nursery 3 — known snap 4 / confirmed 4 / SEMIS 6)
// ---------------------------------------------------------------------------

function seedDemoTestPrimary() {
  var emis = "110101";
  var radar = demoRadar_(emis);
  var daily = demoDaily_(emis, radar);
  storeRadar_(emis, radar, {});
  storeDaily_(emis, daily, radar);
  PropertiesService.getScriptProperties().setProperty("DEFAULT_EMIS", emis);
  return { ok: true, emis: emis, message: "Demo radar+daily stored. Open /exec to preview." };
}

function demoRadar_(emis) {
  var axis = function (label) { return { label: label, A: 4, B: 4, C: 6 }; };
  return {
    schemaVersion: "1",
    kind: "radar_parents",
    emis: emis,
    schoolName: "Test Primary School",
    schoolEmail: "sl-test@sl.p4sgi.com",
    domain: "sl.p4sgi.com",
    classLabel: "Nursery 3",
    asOf: "2026-09-28",
    source: "demo",
    demo: true,
    headline: {
      A: 4, B: 4, C: 6,
      note: "Known Test Primary Nursery 3 snapshot only: snap 4, confirmed 4, SEMIS enrollment 6.",
    },
    valuesNote: "Every axis repeats that single known snapshot so each period can draw a spider (needs at least 3 axes). These are not extra SEMIS schools and not a historical series.",
    series: [
      { id: "A", key: "snap", label: "Snap (face count)", color: "#1E88E5" },
      { id: "B", key: "confirmed", label: "Confirmed", color: "#FDD835" },
      { id: "C", key: "semis", label: "SEMIS enrollment", color: "#43A047" },
    ],
    periods: {
      Daily: { axes: [axis("09-26"), axis("09-27"), axis("09-28")] },
      Weekly: { axes: [axis("W37"), axis("W38"), axis("W39")] },
      Monthly: { axes: [axis("2026-07"), axis("2026-08"), axis("2026-09")] },
      Termly: { axes: [axis("T1"), axis("T2"), axis("T3")] },
      Annual: { axes: [axis("2024"), axis("2025"), axis("2026")] },
    },
  };
}

function demoDaily_(emis, radar) {
  return {
    schemaVersion: "1",
    kind: "daily_attendance_parents",
    emis: emis,
    schoolName: "Test Primary School",
    schoolEmail: "sl-test@sl.p4sgi.com",
    classLabel: "Nursery 3",
    attendanceDate: "2026-09-28",
    asOf: "2026-09-28",
    source: "demo",
    demo: true,
    snap: 4,
    confirmed: 4,
    semis: 6,
    gapPct: 33.3,
    siteRef: "5103-1-08837",
    siteRefNote: "Optional Site/legacy ref — not warehouse EMIS (110101)",
    headline: "Nursery 3 — 2026-09-28",
    bullets: [
      "Date: 2026-09-28",
      "Class: Nursery 3",
      "Present (snap): 4",
      "Confirmed: 4",
      "SEMIS enrollment: 6",
      "Gap vs SEMIS: 33.3%",
      "Site/legacy ref: 5103-1-08837 (not EMIS; warehouse EMIS is 110101)",
    ],
  };
}

// ---------------------------------------------------------------------------
// HtmlService page (SVG spiders — same A/B/C schema as dash radar)
// ---------------------------------------------------------------------------

function buildChartsHtml_(emis, radar, daily, period, embed) {
  var payload = {
    emis: emis,
    radar: radar,
    daily: daily,
    period: period,
    embed: embed,
  };
  var json = JSON.stringify(payload).replace(/</g, "\\u003c");
  return "<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\"/>" +
    "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"/>" +
    "<title>Attendance charts</title><style>" +
    ":root{--bg:#0f1419;--card:#1a222c;--text:#e7eef6;--muted:#93a4b5;--line:#2c3a48;" +
    "--a:#1E88E5;--b:#FDD835;--c:#43A047}" +
    "*{box-sizing:border-box}body{margin:0;font:15px/1.45 system-ui,sans-serif;background:var(--bg);color:var(--text)}" +
    "header,main{max-width:920px;margin:0 auto;padding:.85rem 1rem}body.embed header,body.embed main{max-width:none;padding:.55rem .7rem}" +
    "h1{font-size:1.15rem;margin:0 0 .2rem}.sub,.note{color:var(--muted);font-size:.88rem}" +
    ".metrics{display:grid;grid-template-columns:repeat(auto-fit,minmax(100px,1fr));gap:.5rem;margin:.55rem 0}" +
    ".metric{background:#121920;border:1px solid var(--line);border-radius:10px;padding:.5rem .6rem}" +
    ".metric .lbl{color:var(--muted);font-size:.7rem;text-transform:uppercase;letter-spacing:.04em}" +
    ".metric .val{font-size:1.1rem;font-weight:650;font-variant-numeric:tabular-nums;margin-top:.1rem}" +
    ".metric .val.a{color:#8ec5ff}.metric .val.b{color:#ffe082}.metric .val.c{color:#81c784}.metric .val.gap{color:#ffb4a8}" +
    ".chips{display:flex;flex-wrap:wrap;gap:.35rem;margin:.55rem 0}" +
    "button.chip{background:transparent;color:var(--text);border:1px solid var(--line);border-radius:999px;padding:.3rem .7rem;cursor:pointer}" +
    "button.chip[aria-pressed=\"true\"]{background:#243140;border-color:#8ec5ff}" +
    ".card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:.7rem .75rem 1rem;margin:.55rem 0}" +
    "svg.spider{width:100%;height:auto;display:block}.legend{display:flex;gap:1rem;flex-wrap:wrap;font-size:.85rem;margin-top:.35rem}" +
    ".sw{display:inline-block;width:.75rem;height:.75rem;border-radius:2px;margin-right:.3rem;vertical-align:-1px}" +
    "table{width:100%;border-collapse:collapse;margin-top:.7rem;font-size:.85rem}" +
    "th,td{text-align:left;padding:.3rem .35rem;border-bottom:1px solid var(--line)}.nums{font-variant-numeric:tabular-nums}" +
    "ul.bullets{margin:.35rem 0 0;padding-left:1.1rem}.err{color:#ffb4a8}" +
    "</style></head><body" + (embed ? " class=\"embed\"" : "") + ">" +
    "<header><h1 id=\"title\">Attendance</h1><p class=\"sub\" id=\"sub\">Loading…</p>" +
    "<div class=\"metrics\" id=\"metrics\"></div></header>" +
    "<main><div class=\"card\" id=\"daily-card\"><h2 style=\"font-size:1rem;margin:0 0 .4rem\">Daily summary</h2>" +
    "<div id=\"daily\" class=\"note\"></div></div>" +
    "<div class=\"chips\" id=\"chips\" role=\"tablist\" aria-label=\"Period\"></div>" +
    "<div class=\"card\"><div id=\"chart\"></div><div class=\"legend\" id=\"legend\"></div>" +
    "<p class=\"note\" id=\"mode\"></p>" +
    "<table id=\"tbl\" hidden><thead><tr><th>Axis</th><th>A snap</th><th>B confirmed</th><th>C SEMIS</th></tr></thead>" +
    "<tbody></tbody></table></div>" +
    "<p class=\"note\" id=\"foot\"></p></main>" +
    "<script>window.__P4SGI__=" + json + ";<\/script>" +
    "<script>" + chartsClientJs_() + "<\/script></body></html>";
}

function chartsClientJs_() {
  // Client JS as a string — keeps one-file deploy for HQ paste.
  return [
    "var PERIODS=['Daily','Weekly','Monthly','Termly','Annual'];",
    "var COLORS={A:'#1E88E5',B:'#FDD835',C:'#43A047'};",
    "var boot=window.__P4SGI__||{};",
    "var doc=boot.radar||{};",
    "var daily=boot.daily||{};",
    "var period=boot.period||'Weekly';",
    "if(PERIODS.indexOf(period)<0)period='Weekly';",
    "function esc(s){return String(s==null?'':s).replace(/[&<>\"]/g,function(c){return({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;'}[c]);});}",
    "function fmt(n){if(n==null||n==='')return '—';var x=Number(n);if(!isFinite(x))return esc(n);return (x===Math.floor(x))?String(x):String(x);}",
    "function renderMeta(){",
    "  var name=doc.schoolName||daily.schoolName||'School';",
    "  document.getElementById('title').textContent=name+' · attendance';",
    "  var h=doc.headline||{};",
    "  var cls=daily.classLabel||doc.classLabel||'';",
    "  var date=daily.attendanceDate||daily.asOf||doc.asOf||'';",
    "  document.getElementById('sub').textContent='EMIS '+(doc.emis||boot.emis||'')+(cls?' · '+cls:'')+(date?' · '+date:'')+",
    "    (h.A!=null?' · snap '+h.A+' / confirmed '+h.B+' / SEMIS '+h.C:'')+' · Google-hosted charts';",
    "  var snap=daily.snap!=null?daily.snap:h.A;",
    "  var conf=daily.confirmed!=null?daily.confirmed:h.B;",
    "  var semis=daily.semis!=null?daily.semis:h.C;",
    "  var gap=daily.gapPct;",
    "  if(gap==null&&semis!=null&&semis>0&&(conf!=null||snap!=null)){var p=conf!=null?conf:snap;gap=Math.round(((semis-p)/semis)*1000)/10;}",
    "  var cells=[];",
    "  if(cls)cells.push('<div class=\"metric\"><div class=\"lbl\">Class</div><div class=\"val\">'+esc(cls)+'</div></div>');",
    "  if(date)cells.push('<div class=\"metric\"><div class=\"lbl\">Date</div><div class=\"val\">'+esc(date)+'</div></div>');",
    "  if(snap!=null)cells.push('<div class=\"metric\"><div class=\"lbl\">A · Snap</div><div class=\"val a\">'+fmt(snap)+'</div></div>');",
    "  if(conf!=null)cells.push('<div class=\"metric\"><div class=\"lbl\">B · Confirmed</div><div class=\"val b\">'+fmt(conf)+'</div></div>');",
    "  if(semis!=null)cells.push('<div class=\"metric\"><div class=\"lbl\">C · SEMIS</div><div class=\"val c\">'+fmt(semis)+'</div></div>');",
    "  if(gap!=null)cells.push('<div class=\"metric\"><div class=\"lbl\">Gap vs SEMIS</div><div class=\"val gap\">'+fmt(gap)+'%</div></div>');",
    "  document.getElementById('metrics').innerHTML=cells.join('');",
    "  var bits=[];",
    "  if(daily.headline)bits.push('<p><strong>'+esc(daily.headline)+'</strong></p>');",
    "  if(daily.bullets&&daily.bullets.length)bits.push('<ul class=\"bullets\">'+daily.bullets.map(function(b){return '<li>'+esc(b)+'</li>';}).join('')+'</ul>');",
    "  else if(daily.reportMarkdown)bits.push('<pre style=\"white-space:pre-wrap;font:inherit;margin:0\">'+esc(daily.reportMarkdown)+'</pre>');",
    "  else bits.push('<p class=\"note\">Daily metrics above · spiders below.</p>');",
    "  if(daily.siteRef)bits.push('<p class=\"note\">Site/legacy ref <code>'+esc(daily.siteRef)+'</code> is <strong>not</strong> warehouse EMIS <code>'+esc(doc.emis||boot.emis)+'</code>.</p>');",
    "  if(daily.demo||doc.demo)bits.push('<p class=\"note\">Demo scaffold — tablet POST radar_parents / daily JSON replaces this.</p>');",
    "  document.getElementById('daily').innerHTML=bits.join('');",
    "  document.getElementById('foot').textContent=doc.valuesNote||(h.note||'')||'Hosted by Google Apps Script HtmlService. No Cloudflare required.';",
    "}",
    "function seriesMeta(){var byId={};(doc.series||[]).forEach(function(s){byId[s.id]=s;});",
    "  return ['A','B','C'].map(function(id){return{id:id,label:(byId[id]&&byId[id].label)||id,color:(byId[id]&&byId[id].color)||COLORS[id]};});}",
    "function renderChips(){var box=document.getElementById('chips');box.innerHTML='';",
    "  PERIODS.forEach(function(p){var b=document.createElement('button');b.type='button';b.className='chip';b.textContent=p;",
    "    b.setAttribute('aria-pressed',p===period?'true':'false');b.onclick=function(){period=p;renderChips();render();};box.appendChild(b);});}",
    "function maxOf(axes){var m=1;axes.forEach(function(ax){m=Math.max(m,Number(ax.A)||0,Number(ax.B)||0,Number(ax.C)||0);});return m;}",
    "function spiderSvg(axes,meta){var n=axes.length,max=maxOf(axes),cx=220,cy=210,r=140,rings=4,g='';",
    "  for(var k=1;k<=rings;k++){var pts=[];for(var i=0;i<n;i++){var ang=-Math.PI/2+(i*2*Math.PI/n),rr=r*k/rings;",
    "    pts.push((cx+Math.cos(ang)*rr).toFixed(1)+','+(cy+Math.sin(ang)*rr).toFixed(1));}",
    "    g+='<polygon points=\"'+pts.join(' ')+'\" fill=\"none\" stroke=\"#2c3a48\" stroke-width=\"1\"/>';}",
    "  axes.forEach(function(ax,i){var ang=-Math.PI/2+(i*2*Math.PI/n);var x=cx+Math.cos(ang)*r,y=cy+Math.sin(ang)*r;",
    "    g+='<line x1=\"'+cx+'\" y1=\"'+cy+'\" x2=\"'+x.toFixed(1)+'\" y2=\"'+y.toFixed(1)+'\" stroke=\"#2c3a48\"/>';",
    "    var lx=cx+Math.cos(ang)*(r+22),ly=cy+Math.sin(ang)*(r+22);",
    "    g+='<text x=\"'+lx.toFixed(1)+'\" y=\"'+ly.toFixed(1)+'\" fill=\"#93a4b5\" font-size=\"12\" text-anchor=\"middle\">'+esc(ax.label)+'</text>';});",
    "  meta.forEach(function(s){var pts=axes.map(function(ax,i){var ang=-Math.PI/2+(i*2*Math.PI/n);var rr=r*(Number(ax[s.id])||0)/max;",
    "    return (cx+Math.cos(ang)*rr).toFixed(1)+','+(cy+Math.sin(ang)*rr).toFixed(1);}).join(' ');",
    "    g+='<polygon points=\"'+pts+'\" fill=\"'+s.color+'\" fill-opacity=\"0.22\" stroke=\"'+s.color+'\" stroke-width=\"2.5\"/>';});",
    "  return '<svg class=\"spider\" viewBox=\"0 0 440 420\" role=\"img\" aria-label=\"'+period+' spider\">'+g+'</svg>';}",
    "function barsSvg(axes,meta){var max=maxOf(axes),w=440,h=260,padL=36,padB=36,padT=16;",
    "  var groupW=(w-padL-16)/axes.length,barW=Math.min(28,(groupW-12)/3);",
    "  var g='<line x1=\"'+padL+'\" y1=\"'+(h-padB)+'\" x2=\"'+(w-8)+'\" y2=\"'+(h-padB)+'\" stroke=\"#2c3a48\"/>';",
    "  axes.forEach(function(ax,i){var gx=padL+i*groupW+8;meta.forEach(function(s,si){var val=Number(ax[s.id])||0;var bh=(h-padB-padT)*val/max;",
    "    var x=gx+si*(barW+3),y=h-padB-bh;g+='<rect x=\"'+x.toFixed(1)+'\" y=\"'+y.toFixed(1)+'\" width=\"'+barW.toFixed(1)+'\" height=\"'+bh.toFixed(1)+'\" fill=\"'+s.color+'\" rx=\"2\"/>';});",
    "    g+='<text x=\"'+(gx+barW).toFixed(1)+'\" y=\"'+(h-14)+'\" fill=\"#93a4b5\" font-size=\"12\" text-anchor=\"middle\">'+esc(ax.label)+'</text>';});",
    "  return '<svg class=\"spider\" viewBox=\"0 0 440 260\" role=\"img\">'+g+'</svg>';}",
    "function render(){var block=(doc.periods||{})[period]||{axes:[]};var axes=block.axes||[];var meta=seriesMeta();",
    "  document.getElementById('legend').innerHTML=meta.map(function(s){return '<span><i class=\"sw\" style=\"background:'+s.color+'\"></i>'+s.id+' '+s.label+'</span>';}).join('');",
    "  var mode=document.getElementById('mode'),chart=document.getElementById('chart');",
    "  if(axes.length>=3){mode.textContent=period+' · spider ('+axes.length+' axes).';chart.innerHTML=spiderSvg(axes,meta);}",
    "  else if(axes.length){mode.textContent=period+' · grouped bars ('+axes.length+'). Spider needs ≥3 axes.';chart.innerHTML=barsSvg(axes,meta);}",
    "  else{mode.textContent='No axes for '+period+'.';chart.innerHTML='';}",
    "  var tb=document.querySelector('#tbl tbody'),table=document.getElementById('tbl');tb.innerHTML='';",
    "  axes.forEach(function(ax){var tr=document.createElement('tr');tr.className='nums';",
    "    tr.innerHTML='<td>'+esc(ax.label)+'</td><td>'+ax.A+'</td><td>'+ax.B+'</td><td>'+ax.C+'</td>';tb.appendChild(tr);});",
    "  table.hidden=axes.length===0;}",
    "renderMeta();renderChips();render();",
  ].join("\n");
}

/** Optional smoke tests from the Script editor. */
function testReplaceLocal() {
  doPost({
    postData: {
      contents: JSON.stringify({
        kind: "daily_attendance_parents",
        targetDocId: PropertiesService.getScriptProperties().getProperty("ATTENDANCE_DOC_ID") || "PASTE_ATTENDANCE_DOC_ID_HERE",
        reportMarkdown: "# Daily attendance — test\n\n- **Class:** Nursery 3\n- **Present:** 4\n",
        emis: "110101",
        schoolName: "Test Primary School",
        classLabel: "Nursery 3",
        attendanceDate: "2026-09-28",
        snap: 4,
        confirmed: 4,
        semis: 6,
        gapPct: 33.3,
        siteRef: "5103-1-08837",
      }),
    },
  });
}

function testRadarParents() {
  seedDemoTestPrimary();
  doPost({
    postData: {
      contents: JSON.stringify({
        kind: "radar_parents",
        targetDocId: PropertiesService.getScriptProperties().getProperty("ATTENDANCE_DOC_ID") || "PASTE_ATTENDANCE_DOC_ID_HERE",
        emis: "110101",
        schoolName: "Test Primary School",
        classLabel: "Nursery 3",
        attendanceDate: "2026-09-28",
        radar: demoRadar_("110101"),
      }),
    },
  });
}
