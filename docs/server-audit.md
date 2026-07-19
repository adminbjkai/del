# DEL — Phase 2 Server Audit (bjkai-2tb-ubuntu)

Audit date: 2026-07-19. Read-only inventory across Docker, Nginx, systemd, cron, processes,
and the filesystem. Sources: `/apps/del/data/audit/*.md` (human notes), `/apps/del/data/audit/*.json`
(machine data), `/apps/del/data/initial-inventory.json` (merged summary), and
`/apps/del/docs/ARCHITECTURE.md` (DEL's own design). No system state was changed to produce
this report.

## 1. Executive summary

This server runs a large, organically-grown collection of self-hosted applications — 212
running containers across 111 Compose projects, 271 project directories under `/apps`, 56
custom systemd units, and 39 publicly-bound sockets — accumulated without a consistent
deployment or decommissioning process. The audit found significant reclaimable waste (101.6 GB
of Docker build cache, 65 orphaned volumes, 70 nginx configs that are no longer live, 28
abandoned project directories) and several correctness and security issues that predate DEL
(two failed systemd units, a stale duplicate `fireshare_migrated` deployment pointing at a
compose file under a `/data` path that doesn't exist, Samba and Cockpit exposed on all
interfaces, root-owned dev servers serving production traffic). None of this requires
remediation before DEL can be built: DEL is a purely additive, read-only-by-default discovery
and removal tool, deployed as two new host systemd units (`del-web`, `del-helper`) behind a new
Nginx vhost on port 8075, and it will not touch any existing application until an operator
reviews and approves a removal plan for it specifically. This report documents current state,
the risks DEL's design already accounts for, and the exact set of new and (later) modifiable
files DEL introduces.

## 2. Current deployment patterns

Applications on this host are deployed through at least four different mechanisms with no
single source of truth:

- **Docker Compose** — the dominant pattern: 111 running compose projects, 128 directories with
  a compose file at their top level, 277 compose files discovered in total (many stale/retired
  copies).
- **Bare `docker run` containers** — 3 containers (`netmuxd`, `anisette`, `2fauth`) run outside
  Compose with `restart=always`/`unless-stopped` policies and no project label.
- **Host systemd units** — 56 custom units (52 services, 2 timers, 1 socket, 1 templated
  service) mapping close to 1:1 with `/apps/*` directories; this is the pattern DEL itself will
  follow.
- **Ad hoc / manual processes** — nohup-style orphans with no systemd unit and no container
  (e.g. an `/apps/htmls` node server up 787h, a `claude-code-router` instance up 671h, a manual
  `adb` daemon, a manual gunicorn), plus 4 cron entries launching scripts directly.

There is no manifest, inventory, or naming convention tying a running workload back to its
directory, unit, or nginx site — which is precisely the gap DEL's discovery/correlation engine
is designed to close.

## 3. Docker and Compose findings

| Resource | Count | Notes |
|---|---|---|
| Containers | 212 (all running, 0 stopped) | 93 healthy, 118 no healthcheck, 1 `starting` (`docker-api-1`) |
| Images | 302 repo:tag rows (299 unique) | 3 dangling `<none>`, all 3 still referenced by running containers (version-drift signal, not a real reclaim) |
| Volumes | 160 | **65 orphan candidates (0 attached containers)**, incl. 13 anonymous 64-hex volumes |
| Networks | 109 | 0 empty non-default networks |
| Compose projects | 111 | 3 containers unmanaged by Compose |

**Disk usage (`docker system df`):**

- Images: 323.5 GB total, 76.97 GB reclaimable (23%)
- Containers: 12.08 GB writable layers
- Local volumes: 52.42 GB, 19.69 GB reclaimable (37%)
- **Build cache: 101.6 GB across 879 entries, 0 active** — the single largest reclaim target on
  the host, larger than all reclaimable images and volumes combined
- Largest volumes: `cap4_minio_data` 25.83 GB, `cap_v2_minio_data` 6.66 GB,
  `openshell-cluster-nemoclaw` 4.74 GB, `netdata_netdatacache` 3.40 GB

**Missing compose config files** — two registered compose projects point at files that do not
exist on disk, meaning they can never be recreated with `docker compose up` from their labeled
path:

- `bitwarden` → `/apps/bitwarden/bwdata/docker/docker-compose.yml` (missing; the running
  deployment actually lives at `/apps/bitwarden/bwdata`, 11 containers)
- `fireshare_migrated` → `/data/apps/migrated/fireshare/docker-compose.generated.yml`
  (missing — `/data` does not exist on this host at all; see §10, the **fireshare duplicate**)

**Project-name collisions** — `docker` (two unrelated apps, `/apps/kanbu/docker` and
`/apps/karakeep/docker`, share the generic project name) and `compose`
(`/apps/komodo/compose`) are both collision-prone Compose project names that correlation logic
must disambiguate by working directory, not name alone.

**Restart policies:** 165 `unless-stopped`, 46 `always`, 1 `no` (`blinko-postgres` — healthy now,
but will not restart automatically after a daemon restart/reboot).

No secrets were recorded anywhere in this audit; environment variable names only.

## 4. systemd findings

| Metric | Count |
|---|---|
| System services loaded | 213 |
| System service unit files | 354 |
| Custom units in `/etc/systemd/system` | 56 (52 services, 2 timers, 1 socket, 1 templated) |
| User (bjkai) services loaded / unit files | 70 / 107 (0 user timers) |
| Custom user units | 3 (2 bjkai, 1 orphaned root — root user manager isn't running) |
| System timers | 19 (2 custom: `bjkflix-dizipal-cf.timer`, `bjkflix-ios-refresh.timer`) |
| **Failed units** | **2** |

**Failed units:**

| Unit | State | Cause |
|---|---|---|
| `notecapai-doc-worker.service` | failed, disabled | Retries until compose postgres (:9103) is up; currently down and not enabled, so it won't restart at boot |
| `onlook-web.service` | failed, enabled | Requires `supabase-onlook.service`, which does not exist on this host — will fail every boot |

**Other notable anomalies:**

- Inline secrets in unit files: `openclaw-gateway.service` embeds `NOTION_API_TOKEN` and
  `GH_TOKEN`; `xtr-dashboard.service` embeds `UNDERSTAND_ACCESS_TOKEN` (values redacted from
  this audit; names only recorded). Should move to `EnvironmentFile` with `0600` perms.
- **xtr double-start risk**: `xtr.service` (systemd, active, uses flixapp's venv uvicorn) and
  bjkai's `@reboot` cron both launch `/apps/xtr/app.py` independently.
- **Triplicate `openclaw-gateway` definitions**: an active system unit (with inline secrets), a
  disabled newer bjkai user unit (uses an `EnvironmentFile`), and an orphaned root user unit
  (root user manager isn't running) — only the system unit actually runs.
- Three URL-shortener implementations exist, two active
  (`bjkai_shorturl_by_claude` Rust, `url-shortener` in `/apps/url2`).
- `shows.service` is enabled but inactive (wanted at boot, not currently running).

## 5. Nginx findings

- nginx 1.18.0 (Ubuntu); `nginx -t` passes.
- `sites-available`: 219; `sites-enabled`: 162 (112 symlinks, 50 regular files).
- **13 dead upstream references** across enabled sites (proxy target has no listener):
  `dockhand.bjk.ai`, `fizzy.bjk.ai`, `manynotes.bjk.ai`, `metabase.bjk.ai`, `netdata.bjk.ai`,
  `notecapai.bjk.ai`, `shows.bjk.ai`, `stv.bjk.ai` (2 dead ports), `trflix.bjk.ai` (referenced
  from 3 locations), `twenty.bjk.ai`, `viniplay.bjk.ai`, `zabbix.bjk.ai`, `zettelgarden.bjk.ai`
  (2 dead ports) — 17 dead upstream references in total across these 13 sites.
- **70 "available-only" configs** exist in `sites-available` but are never enabled (mostly
  `.bak`/`.retired`/`.stale` copies) — orphaned config clutter, not live risk.
- Of the 50 regular (non-symlink) files in `sites-enabled`: 36 differ from their same-named
  `sites-available` counterpart (the enabled copy is authoritative; the available copy is
  stale), 13 have no `sites-available` counterpart at all, and 1 is identical.
- 8 shared-upstream-port groups where multiple hostnames proxy to the same backend port (e.g.
  `:8055` served by `baserow.bjk.ai`, `n50.bjk.ai`, and `ppv.bjk.ai`).
- No duplicate `server_name` values across enabled configs.
- Certificates: nearly all sites use the `bjk.ai` wildcard cert; 3 exceptions have their own
  certs (`api.boxy.bjk.ai`, `docs.boxy.bjk.ai`, and a self-signed cert for `openclaw`).
- `ssl_protocols` in `nginx.conf` still permits legacy TLSv1/TLSv1.1.
- **`del.bjk.ai` is unclaimed** — no existing config anywhere references it, confirming DEL's
  new vhost name is free to use.

## 6. Project/repository findings

| Metric | Value |
|---|---|
| Compose files discovered | 277 |
| Project directories inventoried | 271 |
| Dirs with a compose file at top level | 128 |
| Dirs with a `.env` (names/counts only) | 53 |
| Git repositories | 166 |
| **Git repos with uncommitted changes** | **106 (64%)** |
| Running compose projects | 111 |
| **Abandoned candidates** (compose dir, no matching running project) | **28** |
| Dirs > 5 GB | 9 |

Abandoned candidates include `baserow`, `flagsmith`, `kestra`, `wanwu`, `zabbix`, `netdata`
(7.4G), `poco-claw`, `dockhand`, `nginx-ui`, `monitoring`, `notes-dashboard`, and the whole
"Cap" family (`Cap`, `cap3`, `cap5`, `cap_v2` — only `cap4` currently has a running project).
These are candidates for review, not confirmed-safe deletions — some may run under a
differently-named project.

Other duplicate-name clusters worth flagging for correlation logic: `b64pdf`/`b64pdf2` (both
running), `fileshare`/`fileshare2` (14.6G in `fileshare2`), `scrcpy`/`scrcpy2`, `/apps/ui` vs
`/apps/glmflix/ui` (identical compose; `ui` is the one running), `zublo` in `/apps` (running)
vs `/srv/apps/zublo` (data only), `Flussonic-2` + `flussonic2403` + `/opt/flussonic`,
`img2`/`img3`/`zimg`/`g3img`.

## 7. Port and process findings

| Metric | Value |
|---|---|
| Listening sockets (TCP+UDP) | 276 |
| Distinct listening ports | 192 |
| Owned by systemd services | 140 sockets (41 distinct units) |
| Owned by docker | 125 sockets |
| Owned by system (sshd etc.) | 3 sockets |
| Manual / unclassified | 8 sockets |
| Long-running (>1h) non-container interpreters | 38 |
| tmux sessions | 1 (`cai-0`, detached since 2026-06-05, likely forgotten) |

**39 sockets (24 distinct ports) are publicly bound (`0.0.0.0`/`::`/`*`)**, including several
that should not be internet-facing:

- **Samba (139, 445)** and **Cockpit (9091)** bound to all interfaces — verify firewall blocks
  external access.
- Root-owned dev-style servers serving production traffic publicly: AionUi
  electron-forge/webpack dev server (3000, 9000, root), `sema-shopping`/`semashop` `next start`
  (3015, 3019, root), `pm2-root` next-server (3020, root), boxy `fern docs dev` (3901, 3911).
- `anisette` (docker, port 6969) public — worth confirming intent.
- Expected-public items: ssh (22), nginx (80/443), Flussonic RTMP/HTTP (1935, 8050), NoMachine
  NX (4000), RustDesk (21118/21119/37617), Tailscale WireGuard (41641), TURN/STUN for
  nextcloud-aio-talk (3478).

**Manual/unclassified processes** (no systemd unit, not docker): a `claude-code-router` node
process orphaned since boot (671h, port 3456), a manual `adb` server in `/apps/glmflix`, a
manual gunicorn behind nginx for `/apps/17imgshare` (8087), an orphaned node server in
`/apps/htmls` (787h, port 8195), and a manual Playwright websocket server in `/apps/gongyu`
(19988). A duplicate `boxy` fern-docs process runs alongside its systemd-managed twin.

## 8. Cron and timer findings

- Cron entries recorded: 12 (4 stock `/etc/crontab`, 4 active `/etc/cron.d`, 4 in bjkai's
  crontab). Root's crontab exists but is empty.
- bjkai's crontab: `@reboot` launches `/apps/xtr/app.py` directly (overlapping with
  `xtr.service`, a double-start risk — see §4); `@reboot sleep 30` for
  `/apps/cap4/scripts/doc-worker.sh`; a nightly prune (`prune-raw-originals.sh`, 04:30) and a
  6-hourly health check (`health-watch.sh`) for cap4.
- `/etc/cron.d`: `anacron`, `certbot`, `e2scrub_all` are no-ops under systemd (guarded by
  `/run/systemd/system`); `sysstat` entries are commented out.
- 19 system timers total; only 2 are custom (`bjkflix-dizipal-cf.timer` every 12 min,
  `bjkflix-ios-refresh.timer` every 6 days for AltServer re-signing). The remaining 17 are stock
  distro timers (apt, certbot, logrotate, fstrim, etc.) — out of scope for DEL's application
  discovery.

## 9. Storage findings

- Root filesystem `/dev/nvme1n1p1`: 1.8T total, 812G used (47%), 929G available — healthy
  headroom.
- `/var/lib/docker`: 51.3 GiB.
- `/apps` total: ~254 GiB across 271 top-level entries. Largest: `immich-app` 68.4G, `models`
  48.9G, `fileshare2` 14.6G, `LTX-2` 7.6G, `netdata` 7.4G, `ideogram` 7.0G, `dl` 7.0G, `flixapp`
  6.4G, `boxy` 5.2G, `liam` 4.8G.
- Logs are modest and not a concern: `/var/log/journal` 503 MiB, nginx 63 MiB, audit 35 MiB.
- Biggest reclaim opportunities in priority order: **Docker build cache (101.6 GB, 0 active)**,
  reclaimable images (76.97 GB), the 65 orphan volumes, then large model-weight directories
  (`/apps/models` 48.9G, `/apps/LTX-2` 7.6G) if disk pressure ever requires it.

## 10. Shared-resource risks

Resources touched by more than one application are the primary hazard DEL's removal planner
must detect and block by default:

- **Shared networks**: `rowboat_net` (deck-renderer, llm-proxy, rowboat — 6 containers),
  `runtipi_tipi_main_network` (fireshare_migrated, runtipi), default `bridge` (jsoncrack, mtxt,
  plus non-compose anisette/2fauth), and `host` network (beszel, bjk-ai-flix, filebrowser,
  memos, notesnook, plus non-compose netmuxd).
- **Shared images**: 17 repo:tags used by multiple containers, mostly common bases
  (`mariadb:10.11`, `redis:latest`/`7.4-alpine`, `postgres:14`, `pgvector/pgvector:pg17`,
  `mongo:8`/`latest`, `nginx:alpine`, `meilisearch:v1.41.0`) plus a few app images (fireshare,
  opensparrow, gongyu, glean-backend, alpine-chrome) reused across projects. Removing "an app's"
  image could silently break a sibling app still using the same tag.
- **Shared upstream ports** in nginx: 8 groups where multiple hostnames front the same backend
  port (e.g. `baserow.bjk.ai`/`n50.bjk.ai`/`ppv.bjk.ai` all on `:8055`) — removing one site's
  nginx config is safe, but removing the backend behind that port breaks the others.
- **No volumes are shared across projects** — a rare piece of good news; volume removal is
  comparatively low-risk from a cross-app-collision standpoint (still gated by the double
  confirmation flow regardless).
- **The `fireshare` duplicate**: a live `fireshare` project at `/apps/fireshare` coexists with
  `fireshare_migrated`, whose compose file lives under a `/data` path that does not exist on
  this host at all, yet the container reports `running`. This is a stale migration remnant that
  must be surfaced as "uncertain" (§11), never auto-removed, since deleting it blind could
  remove the only running fireshare instance if the correlation is backwards.

## 11. Uncertain associations

These require explicit human confirmation before any removal plan can act on them — the
correlation engine should mark all of the following `probable` or `possible`, never
`confirmed`/`high`:

- `fireshare` vs `fireshare_migrated` — which is the "real" one is not automatically knowable;
  compose file for the latter is missing entirely.
- `bitwarden` — compose label points to a missing file; the actual running stack is one level
  up at `/apps/bitwarden/bwdata`. Path-based correlation must not assume the labeled path is
  authoritative.
- 28 abandoned-candidate directories (§6) — a compose file with no matching running project may
  mean genuinely abandoned, or may mean the project runs under a different (renamed) project
  label. Confidence: possible, pending manual confirmation.
- 65 orphan-candidate volumes, especially the 13 anonymous 64-hex ones and named volumes like
  `2_pg_db_data`, `2_pg_nc_data`, `appwrite_appwrite-*` that look like remnants of renamed or
  removed stacks — no owning container exists to correlate against.
- Project-name collisions `docker` and `compose` (§3) — name-based grouping alone is unsafe
  here; must disambiguate by working directory.
- Manual/orphan processes with no owning unit (htmls node server, claude-code-router,
  boxy's duplicate fern process, manual adb/gunicorn/Playwright servers) — process-level
  evidence only, no systemd or compose anchor, so ownership is inferred from cwd/port alone
  (`possible` confidence at best).
- 3 dangling images still referenced by running containers (postgres/mysql version drift) —
  correlated to their consuming containers, not orphaned, but flagged as a drift signal.

## 12. Recommended architecture

(Summarized from `docs/ARCHITECTURE.md`; see that document for full detail.)

DEL is a self-hosted admin app, served at `https://del.bjk.ai` behind Nginx, bound to
`127.0.0.1:8075` only. Stack: Python 3.12 + FastAPI/Uvicorn, Jinja2 + vanilla JS (no Node
toolchain, no external assets, CSP `default-src 'self'`), SQLite (WAL) at
`/apps/del/database/del.db`.

**Deployment is two host systemd units, not a container.** DEL's job is to inspect and modify
*host* state (systemd units, nginx configs, cron files, arbitrary project directories, the
Docker daemon itself), which would require a fully privileged container (Docker socket, `/etc`,
`/apps`, `/data`, `/run/systemd`, host PID namespace, root) — strictly harder to reason about
than two small host services with normal systemd restart policies and journald logging.

**Helper split** — the core privilege boundary:

- `del-web` runs as user `bjkai` (groups `docker`+`adm`): UI, auth/sessions/CSRF, the read-only
  discovery engine, correlation/confidence scoring, manifests, removal planner (dry-run), job
  orchestration, audit log. It never runs shell commands as root and never constructs shell
  strings from user input.
- `del-helper` runs as root, ~600 lines of stdlib-only Python, listening on a unix socket
  (`/run/del/helper.sock`, `0660 root:bjkai`). It exposes a **fixed operation allowlist** only
  (compose_down, container_stop/rm, image_rm, volume_rm, network_rm, systemd_stop/disable/rm,
  cron_rm, nginx_rm_site, nginx_test_reload, path_delete, tmux_kill, process_term, backup_tar,
  volume_backup, file_backup). Every argument is validated and canonicalized independently of
  `del-web`; every operation executes via subprocess arg-arrays (never `shell=True`) and every
  request is appended to `/apps/del/logs/helper-audit.log`.

This matches the existing on-host deployment pattern already used by 56 custom units on this
server (§4), so DEL introduces no new operational pattern for the sysadmin to learn.

## 13. Permission model

- `del-web` process: user `bjkai`, member of `docker` and `adm` groups (read access to the
  Docker socket and systemd journals; no root).
- `del-helper` process: root, the only process on the host authorized to execute DEL's
  privileged operations, reachable only via a unix socket permissioned `0660 root:bjkai` — no
  network exposure, no other user can reach it.
- Approval flow: `del-web` writes an approved plan, HMAC-signed with a key readable only by
  root and the `del` user, into the database, and passes `plan_id` + step to the helper; the
  helper **re-validates every argument against its own rules regardless of the signature** —
  the signature proves the plan wasn't tampered with in the DB, it does not substitute for
  helper-side validation.
- Protected roots that can never be deleted, even if listed in an approved plan: `/`, `/bin`,
  `/boot`, `/dev`, `/etc`, `/home`, `/lib`, `/lib64`, `/opt`, `/proc`, `/root`, `/run`, `/sbin`,
  `/srv`, `/sys`, `/tmp`, `/usr`, `/var`, `/apps`, `/data`, and `/apps/del` itself (DEL is
  flagged `protected=1` and the planner refuses to plan its own removal).
- No `sudoers.d` entry is required or used — the entire root-privileged surface is the narrow,
  auditable helper allowlist above, not ambient sudo.
- Session cookies: HttpOnly, Secure, SameSite=Lax, server-side session store, 12h expiry. CSRF
  token on every mutating request. Login rate-limited 5/min/IP with backoff. Argon2id password
  hashing (bcrypt fallback); no default admin account — created via CLI only.

## 14. Threat model

| Attacker / vector | Mitigation |
|---|---|
| Internet → Nginx → auth bypass | Nginx terminates TLS with the existing `bjk.ai` wildcard cert and sets security headers; `del-web` binds `127.0.0.1` only (unreachable except through Nginx); auth required on every route except `/healthz`; session cookies HttpOnly/Secure/SameSite=Lax; login rate-limited 5/min/IP with backoff |
| Compromised web session → arbitrary host command | The helper's fixed operation allowlist bounds the blast radius to the ~15 defined operations regardless of what `del-web` is tricked into requesting; the helper independently re-validates every argument, so a compromised `del-web` cannot smuggle an unapproved operation past it |
| Path traversal (`../`, symlink escape) | Every path argument is canonicalized with `realpath` before use; protected roots (§13) are refused unconditionally; paths must resolve under an approved root **and** appear in the specific approved plan |
| Command/argument injection | `del-helper` executes exclusively via subprocess arg-arrays with `shell=False`; `del-web` never constructs shell strings from user input anywhere in the codebase |
| Secrets exposure | Discovery sources strip environment variable *values* at the collection layer (names only, matching how this audit itself was conducted); secrets are never logged, never stored in the DB, never appear in the audit log |

## 15. Migration strategy

**None needed.** DEL is purely additive: it introduces two new systemd units, one new Nginx
vhost, and its own directory tree under `/apps/del`. No existing application, container,
compose project, nginx site, systemd unit, or cron entry is modified, moved, or restarted by
DEL's installation. Every existing app identified in this audit — including the ones with
issues (failed units, dead upstreams, orphan volumes, the fireshare duplicate) — continues
running exactly as-is until an operator explicitly builds and approves a removal plan for that
specific app through DEL's UI. Installing DEL is a zero-downtime, zero-risk operation for
everything already on the host.

## 16. Removal workflow

Removal proceeds through nine ordered stages per job, each recorded as a step (state
`running`→`done`/`failed`) before/after execution, with any safety-validation failure halting
the job before further deletions:

1. **Analyze** — resolve the app's associations against current live state (nothing assumed
   stale from scan time).
2. **Preview** — render the full plan (steps, warnings, preserved/blocked resources, estimated
   reclaimed bytes) for operator review; nothing executes yet.
3. **Backup** — `file_backup`/`volume_backup`/`backup_tar` for every resource the plan will
   touch, written to `/apps/del/backups`, before any mutation.
4. **Quiesce** — stop the running workload (`container_stop`, `systemd_stop`, `tmux_kill`,
   `process_term`) without yet deleting anything, so a failure here is trivially reversible.
5. **Remove (runtime)** — `compose_down`, `container_rm`, `image_rm` (refused if still
   referenced), `volume_rm` (requires plan option + per-volume checkbox + a typed confirmation
   phrase — second confirmation), `network_rm` (refuses bridge/host/none and any network with
   foreign containers still attached).
6. **Remove (host)** — `systemd_disable`/`systemd_rm_unit`, `cron_rm`, `nginx_rm_site` (backs up
   first, `nginx -t`, reload only on pass, automatic restore on failure).
7. **Remove (files)** — `path_delete` for project directories/bind data, canonicalized and
   checked against protected roots and the approved plan on every call.
8. **Validate** — post-removal checks confirm the resources are actually gone and nothing else
   broke (e.g., `nginx -t` still passes, no orphaned dependents appeared).
9. **Report** — final job status, reclaimed bytes, and a durable audit-log record; failed jobs
   are resumable from the failed step after the operator addresses the cause, and nginx/systemd
   stage failures trigger automatic restore from the stage-4 backups.

Only `confirmed`/`high`/`manual`-confidence associations are eligible for automated inclusion in
a plan; `probable` requires explicit per-resource approval; `possible` is always blocked until
manually confirmed by the operator (§11 lists this audit's current `probable`/`possible` cases).

## 17. Backup and recovery strategy

- Every removal plan's backup stage runs before any destructive operation: `file_backup`
  (timestamped copy before config edits — nginx sites, systemd units, cron files),
  `volume_backup` (`docker run --rm -v vol:/src:ro tar` into `/apps/del/backups`), and
  `backup_tar` for arbitrary project directories, all writing exclusively under
  `/apps/del/backups`.
- Backups are content-addressed and tracked in the `backups` table (`sha256`, `size`,
  `created`, linked `job_id`) so any backup can be located and verified independent of the
  filesystem.
- Nginx and systemd removal steps specifically restore automatically from their stage-4 backup
  if a later step in the same job fails (e.g. `nginx -t` fails after a site removal) — recovery
  here is not a manual runbook, it's a built-in job-engine behavior.
- Volume deletion is the one irreversible-by-default operation and is gated by three
  independent controls: the plan must have the option enabled, the specific volume must be
  checked, and the operator must type a confirmation phrase at execution time — not just at
  plan-build time.
- Jobs are resumable: a failed job can be retried from the failed step once the underlying
  cause is fixed, without re-running already-completed (and already-recorded) steps.
- DEL's own database (`del.db`) should be included in the host's existing backup routine (no
  new backup infrastructure required — it's a single SQLite file); the `del-admin` CLI provides
  a `backup db` subcommand for on-demand snapshots.

## 18. Test strategy

- **Unit tests** cover the components with the most correctness risk and the least need for a
  live host: confidence scoring thresholds, manifest load/save round-trips, plan-step ordering
  and stage sequencing, path canonicalization/protected-root refusal logic in the helper, and
  the helper's argument-validation rules for each allowlisted operation — all runnable without
  Docker, nginx, or systemd present.
- **Disposable test-app end-to-end test** — the only way to validate the full discovery →
  correlate → plan → execute path against real host state without touching a real application:
  stand up a minimal disposable stack (one compose project, one nginx site, one systemd unit,
  one cron entry, one bind-mounted data directory) purpose-built for this test, run a full DEL
  scan against it, confirm it's discovered and correlated at `confirmed`/`high` confidence, build
  a plan, execute a **live** (not dry-run) removal job against it, and assert every stage
  completed, the backups exist and are restorable, and every resource (container, volume,
  network, nginx site, systemd unit, cron entry, directory) is actually gone afterward. This is
  the only test permitted to run destructive/live operations, and only against resources it
  created itself.
- Every helper operation must be exercised in `dry_run=True` mode in unit tests to confirm it
  reports what it *would* do without making any change — this is the primary safety net for the
  helper allowlist and should be tested independent of the end-to-end app.
- CI should run unit tests on every change; the disposable end-to-end test is heavier (spins up
  real Docker/nginx/systemd resources) and can run on a slower cadence (e.g. pre-release) or
  on-demand.

## 19. Exact files that will be created

```
/apps/del/
├── backend/del/                      (or del_app — see INTERFACES.md package root)
│   ├── main.py, config.py, db.py, migrations/NNN_*.sql
│   ├── auth.py, models.py
│   ├── discovery/ (docker_src.py, compose_src.py, nginx_src.py, systemd_src.py,
│   │               proc_src.py, cron_src.py, fs_src.py)
│   ├── correlate.py, manifests.py, planner.py, jobs.py, helper_client.py, auditlog.py
│   └── web/ (routes, Jinja2 templates, static/app.css, static/app.js)
├── helper/del_helper.py
├── config/del.toml
├── database/del.db
├── manifests/*.yaml
├── backups/
├── logs/ (audit.log, helper-audit.log)
├── scripts/ (install.sh, del-admin CLI)
└── tests/

/etc/systemd/system/del-web.service
/etc/systemd/system/del-helper.service
/etc/nginx/sites-available/del.bjk.ai
/etc/nginx/sites-enabled/del.bjk.ai   (symlink)
```

`/etc/sudoers.d` — **not needed.** DEL's entire root-privileged surface is the `del-helper`
unix-socket daemon with its fixed operation allowlist (§12, §13); no `sudo` rule is required
anywhere in the design.

## 20. Exact files that may later be modified

Only ever via an approved, HMAC-signed removal plan executed by `del-helper`, never directly by
`del-web`, never outside a plan:

- `/etc/nginx/sites-available/*`, `/etc/nginx/sites-enabled/*` — via `nginx_rm_site` (backup
  first, `nginx -t`, reload only on pass, auto-restore on failure)
- `/etc/systemd/system/*` unit files — via `systemd_stop`/`systemd_disable`/`systemd_rm_unit`
  (restricted to `/etc/systemd/system` + `daemon-reload`)
- `/etc/cron.d/*` and the `bjkai`/root user crontabs — via `cron_rm`
- Application directories under `/apps`, `/data`, `/srv`, `/var/www` — via `path_delete`
  (canonicalized, protected-root-checked, must be listed in the approved plan) — **note:
  `/apps` and `/data` themselves are protected roots (§13); only specific approved subpaths
  within them are ever deletable, and `/apps/del` can never be one of them**

No file outside this list, and no file within it outside an approved plan's specific steps, is
ever touched by DEL.

## 21. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Removal plan targets a shared resource (network, image, port) without operator realizing it's shared | Correlation engine sets `shared=True` on every association where a resource maps to >1 app; shared resources are never auto-included in a plan and surface as explicit warnings |
| Operator approves removal of a resource that's actually still in active use (e.g. `fireshare` vs `fireshare_migrated` ambiguity) | Confidence scoring blocks `possible`-level associations from automated removal; `probable` requires explicit per-resource approval; the disposable-app dry-run preview stage lets the operator see the exact step list before anything executes |
| A plan step fails partway through a live job, leaving host state inconsistent | Every step recorded before/after execution; unsafe failures halt the job immediately; nginx/systemd stage failures auto-restore from stage-4 backups; jobs are resumable from the failed step |
| Volume deletion is irreversible and the biggest data-loss risk in the allowlist | Triple-gated: plan option + per-volume checkbox + typed confirmation phrase at execution time, not at plan-build time |
| `del-helper` itself becomes a privilege-escalation vector if `del-web` is compromised | Fixed operation allowlist (no arbitrary shell), independent argument re-validation on every call regardless of caller, protected-root list enforced helper-side, unix socket permissioned to root:bjkai only |
| DEL accidentally targets itself for removal | `/apps/del` is a protected root and DEL is flagged `protected=1`; the planner refuses to plan its own removal at the model level, not just by convention |
| Pre-existing host issues (2 failed units, dead nginx upstreams, orphan volumes, the fireshare duplicate) are mistaken for something DEL caused | This audit and report document these as pre-existing findings (§3–§11) discovered by DEL's read-only scan, not introduced or altered by installing DEL (§15) |
| Nginx config drift (36 enabled files differ from their `sites-available` counterpart) causes DEL's own vhost creation to collide or be miscategorized | `del.bjk.ai` confirmed unclaimed by any existing config (§5); DEL's install writes a fresh `sites-available` file and a proper symlink into `sites-enabled`, avoiding the regular-file-not-symlink pattern seen elsewhere on this host |
