# Optional dedicated GAM config for geb@p4sgi.com

**Status on gbu (2026-10-06):** The dashboard already mounts `/home/george/.gam`,
and that oauth is **already** Workspace admin `geb@p4sgi.com` on customer
`C03ac36ui` (primary domain `p4sgi.com`). You do **not** need this directory
unless you want an isolated config separate from the host default.

`sl.p4sgi.com` is a **secondary domain** of the same Workspace tenant (not a
separate customer).

## If you ever need a fresh isolated config

Run these yourself on a terminal that can open a browser (agents must not):

```bash
mkdir -p /mnt/drive_14tb/stacks/sl.p4sgi/gam-geb
export GAMCFGDIR=/mnt/drive_14tb/stacks/sl.p4sgi/gam-geb
export PATH=/home/george/bin/gam7:$PATH

# Sign in as geb@p4sgi.com when the browser opens:
gam oauth create

# Or create a GCP project + oauth as geb (GAM wizard):
# gam create project

# Verify (read-only):
gam version
gam info domain
gam oauth info
gam print users query 'isEnrolledIn2Sv=false' maxresults 1
```

Scopes needed for dashboard reports: Directory users/OUs/groups/roles,
Reports audit + usage (readonly), mobile devices, Chrome OS devices,
Cloud Identity devices/groups as applicable. The existing `~/.gam` oauth
already includes these.

### Domain-wide delegation (only if you later need service-account / DWD)

If you enable `oauth2service.json` features:

1. In Google Cloud Console (project owned by geb), note the service account
   client ID from `oauth2service.json` (`client_id` field — not the private key).
2. Workspace Admin → Security → API controls → Domain-wide delegation →
   Add the client ID with the same scopes GAM lists.
3. Or: `gam oauth create` / GAM wiki "Domain Wide Delegation" steps while
   signed in as geb@p4sgi.com.

### Point the dashboard at this dir

In `docker-compose.yml`, uncomment the `./gam-geb` volume and set
`GAMCFGDIR=/gam-geb` (or host path via env). Then:

```bash
cd /mnt/drive_14tb/stacks/sl.p4sgi
docker compose up -d --build
```
