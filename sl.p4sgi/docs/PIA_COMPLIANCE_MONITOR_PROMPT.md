# PROMPT: PIA Compliance Monitor (GAM + Vault + DHIS2) for MBSSE Sierra Leone

> Hand this whole file to the implementer (Grok / Claude Code). It is self-contained.
> Source of truth: `PIA_Tool_ULID_SierraLeone_Oct_v6.xlsx` (11 sheets, assessed 2026-10-05, prepared by D. Dillé & E. Adu-Gyamfi, ref LRPS-2025-9200546).
> Target repo: `program4results/sl-p4sgi` (stack `sl.p4sgi`, currently 0.4.19). Build on a feature branch; open a PR; do not touch `master`.

## 0. Role and objective

You are a senior engineer adding a **read-only privacy-compliance monitor** to the existing p4sgi dashboard.
It must (1) load the agreed PIA workbook as the control framework, (2) continuously collect **evidence** of whether each PIA control is actually working at national, district and school level, and (3) show a **radar dashboard that triangulates** the *ideal compliant ministry* against *what MBSSE declares* against *what is observed in districts and schools*.

The point of the product is the **gap between claim and evidence**. It must never flatter: no data is shown as "no data", never as 0 or 100.

## 1. Ground truth from the workbook (verified by reading every sheet)

| Fact | Value |
|---|---|
| Sheets | Instructions, 1 Project Overview, **3 Country Assessment (appears before 2)**, 2 Data Inventory, 4 Risk Register, 5 Stakeholder Consultation, 6 Mitigation Tracker, 7 Legal Context, 8 User Access, 9 Scoring Guide, 10 Sign-off |
| Document status | **DRAFT** ("not certified or legally-approved"); legal basis is the *Data Protection and Right to Access Information Bill, 2025*, **not enacted** (no assent, no commencement date). Applied voluntarily. |
| Controls (tab 3) | **40**, IDs A01-A40. **All 40 "Not started"; 0 evidence, 0 owner, 0 target date filled.** |
| Risks (tab 4) | **18** (R1-R18). Inherent: 7 Critical, 6 High, 5 Medium. **Declared residual: 1 Critical (R16), 3 High (R1, R10, R11), 12 Medium, 2 Low.** |
| Actions (tab 6) | **16** (ACT-01..16): 3 Critical, 8 High, 5 Medium. **All 16 "Not started"; all target dates are the placeholder `[Enter date]`.** |
| Residual-vs-reality | 12 risks (R1,R2,R3,R5,R6,R7,R8,R9,R12,R13,R14,R18) carry residual < inherent while **every linked action is Not started**; R4, R15, R17 have a reduced residual and **no linked action at all**. Declared residual therefore reflects *planned* design, not evidence. |
| Stakeholders (tab 5) | 13, all "Not started" (incl. parents/PTAs, NCRA, WAEC, TSC, Ministry of Justice) |
| Data inventory (tab 2) | 16 data elements; column I "Retention approved / Not approved" empty for all 16 |
| Sign-off (tab 10) | 6 roles, names/dates empty, overall decision = `[Select]` |
| Pilot scope | Western Area Rural, Bombali, Kailahun, Moyamba; national scale-up = all 16 districts |
| Processors/systems | HISP UiO, HISP WCA, Saudigitus, CGA Technologies (Wi De Ya), SurveyCTO, TSC/TMIS, SQAMR, NCRA, WAEC, **Google Workspace for Education ("optional")** |

**Do not assume "everyone agreed".** The workbook records no sign-off and no completed consultation. The module must treat *agreement* as an evidenced state (a signed record in tab 10 / a signed document in the evidence vault), show it as such, and show "not evidenced" until it exists.

