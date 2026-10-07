/**
 * SL-P4SGI — Google-hosted radar spiders (HtmlService). Pilot path — no Cloudflare.
 *
 * Stack copy: docs/apps-script-radar-html.gs  (v0.4.11)
 *
 * What this does
 *   doGet  → serves SVG spiders from Script Properties JSON (radar_parents v1).
 *   doPost → accepts tiny radar_parents JSON (no PNG / no base64); stores per EMIS.
 *   Seed   → Test Primary EMIS 110101, Nursery 3, snap 4 / confirmed 4 / SEMIS 6.
 *
 * HQ deploy (you must click these — agents cannot deploy for you)
 *   1. https://script.google.com → New project (or open existing radar project)
 *   2. Rename project e.g. "sl.p4sgi radar HtmlService"
 *   3. Replace Code.gs contents with THIS file (paste entire file)
 *   4. (Optional) File → New → HTML → name "Radar" and paste RADAR_HTML from
 *      docs/apps-script-radar.html — not required; this .gs embeds HTML as a string.
 *   5. Run seedDemoRadar_ once (Run ▶) → Authorize → allow
 *   6. Deploy → New deployment → Type: Web app
 *        Execute as: Me
 *        Who has access: Anyone
 *   7. Authorize → Copy the Web app URL ending in /exec
 *   8. Paste that URL into school-configs/110101.json → googleRadarExecUrl
 *      (and optionally Site Embed — see docs/SITE_ATTENDANCE_EMBED.md)
 *
 * Parent Site embed URL example:
 *   https://script.google.com/macros/s/DEPLOYMENT_ID/exec?emis=110101&embed=1
 *
 * Tablet / webhook POST: same /exec URL, Content-Type application/json,
 * body = radar_parents v1 (or { kind:"radar_parents", radar:{...} }).
 * PUBLIC_BASE_URL / Cloudflare NOT required for parent delivery.
 */

var PROP_PREFIX = "radar_";
var PROP_DAILY_PREFIX = "daily_";
var DEFAULT_EMIS = "110101";

function doGet(e) {
  e = e || { parameter: {} };
  var p = e.parameter || {};
  var emis = String(p.emis || DEFAULT_EMIS).trim() || DEFAULT_EMIS;
  var embed = String(p.embed || "1") === "1";
  var period = String(p.period || "Daily");
  if (String(p.format || "").toLowerCase() === "json") {
    return jsonOut_({ ok: true, emis: emis, radar: loadRadar_(emis), daily: loadDaily_(emis) });
  }
  var doc = loadRadar_(emis);
  var daily = loadDaily_(emis);
  var html = buildRadarPage_(doc, {
    emis: emis,
    embed: embed,
    period: period,
    daily: daily,
    execBase: ScriptApp.getService().getUrl() || "",
  });
  return HtmlService.createHtmlOutput(html)
    .setTitle((doc.schoolName || "Radar") + " — progressive radar")
    .setXFrameOptionsMode(HtmlService.XFrameOptionsMode.ALLOWALL)
    .addMetaTag("viewport", "width=device-width, initial-scale=1");
}

