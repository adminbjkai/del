# DEL — Architecture

DEL is a self-hosted administrative application for discovering, reviewing, and
completely uninstalling applications from this server (bjkai-2tb-ubuntu). It is
served at https://del.bjk.ai behind Nginx, bound only to localhost.

## Stack

| Layer | Choice | Rationale |
|---|---|---|
| Language | Python 3.10 (system python3, venv) | Already on host; excellent for sysadmin tooling; typed with dataclasses/pydantic |
| Web framework | FastAPI + Uvicorn | Typed request/response models, async, small footprint |
| Templates/UI | Jinja2 server-rendered + vanilla JS + single CSS file | No Node toolchain; fully self-contained (CSP-friendly, no CDN); dark-mode via CSS |
| Database | SQLite (WAL mode) via sqlite3 + migration runner | Single admin user; zero-ops; file lives in /apps/del/database/del.db |
| Privileged layer | del-helper: separate root daemon on a unix socket | Strict allowlist; web app never runs shell as root |
| Deployment | Host systemd units (del-web.service, del-helper.service, del-docs.service) | See below |

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
- **del-helper.service** — the privileged root daemon on the unix socket.
- **del-docs.service** — serves the Fern-built documentation site (this repo's
  `fern/` sources) on 127.0.0.1:8072/8073, fronted by Nginx at `/docs` (and its
  `/_next` static assets) behind HTTP basic auth, independent of the app's own
  session auth. It is part of the DEL deployment but carries no privileged
  access — it only serves static/rendered docs content.

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
del-helper (root, Python, ~600 lines, no web framework)
  • Fixed operation allowlist (see below)
  • Validates every argument; canonicalizes paths (realpath, no symlink escape)
  • Refuses protected roots; refuses paths outside approved roots
  • Every request logged to /apps/del/logs/helper-audit.log (append-only)
  • dry_run flag honored on every operation