### Workbook defects the importer must handle (do not silently "fix")
1. Tab 3 Status dropdown = `Not started, Scheduled, In progress, Completed`, but its conditional formatting keys on `Compliant / Partial / Gap / To validate`. These are two different things. Model **`workflow_status`** and **`compliance_state`** separately; never infer compliance from workflow.
2. Tab 4 R15 status `Accepted (existing practice)` is outside the tab's own dropdown (`Accepted`). Import verbatim, normalise to `Accepted` + keep the note, flag as `data_quality`.
3. Tab 6 dates are the literal string `[Enter date]`; treat as null, count as "undated".
4. Tab 9 spans 1,000 rows, mostly empty; read only populated cells. Scoring bands **must be read from tab 9**, not hardcoded: Low 1-4, Medium 5-9, High 10-15, Critical 16-25; the tab-4 formulas agree (>=16/>=10/>=5).
5. Sheet order is not numeric (3 before 2). Key everything by sheet *title prefix*, not position.
6. The workbook is versioned by file name (v6). Store the SHA-256 and diff each import against the previous one (added/removed/changed IDs).

## 2. What GAM can and cannot see (design consequence, read this twice)

A GAM-only module observes **Google Workspace** only. The PIA's crown jewels (SEMIS enrolment, ULID, NIN/biometric linkage, retention, DHIS2 roles) live in **DHIS2** and partner systems, and the PIA itself calls Google Workspace *optional* (R7, ACT-12, tab-5 Google row = "Conditional"). From the repo, Workspace is already in production (school email, attendance Docs, Sites, ~29 domains), so those "if adopted" conditions look **already triggered**. Surface that contradiction as a finding; do not paper over it.

Of the 40 controls: **15 have a GAM/Workspace signal, 15 have a DHIS2 signal, 12 are fully automatic, 23 semi-automatic, 5 manual.** Therefore build **five collectors**, each emitting the same `observation` record:

| Collector | Reads | Notes |
|---|---|---|
| **G - GAM / Workspace** | cached allowlisted reports: `users_full`, `admins`, `groups`, `ous`, `login_activity`, `token_activity`, `drive_activity`, `devices_mobile/ci/cros`, `gemini_activity`, `notebooklm_activity`; plus *new* allowlisted read-only reports you add to `scripts/run_gam_report.sh` (e.g. external group members, Vault matters/holds/exports, Alert Center). | Reuse 0.4.19 `security_audit.py` analysers (device compliance, 2SV, admin flags). **Verify every GAM7 command on one account before relying on it.** |
| **V - Evidence vault** | A Drive folder tree (one folder per control, e.g. `A19_access-review/`), file naming `A19_<slug>_<YYYYMMDD>`; metadata via Drive `properties`/description: `approved_by`, `valid_until`, `scope`. Plus **Google Vault** (holds, matters, exports) where licensed. | Existence + freshness + approval are measurable; content is not read. To my knowledge Vault *retention rules* are not exposed by the Vault API, so treat them as attested unless your GAM7 build proves otherwise. Confirm the Vault licence tier. |
| **D - DHIS2 (read-only)** | Metadata API (data elements, attributes, user roles/groups, org-unit assignments, 2FA flag, superusers), aggregate counts via analytics, audit-log availability. | A scoped read-only token. **Never pull individual learner records into this DB**; count server-side. |
| **S - this stack** | `attendance_sync_events`, `publish_events`, `devices`, `telemetry_events`, radar/attendance JSON in `DATA_DIR`, published Docs. | Check payloads for direct identifiers (note `absent_named` exists in `attendance_sync_events`: verify whether names reach any published page). |
| **W - web check** | Public school Sites / enrolment pages we own | Privacy notice present, language, version. |
| **A - attestation** | A form a district officer / head teacher submits, signed, with optional evidence upload | Always labelled **Attested**, never **Verified**. |

Every observation carries `confidence`: `verified` (machine-measured) > `evidenced` (approved, in-date document) > `attested` (self-declared) > `missing`.

## 3. Control catalogue (generated from tab 3; implement exactly these IDs)

Axes are the radar spokes. `Auto?`: Auto = machine can decide; Semi = machine checks existence/freshness/proxy, human approves; Manual = attestation. `Scopes`: N = national only; N/D/S = also measurable per district and per school.

