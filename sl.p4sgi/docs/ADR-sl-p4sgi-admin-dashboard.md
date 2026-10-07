# ADR: SL-P4SGI Admin Dashboard

- **Status:** Accepted
- **Date:** 2026-09-30
- **Accepted:** 2026-09-30

## Context

The SEMIS/SL-P4SGI pilot needs an administrative dashboard for GAM extracts and school-config management. Tablet apps remain in snapait for now, while the administrative dashboard should run in Docker.

## Decision

Build the SL-P4SGI administrative dashboard as a Docker application in `sl.p4sgi`, rather than embedding it in Workspace Admin. Store its data in `/home/george/drive_14tb/docker-data/sl.p4sgi`. Provide three roles: **Superadmin**, **District**, and **School**. Use the workflow **GAM → approve → publish school-config**, with every publish action audited. Provide an LLM interface over the warehouse as read-only. Keep Android in place until a later cutover.

## Consequences

We gain a deployable, role-based administrative surface with controlled school-config publication, traceable publishes, and useful read-only warehouse assistance. We reject Sheet-as-source-of-truth, Sites API embed automation, and any direct LLM-to-GAM write path without a review and approval boundary.

## Non-goals

This ADR does not move the tablet apps, replace snapait, automate Workspace Admin/Sites embedding, or permit direct LLM writes to GAM.

## Phases

1. **Phase 1:** Establish the Docker dashboard, storage boundary, roles, and read-only warehouse access.
2. **Phase 2:** Implement GAM extract review, approval, school-config publishing, and publish audit.
3. **Phase 3:** Operate the pilot and evaluate the later Android cutover; retain Android in place until that decision.
