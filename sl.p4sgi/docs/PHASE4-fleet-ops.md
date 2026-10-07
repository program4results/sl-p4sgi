# Phase 4 backlog — fleet operations

**Status:** **4a done** (registry + telemetry stubs, v0.4.1) — see [PHASE4a.md](./PHASE4a.md). **4a-lite:** on-demand GAM reports (Phase 3.1). Remaining: 4b charts, 4c HT move, 4d WhatsApp, 4e Play.

## Links

- Previous phase: [PHASE3.md](./PHASE3.md)
- Decision record: [ADR-sl-p4sgi-admin-dashboard.md](./ADR-sl-p4sgi-admin-dashboard.md)
- Site template + GAM provision (~100): [SCHOOL_SITE_TEMPLATE_PROVISION.md](./SCHOOL_SITE_TEMPLATE_PROVISION.md)
- Site attendance embed: [SITE_ATTENDANCE_EMBED.md](./SITE_ATTENDANCE_EMBED.md)

## Fleet requirements

The next operating scope is approximately **100 remote tablets**, with an offline-tolerant outbox that synchronises queued work when a tablet is online.

1. **Outbox and connectivity** — Queue tablet events/actions locally and sync the outbox when connectivity returns, with enough status to identify pending, synced, and failed items.
2. **Operational metrics** — Capture and expose counts for skills run, prompts, and web publishes.
3. **HT reassignment** — Support head-teacher (HT) moves between schools, including reassignment by EMIS and by email; do not assume an email alone is a permanent school identity.
4. **SEMIS/Snap visibility** — Show the Snap-versus-SEMIS gap, a cumulative radar view on the dashboard, and data-quality trends over time.
5. **People and attendance** — Track learners and teachers enrolled, together with attendance.
6. **LLM usage** — Distinguish local Gemma usage from online LLM usage.
7. **Data consumption** — Report data consumption per tablet and cumulatively across the fleet.
8. **Identity registry** — Maintain the mapping `EMIS ↔ email ↔ tablet ID ↔ SIM ↔ WhatsApp`.
9. **WhatsApp news** — Support broadcast WhatsApp news to the relevant fleet/audience.
10. **Private Play** — Automate Private Play app versioning. This is blocked until the upload keystore is available.

## Ordered delivery phases

### 4a — Registry + telemetry

**Done (Phase 4a / v0.4.1):** see [PHASE4a.md](./PHASE4a.md). Device registry + telemetry_events + summary dash cards + APK outbox contract (docs only). **4a-lite (Phase 3.1):** host GAM report runner.

Establish the fleet registry and stable tablet/HT identity links, then capture outbox sync state, skills run, prompt, web-publish, local-versus-online LLM, and per-tablet/cumulative data-consumption telemetry.

### 4b — Charts

Add dashboard views for the Snap-versus-SEMIS gap, cumulative radar, data-quality trends, learners/teachers enrolled, attendance, LLM usage, and per-tablet/cumulative data consumption.

### 4c — HT move

Deliver the HT reassignment workflow for school moves, resolving and updating relationships through EMIS and email while preserving the reassignment history.

### 4d — WhatsApp

Deliver the registry-backed broadcast path for WhatsApp news, with the intended audience determined from the fleet registry.

### 4e — Play

Deliver Private Play automatic versioning once the upload keystore dependency is unblocked. Until then, this phase remains blocked.

## Constraint

4a shipped (v0.4.1). 4b–4e remain backlog. Rebuild required when API changes.