| ID | Control (PIA tab 3, abridged) | Axis | Evidence | Auto? | Scopes | Bill ref (as drafted) | What the module measures |
|---|---|---|---|---|---|---|---|
| A01 | Has MBSSE formally identified the Data Controller and accountable s... | 1 Governance | V | Semi | N | Part VIII / Sec 27 | Controller + accountable official named in a signed record (tab 10 / register doc) |
| A02 | Has a Data Protection / Privacy focal point or equivalent function ... | 1 Governance | V | Semi | N | Part VIII | DPO / focal-point designation letter exists, dated, signed (R16, ACT-01) |
| A03 | Is the lawful basis for each major processing activity documented a... | 2 Legal basis & children | V | Semi | N | Secs 38-39 | Lawful-basis register per processing activity (doc, approved) |
| A04 | Has MBSSE Legal confirmed the current status and applicability of S... | 2 Legal basis & children | V | Semi | N | - | Legal-status opinion dated; Bill status field Bill/Assented/Commenced |
| A05 | Is processing of children's data supported by the required legal/co... | 2 Legal basis & children | D+V | Semi | N/D/S | Sec 36 | Consent/assent protocol doc + % learners with guardian-notice/consent flag in SEMIS |
| A06 | Is a clear privacy notice provided at or before collection, in an a... | 3 Transparency & rights | W+V | Semi | N/D/S | Sec 28 | Privacy notice present on enrolment screen / school Site, language(s), version |
| A07 | Has every collected data field a documented purpose and necessity | 4 Minimisation & retention | D | Auto | N | Sec 27 | DHIS2 data elements/attributes vs the 16 approved inventory items (extras = breach) |
| A08 | Are ULID/NIN data prevented from being reused for incompatible purp... | 4 Minimisation & retention | D+V | Manual | N | Sec 40 | API consumers/joins vs DSA purposes; NIN used outside approved joins |
| A09 | Is there a documented process for correcting inaccurate learner rec... | 3 Transparency & rights | S+D | Semi | N/D/S | Sec 27 / Part VII | Correction requests: count, median days to close |
| A10 | Can learners/parents/guardians request access, correction or other ... | 3 Transparency & rights | S | Semi | N/D/S | Part VII | Rights-request log: open, overdue, closed; channel published |
| A11 | Is there an approved retention and disposal schedule for each data ... | 4 Minimisation & retention | D+V | Semi | N/D/S | Sec 41 | Records past retention still identifiable; approved schedule coverage (tab 2 col I) |
| A12 | Are long-term analytical/statistical datasets anonymised or appropr... | 4 Minimisation & retention | D+S | Semi | N | Sec 41 | Analytics/public datasets contain no direct identifiers |
| A13 | Is the ULID non-specific and free of names, DOB, sex, school, distr... | 5 Identity design | D | Auto | N | - | ULID values pass format test: no name/DOB/sex/school/district/NIN encoded |
| A14 | Is NIN linkage separately justified, authorised and limited to nece... | 5 Identity design | D+V | Semi | N | Sec 40 | Who/what can read NIN attribute; separate authorisation doc |
| A15 | If biometric data are involved, are they processed only by the auth... | 5 Identity design | D | Auto | N | Secs 30-31 | No fingerprint/photo attributes or file resources exist in the Hub (target 0) |
| A16 | Is every external sharing arrangement supported by documented purpo... | 6 Sharing & third parties | V+G | Semi | N | Sec 40 / 59-60 | Signed DSA per partner (NCRA, TSC, WAEC, SurveyCTO, SQAMR, Google) + external-share events |
| A17 | Are processors bound by written agreements, confidentiality, securi... | 6 Sharing & third parties | V+G | Semi | N | Secs 59(4)-(5), 60 | DPAs with HISP UiO/WCA, Saudigitus, CGA; external-domain accounts and roles |
| A18 | Has each external hosting/processing location been assessed and app... | 6 Sharing & third parties | A+V | Manual | N | Sec 42 | Hosting/processing location register + approval record (hosting provider still TBD) |
| A19 | Is access role-based, least-privilege, named-user and approved | 7 Access control | G+D | Auto | N/D/S | Secs 59-60 | Actual roles vs tab-8 matrix; shared/generic accounts; school multi-scope; DHIS2 role mismatch |
| A20 | Are administrator and technical-provider accounts restricted, logge... | 7 Access control | G+D | Auto | N | Secs 59-60 | Super/delegated admins, external admins, no 2SV, no expiry (DHIS2 superusers) |
| A21 | Are user accounts and privileges reviewed at least quarterly | 7 Access control | G+A | Semi | N/D/S | Secs 59-60 | Quarterly review record age; dormant-but-active accounts |
| A22 | Are strong authentication controls applied to privileged and sensit... | 7 Access control | G+D | Auto | N/D/S | Secs 59-60 | 2SV enrolled/enforced % (privileged first); DHIS2 2FA |
| A23 | Are authentication, lookup, edit, merge, export and administrative ... | 8 Logging & monitoring | G+D | Auto | N | Secs 59-60 | Audit sources present & non-empty (login, token, drive, admin); DHIS2 audit on |
| A24 | Are security logs reviewed and suspicious activity investigated | 8 Logging & monitoring | A+G | Manual | N | Secs 59-60 | Evidence of log review + Alert Center rules; investigation records |
| A25 | Is personal data protected in transit and appropriately protected a... | 9 Security, devices & incident | G+A | Semi | N/D/S | Secs 59-60 | Device encryption %; hosting/backup encryption attestation |
| A26 | Are tablets/devices protected by authentication, encryption, secure... | 9 Security, devices & incident | G+S | Auto | N/D/S | Secs 59-60 | Tablet compliance (0.4.19 devices), stale sync, passcode, dev mode, loss/theft wipe |
| A27 | Are bulk learner-level exports restricted, approved, logged and min... | 8 Logging & monitoring | G+D | Auto | N/D/S | Secs 59-60 | Bulk download/export events per user (Drive audit; DHIS2 export audit) |
| A28 | Are uncertain matches reviewed by authorised staff and merges logge... | 5 Identity design | D | Semi | N | - | Merge/uncertain-match log exists, reviewer named, merges reversible |
| A29 | Is there a documented personal-data breach and security incident pr... | 9 Security, devices & incident | V | Semi | N | Sec 61 | Incident procedure doc approved; last drill date |
| A30 | Are internal escalation, regulator/authority and data-subject notif... | 9 Security, devices & incident | V | Semi | N | Sec 61 | Escalation matrix incl. 48h processor->controller, 72h->Authority timers |
| A31 | Are backups tested and protected against unauthorised access or loss | 9 Security, devices & incident | A | Manual | N | Secs 59-60 | Backup restore-test record age |
| A32 | Are application, server, dependency and device vulnerabilities iden... | 9 Security, devices & incident | G+A | Semi | N/D/S | Secs 59-60 | OS/security-patch age distribution; vulnerability scan reports |
| A33 | Where AI/predictive analytics are used, are human review, bias, exp... | 10 AI, publication & training | G+V | Semi | N/D/S | Sec 47 | Gemini/NotebookLM use by OU; human-in-loop SOP; automated flags with no human review |
| A34 | Are aggregation, suppression and disclosure-control rules applied t... | 10 AI, publication & training | S+G+W | Auto | N/D/S | Sec 27 | Published pages/JSON: no identifiers, no cell < k, no public links to learner data |
| A35 | Have staff handling learner data completed privacy, confidentiality... | 10 AI, publication & training | V+G | Semi | N/D/S | Sec 27 | Training register matched to staff list (users_full): % trained |
| A36 | Are key procedures approved and used: registration, access, rights,... | 1 Governance | V | Semi | N | Sec 27 | SOP set present, approved, within review age |
| A37 | Can MBSSE produce evidence of controls, approvals, logs, agreements... | 1 Governance | computed | Auto | N | Sec 27 | % of controls with fresh, approved evidence (computed by the module) |
| A38 | Does a material system/data-sharing change trigger DPIA/PIA review ... | 1 Governance | V+S | Manual | N | Sec 35 | System/sharing change log vs PIA review date (release tags, new processors) |
| A39 | Is there a documented channel for privacy complaints and escalation | 3 Transparency & rights | W+S | Semi | N/D/S | Part VII | Complaint channel published + complaints log |
| A40 | Are all critical/high gaps closed or formally accepted before natio... | 1 Governance | computed | Auto | N | Sec 35 | Scale-up gate computed from risk register + tracker (see Gate) |

