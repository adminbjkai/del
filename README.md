# DEL — App Inventory & Safe Uninstaller

DEL is a self-hosted administrative application for **discovering, reviewing, and
safely uninstalling** applications from this host (bjkai-2tb-ubuntu). It scans
Docker/Compose, Nginx, systemd, cron, running processes, and the filesystem;
correlates what it finds into applications with a confidence score; and drives
removal through a six-stage, dry-run-by-default job engine executed by a separate
privileged helper over a unix socket (backups are per-plan and opt-in — the
default backup mode is None). DEL cannot remove itself.

The authenticated **View Apps** tab at `/view-apps` is a homelab-style launcher
for current, enabled domains that pass a live HTTPS check. It supports search,
categories, favorites, grid/list layouts, card sizing, density, hiding, and drag
ordering; personal layout preferences stay in browser local storage and do not
change DEL inventory or removal data. Card icons are proxied through DEL's own
`/app-icon/{domain}` route rather than loaded from each app's origin, so an app
behind HTTP basic auth cannot pop a credential prompt over the gallery.

## Quick facts

| Item | Value |
|---|---|
| URL | https://del.bjk.ai |
| Bind | 127.0.0.1:8075 (Nginx-fronted only, not publicly reachable directly) |
| Web unit | `del-web.service` — runs as user `bjkai` (groups `bjkai`, `docker`, `adm`) |
| Helper unit | `del-helper.service` — runs as `root`, executing the root-owned deployed copy at `/usr/local/lib/del-helper/` with its policy at `/etc/del/helper-policy.json` (the repo copies under `helper/` and `config/` are the source; `install.sh` deploys them) |
| Docs unit | `del-docs.service` — Fern docs site, runs as `bjkai`, ports 8072/8073, `/docs` + `/_next` open (no basic auth; app UI still session-login). **`install.sh` does not install this unit** — see INSTALL.md |
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
| Inventory export | Self-contained, whole-server inventory dump (`/apps/del/miscwork/`, gitignored) served at `https://del.bjk.ai/miscwork.html` (and aliased at `/inventory`), the only basic-auth-protected location in the vhost (`auth_basic_user_file /etc/nginx/.del-docs-htpasswd`). `/docs` has no `auth_basic` at all |
| Unauthenticated routes | `/login`, `/healthz`, `/favicon.ico`, and the four `/static/*` assets (`app.css`, `app.js`, `ag-grid-community.min.js`, `favicon.svg`). Everything else — including `/app-icon/{domain}` — requires a session |

## Quick start

```bash
cd /apps/del
./scripts/install.sh                       # deploys the helper, installs del-web/del-helper, nginx site, checks health
./scripts/del-admin create-admin           # create the one admin account
```
Then open https://del.bjk.ai, log in, and run a scan from Settings (or `POST /scan`).

`install.sh` installs `del-web.service` and `del-helper.service` only. Two things
it does **not** do, and which `/docs` and `/miscwork.html` respectively need — see
INSTALL.md for the commands: install `config/del-docs.service`, and create
`/etc/nginx/.del-docs-htpasswd`.

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
| [docs/DISCOVERY.md](docs/DISCOVERY.md) | Discovery sources, confidence scoring, correlation rules, manifest format |
| [docs/REMOVAL-LIFECYCLE.md](docs/REMOVAL-LIFECYCLE.md) | The six removal-job stages and the safety gates around them |
| [docs/DEPLOYMENT-CONVENTION.md](docs/DEPLOYMENT-CONVENTION.md) | The house standard every app on this server follows (layout, ports, nginx, manifests, decommissioning) |
| [docs/SYSTEM-STATE.md](docs/SYSTEM-STATE.md) | Consolidated point-in-time audit of the whole host (directory classification, shared resources, known exceptions) |

`docs/` holds durable reference only. Finished one-off audits and working notes
live under `reports/<date>/`:

| Report | Covers |
|---|---|
| [reports/2026-07-20/](reports/2026-07-20/) | 5-lane repo/docs consistency audit, plus the 5-app removal working note |
| [reports/2026-07-21/](reports/2026-07-21/) | Full host audit and its remediation plan |
| [reports/2026-07-26/](reports/2026-07-26/) | Optimization pass: Installed dates, Eastern UI times, docs open, validation log |
| [reports/2026-08-13/](reports/2026-08-13/) | Recovery of DEL job 191 (netdata live removal that touched foreign units) |
| [reports/2026-08-24/](reports/2026-08-24/) | Frontend/accessibility audit fixes |

### Local-only files this repo references but does not contain

These are gitignored (see `.gitignore`); they exist on the deployment host only.
If you are reading the committed repo, do not go looking for them:

| Path | What it is |
|---|---|
| `docs/INTERFACES.md` | Internal module/function contracts. `backend/del_app/planner.py:2` calls it authoritative for stage order and safety rules — that content is mirrored in docs/ARCHITECTURE.md and docs/REMOVAL-LIFECYCLE.md, which are committed |
| `docs/PORT-REGISTRY.md` | Auto-generated port/subdomain map (`scripts/gen-registry.py`); regenerate on demand rather than trusting a stale copy |
| `docs/server-audit.md` | The phase-2 host audit this design was built from |
| `PROGRESS.md` | Scratch working notes for whatever change is in flight; finished ones are moved into `reports/<date>/` |
| `miscwork/` | Build scripts and output for the whole-server inventory export served at `/miscwork.html` |

## Tests

```bash
cd /apps/del/backend && ../.venv/bin/python -m pytest ../tests/ -q
```