function doPost(e) {
  try {
    var raw = (e && e.postData && e.postData.contents) || "{}";
    var data = JSON.parse(raw);
    // Reject PNG / base64 anywhere in the payload.
    if (/data:image|"png"|base64/i.test(raw)) {
      return jsonOut_({ ok: false, error: "PNG/base64 rejected; send tiny JSON series only" });
    }

    // Daily digest (optional) — fills metrics on the HtmlService page.
    if (data && data.kind === "daily_attendance_parents") {
      var demis = String(data.emis || DEFAULT_EMIS).trim();
      var daily = normalizeDaily_(data, demis, null);
      saveDaily_(demis, daily);
      var durl = (ScriptApp.getService().getUrl() || "") + "?emis=" + encodeURIComponent(demis) + "&embed=1";
      return jsonOut_({
        ok: true,
        emis: demis,
        kind: "daily_attendance_parents",
        googleRadarExecUrl: durl,
        storedDaily: true,
        classLabel: daily.classLabel || "",
        gapPct: daily.gapPct,
      });
    }

    var radar = null;
    if (data && data.kind === "radar_parents" && data.series && data.periods) {
      radar = data;
    } else if (data && data.radar && typeof data.radar === "object") {
      radar = data.radar;
      if (!radar.kind) radar.kind = "radar_parents";
    } else if (data && data.series && data.periods) {
      radar = data;
      radar.kind = radar.kind || "radar_parents";
    }
    if (!radar) {
      return jsonOut_({ ok: false, error: "expected radar_parents or daily_attendance_parents JSON (no PNG)" });
    }
    if (radar.kind && radar.kind !== "radar_parents") {
      return jsonOut_({ ok: false, error: "unsupported kind: " + radar.kind });
    }
    if (radar.png || radar.imageBase64 || radar.chartPng || radar.image) {
      return jsonOut_({ ok: false, error: "PNG/base64 charts rejected; send series JSON only" });
    }
    radar.schemaVersion = String(radar.schemaVersion || "1");
    radar.kind = "radar_parents";
    radar.source = radar.source || "apps_script";
    var emis = String(radar.emis || data.emis || DEFAULT_EMIS).trim();
    if (!emis) return jsonOut_({ ok: false, error: "missing emis" });
    radar.emis = emis;
    saveRadar_(emis, radar);
    // Also refresh daily metrics from radar headline when present.
    if (radar.headline || data.classLabel || data.snap != null) {
      saveDaily_(emis, normalizeDaily_(data, emis, radar));
    }
    var url = (ScriptApp.getService().getUrl() || "") + "?emis=" + encodeURIComponent(emis) + "&embed=1";
    return jsonOut_({
      ok: true,
      emis: emis,
      googleRadarExecUrl: url,
      stored: true,
      classLabel: radar.classLabel || "",
      asOf: radar.asOf || "",
    });
  } catch (err) {
    return jsonOut_({ ok: false, error: String(err) });
  }
}

/** Run once from the Apps Script editor to seed Test Primary demo (4/4/6 Nursery 3). */
function seedDemoRadar_() {
  var demo = demoRadar110101_();
  saveRadar_(demo.emis, demo);
  saveDaily_(demo.emis, normalizeDaily_({
    emis: demo.emis,
    schoolName: demo.schoolName,
    classLabel: demo.classLabel,
    attendanceDate: demo.asOf,
    snap: 4,
    confirmed: 4,
    semis: 6,
    gapPct: 33.3,
    siteRef: "5103-1-08837",
    demo: true,
    source: "demo",
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
  }, demo.emis, demo));
  Logger.log("Seeded radar+daily for EMIS " + demo.emis + " (" + demo.classLabel + " A/B/C=" +
    demo.headline.A + "/" + demo.headline.B + "/" + demo.headline.C + ")");
}

function loadRadar_(emis) {
  var props = PropertiesService.getScriptProperties();
  var raw = props.getProperty(PROP_PREFIX + emis);
  if (raw) {
    try {
      return JSON.parse(raw);
    } catch (err) {
      /* fall through to demo */
    }
  }
  if (emis === DEFAULT_EMIS) {
    var demo = demoRadar110101_();
    saveRadar_(emis, demo);
    return demo;
  }
  return {
    schemaVersion: "1",
    kind: "radar_parents",
    emis: emis,
    schoolName: "",
    classLabel: "",
    asOf: "",
    source: "empty",
    headline: { A: 0, B: 0, C: 0, note: "No radar JSON stored yet. POST radar_parents or run seedDemoRadar_." },
    series: [
      { id: "A", key: "snap", label: "Snap (face count)", color: "#1E88E5" },
      { id: "B", key: "confirmed", label: "Confirmed", color: "#FDD835" },
      { id: "C", key: "semis", label: "SEMIS enrollment", color: "#43A047" },
    ],
    periods: {
      Daily: { axes: [] },
      Weekly: { axes: [] },
      Monthly: { axes: [] },
      Termly: { axes: [] },
      Annual: { axes: [] },
    },
  };
}