**Axis coverage per scope** (how many controls can possibly be observed; radar shows hatched where coverage is 0):

| Axis | Controls | Observable per district/school | Fully automatic | Radar district/school polygon |
|---|---|---|---|---|
| 1 Governance | 6 | 0 | 2 | hatched (no school-level evidence) |
| 2 Legal basis & children | 3 | 1 | 0 | drawn from 1 control |
| 3 Transparency & rights | 4 | 4 | 0 | drawn from 4 controls |
| 4 Minimisation & retention | 4 | 1 | 1 | drawn from 1 control |
| 5 Identity design | 4 | 0 | 2 | hatched (no school-level evidence) |
| 6 Sharing & third parties | 3 | 0 | 0 | hatched (no school-level evidence) |
| 7 Access control | 4 | 3 | 3 | drawn from 3 controls |
| 8 Logging & monitoring | 3 | 1 | 2 | drawn from 1 control |
| 9 Security, devices & incident | 6 | 3 | 1 | drawn from 3 controls |
| 10 AI, publication & training | 3 | 3 | 1 | drawn from 3 controls |

Bill references are as the workbook cites them ("as drafted"); never describe the Bill as law in any label, tooltip or export. Use "aligned with the Bill (voluntary)" until a legal-status record says Assented/Commenced.

