# RUSTAiDMIN: remote desktop and ADB helper for field tablets (0.4.26, experimental)

A collapsible section right after the **Domain filter**. Additive: its own module (`app/rustadmin.py`), its own state
(`DATA_DIR/rustadmin/audit.jsonl`), kill switch `RUSTADMIN_ENABLED=0`. It never touches Workspace or DHIS2.

## What it is, and what it is not

* The dashboard does **not** contain a RustDesk server. Two optional compose services (`--profile rustdesk`) run the upstream
  server (`rustdesk/rustdesk-server`: `hbbs` ID/rendezvous + `hbbr` relay) and an optional community admin console
  (`lejianwen/rustdesk-api`). The section shows the settings the tablets need (ID server, relay, **public** key), links to the
  console, and has a Connect box that opens the RustDesk desktop client (`rustdesk://` link; needs the desktop client installed).
* ADB is a separate, optional, **super-admin-only** helper (below).

## Fork or upstream? (answer to "so it updates itself")

Use **upstream images, not a fork**. A fork has to be merged by hand to get security fixes; an upstream image is refreshed with
`docker compose --profile rustdesk pull && docker compose --profile rustdesk up -d`. Pin `RUSTDESK_SERVER_TAG` /
`RUSTDESK_CONSOLE_TAG` in `.env` if you want to choose when an update lands. There is no automatic updater in this repo; if you
want one, add Watchtower yourself, knowing it can restart the relay in the middle of a support session.

Facts checked on 2026-10-08 from the project pages: `rustdesk/rustdesk-server` is AGPL-3.0 (about 10.4k GitHub stars, Docker Hub 10M+
pulls); `lejianwen/rustdesk-api` is MIT (about 3.1k stars) and provides a web admin, address book, user management, logs and OAuth/LDAP.
`marcpope/cortendesk` (AGPL-3.0, 12 stars) was looked at and not chosen. The RustDesk server's own web console is a Pro (paid) feature.
Stars are popularity, not a security review: read each project's issues before exposing the console.

## The 120 tablets (RustDesk Android 1.2.3, 2023-10-13)

**Not verified.** No page I could read states which client versions the current server or console supports. The RustDesk
client/server protocol has stayed stable across 1.x, so an old client is expected to register with a new server, but test **one**
tablet end to end (ID server + relay + key, then connect) before relying on it. Practical consequences:

* A 2023 client may lack later security fixes. CVE listings for RustDesk appeared in a web search, but I did not verify which versions
  are affected, so check the RustDesk release notes and advisories. Updating the tablets is the real fix; a new server does not patch the client.
* Enter the ID server, relay server and **key** in the tablet's RustDesk network settings (menu names may differ in 1.2.3). The key lets the tablet check it is talking to your server.
* Open TCP 21115-21117 and UDP 21116 only; leave 21118/21119 closed unless you use the web client.

## ADB: is it possible?

Yes, with limits.

* **RustDesk does not carry ADB.** ADB needs USB debugging or wireless debugging enabled on the tablet, and the machine running
  `adb` needs its own network path to it: USB, the same LAN, or a VPN (WireGuard/Tailscale). A tablet behind mobile data or a school NAT is
  not reachable by the dashboard server without that.
* Wireless debugging left open is a security risk. The section only accepts targets inside `RUSTADMIN_ADB_NETS`
  (default private ranges, loopback and 100.64.0.0/10), runs `adb` with a fixed argument list (**no shell**), validates every
  argument against a strict pattern, caps output and time, and writes each run to the audit log.
* Off by default: `RUSTADMIN_ADB_ENABLED=1` and build the web image with `INSTALL_ADB=1`.
* **Read-only** diagnostics (properties, battery, storage, processes, SELinux mode, developer/USB-debugging/unknown-sources
  settings, package list with installer, permission list, usage stats, per-app data use, accounts, network, recent errors) run for super-admins.
* Commands that **change** a tablet (start/stop app, grant/revoke permission, key/tap, screencap, pull, `settings put`, `pm clear`,
  uninstall, push, rm, reboot) are **plan-only** (shown, not run) unless `RUSTADMIN_ADB_ALLOW_CHANGES=1`; then the super-admin must
  type the target again to confirm. `install` of arbitrary APKs, `logcat` streaming, `screenrecord`, `bugreport`, free-form `shell`
  and `adb tcpip` are deliberately **not** offered in this version.
* Some commands need more rights than a normal tablet gives (`dmesg`, some `dumpsys`); the output will say so.
* Several outputs contain personal data (app-use history, accounts, logs, screenshots). Use them for a support reason only.

## Setup

```
# .env
RUSTDESK_HOST=rd.example.org          # public name/IP the tablets can reach
RUSTDESK_CONSOLE_URL=http://rd.example.org:21114/_admin/
INSTALL_ADB=1                         # optional
RUSTADMIN_ADB_ENABLED=1               # optional

docker compose --profile rustdesk up -d      # server + console (optional)
docker compose up -d --build web             # picks up the new section
```

The server writes its keys to `../../docker-data/sl.p4sgi/rustdesk/`; the dashboard mounts that folder read-only and shows only
`id_ed25519.pub` (the public key). The private key (`id_ed25519`) is never read. Change the console's default admin password on first login.

## API (all under `/api/v1/rustadmin`)

`GET /status`, `GET /adb/catalog`, `POST /adb/run` (super-admin), `GET /adb/audit` (super-admin).
