# DEL — App Inventory & Safe Uninstaller

DEL is a self-hosted administrative application for **discovering, reviewing, and
safely uninstalling** applications from this host (bjkai-2tb-ubuntu). It scans
Docker/Compose, Nginx, systemd, cron, running processes, and the filesystem;
correlates what it finds into applications with a confidence score; and drives
removal through a six-stage job engine executed by a separate privileged
helper over a unix socket (backups are per-plan and opt-in — the default
backup mode is None). The step-by-step plan builder defaults to dry-run;
a one-click "Remove app now" button on each app's detail page instead builds
a complete-removal plan and runs it live immediately, behind a single confirm
dialog. DEL cannot remove itself.

The **dashboard** draws the host as a site plan: every application is a tile
grouped by kind or status, with a filter for name, slug, kind, status and
enabled site names. Next to it are the figures (orphans, shared resources, disk
usage including Docker volumes, Docker's reclaimable space), what changed since
the previous scan, apps that need review, and recent jobs. A **scan stamp** in
the sidebar shows which completed scan every page is reading, how old it is,
and runs a new scan in place. It is marked stale when the inventory is older
than `scan_interval_hours` (or a day, when automatic scans are off). Optional
automatic scans keep it fresh (`scan_interval_hours` in `config/del.toml`, off
by default); the dashboard's title block shows the scan, when it ran, how long
it took and when the next automatic one is due. An application with exactly one
enabled domain has an **Open site** action; several domains stay as links, so
DEL does not guess which one to open.

Each **application page** opens on an overview: its status in every recent
scan, and how it is wired — the domains that reach it, the ports it listens on,
what runs it and where it keeps data (data-bearing storage first) — above its
install/change record and per-type resource tabs. **Settings** charts
applications, resources and scan time over the last 90 scans, and lists your
active sign-ins with a "sign out other sessions" action.

The authenticated **View Apps** tab at `/view-apps` is a homelab-style launcher
for current, enabled domains that pass a live HTTPS check. It features real-time
HTTPS latency badges (graded green/amber/red with sonar animations), persistent
disk-cached probing (`probe-cache.json`), hover micro-actions (1-click URL copy and
inventory drilldown), search, categories, favorites, grid/list layouts, card sizing,
density, hiding, and drag ordering; personal layout preferences stay in browser
local storage and do not change DEL inventory or removal data. Domains that fail
the check — or show only a login page while nothing listens behind it — are listed
under **Unavailable** with the reason. Card icons are proxied through DEL's own
`/app-icon/{domain}` route rather than loaded from each app's origin, so an app
behind HTTP basic auth cannot pop a credential prompt over the gallery.

The UI is self-contained (no third-party code, fonts self-hosted, content-hashed
assets cached as immutable) in a hyper-refined dark cyanotype or light drafting-film
architectural CAD theme with dual-tier measuring grids (`localStorage["del.theme"]`,
else the OS `prefers-color-scheme`, else dark). Every inventory table has search,
sorting, per-column filters, quick filter chips, a column chooser, pagination and
CSV export. `Ctrl`/`Cmd`+`K` opens an instant command palette for pages, applications,
their sites, listening ports (`:port`), and actions; apps opened in this browser are
listed first. Keyboard shortcuts include `g` jumps, `/` search, `[`/`]` drawer toggles,
`h` help dock, `c` copy page URL, `t` theme toggle, and `?` for the shortcut cheat sheet.
HUD notifications use frosted glass banners with interactive progress timers.
See [docs/UI.md](docs/UI.md).

The **Assistant** is a read-only Q&A over the latest inventory (Ollama Cloud
`glm-5.3-flash`): a dedicated page at `/assistant` plus an **Ask** tab in the
right-rail Help|Ask dock on every authenticated page. Suggested prompts cover
the whole server, one app, orphans, or images/containers/networks/volumes
(including “is this shared / ok to delete?”). It cannot change anything; removal
still uses the planner. See [docs/ASSISTANT.md](docs/ASSISTANT.md).

## Quick facts

| Item | Value |
|---|---|
| URL | https://del.bjk.ai |
| Bind | 127.0.0.1:8075 (Nginx-fronted only, not publicly reachable directly) |
| Web unit | `del-web.service` — runs as user `bjkai` (groups `bjkai`, `docker`, `adm`) |
| Helper unit | `del-helper.service` — runs as `root`, executing the root-owned deployed copy at `/usr/local/lib/del-helper/` with its policy at `/etc/del/helper-policy.json` (the repo copies under `helper/` and `config/` are the source; `install.sh` deploys them) |
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
| Inventory export | Self-contained, whole-server inventory dump (`/apps/del/miscwork/`, gitignored) served at `https://del.bjk.ai/miscwork.html` (and aliased at `/inventory`), the only basic-auth-protected location in the vhost (`auth_basic_user_file /etc/nginx/.del-docs-htpasswd`) |
| Unauthenticated routes | `/login`, `/healthz`, `/favicon.ico`, and `/static/*` (`app.css`, `app.js`, `assistant.js`, `removal.js`, `gallery.js`, `theme-init.js`, `favicon.svg`, and `fonts/`). Login (`login.html`) loads only `app.css`, `theme-init.js`, the fonts and the favicon. Everything else — including `/app-icon/{domain}` and `/palette.json` — requires a session |

## Quick start

```bash
cd /apps/del
./scripts/install.sh                       # deploys the helper, installs del-web/del-helper, nginx site, checks health
./scripts/del-admin create-admin           # create the one admin account
```
Then open https://del.bjk.ai, log in, and run a scan from the sidebar scan stamp or Settings.

`install.sh` installs `del-web.service` and `del-helper.service` only. It does
**not** create `/etc/nginx/.del-docs-htpasswd`, which `/miscwork.html` needs — see
INSTALL.md.

## Documentation index

| Doc | Covers |
|---|---|
| [INSTALL.md](INSTALL.md) | Prerequisites, running `install.sh`, admin creation, health checks, DNS |
| [OPERATIONS.md](OPERATIONS.md) | Day-to-day commands: start/stop/status, logs, updates, rescan, backup/restore, password change |
| [SECURITY.md](SECURITY.md) | Auth model, session/CSRF/rate limiting, helper privilege split, threat model |
| [RECOVERY.md](RECOVERY.md) | DB restore, helper socket troubleshooting, nginx rollback, venv rebuild, outage behavior |
| [UNINSTALL.md](UNINSTALL.md) | Manual steps to remove DEL itself (DEL cannot do this to itself) |
| [docs/ASSISTANT.md](docs/ASSISTANT.md) | Read-only inventory assistant (Ollama Cloud, scopes, prompts, security) |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Stack, process/privilege model, component layout, data model |
| [docs/UI.md](docs/UI.md) | Visual language, shell, components, table contract, keyboard shortcuts, stored preferences |
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
| [reports/2026-09-13/](reports/2026-09-13/) | Read-only Nginx alias audit of scan 249's confidence-60 site associations |
| [reports/2026-10-02/](reports/2026-10-02/) | Scan integrity, review persistence, CI cleanup, performance measurement, and live validation |
| [reports/2026-10-03/](reports/2026-10-03/) | UI rebuild ("survey sheet"): feature-checklist verification, old-vs-new accuracy comparison, payload, live checks |
| [reports/2026-10-04/](reports/2026-10-04/) | Refinement pass: old-vs-new parity on one snapshot, CSP crawl, new features checked against the DB, review |

### Local-only files this repo references but does not contain

These are gitignored (see `.gitignore`); they exist on the deployment host only.
If you are reading the committed repo, do not go looking for them:

| Path | What it is |
|---|---|
| `docs/INTERFACES.md` | The original July 2026 module contracts, kept for history only. The code no longer cites it; docs/ARCHITECTURE.md and docs/REMOVAL-LIFECYCLE.md are current |
| `docs/PORT-REGISTRY.md` | Auto-generated port/subdomain map (`scripts/gen-registry.py`); regenerate on demand rather than trusting a stale copy |
| `docs/server-audit.md` | The phase-2 host audit this design was built from |
| `PROGRESS.md` | Scratch working notes for whatever change is in flight; finished ones are moved into `reports/<date>/` |
| `miscwork/` | Build scripts and output for the whole-server inventory export served at `/miscwork.html` |

### Documentation structure

The root and `docs/` markdown files are the only documentation (the Fern site
at `/docs` was removed 2026-09-27). The `reports/` table
above is intentional audit history, not stale content — leave it as-is.

## Tests

```bash
cd /apps/del/backend && ../.venv/bin/python -m pytest ../tests/ -q -W error
```

Every change must leave this at zero failures with warnings treated as errors.
GitHub CI also runs Pyflakes and `node --check` on every application JavaScript
file. Dependency pins in `requirements*.txt` are shared by CI and production.

Scans publish inventory atomically and keep the previous inventory on failure.
Web and CLI scans share a file lock. Settings shows scan progress and outcomes;
manual exclusions and shared flags survive for still-associated resources, and
approvals survive only while the safety classification is unchanged. See
[scan persistence](docs/DISCOVERY.md#operator-decisions-across-rescans).
The pass/skip counts change with nearly every commit, so they are not pinned
here; each pass records its own count in `CHANGELOG.md`.