## 4. Per-tab monitoring spec (capture, measure, alert)

**Instructions / Legend** - store workbook version + SHA, the DRAFT banner text, and show the banner on every dashboard view until the legal-status record changes.

**1 Project Overview** - build registries: `systems` and `processors` (names above), `districts` (16; pilot flag for the 4), hosting provider (currently unconfirmed; alert while empty), legal basis text, assessment date (2026-10-05), "recommended next review". Computed triggers (each raises a banner + a re-assessment task): Bill assented/commenced; scale-up beyond the 4 pilot districts; new processor; hosting confirmed; AI module enabled.

**2 Data Inventory** (16 elements) - for each element monitor: (a) *minimisation*: DHIS2 attributes/data elements not in the inventory = finding; inventory items not present = note; (b) *retention*: clock = enrolment end + 3y (per column H), count identifiable records past it per district; approved/not approved (column I) coverage; (c) *pseudonymisation*: no names in analytics/public datasets; (d) *sharing*: actual consumers vs column K; (e) *biometrics*: assert zero fingerprint/photo attributes or file resources in the Hub (items 5, 6); (f) *small cells*: disability (item 7), attendance, sex counts suppressed below `K_MIN` (config, default 10; **needs MBSSE decision**).

**3 Country Assessment** - the 40 controls above. Per control and scope store `workflow_status` (from workbook), `compliance_state` (module-computed), `evidence_ref`, `gap`, `corrective_action`, `owner`, `target_date`, `last_evaluated`, `valid_until`.

**4 Risk Register** - recompute scores exactly (L x I, bands from tab 9). Show **three** ratings per risk: *inherent*, *declared residual*, **evidence-adjusted residual** = declared residual only if every linked action is Complete **and** the related KRI is in tolerance; otherwise the inherent score. Attach a KRI to each risk:

