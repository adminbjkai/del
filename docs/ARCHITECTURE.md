# DEL — Architecture

DEL is a self-hosted administrative application for discovering, reviewing, and
completely uninstalling applications from this server (bjkai-2tb-ubuntu). It is
served at https://del.bjk.ai behind Nginx, bound only to localhost.

## Stack

| Layer | Choice | Rationale |
|---|---|---|
| Language | Python 3.10 (system python3, venv) | Already on host; excellent for sysadmin tooling; typed with dataclasses/pydantic |
| Web framework | FastAPI + Uvicorn | Typed request/response models, async, small footprint |
| Templates/UI | Jinja2 server-rendered + vanilla JS + one `app.css`, self-hosted fonts | No Node toolchain, no third-party JS; fully self-contained (CSP `script-src 'self'`, no CDN); content-hashed asset URLs cached as immutable; theme follows `localStorage`, else OS `prefers-color-scheme`, else dark (`docs/UI.md`) |
| Database | SQLite (WAL mode) via sqlite3 + migration runner | Single admin user; zero-ops; file lives in /apps/del/database/del.db |
| Privileged layer | del-helper: separate root daemon on a unix socket | Strict allowlist; web app never runs shell as root |
| Deployment | Host systemd units (del-web.service, del-helper.service) | See below |

### Why systemd, not a container

DEL's job is to inspect and modify *host* state: systemd units, nginx configs,
cron files, arbitrary project directories, tmux sessions, and the Docker daemon.
A containerized DEL would need: the Docker socket, /etc, /apps, /data, /run/systemd,
host PID namespace, and root — i.e. a fully privileged container that is strictly
harder to reason about than two small host services. The spec permits Compose
"where appropriate"; here it is not. DEL is instead deployed as systemd units
with pinned dependencies in a dedicated venv, which gives restart policies, health
via systemd, and journald logging.

### Deployment units

