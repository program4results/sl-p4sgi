# Privacy compliance monitor (PIA) — 0.4.20, EXPERIMENTAL

Tracks the Sierra Leone ULID Privacy Impact Assessment (`PIA_Tool_ULID_SierraLeone_Oct_v6.xlsx`: 40 controls,
18 risks, 16 actions, 13 stakeholders, 6 signatories) against **evidence**, not against what the workbook claims.
Voluntary alignment with the Data Protection and Right to Access Information Bill 2025, which is **not enacted**.

Code: `apps/api/app/privacy_monitor.py`, `privacy_catalogue.py`, `privacy_seed.json` · Tests: `apps/api/tests/test_privacy_monitor.py`
UI: collapsed **Privacy compliance (PIA)** panel in `static/index.html` (loads only on click). Spec: `PIA_COMPLIANCE_MONITOR_PROMPT.md`.

## Required DPO email
The panel is blocked until a Data Protection Officer / focal-point email is entered (super-admin; `PUT /api/v1/privacy/dpo`).
Placeholders (example.com, `test@…`) and malformed addresses are rejected; personal mailboxes (gmail…) are accepted with a warning.
Until it is set, every write endpoint answers `409 dpo_required` (feedback excepted) and the gate shows BLOCKED (ACT-01 / R16).
Setting it gives control A02 only partial, low-weight credit; a linked designation letter is needed for more. **No email is sent**:
the UI offers a `mailto:` link only.

## How state is derived (nothing is "agreed" without evidence)
1. **Measured** from cached GAM CSVs: A20, A21, A22, A23, A25, A26, A32 (7 of 40). Capped at 90% until an *evidenced* attestation covers the companion evidence.
2. **Attestation** (`POST /attestations`): with an evidence link = *evidenced* (0.75), without = *attested* (0.4). Expires after `evidence_valid_days` (half weight, then 0 after a further year).
3. **Computed**: A37 (fresh evidence share), A40 (gate).
4. The workbook's own status is shown as **declared** and is never scored as observed.
A measurement that contradicts an attestation wins and is flagged `conflict`.

Radar (10 axes): Ideal, MBSSE declared, National observed (measured controls only), National assured (unmeasured = 0), selected unit observed, optional go-live threshold.
Triangulation tiles: ambition gap, assurance gap, equity spread, tail risk, evidence freshness, coverage.
Risks show inherent, declared residual and **evidence-adjusted** (residual credited only if every linked action is Completed AND linked controls average ≥ tolerance).
Gate: BLOCKED (no DPO, Critical risk not accepted, sign-off incomplete) / CONDITIONAL (High outstanding) / OPEN.
Sign-off state machine: Draft → In review → Signed (all 6 name+date) → Decision recorded.

## Endpoints (`/api/v1/privacy`)
GET `status summary controls risks radar heatmap triangulation gate workbook signoff settings trend report feedback`;
PUT `dpo settings` (super-admin); POST `attestations` (national = super-admin, unit = in-scope user), `tracker signoff risk-acceptance import snapshot` (super-admin), `feedback` (anyone).
`import` takes the raw `.xlsx` body, keeps a versioned copy and returns data-quality findings. All GETs honour `?domain=` and the usual domain scoping.

## Privacy of the monitor itself
Own JSON state in `DATA_DIR/privacy/` (`state.json`, `attestations.jsonl`, `audit.jsonl`, `feedback.jsonl`, `snapshots.jsonl`, `workbooks/`). No schema change, no Postgres.
Aggregates only: no names, emails of learners, serials or per-person rows are stored or returned. Units with fewer than `k_min` (default 10) active accounts are suppressed.

## Safety model
Additive: one guarded block at the end of `main.py`; route table 67 → 90, 0 removed or changed. Kill switch `PRIVACY_MONITOR_ENABLED=0`.
Import failure disables only this module (logged as `[privacy_monitor] disabled`). Read-only toward Workspace and DHIS2.

## NOT done / limits
* DHIS2 and Google Vault collectors are not built; those 33 controls need attestations with an evidence link.
* Units are **email domains**; no domain → district/school mapping exists in the cached data, so there are no separate District/School polygons yet.
* A19, A27, A33 (shared accounts, bulk export, Gemini/NotebookLM use) need new allowlisted GAM reports; not measured.
* Weights, tolerance, thresholds and the risk→control map (`RISK_CONTROLS`) are proposals needing MBSSE sign-off. Use the Feedback tab.
* Docker image build and a real-tenant run are unverified in the dev sandbox (no Docker daemon). Tests run on Python 3.12 with the pinned requirements.

## Deploy / roll back
```bash
cd /home/george/drive_14tb/stacks/sl.p4sgi && git fetch origin && git checkout feature/privacy-pia-monitor
docker compose up -d --build
curl -s http://127.0.0.1:8088/api/v1/privacy/status | python3 -m json.tool | head
```
Dashboard: hard-reload → *Privacy compliance (PIA)* → Load → enter the DPO email. Roll back with `PRIVACY_MONITOR_ENABLED=0` or checkout the previous branch; state files can simply be deleted.