| Risk | KRI (target) | Source |
|---|---|---|
| R1 no enacted law | Bill status field; days since legal review; ACT-02 state | V |
| R2 biometrics | biometric attributes in Hub (=0); NCRA DSA doc present | D+V |
| R3 children / consent | % learners with guardian notice/consent flag; consent protocol doc (ACT-03) | D+V |
| R4 disability data | users with PII view of disability; published disability cells < K_MIN (=0) | D+S |
| R5 NIN function creep | accounts/systems able to read NIN; joins on NIN outside approved DSAs | D |
| R6 sharing w/o agreements | partners with signed DSA / 6; external shares to unlisted domains | V+G |
| R7 Google cross-border | Workspace in production? DPA signed? data-region record | G+V |
| R8 AI / automated decisions | Gemini/NotebookLM use by OU; human-review SOP; flags actioned with no human log | G+V+D |
| R9 offline devices | % tablets compliant / encrypted / locked; mean days since sync; unwiped lost devices | G+S |
| R10 retention | identifiable records past retention; approved schedule coverage (0/16 today) | D+V |
| R11 data-subject rights | open/overdue requests; median days to close; channel published | S+W |
| R12 vendor access | external-domain admins/roles w/o expiry or 2SV; partner DHIS2 users with PII view | G+D |
| R13 cyber | 2SV on privileged accounts; patch age; suspicious-login flags; restore-test age | G+A |
| R14 re-identification | published cells < K_MIN (=0), direct identifiers on published pages (=0) | S |
| R15 teacher biometrics | alternatives-assessment doc; accepted-risk review date | V |
| R16 no DPO | DPO designated? days since assessment without one | V |
| R17 registration readiness | processor register completeness (contract, security contact, DPO) | V |
| R18 PIA not submitted | sign-off completeness; Sec. 35(3) 60-day timer (only once an Authority exists) | V |

Flag integrity issues: risks with no linked action (today R4, R15, R17), actions linked to nothing, residual < inherent with no completed action.

**5 Stakeholder Consultation** - 13 stakeholders; evidence = dated minutes/attendance in the vault. Flag `Required = Yes` with status Not started older than `CONSULT_SLA_DAYS` (config). Parents/PTAs, MoJ and NCRA are the critical path (R3, R1, R2).

**6 Mitigation Tracker** - 16 actions: overdue (once dated), undated, blocked, priority-vs-risk consistency (a Critical risk whose actions are all Medium). Burn-down by priority. Gate input.

**7 Legal Context** - a `legal_status` record {Bill | Assented | Commenced, date, source doc}; tag every control with its Bill section; timers: breach (processor->controller 48h, ->Authority 72h, Sec. 61 as drafted), DPIA submission 60 days (Sec. 35(3)). Timers are inert until status = Commenced.

**8 User Access Matrix** - machine-readable policy: 11 roles x (View PII, Create, Edit, Adjudicate, Export PII, Analytics, Audit logs, Approval condition). Compare to **actual** roles in Workspace (admin roles, groups, OU) and DHIS2 (roles, groups, org-unit scope, superuser). Findings include: school-wide shared login instead of named user (each school appears to use one school mailbox), school user scoped to >1 school, district role with bulk export, analyst with PII view and no approval record, external processor without expiry, privileged account without 2SV, access not reviewed within 90 days. Treat the shared-login point as a **hypothesis to test**, not a fact.

**9 Scoring Guide** - load scales and bands; render the 5x5 heatmap with risks plotted at inherent, declared-residual and evidence-adjusted positions; map band -> response expectation as the **gate**: Critical = must be addressed before approval/go-live; High = before go-live of the affected component.

**10 Sign-off** - 6 sign-off roles + overall decision + conditions. State machine: `Draft -> In review -> Signed (all 6) -> Decision recorded`. Overall decision options: Proceed / Proceed with conditions / Do not proceed. Dashboard shows "Agreement: not evidenced" until signatures exist.

## 5. Scoring and triangulation (the radar)

Per control and scope: `state` in {`verified`, `evidenced`, `attested`, `partial`, `gap`, `no_data`} with weights (configurable, needs MBSSE sign-off): verified 1.0, evidenced 0.75, attested 0.4, partial = measured fraction capped 0.9, gap 0, no_data = excluded. Evidence past `valid_until` counts half; two periods past counts 0.
Axis score = weighted mean over *observable* controls; always show **coverage** = observable / total controls on the axis.

Radar series (10 spokes = axes in section 3), all selectable, same scale 0-100:

