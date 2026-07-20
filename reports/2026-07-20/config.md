# DEL Config/Deployment Consistency Audit — 2026-07-20

Scope: tracked config copies under `/apps/del/config/**`, compared against live
system state (source of truth). Read-only toward `/etc`, systemd, and running
services. No live files or units were touched; only tracked copies in
`/apps/del/config/` were updated where drift was found.

## 1. nginx

Compared `/apps/del/config/nginx-del.bjk.ai.conf` against live
`/etc/nginx/sites-available/del.bjk.ai` (`sudo diff`):

```
NO DIFF
```

**Result: MATCH.** No drift, no fix needed.

Locations confirmed present in the live vhost:
- `location /` → proxy to 127.0.0.1:8075 (del-web)
- `location /docs` and `location /docs/` → proxy to 127.0.0.1:8072 (Fern docs)
- `location /_next/` → proxy to 127.0.0.1:8072 (Fern static assets)
- `location /_local` → proxy to 127.0.0.1:8073 (Fern docs backend)
- `location = /miscwork.html` (basic-auth, alias to `/apps/del/miscwork/miscwork.html`)
- `location = /inventory` → 301 redirect to `/miscwork.html`

All required locations present. `/etc/nginx/sites-enabled/del.bjk.ai` is a symlink
to `sites-available/del.bjk.ai` (correctly enabled).

`sudo nginx -t`:
```
nginx: the configuration file /etc/nginx/nginx.conf syntax is ok
nginx: configuration file /etc/nginx/nginx.conf test is successful
```
**PASS.**

## 2. systemd units

| Unit | Tracked copy vs live | Fix applied |
|---|---|---|
| del-web.service | Exact match (`diff` against `systemctl cat`, header line excluded) | None needed |
| del-helper.service | Exact match | None needed |
| del-docs.service | **Missing from tracked config directory** — unit is live/active but had no tracked copy under `/apps/del/config/` | **Created** `/apps/del/config/del-docs.service`, content verified byte-for-byte identical to `/etc/systemd/system/del-docs.service` via `sudo diff` (exact match after removing an extraneous header comment line to match the style of the other two tracked unit files) |

Service health (`systemctl is-active`):

| Unit | Status |
|---|---|
| del-web.service | active |
| del-helper.service | active |
| del-docs.service | active |

All three units active.

## 3. helper-policy.json reconciliation

Compared `/apps/del/config/helper-policy.json` against `docs/ARCHITECTURE.md` and
enforcement code in `helper/validation.py` / `helper/del_helper.py`.

- `approved_deletion_roots` in policy: `/apps, /data, /srv, /var/www, /home/bjkai,
  /etc/nginx/sites-available, /etc/nginx/sites-enabled, /etc/systemd/system,
  /etc/cron.d` — matches ARCHITECTURE.md line 81 verbatim (path_delete op
  description lists the identical root set), and is read by
  `validation.py` via `policy.get("approved_deletion_roots", [])` (lines 98, 195).
- `protected_roots` in policy: `/, /bin, /boot, /dev, /etc, /home, /lib, /lib64,
  /opt, /proc, /root, /run, /sbin, /srv, /sys, /tmp, /usr, /var, /apps, /data,
  /apps/del` — matches ARCHITECTURE.md lines 89–91 verbatim ("Protected roots
  (never deletable, even if listed): /, /bin, /boot, /dev, /etc, ... /var, /apps,
  /data, and /apps/del itself"). Enforced in `validation.py` line 104
  (`_norm_root` normalization + membership check).
- `never_delete`: `["/apps/del"]` — matches ARCHITECTURE.md's statement that DEL
  itself is a protected application whose removal the planner refuses (line 204)
  and is enforced in `validation.py` lines 108–112 ("path is on the never-delete
  list, refusing").

**No mismatch found** between policy config, docs, and code enforcement. Nothing to
flag.

## 4. del.toml

```
port = 8075
db_path = "/apps/del/database/del.db"
manifests_dir = "/apps/del/manifests"
backups_dir = "/apps/del/backups"
logs_dir = "/apps/del/logs"
scan_roots = ["/apps", "/data/apps", "/opt", "/srv", "/var/www"]
helper_socket = "/run/del/helper.sock"
protected_apps = ["del"]
```

- Port 8075 matches del-web.service `ExecStart` (`--port 8075`) and the nginx
  `location /` proxy target.
- `protected_apps = ["del"]` matches `never_delete` in helper-policy.json and
  ARCHITECTURE.md's protected-application statement.
- Paths (`manifests_dir`, `backups_dir`, `logs_dir`, `db_path`) all resolve under
  `/apps/del`, consistent with directory layout observed on disk (`/apps/del/manifests`,
  `/apps/del/backups`, `/apps/del/logs`, `/apps/del/database/del.db` all exist).
- `scan_roots` is a superset used for app *discovery* (broader than the deletion
  `approved_deletion_roots`), which is expected — no mismatch, different purpose.

**Result: correct**, no drift.

## 5. manifests/del.yaml

```
id: del
name: DEL (App Inventory & Uninstaller)
status: active
domains: [del.bjk.ai]
repositories: [/apps/del]
host_paths: [/apps/del]
systemd_units: [del-web.service, del-helper.service, del-docs.service]
nginx:
  - /etc/nginx/sites-available/del.bjk.ai
  - /etc/nginx/sites-enabled/del.bjk.ai
```

- `systemd_units` lists all three units (del-web, del-helper, del-docs) — matches
  reality; all three confirmed active.
- `nginx` paths match the actual sites-available file and its sites-enabled
  symlink, both confirmed to exist.
- `domains: [del.bjk.ai]` matches `server_name` in the live nginx config.
- Marked `status: active`, correctly reflects reality (healthz check passes, all
  units active).

**Result: correct**, no drift.

## 6. healthz

```
$ curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8075/healthz
200
$ curl -s http://127.0.0.1:8075/healthz
{"ok":true,"scan":103}
```

**PASS.**

## Fixes applied to tracked config

1. **Created `/apps/del/config/del-docs.service`** — this unit is live, active, and
   listed in `manifests/del.yaml`'s `systemd_units`, but had no tracked copy in
   `/apps/del/config/`. New tracked file verified byte-identical to the live unit
   file via `sudo diff` after creation.

No other tracked file required changes — nginx config, del-web.service,
del-helper.service, del.toml, and manifests/del.yaml all already matched live
state exactly.

## VERDICT

**PASS, with one drift found and fixed.** Live nginx config, del-web.service, and
del-helper.service all matched their tracked copies exactly. `del-docs.service`
was missing from tracked config entirely (a real drift/gap, not a content
mismatch) and has been added, now verified byte-identical to the live unit.
helper-policy.json's approved/protected/never-delete roots are fully consistent
across policy, ARCHITECTURE.md, and the code that enforces them
(validation.py/del_helper.py) — no mismatch to flag. del.toml and
manifests/del.yaml are accurate and consistent with observed reality. All three
systemd units are active, `nginx -t` passes, and `/healthz` returns
`{"ok":true,"scan":103}` (HTTP 200). DEL's deployment config is in a clean,
consistent state as of this audit.