- **del-web.service** — the FastAPI app (this document's main subject), port 8075,
  bound to 127.0.0.1, fronted by Nginx at https://del.bjk.ai.
- **del-helper.service** — the privileged root daemon on the unix socket. It
  executes `/usr/local/lib/del-helper/del_helper.py` with the policy at
  `/etc/del/helper-policy.json` — root-owned deployed copies installed by
  `scripts/install.sh`, *not* the repo working tree. `/apps/del` is writable by
  the `bjkai` user that runs `del-web`, so running the helper from there would let
  a compromised web tier rewrite the code root executes and the allowlist that
  bounds it. The repo copies (`helper/*.py`, `config/helper-policy.json`) are the
  source; redeploy them before restarting the unit after an edit.

## Process / privilege model

```
Browser
  ↓ HTTPS (443)
Nginx (del.bjk.ai) — TLS, security headers
  ↓ HTTP 127.0.0.1:<PORT>
del-web (user bjkai, groups docker+adm)
  • UI, auth, sessions, CSRF
  • Discovery engine (read-only: docker socket, /etc/nginx, systemctl show, ss, ps, filesystem stat)
  • Correlation engine + confidence scoring
  • Manifests, removal planner (dry-run), job orchestration, audit log
  ↓ JSON over unix socket /run/del/helper.sock (0660 root:bjkai)
del-helper (root, Python stdlib only, ~1,100 lines across del_helper.py +
            validation.py, no web framework; runs from /usr/local/lib/del-helper/)
  • Fixed 24-operation allowlist (see below)
  • Validates every argument; canonicalizes paths (realpath, no symlink escape)
  • Refuses protected roots; refuses paths outside approved roots
  • Refuses protected units (del-*, sshd, nginx, docker, systemd-*, cron, …)
  • Every request logged to /apps/del/logs/helper-audit.log (append-only), with
    plan_id/step_id/job_id/requested_by when the caller supplies them
  • dry_run flag honored on every operation
```

The helper reads `op`, `args` and `dry_run` and nothing else that affects its
decisions. It has **no concept of a plan**: plan integrity is a web-side control
(`planner.verify_plan`, an HMAC over the canonical steps JSON, checked before job
creation and again before execution). What bounds a compromised `del-web` is the
op allowlist plus the helper's independent argument validation — read every "must
appear in the approved plan" note in the table below as a planner-side constraint.

del-web never constructs shell strings from user input. Every privileged action is
a typed operation name + validated structured arguments. del-helper executes via
subprocess arg-arrays (never shell=True).

### Helper operation allowlist (complete)

| Operation | Args | Notes |
|---|---|---|
| ping | — | health |
| list_listeners | — | read-only `ss -lntp` as root; used by `proc_src.py` to resolve listener ownership when the caller can't run `ss` itself |
| read_nginx_config | path | read-only; returns one file's text. Realpath must be a regular file inside /etc/nginx/sites-enabled, sites-available or conf.d, ≤ 1 MiB, opened O_NOFOLLOW. Used by `nginx_src.py` for root-only (0600) site files — del-web runs with NoNewPrivileges, so `sudo` cannot work |
| read_crontab | user | read-only `crontab -l -u <user>`; user must be a valid existing login name (never starts with `-`); "no crontab" → empty. Used by `cron_src.py` |
| compose_down | project, config_files[], remove_volumes?, remove_images_mode? | config files must exist & be under approved roots |

`compose_down` tears the project down by config file and then sweeps any
straggler by Docker label (`docker ps -aq --filter
label=com.docker.compose.project=<project>`, then `docker rm -f`). That sweep is
**host-wide**, so the planner never names a project after a generic layout
directory: a compose file at `/apps/<app>/docker/` would otherwise yield the
project name `docker` and force-remove every container on the host carrying
that label. An explicitly declared compose project name wins; a generic
basename falls back to the app slug.

| container_stop / container_rm | container_id | id validated against docker inspect |
| image_rm | image_id | refused if other containers reference it |
| volume_rm | volume_name | refused unless plan approved w/ double confirmation flag |
| network_rm | network_name | refuses bridge/host/none and networks with foreign containers |
| systemd_stop / systemd_disable / systemd_rm_unit | unit | name must match `^[A-Za-z0-9@_.-]+\.(service\|timer)$` and must not match `protected_units`; rm restricted to /etc/systemd/system + daemon-reload; `systemd_rm_unit` re-resolves the unit file's realpath (`recheck_realpath`) immediately before the delete call, closing the TOCTOU window between validation and the actual removal |
| cron_rm | path | **`/etc/cron.d` files only.** The op takes a single `path` argument, validated to resolve under the policy's `cron_d_dir`. It cannot edit a user crontab — there is no `crontab -l` diff and no way to remove a user crontab line through DEL |
| nginx_rm_site | paths[] | only under /etc/nginx/sites-{enabled,available}; backup first; nginx -t; reload only on pass; restore on fail |
| nginx_test | — | read-only `nginx -t`, never reloads; safe in dry_run and live |
| nginx_test_reload | — | nginx -t, reload if ok |
| path_delete | path | canonicalized; must resolve at least one component deep under an approved root (/apps, /data, /srv, /var/www, /home/bjkai, /etc/nginx/sites-{available,enabled}, /etc/systemd/system, /etc/cron.d), must not be a protected root or on `never_delete`; refuses mountpoints; re-resolves the realpath (`recheck_realpath`) immediately before the destructive call and refuses if it no longer matches the realpath computed at validation time |
| path_restore | backup_path, original_path | `cp -a` of a prior backup back to original_path. backup_path must be under /apps/del/backups and exist; original_path must resolve under a DEL-managed root, not protected/never-delete, and **must have the same basename as the backup** — a restore may only replace the file it was taken from. A protected unit cannot be recreated in /etc/systemd/system this way |
| tmux_kill | session | exact session name |
| process_term | pid, expected_exe | TERM then KILL after grace; pid+exe must still match |
| backup_tar | src_path, dest | src_path confined to DEL-managed roots (so root cannot be made to archive /etc/shadow or a TLS key); dest under /apps/del/backups only |
| volume_backup | volume, dest | docker run --rm -v vol:/src:ro tar → /apps/del/backups |
| file_backup | path, dest | timestamped copy before config edits; path confined to DEL-managed roots, dest under /apps/del/backups |

"DEL-managed roots" = the approved deletion roots plus the three system config
directories DEL is allowed to remove files from and therefore must be able to back
up and restore: nginx sites, `/etc/systemd/system`, `/etc/cron.d`.

Protected roots (never deletable, even if listed): /, /bin, /boot, /dev, /etc,
/home, /lib, /lib64, /opt, /proc, /root, /run, /sbin, /srv, /sys, /tmp, /usr,
/var, /apps, /data, and /apps/del itself (DEL is a protected application).

Protected units (never stopped, disabled or removed): `del-*.service`,
`del-*.timer`, `ssh`/`sshd`, `nginx`, `docker`/`containerd`, `systemd-*`, `cron`,
`dbus`, `network*`, `polkit`, `rsyslog`, `fail2ban`, `ufw` — the full glob list is
`protected_units` in `helper-policy.json`. DEL's own units come first deliberately:
stopping `del-helper` is the opening move in a helper-code-swap attack.

Approval flow: `del-web` builds a plan, signs it with an HMAC over the canonical
steps JSON, and stores it. `planner.verify_plan()` re-checks that signature before
a job is created and again before it executes — **web-side**. The helper is sent
`op`/`args`/`dry_run` (plus correlation ids it only logs) and re-validates every
argument against its own policy independently. Neither layer depends on the other
being uncompromised.

## Components (code layout)

```
/apps/del/
├── backend/del_app/
│   ├── main.py            FastAPI app factory, routes mounting
│   ├── config.py          settings (port, paths) from /apps/del/config/del.toml
│   ├── db.py              sqlite connection, migration runner, WAL & mmap configuration
│   ├── migrations/        001_init.sql through 005_drop_settings.sql (schema, indexes, assistant, reviews)
│   ├── auth.py            login, argon2id hashing, sessions, CSRF, rate limit
│   ├── models.py          typed dataclasses / pydantic models
│   ├── discovery/
│   │   ├── docker_src.py  containers/images/volumes/networks via docker socket
│   │   ├── compose_src.py compose file scanner + parser
│   │   ├── nginx_src.py   site config parser
│   │   ├── systemd_src.py units/timers via systemctl show
│   │   ├── proc_src.py    ss/ps/tmux/screen
│   │   ├── cron_src.py    crontabs/cron.d
│   │   └── fs_src.py      project dirs, git repos, du
│   ├── scanner.py         run_scan() orchestration + scan_state() (web/CLI scan state: running/scan_id/started, for polling)
  ├── correlate.py       evidence-based association + confidence scoring
│   ├── manifests.py       YAML manifests read/write/validate
│   ├── planner.py         removal plan generation (dry-run), impact/risk report, HMAC
│   ├── jobs.py            staged job engine (backup→quiesce→remove_runtime→remove_host→remove_files→validate), step records, backup recording, in-job restore
│   ├── helper_client.py   unix-socket client to del-helper
│   ├── auditlog.py        append-only audit records
│   └── web/               routes.py (router aggregator) + render.py, formatting.py,
│                          queries.py, orphans.py, gallery.py, docker_df.py,
│                          auth_routes.py, dashboard.py, apps.py, plans_jobs.py,
│                          resources.py, assistant.py, settings.py,
│                          static_routes.py + Jinja2 templates + static/
├── helper/
│   ├── del_helper.py      root daemon (stdlib only) — source; deployed to /usr/local/lib/del-helper/
│   └── validation.py      pure argument/path validation, imported by the daemon
├── config/                del.toml, the three .service units, nginx site, helper-policy.json
├── database/del.db
├── manifests/*.yaml
├── backups/
├── logs/
├── docs/                  durable reference (this file, DISCOVERY, REMOVAL-LIFECYCLE, …)
├── reports/<date>/        finished one-off audits and working notes
├── scripts/
│   ├── install.sh         idempotent installer (helper deploy, units, nginx, health)
│   ├── del-admin          CLI: create-admin, change-password, migrate, rescan, backup-db
│   ├── gen-registry.py    writes docs/PORT-REGISTRY.md from the latest scan (read-only)
│   ├── gen-registry.sh    thin wrapper around gen-registry.py
│   └── make-demo-app.sh   builds a disposable demo app for exercising removal
└── tests/
```

### Frontend (`web/templates`, `web/static`)

Server-rendered Jinja2: `base.html` is the shell, one template per route,
`_macros.html` for shared markup (confidence meter, status badge, dry-run/LIVE
tag, copyable ids), `_icons.html` for inline SVG icons, `_glossary.html` for the
Help rail and its mobile sheet, `_assistant_dock.html` for the Ask tab. The
visual language, component classes, table contract, shortcuts and stored
preferences are documented in `docs/UI.md`.

`render.py` owns the shared template context: the user, CSRF token, the
assistant status, the glossary context for the page (`glossary_ctx`), and the
sidebar **scan stamp** (`_scan_block`: whether discovery is running, plus the
latest completed scan's id, age and duration; it never fails a render).

Static files and who loads them:

| File | Loaded by |
|---|---|
| `app.css`, `theme-init.js`, `favicon.svg`, `fonts/*.woff2` | every page, including `login.html` |
| `app.js`, `assistant.js` | every authenticated page (`base.html`) |
| `removal.js` | `plan.html`, `job_detail.html` |
| `gallery.js` | `view_apps.html` |

All are served unauthenticated by `static_routes.py` (only `static/` itself
and `static/fonts/` are public; `static/icons/` stays private for
`/app-icon/{domain}`). Templates link them with `asset('name')`, which appends
a short content hash; a request with the current hash gets
`Cache-Control: public, max-age=31536000, immutable`, so repeat page views
fetch only the HTML. CSP is `script-src 'self'; style-src 'self'; font-src
'self'`, no `'unsafe-inline'`, no CDN, no third-party code.

`app.js` exposes `window.DEL` (`toast`, `theme`, `util`, `sortValue`,
`runScan`, `palette`; `assistant.js` adds `DEL.assistant`) and runs page
blocks guarded by element checks:

- The table engine (`enhanceTable`, every `table.table` except `.table-plain`
  / `.job-steps`): search, sort on parsed values, per-column filter popovers,
  quick chips, column chooser and density, pagination, column resize, CSV
  export, clickable rows; per-table state in `localStorage["del.table.<id>"]`.
- The scan stamp: run buttons post `/scan` with `Accept: application/json`
  (the route answers JSON, or redirects for a plain form post); while a scan
  runs it polls `GET /scan/status` every 2.5 s and reloads the page once the
  new inventory is published.
- Shell: sidebar collapse and resize, rail tabs (Help glossary filtered by
  `<body data-glossary>`, Ask dock), mobile drawer and bottom sheets, a shared
  focus trap, toasts, flash banners, `data-confirm`, submit guards.
- ARIA tabs with `#tab-id` deep links; remembered `details.section` state;
  the dashboard site plan grouping.
- The command palette (`<dialog id="cmdk">`, Ctrl/Cmd+K): pages from the
  sidebar, applications from `GET /palette.json` (fetched once, best-effort),
  up to six of those the browser has opened (`localStorage["del.recentApps"]`),
  their domains as "Open site", and actions; plus `g`-key navigation, `/`
  (site-plan filter on the dashboard, otherwise table or gallery search), `t`
  and `?` shortcuts.
- The dashboard site plan filters in the browser on `data-find` (name, slug,
  kind, status, enabled domains). Enter opens the first visible lot. The scan
  stamp adds `is-stale` when the inventory is older than the auto-scan
  interval, or 24 hours when that interval is off.

`removal.js` holds the plan page's complete-removal preset and the execute gate
(typed confirmation for volume deletion, live-mode styling), and the job poller
(`GET /jobs/{id}/status` every 2 s backing off to 10 s, paused while the tab is
hidden, not started for a job that is already finished). `gallery.js` holds
the View Apps launchpad (search operators, categories, favourites, hidden apps,
layout, drag order in `localStorage["del.appGallery.v1"]`).

The Assistant is a **read-only** inventory chat (`docs/ASSISTANT.md`): Ollama
Cloud model, no helper or planner access, suggested prompts per scope (general
/ app / orphans / resource type / resource). It lives on `/assistant` and as the
Ask tab of the right rail on every other authenticated page. The dock does not
request prompts or targets until that tab is opened.

## Data model (SQLite)

- users(id, username, password_hash, created_at, last_login)
- sessions(token_hash, user_id, created, expires, ip) — Settings → Account lists
  the signed-in user's unexpired rows (time, expiry, IP) and can delete all but
  the current one.
- scans(id, started, finished, status, stats_json)
- applications(id, slug, name, status, kind, protected, manifest_path, first_seen, last_seen)
  — `first_seen` / `last_seen` are scan IDs. The UI resolves them to `scans.started`
  timestamps. **Installed** and **Last changed** are computed at read time (not
  columns) by `formatting._app_dates` from the app's own non-shared resources.
  Installed = earliest creation time: directory `birthtime`, custom unit
  `fragment_birthtime`, enabled nginx site `file_birthtime`, container `created`;
  capped by when DEL first saw the app (`applications.first_seen`; shown as
  "by"/"on or before"). Directory ctime/mtime are never install signals — they
  move whenever an entry is added. Last changed = newest of directory `mtime`,
  git `head_committed_at`, container `created`, unit `fragment_mtime`, site `file_mtime`.
- resources(id, type, key, display, path, state, data_json, first_seen scan, last_seen scan)
  — directory resources include `mtime`/`ctime`/`birthtime` ISO timestamps in data_json
  (birth time from statx via `stat -c %W`, `discovery/file_times.py`) and git
  `head_committed_at`; custom systemd units add `fragment_birthtime`/`fragment_mtime`;
  nginx sites add `file_birthtime`/`file_mtime`; containers include Docker `created`.

- associations(app_id, resource_id, confidence, ownership, shared, data_loss_risk,
  removal_eligible, recommended_action, evidence_json, source, approved_by_user, excluded,
  user_excluded, user_shared)
- plans(id, app_id, created, options_json, steps_json, status, hmac)
- jobs(id, plan_id, mode dry_run|live, started, finished, status, user_id)
- job_steps(id, job_id, seq, stage, operation, args_json, state, exit_code, output_sanitized, started, finished, reversible)
- backups(id, job_id, kind, src, dest, sha256, size, created)
  — one row per completed backup step of a **live** job, written by `jobs._record_backup`
  before any deletion runs; this is what the in-job restore path reads. `kind` is the
  operation name (`file_backup` / `volume_backup` / `backup_tar`), `src` is the path
  (or volume) that was backed up and `dest` the archive. **`sha256` and `size` are
  declared but never populated** — backups are plain copies, not content-addressed.
- audit_log(id, ts, user_id, action, subject, details_json)  — no secrets ever.
  Deliberately not indexed: nothing in the codebase reads it. It needs a retention
  policy, not an index.
- settings(key, value) — dropped in migration 005 in favor of `del.toml`.

### Migrations & Database Performance

- `001_init.sql`: Base tables (users, sessions, scans, applications, resources, associations, manifests, plans, jobs, job_steps, backups, audit_log, settings).
- `002_indexes.sql`: 11 secondary indexes on hot foreign keys and filters (associations, resources, applications, jobs, scans).
- `003_assistant.sql`: Persistent conversation history (`assistant_threads`, `assistant_messages`) for Ollama AI advisor.
- `004_association_reviews.sql`: Operator review annotations (`user_excluded`, `user_shared`, `approved_by_user`) preserved across scans.
- `005_drop_settings.sql`: Removed obsolete `settings` table.

Every SQLite connection initializes with:
- `PRAGMA journal_mode = WAL;` (concurrent reader/writer isolation)
- `PRAGMA synchronous = NORMAL;` (safe for WAL, minimizes fsync stalls)
- `PRAGMA foreign_keys = ON;`
- `PRAGMA busy_timeout = 5000;`
- `PRAGMA cache_size = -32000;` (32MB dedicated page cache per connection)
- `PRAGMA mmap_size = 67108864;` (64MB memory-mapped zero-copy I/O for hot reads)
- `PRAGMA temp_store = MEMORY;` (in-memory sorting and transient tables)

Two omissions are deliberate and were measured: no standalone
`resources(last_seen)` (it makes the query planner flip a join and the whole set
regresses; the composite covers it), and no `ANALYZE` (with `sqlite_stat1` present
the dashboard's "uncertain" query picks a skip-scan and degrades ~10x).

### Host Telemetry API (`/api/telemetry`)

Authenticated endpoint providing real-time system performance telemetry:
- System load averages (1m, 5m, 15m) via `os.getloadavg()`.
- Host memory statistics parsed from `/proc/meminfo` (`total_bytes`, `available_bytes`, `used_pct`).
- Root filesystem disk capacity and usage (`total_bytes`, `used_bytes`, `free_bytes`, `used_pct`).
- Docker reclaimable space from cached `docker_df`.
- Background scanner engine status (`running`, `scan_id`, `started`).

### Display timezone (UI)

All human-facing datetimes in the web UI are rendered in **America/New_York**
(Eastern) as compact `MM-DD-YY H:MM AM/PM` (e.g. `05-13-26 7:31 AM`) — no
timezone suffix. Storage remains UTC (sqlite `datetime('now')`, Docker
`Created` with `Z`, filesystem ISO-Z). Naive timestamps read from SQLite are
treated as UTC. Calendar day follows Eastern. Helpers: `_format_dt`,
`_relative_dt`, `_iso_sort_key` in `backend/del_app/web/formatting.py`,
exposed to templates as the globals `format_dt`, `relative_dt` and
`iso_sort` (`backend/del_app/web/render.py`).

One exception, outside the UI: `del-admin backup-db` names its snapshot with
`datetime.now()` — host local time, not UTC.

### Dashboard (`/`)

`dashboard.py` renders one read transaction over the latest completed scan:

- **Site plan**: every current application as a tile ("lot") showing its
  resource count, grouped by kind (compose, container, systemd, manifest, …)
  or by status (running, stopped, absent, unknown); a corner flag marks apps
  holding weak claims to review (ownership "possible" or confidence below 60),
  and protected apps are marked.
- **Figures**: applications, orphan rows, shared resources (used by 2+ current
  apps or a system path), uncertain mappings, disk usage (project directories
  plus Docker volumes) and Docker's reclaimable space, running jobs. The two
  Docker numbers come from `docker_df.py`: `docker system df` runs on a
  background thread and is cached for five minutes (stale-while-revalidate);
  until the first measurement finishes they show "measuring" rather than 0,
  and if docker does not answer they keep the last real figures (or say
  "Docker did not answer") and retry after a minute.
- **Since the previous scan**: apps that appeared or disappeared, resources
  added/removed per type, and app status changes (from the `app_status` map
  each scan stores in `scans.stats_json`).
- **Needs attention** (apps with uncertain mappings, linking to their
  Readiness tab) and **Recent jobs**.

### Live app gallery (`/view-apps`)

`GET /view-apps` is an authenticated, read-only projection of the inventory. A
domain is eligible only when it belongs to an application and an **enabled**
Nginx resource from the latest completed scan. DEL verifies each candidate over
HTTPS (DNS, certificate, proxy route, and answering upstream). A domain becomes
a launch card when it answers below 500, **except** a 401/403 auth wall whose
site proxies to loopback ports where nothing accepts a TCP connection: nginx
answers basic-auth challenges itself, so that response proves only nginx is up.
Everything that fails — connection or TLS errors, 5xx, or an auth wall in
front of a dead upstream — is listed in the page's **Unavailable** table with
the reason ("HTTP 502", the error, or "login page only: nothing listens on
port N") and the app's status, instead of silently disappearing.
Health results are cached in-process for five minutes and served
**stale-while-revalidate**: whatever is cached renders immediately and a stale
batch is re-probed on a background thread, coalesced so concurrent requests do not
stack up refreshes. Only two cases block on the network — an explicit `?refresh=1`,
and a domain with nothing cached at all (the first load after a restart, where
blocking is the difference between a populated gallery and an empty one). The
gallery does not write to SQLite: categories, stars, hidden cards, size/density,
view mode, and drag order are browser-local settings (`gallery.js`).

**Icons are proxied, never loaded cross-origin.** Cards point `<img>` at
`GET /app-icon/{domain}`, which is itself session-authenticated. That route
normalizes the hostname, requires it to be an enabled Nginx site in the latest
scan (so it cannot be used as a general-purpose outbound fetcher; the set of
allowed domains is computed once per completed scan), then tries a bundled icon,
`/favicon.ico|png|svg`, and finally the root page's `<link rel="icon">` /
apple-touch-icon, server-side with short timeouts. Bodies are capped at
128 KiB and must be images. Results are cached twice: in process memory (at
most 256 domains, oldest evicted first) and as one small file per domain in
`icon-cache/` next to the database (`<domain>.icon`: content type, newline,
body; an empty file is a cached miss), so a restart does not refetch every
icon. A hit is kept 7 days, a miss 6 hours; concurrent requests for one domain
share a single lookup. Anything that is not a usable image, including a `401`
with a `WWW-Authenticate` header, becomes an empty **204** and the card falls
back to its initial letter.

The reason is concrete: pointing the `<img>` straight at the third-party origin
made the *browser* issue those requests, so every app behind HTTP basic auth
answered `401 + WWW-Authenticate` and the browser opened a credential dialog on
top of a page the operator was already authenticated to.

### Other routes worth knowing

- `GET /favicon.ico` — 301 to `/static/favicon.svg`. Browsers request it
  unprompted; it used to 404 on every page load.
- `/static/*` — the files in `static/` and `static/fonts/` (`app.css`,
  `app.js`, `assistant.js`, `removal.js`, `gallery.js`, `theme-init.js`,
  `favicon.svg`, the woff2 fonts and their licences). A request carrying the
  file's current content hash (`?v=`, as `asset()` writes it) and every font
  get `Cache-Control: public, max-age=31536000, immutable`; anything else gets
  `public, max-age=300, must-revalidate`. Other paths, including
  `static/icons/`, return 404.
- Unauthenticated routes are exactly `/login`, `/healthz`, `/favicon.ico` and
  those `/static/*` paths. Everything else, `/app-icon/{domain}` and
  `GET /palette.json` included, depends on `auth.require_user`.
- `POST /scan` starts the scan on a background thread. With
  `Accept: application/json` (the sidebar stamp and Settings button) it answers
  `{"started": true}`, or 409 `{"started": false, "error": …}` when a scan is
  already running; a plain form post redirects with `flash=Scan+started`.
  `GET /scan/status` returns the current `scanner.scan_state()` plus the most
  recent `scans` row as JSON, for the scan stamp to poll.
- `/apps/{slug}`, `/apps/{slug}/plan` and `/plans/{id}` return 404 for an
  unknown slug/id. `/plans/{id}/execute` returns 404 when the plan id does not
  exist (`planner.PlanNotFoundError`) or 409 when the plan exists but fails
  integrity/other `PlanError` checks. `/jobs/{id}/status` returns a 404 JSON
  body for an unknown job and otherwise includes `progress {done, total, pct}`
  and `current_step` alongside the raw job status.
- `POST /apps/{slug}/remove` is the one-click "Remove app now" button on the
  app detail page: it builds a complete-removal plan (all discovered named
  volumes approved, `remove_images=exclusive`, bind data/repo/networks all
  on, backup=none) and runs it live immediately — no separate dry-run or
  execute step. It still goes through `planner.build_plan`/`persist_plan`, so
  the same safe-delete checks, shared-resource preservation and
  `probable`/`possible` preservation apply as the step-by-step plan builder.
  404 for an unknown slug, 403 if the app is `protected`. The browser's single
  native confirm dialog is the only prompt; there is no typed-phrase gate
  (unlike `/plans/{id}/execute`, which still demands the `y` phrase for live
  volume deletion, this route supplies that phrase internally). The
  step-by-step "Plan removal…" flow (`/apps/{slug}/plan`) still exists
  alongside it for anyone who wants to review or dry-run before running live.

## Confidence scoring

Levels: confirmed (95–100), high (80–94), probable (60–79), possible (30–59),
unrelated (<30), manual (`source = 'manual'`). Each association stores evidence
items {source, statement, weight}. Compose project label = confirmed. Nginx
proxy_pass port → published container port = high. For host-network containers (no
published port mapping to key off), `proc_src` traces a listening port's pid back to
its owning container by matching the socket inode in `/proc/<pid>/fd` — an exact
match, with no uid-based fallback; if it cannot be resolved, "unresolved" is the
answer. A proxy_pass port matching that cgroup-resolved container's listener is
high confidence, with evidence naming the container and noting "(host network)".
Networks are correlated the
same way compose projects are seeded plus by attached-container name (not id, so
a network survives container recreation without losing its owner mapping); a
network attached to containers from more than one app is `shared` and preserved
unless approved per-app. Nginx configs that no longer have a live upstream to
match (app already stopped) are still attached to the correct app when the
config's `server_name` slugifies to *exactly* the app's slug — this catches
config debris (`.conf`/`.bak`/disabled copies included) that plain port-matching
would otherwise leave behind, and upgrades (rather than skips) a weaker
port-match claim the app may already hold on that same file, so an app's own
stale `sites-available` copy is always removal-eligible along with the rest of
the app. Name similarity alone = possible, never auto-removable.

**What actually becomes a plan step** (`planner._classify`): only `confirmed`,
`high` and `manual` associations, and only when they are not excluded, not
`removal_eligible = 'blocked'`, and not (shared and unapproved). Everything else is
recorded as a preserved resource plus a warning. In particular:

- `probable` is **never** a step. `_classify` returns "not a step, requires
  per-resource approval" for every `probable` row unconditionally — it does not
  consult `approved_by_user`. Approving a `probable` association therefore does not
  make it removable; raise it with a manifest entry instead.
- `approved_by_user` is read in exactly two places: to unblock a
  `confirmed`/`high`/`manual` association that is flagged `shared`, and to approve
  an individual named volume for `volume_rm`.
- `possible` and `unrelated` are always preserved.

Manifest entries are set to `level = "manual"` in memory by `correlate.py`, but
`scanner.py` persists the literal string `"correlate"` into `associations.source`
for every row it writes. Both `_level()` implementations (`planner.py`,
`web/routes.py`) derive `manual` only from `source == 'manual'`, so a manifest
association — confidence 100 — is displayed and classified as **`confirmed`**, not
`manual`. It is fully removal-eligible either way; only the badge differs from what
these docs used to promise.

A compose project whose name matches an existing app is only merged into it
when the compose file's *own* top-level project directory (the
`{scan_root}/{first-component}` directory containing it) also belongs to that
app. If it instead sits inside a *different* app's project tree — an archived
clone at `/apps/agyinstall/boxy/docker-compose.yml` matching the real
`/apps/boxy` app by name — it is attached to the owning tree's app (seeding a
placeholder app for it if needed) at confidence 55 / `possible` / `shared`,
with an evidence statement naming both the foreign owner and the same-named
app it did *not* merge into, rather than being silently absorbed at full
confidence.

Applications are not only Docker/Compose — a purely systemd-managed service
(no container at all) is seeded as its own first-class application (`kind
"systemd"`) whenever one of its custom (non-vendor) unit's
`WorkingDirectory`/`ExecStart` resolves to a directory directly under a scan
root, e.g. `/apps/xtr`. `systemd_src.py` also captures custom unit *files* that
are currently disabled/inactive (not just ones `systemctl list-units` reports as
loaded), so a stopped non-Docker service (e.g. `htmls`, `ppv`) is still
discovered and correlated instead of being invisible. See docs/DISCOVERY.md
"Correlation rules" for the full attachment-order detail.

## Scan publication and review persistence

The scanner gathers host data before opening a SQLite write transaction. A batched
resource upsert, app updates, association replacement, stale-owner cleanup, and the
completed scan marker commit atomically. Discovery exceptions, invalid manifests,
correlation errors, or write failures retain the previous inventory and record a
failed scan with an outcome shown in Settings. Individual collectors still handle
some partial failures internally by logging and skipping an artifact.

Scans start from the sidebar stamp or Settings (`POST /scan`), from
`del-admin rescan`, after every live removal job, and — when
`scan_interval_hours` in `del.toml` is above 0 — from the in-process scheduler
(`scanner.start_scheduler`, started in `main.py`'s lifespan): every ten minutes
it scans if the latest completed scan is older than the interval, waits
min(interval, 1 h) after a failed attempt, and skips while a removal job runs.

`database/del.scan.lock` uses Linux `flock` to serialize web and CLI scans. The
thread lock prevents concurrent calls within a process. Startup cleanup checks
the file lock before abandoning a running scan, and a crashed CLI's stale row does
not block a new scan. Inventory routes, planner reads, and assistant context open
read transactions to keep multiple queries on the same published inventory.

Migration 004 adds explicit operator exclusion/shared flags. Review decisions
survive for a still-associated app/resource pair; approval is revoked when its
safety classification changes. Correlation-derived shared flags are recomputed.
Resource owner lookups omit stale apps and batch IDs in groups of 400. The
orphan-count cache is invalidated when an operator reviews an association.

`GET /healthz` returns HTTP 503 with `ok: false` when the database cannot be opened
or required tables/review columns are unavailable. It does not test host discovery,
the privileged helper, Nginx routing, or the assistant provider.

## Removal job engine

Plans are immutable once approved (any edit to `steps_json` fails the HMAC check).
A job executes plan steps in stage order; each step is recorded before execution
(state=running) and after (done/failed). Any failure halts the job before
downstream deletions. Steps carry reversible=true/false.

Workers are in-process daemon threads. At process startup, any job still marked
`pending` or `running` is failed and audited as `job_abandoned`, because its
worker cannot have survived the restart.

`planner.STAGE_ORDER` is exactly six values — `backup`, `quiesce`,
`remove_runtime`, `remove_host`, `remove_files`, `validate` — and those are the
only strings that ever appear in `job_steps.stage`. Analysis and preview happen at
plan-build time, and the report is the job's terminal status plus its audit
records; none of the three produce step rows.

`_check_stage_order` enforces `STAGE_ORDER` as non-decreasing across a plan's
steps, raising `PlanError` if violated; both `build_plan` and `persist_plan`
call it, so a stage-order bug fails at build/persist time rather than at
execution. Separately, when building a `compose_down` step the planner checks
whether another app's compose project would resolve to the *same* Docker
Compose project label under a different path, and if so appends a plan
warning (not a hard error) naming both apps and paths — `compose_down`'s
label sweep is host-wide, so that collision could remove the other app's
containers too.

**Plan build refuses on an empty result.** `build_plan` scopes an app's
associations to the latest scan with `status = 'done'`. If the app has
associations but none in that scan, it raises `PlanError` telling the operator to
re-scan, rather than emitting an empty plan that would execute "successfully"
having removed nothing. That is exactly what used to happen when a plan was built
while a scan was running.

**Rollback.** Live backup steps write a `backups` row before any deletion. If a
`nginx_rm_site`, `nginx_test_reload`, `systemd_disable` or `systemd_rm_unit` step
fails, the engine restores this job's recorded backups newest-first via the
helper's `path_restore` and audits the real per-backup outcome. Volume archives are
skipped and reported as manual-restore — a `volume_backup` is a tar of contents,
not a filesystem path. Independently of all this, `nginx_rm_site` restores its own
files inside the helper if `nginx -t` fails after removal; that path never depended
on the `backups` table and has always worked.

**Resume exists in the engine but is not exposed.** `jobs.retry_job()` resets the
first failed step and everything after it and re-executes. Nothing calls it —
there is no `/jobs/{id}/retry` route, no UI button and no CLI subcommand. Resuming
a failed job today means calling it from a Python shell.

Live volume deletion requires: plan option enabled + per-volume checkbox (or a
per-association approval) + typed confirmation phrase at execution time.

**After a successful live job**, the engine runs a rescan so the UI reflects the
new reality. A rescan failure is logged and audited as
`post_removal_rescan_failed` rather than swallowed.

## Security summary

- Bind 127.0.0.1 only; Nginx terminates TLS with the bjk.ai wildcard cert.
- Session cookies: HttpOnly, Secure, SameSite=Lax; server-side session store; 12h
  expiry; expired rows swept at login.
- CSRF token on every mutating form/request; login rate limiting — a plain
  5-per-60s in-memory sliding window per IP, no backoff.
- Argon2id password hashing (`argon2.PasswordHasher`, the only hasher — no bcrypt
  fallback); admin account created via CLI, no defaults.
- CSP is sent by Nginx: `default-src 'self'`; scripts, styles and fonts are
  self-only (`img-src` also allows `data:`, used by the `/miscwork.html`
  inventory export on the same vhost, not by the app UI). HSTS
  carries `includeSubDomains`. No external asset origins, no third-party code.
- Secrets never logged; env values stripped at the discovery source layer; the DB
  and backups directory are not world-readable.
- Helper socket 0660 root:bjkai; 24 allowlisted operations; args validated twice;
  helper code and policy deployed root-owned outside `/apps/del`.
- DEL itself flagged protected=1; planner refuses to plan its removal.