```

del-web never constructs shell strings from user input. Every privileged action is
a typed operation name + validated structured arguments. del-helper executes via
subprocess arg-arrays (never shell=True).

### Helper operation allowlist (complete)

| Operation | Args | Notes |
|---|---|---|
| ping | — | health |
| list_listeners | — | read-only `ss -lntp` as root; used by `proc_src.py` to resolve listener ownership when the caller can't run `ss` itself |
| compose_down | project, config_files[], remove_volumes?, remove_images_mode? | config files must exist & be under approved roots |
| container_stop / container_rm | container_id | id validated against docker inspect |
| image_rm | image_id | refused if other containers reference it |
| volume_rm | volume_name | refused unless plan approved w/ double confirmation flag |
| network_rm | network_name | refuses bridge/host/none and networks with foreign containers |
| systemd_stop / systemd_disable / systemd_rm_unit | unit | unit must be in the app's approved plan; rm restricted to /etc/systemd/system + daemon-reload |
| cron_rm | file path or crontab line | /etc/cron.d files under plan only; user crontab edits via crontab -l diff |
| nginx_rm_site | paths[] | only under /etc/nginx/sites-{enabled,available}; backup first; nginx -t; reload only on pass; restore on fail |
| nginx_test | — | read-only `nginx -t`, never reloads; safe in dry_run and live |
| nginx_test_reload | — | nginx -t, reload if ok |
| path_delete | path | canonicalized; must be under approved roots (/apps, /data, /srv, /var/www, /home/bjkai, /etc/nginx/sites-{available,enabled}, /etc/systemd/system, /etc/cron.d) AND not a protected root AND listed in the approved plan; refuses mountpoints |
| path_restore | backup_path, original_path | restores a prior backup_tar/file_backup copy back to original_path (`cp -a`); original_path must be absolute |
| tmux_kill | session | exact name from plan |
| process_term | pid, expected_exe | TERM then KILL after grace; pid+exe must still match |
| backup_tar | src_path, dest | dest under /apps/del/backups only |
| volume_backup | volume, dest | docker run --rm -v vol:/src:ro tar → /apps/del/backups |
| file_backup | path, dest | timestamped copy before config edits |

Protected roots (never deletable, even if listed): /, /bin, /boot, /dev, /etc,
/home, /lib, /lib64, /opt, /proc, /root, /run, /sbin, /srv, /sys, /tmp, /usr,
/var, /apps, /data, and /apps/del itself (DEL is a protected application).

Approval flow: del-web writes an approved plan (signed with an HMAC using a key
readable only by root and the del user) into the DB and passes plan_id + step to
the helper; the helper re-validates each argument against its own rules regardless.

## Components (code layout)

```
/apps/del/
├── backend/del_app/
│   ├── main.py            FastAPI app factory, routes mounting
│   ├── config.py          settings (port, paths) from /apps/del/config/del.toml
│   ├── db.py              sqlite connection, migration runner
│   ├── migrations/        NNN_*.sql
│   ├── auth.py            login, argon2/bcrypt hashing, sessions, CSRF, rate limit
│   ├── models.py          typed dataclasses / pydantic models
│   ├── discovery/
│   │   ├── docker_src.py  containers/images/volumes/networks via docker socket
│   │   ├── compose_src.py compose file scanner + parser
│   │   ├── nginx_src.py   site config parser
│   │   ├── systemd_src.py units/timers via systemctl show
│   │   ├── proc_src.py    ss/ps/tmux/screen
│   │   ├── cron_src.py    crontabs/cron.d
│   │   └── fs_src.py      project dirs, git repos, du
│   ├── correlate.py       evidence-based association + confidence scoring
│   ├── manifests.py       YAML manifests read/write/validate
│   ├── planner.py         removal plan generation (dry-run), impact/risk report
│   ├── jobs.py            staged job engine (analyze→preview→backup→quiesce→remove→validate→report), step records, resume
│   ├── helper_client.py   unix-socket client to del-helper
│   ├── auditlog.py        append-only audit records
│   └── web/               routes + Jinja2 templates + static/
├── helper/del_helper.py   root daemon (stdlib only)
├── config/del.toml
├── database/del.db
├── manifests/*.yaml
├── backups/
├── logs/
├── scripts/ (install.sh, del-admin CLI: create-admin, change-password, rescan, backup db)
└── tests/
```

## Data model (SQLite)

- users(id, username, password_hash, created_at, last_login)
- sessions(token_hash, user_id, created, expires, ip)
- scans(id, started, finished, status, stats_json)
- applications(id, slug, name, status, kind, protected, manifest_path, first_seen, last_seen)
- resources(id, type, key, display, path, state, data_json, first_seen scan, last_seen scan)
- associations(app_id, resource_id, confidence, ownership, shared, data_loss_risk,
  removal_eligible, recommended_action, evidence_json, source, approved_by_user, excluded)
- plans(id, app_id, created, options_json, steps_json, status, hmac)
- jobs(id, plan_id, mode dry_run|live, started, finished, status, user_id)
- job_steps(id, job_id, seq, stage, operation, args_json, state, exit_code, output_sanitized, started, finished, reversible)
- backups(id, job_id, kind, src, dest, sha256, size, created)
- audit_log(id, ts, user_id, action, subject, details_json)  — no secrets ever
- settings(key, value)

## Confidence scoring

Levels: confirmed (95–100), high (80–94), probable (60–79), possible (30–59),
unrelated (<30), manual (user-assigned). Each association stores evidence items
{source, statement, weight}. Compose project label = confirmed. Nginx proxy_pass
port → published container port = high. For host-network containers (no published
port mapping to key off), `proc_src` traces a listening port's pid back to its
owning container via `/proc/<pid>/cgroup`; a proxy_pass port matching that
cgroup-resolved container's listener is also high confidence, with evidence
naming the container and noting "(host network)". Networks are correlated the
same way compose projects are seeded plus by attached-container name (not id, so
a network survives container recreation without losing its owner mapping); a
network attached to containers from more than one app is `shared` and preserved
unless approved per-app. Nginx configs that no longer have a live upstream to
match (app already stopped) are still attached to the correct app when the
config's `server_name` slugifies to *exactly* the app's slug — this catches
config debris (`.conf`/`.bak`/disabled copies included) that plain port-matching
would otherwise leave behind. Name similarity alone = possible, never
auto-removable. Only confirmed/high/manual associations are eligible for removal;
probable requires explicit user approval per-resource; possible is always blocked
until manually confirmed.

## Removal job engine

Plans are immutable once approved. A job executes plan steps in stage order; each
step is recorded before execution (state=running) and after (done/failed). Any
safety-validation failure halts the job before downstream deletions. Steps carry
reversible=true/false; failures in the nginx/systemd stages trigger automatic
restore from the timestamped backups taken in the backup stage. Jobs are resumable:
a failed job can retry from the failed step after the operator fixes the cause.
Live volume deletion requires: plan option enabled + per-volume checkbox + typed
confirmation phrase at execution time (second confirmation).

## Security summary

- Bind 127.0.0.1 only; Nginx terminates TLS with the bjk.ai wildcard cert.
- Session cookies: HttpOnly, Secure, SameSite=Lax; server-side session store; 12h expiry.
- CSRF token on every mutating form/request; login rate limiting (5/min/IP, backoff).
- Argon2id password hashing (fallback bcrypt); admin account created via CLI, no defaults.
- CSP: default-src 'self'; no external assets. Security headers set in app + Nginx.
- Secrets never logged; env values stripped at the discovery source layer.
- Helper socket 0660 root:bjkai; operations allowlisted; args validated twice.
- DEL itself flagged protected=1; planner refuses to plan its removal.