function saveRadar_(emis, radar) {
  PropertiesService.getScriptProperties().setProperty(
    PROP_PREFIX + emis,
    JSON.stringify(radar)
  );
}

function loadDaily_(emis) {
  var raw = PropertiesService.getScriptProperties().getProperty(PROP_DAILY_PREFIX + emis);
  if (!raw) return null;
  try { return JSON.parse(raw); } catch (err) { return null; }
}

function saveDaily_(emis, daily) {
  PropertiesService.getScriptProperties().setProperty(PROP_DAILY_PREFIX + emis, JSON.stringify(daily));
}

function normalizeDaily_(data, emis, radar) {
  var h = (radar && radar.headline) || {};
  var snap = data.snap != null ? data.snap : (h.A != null ? h.A : null);
  var confirmed = data.confirmed != null ? data.confirmed : (h.B != null ? h.B : null);
  var semis = data.semis != null ? data.semis : (h.C != null ? h.C : null);
  var gap = data.gapPct;
  if (gap == null && semis != null && semis > 0 && (confirmed != null || snap != null)) {
    var present = confirmed != null ? confirmed : snap;
    gap = Math.round(((semis - present) / semis) * 1000) / 10;
  }
  var siteRef = data.siteRef || "";
  if (siteRef && String(siteRef) === String(emis)) siteRef = "";
  return {
    schemaVersion: "1",
    kind: "daily_attendance_parents",
    emis: emis,
    schoolName: data.schoolName || (radar && radar.schoolName) || "",
    classLabel: data.classLabel || (radar && radar.classLabel) || "",
    attendanceDate: data.attendanceDate || data.asOf || (radar && radar.asOf) || "",
    asOf: data.asOf || data.attendanceDate || (radar && radar.asOf) || "",
    source: data.source || "apps_script",
    demo: !!data.demo || !!(radar && radar.demo),
    snap: snap,
    confirmed: confirmed,
    semis: semis,
    gapPct: gap,
    siteRef: siteRef || null,
    siteRefNote: siteRef ? "Optional Site/legacy ref — not warehouse EMIS" : null,
    headline: data.headline || null,
    bullets: data.bullets || null,
  };
}



function jsonOut_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(
    ContentService.MimeType.JSON
  );
}

/** Known Test Primary Nursery 3 snapshot only — not extra SEMIS schools. */
function demoRadar110101_() {
  function axes(labels) {
    return labels.map(function (label) {
      return { label: label, A: 4, B: 4, C: 6 };
    });
  }
  return {
    schemaVersion: "1",
    kind: "radar_parents",
    emis: "110101",
    schoolName: "Test Primary School",
    schoolEmail: "sl-test@sl.p4sgi.com",
    domain: "sl.p4sgi.com",
    classLabel: "Nursery 3",
    asOf: "2026-09-28",
    source: "demo",
    demo: true,
    headline: {
      A: 4,
      B: 4,
      C: 6,
      note: "Known Test Primary Nursery 3 snapshot only: snap 4, confirmed 4, SEMIS enrollment 6.",
    },
    valuesNote:
      "Every axis repeats that single known snapshot so each period can draw a spider (needs at least 3 axes). These are not extra SEMIS schools and not a historical series.",
    series: [
      { id: "A", key: "snap", label: "Snap (face count)", color: "#1E88E5" },
      { id: "B", key: "confirmed", label: "Confirmed", color: "#FDD835" },
      { id: "C", key: "semis", label: "SEMIS enrollment", color: "#43A047" },
    ],
    periods: {
      Daily: { axes: axes(["09-26", "09-27", "09-28"]) },
      Weekly: { axes: axes(["W37", "W38", "W39"]) },
      Monthly: { axes: axes(["2026-07", "2026-08", "2026-09"]) },
      Termly: { axes: axes(["T1", "T2", "T3"]) },
      Annual: { axes: axes(["2024", "2025", "2026"]) },
    },
  };
}