1. **Ideal** - 100 on every axis (every control verified-compliant). Reference ring, not data.
2. **MBSSE declared** - from the workbook: Completed=1.0, In progress=0.5, Scheduled=0.25, Not started=0 (today: 0 everywhere; that is correct, show it).
3. **National observed** - HQ-level controls from collectors G, V, D, S.
4. **District observed** - median of its schools + the district-own controls; pick one of 16 (pilot 4 highlighted).
5. **School observed** - median and worst quartile within the selected district; pick one school.
6. *(optional)* **Go-live threshold** - configurable minimum per axis (default off; MBSSE must set it).

Triangulation readouts (computed, labelled, one tile each):
- **Ambition gap** = Ideal - Declared.
- **Assurance gap** = Declared - Observed (positive = claims not borne out). Flag any control declared Completed but observed gap/no_data.
- **Equity spread** = max-min district score per axis (and Gini across schools).
- **Tail risk** = worst-quartile school vs district median.
- **Evidence freshness** = % evidence in date; **coverage** = observable controls / 40.

Other views: district x axis heatmap (16 x 10); 5x5 risk matrix (3 positions per risk); gate panel ("Go-live gate: BLOCKED/OPEN" with the Critical/High risks and actions holding it); trend line per axis from snapshots; evidence expiry list; sign-off status; workbook data-quality list.

Show a **school or district only to users scoped to it** (reuse the existing domain scope). HQ super-admins see all. Never rank-shame schools publicly: school identities appear only in scoped views and in the controls' own exports.

## 6. Privacy of the monitor itself (non-negotiable)

- Store **no learner PII**. Findings carry counts, ids of systems/accounts/files, never names, DOB, NIN, addresses. File names may contain learner names: mask outside the owner's scope.
- Staff data (logins, devices, roles) is personal data: collect the minimum, keep 13 months of snapshots by default (config), and log who viewed what.
- Apply small-cell suppression (`K_MIN`) **to this dashboard** (R14 applies to us).
- Read-only against Google Workspace, DHIS2 and the DB of other systems. Any remediation is a **dry-run plan** for a human (same pattern as `/api/v1/security/actions/plan` in 0.4.19).
- Add the monitor to the PIA itself as a proposed new processing activity/risk (suggest R19 "compliance monitor processes staff and device data"); do not edit the workbook automatically.

## 7. Architecture, constrained to this repo (do not break the working platform)

Follow the 0.4.19 pattern exactly:
- New module `apps/api/app/privacy_monitor.py` (+ small sub-modules if needed) exposing `build_router(Helpers)`; **one guarded `include_router` block appended to `main.py`**, kill switch `PRIVACY_MONITOR_ENABLED=0`. A crash in the module must not stop the API (test it).
- Reuse helpers passed in from `main.py` (`_insight_scope`, `_load_sources`, `_in_scope`, `_row_email`, `_request_scope`, `is_superadmin`); do not import `main` from the module.
- DB: additive tables in `apps/api/sql/init.sql` using `CREATE TABLE IF NOT EXISTS`. `db._split_sql` splits on `;` and strips `--` comments: **no `$$` function bodies, no semicolons in strings.** Suggested tables: `pia_workbooks`, `pia_controls`, `pia_risks`, `pia_actions`, `pia_stakeholders`, `pia_inventory`, `pia_access_policy`, `pia_signoff`, `pia_legal_status`, `pia_observations` (control_id, scope_type, scope_id, state, confidence, measured, evidence_ref, collected_at, valid_until, run_id), `pia_snapshots` (axis scores per scope per day). Keep raw collector files under `DATA_DIR/privacy/`.
- New GAM reports go in **`scripts/run_gam_report.sh` at the repo's top-level `scripts/`** (that is the copy compose mounts at `/scripts`; the copy under `apps/api/scripts` is older and stale). Allowlist only, no client strings to a shell, domain validation as existing.
- UI: one collapsed panel in `apps/api/static/index.html` using the existing `section-toggle` pattern, loads on click; `apps/web` is a stale copy: do not edit. Bump version consistently (API `APP_VERSION`, meta tag, `uiVersion`, `UI_VERSION`).
- Radar drawn in inline SVG (no CDN), dark theme, works at phone width; colour-blind safe (pattern + label, not colour alone); hatched = no data.

