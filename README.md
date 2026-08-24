# DEL — App Inventory & Safe Uninstaller

DEL is a self-hosted administrative application for **discovering, reviewing, and
safely uninstalling** applications from this host (bjkai-2tb-ubuntu). It scans
Docker/Compose, Nginx, systemd, cron, running processes, and the filesystem;
correlates what it finds into applications with a confidence score; and drives
removal through a staged, backed-up, dry-run-by-default job engine executed by a
separate privileged helper over a unix socket. DEL cannot remove itself.

The authenticated **View Apps** tab at `/view-apps` is a homelab-style launcher
for current, enabled domains that pass a live HTTPS check. It supports search,
categories, favorites, grid/list layouts, card sizing, density, hiding, and drag
ordering; personal layout preferences stay in browser local storage and do not
change DEL inventory or removal data.

## Quick facts

| Item | Value |
|---|---|
| URL | https://del.bjk.ai |
| Bind | 127.0.0.1:8075 (Nginx-fronted only, not publicly reachable directly) |
| Web unit | `del-web.service` — runs as user `bjkai` (groups `bjkai`, `docker`, `adm`) |
| Helper unit | `del-helper.service` — runs as `root` |
| Docs unit | `del-docs.service` — Fern docs site, runs as `bjkai`, ports 8072/8073, `/docs` + `/_next` open (no basic auth; app UI still session-login) |
| Helper socket | `/run/del/helper.sock`, mode `0660`, owner `root:bjkai` |
| Project root | `/apps/del` (also reachable via `/opt/del`, a symlink to `/apps/del`) |
| Backend package | `/apps/del/backend/del_app` (import as `del_app`), Python 3.10 venv at `/apps/del/.venv` |
| Database | SQLite, WAL mode, `/apps/del/database/del.db` |
| Manifests | `/apps/del/manifests/*.yaml` |
| Backups | `/apps/del/backups/` |
| Logs | `/apps/del/logs/` (+ `journalctl -u del-web -u del-helper`) |
| Config | `/apps/del/config/del.toml` |
| Admin CLI | `/apps/del/scripts/del-admin` (`create-admin`, `change-password`, `migrate`, `rescan`, `backup-db`) |
| TLS | Nginx, existing `bjk.ai` wildcard cert |
| Protection | DEL is flagged `protected=1`; the planner refuses to build a removal plan for it |
| Inventory export | Self-contained, whole-server inventory dump (`/apps/del/miscwork/`, gitignored) served at `https://del.bjk.ai/miscwork.html` (and aliased at `/inventory`), basic-auth protected via the same Nginx `auth_basic_user_file` as `/docs` |

## Quick start

```bash
cd /apps/del
./scripts/install.sh                       # installs units, nginx site, checks health
./scripts/del-admin create-admin           # create the one admin account
```
Then open https://del.bjk.ai, log in, and run a scan from Settings (or `POST /scan`).

Rendered documentation (Fern) is served at https://del.bjk.ai/docs (no basic auth).

## Documentation index

| Doc | Covers |
|---|---|
| [INSTALL.md](INSTALL.md) | Prerequisites, running `install.sh`, admin creation, health checks, DNS |
| [OPERATIONS.md](OPERATIONS.md) | Day-to-day commands: start/stop/status, logs, updates, rescan, backup/restore, password change |
| [SECURITY.md](SECURITY.md) | Auth model, session/CSRF/rate limiting, helper privilege split, threat model |
| [RECOVERY.md](RECOVERY.md) | DB restore, helper socket troubleshooting, nginx rollback, venv rebuild, outage behavior |
| [UNINSTALL.md](UNINSTALL.md) | Manual steps to remove DEL itself (DEL cannot do this to itself) |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Stack, process/privilege model, component layout, data model |
| `docs/INTERFACES.md` | internal module/function contracts — **local only, not committed** |
| [docs/DISCOVERY.md](docs/DISCOVERY.md) | Discovery sources, confidence scoring, correlation rules, manifest format |
| [docs/REMOVAL-LIFECYCLE.md](docs/REMOVAL-LIFECYCLE.md) | The 9-stage removal job lifecycle and its safety gates |
| [docs/DEPLOYMENT-CONVENTION.md](docs/DEPLOYMENT-CONVENTION.md) | The house standard every app on this server follows (layout, ports, nginx, manifests, decommissioning) |
| [docs/SYSTEM-STATE.md](docs/SYSTEM-STATE.md) | Consolidated point-in-time audit of the whole host (directory classification, shared resources, known exceptions) |
| [docs/OPTIMIZATION-2026-07-26.md](docs/OPTIMIZATION-2026-07-26.md) | 2026-07-26 optimization pass: Installed dates, Eastern UI times, docs open, validation log |
| `docs/PORT-REGISTRY.md` | auto-generated port/subdomain map (`scripts/gen-registry.py`) — **local only, gitignored**, regenerate on demand |
| `docs/server-audit.md` | Phase-2 host audit this design was built from — **local only, not committed** |

## Tests

```bash
cd /apps/del/backend && ../.venv/bin/python -m pytest ../tests/ -q
```