/**
 * Inline HtmlService page (same spider logic as apps/api/static/radar/index.html).
 * Compact embed=1 layout (~650–720px) for Google Sites — MUST stay in sync with static radar.
 * Optional separate file: docs/apps-script-radar.html — copy into Apps Script as HTML "Radar"
 * only if you switch doGet to createTemplateFromFile; default path uses this string.
 */
function buildRadarPage_(doc, opts) {
  var payload = JSON.stringify(doc || {});
  var embedClass = opts.embed ? "embed" : "";
  var initialPeriod = opts.period || "Daily";
  var emis = opts.emis || doc.emis || DEFAULT_EMIS;
  // Escape </script> in JSON for safe embedding
  var safeJson = payload.replace(/</g, "\\u003c");
  var dailyJson = JSON.stringify(opts.daily || null).replace(/</g, "\\u003c");
  return (
    "<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\"/>" +
    "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"/>" +
    "<title>Radar — p4sgi</title><style>" +
    ":root{--bg:#0f1419;--card:#1a222c;--text:#e7eef6;--muted:#93a4b5;--line:#2c3a48;--line-soft:#1e2832}" +
    "*{box-sizing:border-box}body{margin:0;font:15px/1.45 system-ui,sans-serif;background:var(--bg);color:var(--text)}" +
    "header,main{max-width:960px;margin:0 auto;padding:1rem 1.15rem;width:100%}h1{font-size:1.25rem;margin:0 0 .25rem}" +
    ".sub,.note{color:var(--muted);font-size:.88rem}" +
    ".metrics{display:grid;grid-template-columns:repeat(auto-fit,minmax(110px,1fr));gap:.55rem;margin:.55rem 0 .35rem}" +
    ".metric{background:#121920;border:1px solid var(--line-soft);border-radius:10px;padding:.5rem .6rem}" +
    ".metric .lbl{color:var(--muted);font-size:.68rem;text-transform:uppercase;letter-spacing:.04em}" +
    ".metric .val{font-size:1.05rem;font-weight:650;font-variant-numeric:tabular-nums;margin-top:.08rem}" +
    ".metric .val.a{color:#8ec5ff}.metric .val.b{color:#ffe082}.metric .val.c{color:#81c784}.metric .val.gap{color:#ffb4a8}" +
    ".chips{display:flex;flex-wrap:wrap;gap:.4rem;margin:.65rem 0 .85rem}" +
    "button.chip{background:#121920;color:var(--text);border:1px solid var(--line);border-radius:8px;padding:.35rem .75rem;cursor:pointer;font:inherit;font-size:.84rem;min-width:4.2rem;text-align:center}" +
    "button.chip[aria-pressed=\"true\"]{background:#243140;border-color:#8ec5ff;color:#e7eef6}" +
    ".card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:.85rem 1rem 1rem;display:flex;flex-direction:column}" +
    "#chart{width:100%;margin:.2rem 0 0;display:flex;justify-content:center;align-items:center}" +
    "svg.spider{width:100%;max-width:480px;height:auto;display:block;margin:0 auto}" +
    ".legend{display:flex;gap:.65rem 1rem;flex-wrap:wrap;font-size:.82rem;margin:.65rem 0 .2rem;justify-content:center}" +
    ".sw{display:inline-block;width:.7rem;height:.7rem;border-radius:2px;margin-right:.28rem;vertical-align:-1px}" +
    "#mode{margin:.2rem 0 0}" +
    "table{width:100%;border-collapse:collapse;margin-top:1.1rem;font-size:.86rem;border:none}" +
    "thead th{text-align:left;padding:.5rem .4rem .35rem;color:var(--muted);font-weight:600;font-size:.7rem;text-transform:uppercase;letter-spacing:.04em;border:none;background:transparent}" +
    "tbody td{text-align:left;padding:.4rem .4rem;border:none;border-bottom:1px solid rgba(30,40,50,.9)}" +
    "tbody tr:last-child td{border-bottom:none}.err{color:#ffb4a8}.nums{font-variant-numeric:tabular-nums}" +
    /* embed compact */
    "body.embed{font-size:13px;line-height:1.3;overflow:hidden}" +
    "body.embed header,body.embed main{max-width:none;width:100%;padding:.28rem .4rem}" +
    "body.embed header{padding-bottom:0;padding-top:.28rem}body.embed header h1{font-size:.95rem;margin:0}" +
    "body.embed .sub{font-size:.72rem;margin:.08rem 0 0}" +
    "body.embed .metrics{gap:.3rem;margin:.3rem 0 .15rem;grid-template-columns:repeat(auto-fit,minmax(68px,1fr))}" +
    "body.embed .metric{padding:.22rem .35rem;border-radius:6px}" +
    "body.embed .metric .lbl{font-size:.55rem;letter-spacing:.03em}body.embed .metric .val{font-size:.88rem;margin-top:0}" +
    "body.embed .chips{gap:.22rem;margin:.22rem 0 .3rem}" +
    "body.embed button.chip{padding:.18rem .42rem;font-size:.7rem;min-width:3rem;border-radius:5px}" +
    "body.embed main{padding-top:.1rem;padding-bottom:.25rem}" +
    "body.embed .card{background:#151c24;border-color:#243140;padding:.35rem .4rem .4rem;border-radius:8px}" +
    "body.embed #chart{margin:0}body.embed svg.spider{max-width:min(300px,94%)}" +
    "body.embed .legend{margin:.28rem 0 0;gap:.35rem .65rem;font-size:.68rem;justify-content:center}" +
    "body.embed .sw{width:.5rem;height:.5rem;margin-right:.2rem}" +
    "body.embed #mode,body.embed #foot,body.embed #tbl{display:none!important}" +
    "</style></head><body class=\"" +
    embedClass +
    "\">" +
    "<header><h1 id=\"title\">Progressive radar</h1><p class=\"sub\" id=\"sub\">Loading…</p><div class=\"metrics\" id=\"metrics\"></div></header>" +
    "<main><div class=\"chips\" id=\"chips\" role=\"tablist\" aria-label=\"Period\"></div>" +
    "<div class=\"card\"><div id=\"chart\"></div><div class=\"legend\" id=\"legend\"></div>" +
    "<p class=\"note\" id=\"mode\"></p>" +
    "<table id=\"tbl\" hidden><thead><tr><th>Axis</th><th>A snap</th><th>B confirmed</th><th>C SEMIS</th></tr></thead><tbody></tbody></table>" +
    "</div><p class=\"note\" id=\"foot\"></p></main>" +
    "<script>" +
    "var PERIODS=['Daily','Weekly','Monthly','Termly','Annual'];" +
    "var COLORS={A:'#1E88E5',B:'#FDD835',C:'#43A047'};" +
    "var doc=" + safeJson + ";" +
    "var daily=" + dailyJson + ";" +
    "var period=" + JSON.stringify(initialPeriod) + ";" +
    "var emis=" + JSON.stringify(emis) + ";" +
    "var isEmbed=document.body.classList.contains('embed');" +
    "if(PERIODS.indexOf(period)<0)period='Daily';" +
    "function esc(s){return String(s).replace(/[&<>\"]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;'}[c];});}" +
    "function fmt(n){if(n==null||n==='')return'—';var x=+n;return (x===Math.floor(x))?String(x):String(x);}" +
    "function maxOf(axes){var m=1;axes.forEach(function(ax){m=Math.max(m,+ax.A||0,+ax.B||0,+ax.C||0);});return m;}" +
    "function seriesMeta(){var byId={};(doc.series||[]).forEach(function(s){byId[s.id]=s;});" +
    "return ['A','B','C'].map(function(id){return{id:id,label:(byId[id]&&byId[id].label)||id,color:(byId[id]&&byId[id].color)||COLORS[id]};});}" +
    "function spiderSvg(axes,meta){var n=axes.length,max=maxOf(axes),compact=isEmbed;" +
    "var cx=compact?160:220,cy=compact?145:200,r=compact?112:158,labelR=compact?r+16:r+22;" +
    "var fontSize=compact?10:12,strokeW=compact?2:2.5,vbW=compact?320:440,vbH=compact?290:400,rings=4,g='';" +
    "for(var k=1;k<=rings;k++){var pts=[];for(var i=0;i<n;i++){var ang=-Math.PI/2+(i*2*Math.PI/n),rr=r*k/rings;" +
    "pts.push((cx+Math.cos(ang)*rr).toFixed(1)+','+(cy+Math.sin(ang)*rr).toFixed(1));}" +
    "g+='<polygon points=\"'+pts.join(' ')+'\" fill=\"none\" stroke=\"#2c3a48\" stroke-width=\"1\"/>';}" +
    "axes.forEach(function(ax,i){var ang=-Math.PI/2+(i*2*Math.PI/n);" +
    "g+='<line x1=\"'+cx+'\" y1=\"'+cy+'\" x2=\"'+(cx+Math.cos(ang)*r).toFixed(1)+'\" y2=\"'+(cy+Math.sin(ang)*r).toFixed(1)+'\" stroke=\"#2c3a48\"/>';" +
    "g+='<text x=\"'+(cx+Math.cos(ang)*labelR).toFixed(1)+'\" y=\"'+(cy+Math.sin(ang)*labelR).toFixed(1)+'\" fill=\"#93a4b5\" font-size=\"'+fontSize+'\" text-anchor=\"middle\">'+esc(ax.label)+'</text>';});" +
    "meta.forEach(function(s){var pts=axes.map(function(ax,i){var ang=-Math.PI/2+(i*2*Math.PI/n),rr=r*(+ax[s.id]||0)/max;" +
    "return (cx+Math.cos(ang)*rr).toFixed(1)+','+(cy+Math.sin(ang)*rr).toFixed(1);}).join(' ');" +
    "g+='<polygon points=\"'+pts+'\" fill=\"'+s.color+'\" fill-opacity=\"0.22\" stroke=\"'+s.color+'\" stroke-width=\"'+strokeW+'\"/>';});" +
    "return '<svg class=\"spider\" viewBox=\"0 0 '+vbW+' '+vbH+'\" role=\"img\" aria-label=\"'+period+' spider\">'+g+'</svg>';}" +
    "function barsSvg(axes,meta){var max=maxOf(axes),compact=isEmbed,w=compact?320:440,h=compact?180:260,padL=22,padB=28,padT=12;" +
    "var groupW=(w-padL-12)/axes.length,barW=Math.min(compact?18:28,(groupW-10)/3);" +
    "var g='<line x1=\"'+padL+'\" y1=\"'+(h-padB)+'\" x2=\"'+(w-6)+'\" y2=\"'+(h-padB)+'\" stroke=\"#2c3a48\"/>';" +
    "axes.forEach(function(ax,i){var gx=padL+i*groupW+6;meta.forEach(function(s,si){var val=+ax[s.id]||0,bh=(h-padB-padT)*val/max;" +
    "var x=gx+si*(barW+2),y=h-padB-bh;g+='<rect x=\"'+x.toFixed(1)+'\" y=\"'+y.toFixed(1)+'\" width=\"'+barW.toFixed(1)+'\" height=\"'+bh.toFixed(1)+'\" fill=\"'+s.color+'\" rx=\"2\"/>';});" +
    "g+='<text x=\"'+(gx+barW).toFixed(1)+'\" y=\"'+(h-10)+'\" fill=\"#93a4b5\" font-size=\"'+(compact?10:12)+'\" text-anchor=\"middle\">'+esc(ax.label)+'</text>';});" +
    "return '<svg class=\"spider\" viewBox=\"0 0 '+w+' '+h+'\" role=\"img\">'+g+'</svg>';}" +
    "function renderChips(){var box=document.getElementById('chips');box.innerHTML='';" +
    "PERIODS.forEach(function(p){var b=document.createElement('button');b.type='button';b.className='chip';b.textContent=p;" +
    "b.setAttribute('aria-pressed',p===period?'true':'false');b.onclick=function(){period=p;renderChips();render();};box.appendChild(b);});}" +
    "function render(){var block=(doc.periods||{})[period]||{axes:[]};var axes=block.axes||[];var meta=seriesMeta();" +
    "document.getElementById('legend').innerHTML=meta.map(function(s){return '<span><i class=\"sw\" style=\"background:'+s.color+'\"></i>'+s.id+' '+s.label+'</span>';}).join('');" +
    "var mode=document.getElementById('mode'),chart=document.getElementById('chart');" +
    "if(axes.length>=3){mode.textContent=period+' · spider ('+axes.length+' axes).';chart.innerHTML=spiderSvg(axes,meta);}" +
    "else if(axes.length){mode.textContent=period+' · grouped bars ('+axes.length+' axes).';chart.innerHTML=barsSvg(axes,meta);}" +
    "else{mode.textContent='No axes for '+period+'.';chart.innerHTML='';}" +
    "var tb=document.querySelector('#tbl tbody'),table=document.getElementById('tbl');tb.innerHTML='';" +
    "var rows=isEmbed?axes.slice(0,3):axes;" +
    "rows.forEach(function(ax){var tr=document.createElement('tr');tr.className='nums';" +
    "tr.innerHTML='<td>'+esc(ax.label)+'</td><td>'+ax.A+'</td><td>'+ax.B+'</td><td>'+ax.C+'</td>';tb.appendChild(tr);});" +
    "table.hidden=isEmbed||axes.length===0;}" +
    "document.getElementById('title').textContent=(doc.schoolName||(daily&&daily.schoolName)||'Radar')+' · attendance';" +
    "var h=doc.headline||{};var cls=(daily&&daily.classLabel)||doc.classLabel||'';var date=(daily&&(daily.attendanceDate||daily.asOf))||doc.asOf||'';" +
    "var snap=(daily&&daily.snap!=null)?daily.snap:h.A;var conf=(daily&&daily.confirmed!=null)?daily.confirmed:h.B;var semis=(daily&&daily.semis!=null)?daily.semis:h.C;" +
    "var gap=(daily&&daily.gapPct!=null)?daily.gapPct:null;if(gap==null&&semis>0&&(conf!=null||snap!=null)){var pr=conf!=null?conf:snap;gap=Math.round(((semis-pr)/semis)*1000)/10;}" +
    "document.getElementById('sub').textContent=['EMIS '+emis,cls,date,snap!=null?('snap '+snap+' / confirmed '+conf+' / SEMIS '+semis):''].filter(Boolean).join(' · ');" +
    "var cells=[];" +
    "if(cls)cells.push('<div class=\"metric\"><div class=\"lbl\">Class</div><div class=\"val\">'+esc(cls)+'</div></div>');" +
    "if(date)cells.push('<div class=\"metric\"><div class=\"lbl\">Date</div><div class=\"val\">'+esc(date)+'</div></div>');" +
    "if(snap!=null)cells.push('<div class=\"metric\"><div class=\"lbl\">A · Snap</div><div class=\"val a\">'+fmt(snap)+'</div></div>');" +
    "if(conf!=null)cells.push('<div class=\"metric\"><div class=\"lbl\">B · Confirmed</div><div class=\"val b\">'+fmt(conf)+'</div></div>');" +
    "if(semis!=null)cells.push('<div class=\"metric\"><div class=\"lbl\">C · SEMIS</div><div class=\"val c\">'+fmt(semis)+'</div></div>');" +
    "if(gap!=null)cells.push('<div class=\"metric\"><div class=\"lbl\">Gap vs SEMIS</div><div class=\"val gap\">'+fmt(gap)+'%</div></div>');" +
    "document.getElementById('metrics').innerHTML=cells.join('');" +
    "document.getElementById('foot').textContent=doc.valuesNote||(h.note)||'Google-hosted HtmlService · no Cloudflare · no PNG';" +
    "renderChips();render();" +
    "</script></body></html>"
  );
}
