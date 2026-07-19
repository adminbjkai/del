# DEL — App Inventory & Safe Uninstaller

DEL is a self-hosted administrative application for **discovering, reviewing, and
safely uninstalling** applications from this host (bjkai-2tb-ubuntu). It scans
Docker/Compose, Nginx, systemd, cron, running processes, and the filesystem;
correlates what it finds into applications with a confidence score; and drives
removal through a staged, backed-up, dry-run-by-default job engine executed by a
separate privileged helper over a unix socket. DEL cannot remove itself.

## Quick facts

| Item | Value |
|---|---|
| URL | https://del.bjk.ai |
| Bind | 127.0.0.1:8075 (Nginx-fronted only, not publicly reachable directly) |
| Web unit | `del-web.service` — runs as user `bjkai` (groups `bjkai`, `docker`, `adm`) |
| Helper unit | `del-helper.service` — runs as `root` |
| Docs unit | `del-docs.service` — Fern docs site, runs as `bjkai`, ports 8072/8073, `/docs` + `/_next` basic-auth protected via Nginx |
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

## Quick start

```bash
cd /apps/del
./scripts/install.sh                       # installs units, nginx site, checks health
./scripts/del-admin create-admin           # create the one admin account
```
Then open https://del.bjk.ai, log in, and run a scan from Settings (or `POST /scan`).

Rendered documentation (Fern) is served at https://del.bjk.ai/docs (basic-auth protected).

## Documentation index

| Doc | Covers |
|---|---|
| [INSTALL.md](INSTALL.md) | Prerequisites, running `install.sh`, admin creation, health checks, DNS |
| [OPERATIONS.md](OPERATIONS.md) | Day-to-day commands: start/stop/status, logs, updates, rescan, backup/restore, password change |
| [SECURITY.md](SECURITY.md) | Auth model, session/CSRF/rate limiting, helper privilege split, threat model |
| [RECOVERY.md](RECOVERY.md) | DB restore, helper socket troubleshooting, nginx rollback, venv rebuild, outage behavior |
| [UNINSTALL.md](UNINSTALL.md) | Manual steps to remove DEL itself (DEL cannot do this to itself) |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Stack, process/privilege model, component layout, data model |
| [docs/INTERFACES.md](docs/INTERFACES.md) | Authoritative module/function contracts for the backend |
| [docs/DISCOVERY.md](docs/DISCOVERY.md) | Discovery sources, confidence scoring, correlation rules, manifest format |
| [docs/REMOVAL-LIFECYCLE.md](docs/REMOVAL-LIFECYCLE.md) | The 9-stage removal job lifecycle and its safety gates |
| [docs/server-audit.md](docs/server-audit.md) | Phase-2 host audit this design was built from |

## Tests

```bash
cd /apps/del/backend && ../.venv/bin/python -m pytest ../tests/ -q
```