Endpoints (all `GET` unless noted, all scoped, all return a `sources`/`freshness` block):
```
POST /api/v1/privacy/pia/import            multipart xlsx -> validate, version, diff, data_quality list (super-admin)
GET  /api/v1/privacy/pia/summary           counts, banners, gate state, agreement state
GET  /api/v1/privacy/controls?axis=&scope= control table with state/confidence/evidence
GET  /api/v1/privacy/risks                 inherent / declared / evidence-adjusted + KRIs
GET  /api/v1/privacy/radar?scope=national|district|school&id=&series=   10-axis scores + coverage
GET  /api/v1/privacy/triangulation?district=   ambition / assurance / equity / tail tiles
GET  /api/v1/privacy/heatmap               district x axis
GET  /api/v1/privacy/gate                  go-live gate with blocking items
POST /api/v1/privacy/attestations          district/school self-assessment (named, dated)
POST /api/v1/privacy/collect/{collector}   run a collector now (super-admin; async, cached)
GET  /api/v1/privacy/status                limits, collectors health, thresholds
```

## 8. Delivery plan and acceptance criteria

| Phase | Deliver | Done when |
|---|---|---|
| 0 | Importer + baseline views from the workbook only | Importing v6 reproduces: 40 controls Not started, 18 risks (7C/6H/5M inherent), 16 undated actions, gate = BLOCKED, agreement = not evidenced, declared radar = 0, and raises these data-quality findings: status-vocabulary mismatch (tab 3), R15 status outside dropdown, 16 placeholder dates, 3 risks with no linked action (R4, R15, R17), 15 risks whose residual is below inherent with no completed action. |
| 1 | G collector for the 15 GAM-signal controls (reuse 0.4.19) | Each of those controls shows verified/partial/gap from cached CSVs; missing reports show "run report X". |
| 2 | Evidence vault + attestations + Sign-off state machine | Uploading a dated, approved file moves a control to `evidenced`; expiry degrades it; attestation never shows as verified. |
| 3 | D collector (DHIS2, read-only) | A07/A13/A15/A19/A20/A22/A23 measured from metadata; zero individual records stored. |
| 4 | Radar + triangulation + district heatmap + gate + trend | All five series render; assurance gap flags declared-Completed/observed-gap; hatched where no data. |
| 5 | Legal triggers + timers + R19 proposal | Changing `legal_status` to Commenced relabels the module and arms timers; nothing else changes. |

Tests (pytest, no DB/GAM/network, same harness as `tests/test_security_audit.py`): importer fixtures incl. all six defects; scoring bands vs tab 9; evidence-adjusted residual rule; confidence weighting and expiry; coverage/no-data never plotted as 0; scoping (district user cannot read another district/school); no learner PII in any response or stored row (fuzz with names/DOB/NIN patterns); route-table diff vs `master` = no routes removed or changed; kill switch removes all `/api/v1/privacy/*`; simulated module crash still boots; headless-browser render with console-error diff vs `master`.

## 9. Do not

- Do not mark anything "compliant" from workflow status alone, or from attestation alone.
- Do not describe the Bill as law, or the workbook as certified, or the PIA as agreed, without a recorded basis.
- Do not execute GAM writes or DHIS2 writes. Do not store or display learner personal data. Do not edit the PIA workbook.
- Do not apply Grok's earlier docker-compose/Dockerfile/requirements/.env (different stack, `docker.sock` mount).
- Do not claim a GAM command works until it has been run on one real account; state which were not run.

## 10. Open decisions to record (ask the product owner; defaults in brackets)

1. "Vault" = Google Vault, an evidence Drive vault, or both? [both]
2. Workspace edition and Vault licence tier, and whether Workspace is formally "adopted" for the PIA (R7/ACT-12). [assume yes, flag]
3. DHIS2 base URL, version, and who issues a scoped read-only token. [none yet: Phase 3 blocked]
4. `K_MIN` small-cell threshold and `CONSULT_SLA_DAYS`. [10; 30]
5. Evidence weights and the go-live threshold per axis: need MBSSE approval. [defaults above]
6. Who is the named DPO/focal point (ACT-01)? The dashboard needs an owner to notify. [unknown]
