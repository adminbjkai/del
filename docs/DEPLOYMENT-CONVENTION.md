# Deployment Convention — bjkai-2tb-ubuntu

The house standard for every app on this server (~192 apps and counting). Follow
this for anything new; use it as the reference when auditing anything old. It
covers both Docker Compose stacks and non-Docker (systemd/cron) services — this
server runs a lot of both, and they must be tracked the same way.

If you only read one section, read **§1 Principles** and **§8 The DEL manifest**.

---

## 1. Principles

1. **One source of truth per app: its DEL manifest.** Every app that exists on
   this server has (or should have) a YAML file at `/apps/del/manifests/<id>.yaml`
   that lists every resource it owns — domains, repo/host paths, systemd units,
   nginx configs, compose files, cron entries. If it isn't in the manifest, DEL's
   correlation engine has to *infer* it (fragile); if it is, it's authoritative
   ("confirmed" confidence). See §8.
2. **nginx is the single subdomain chokepoint, regardless of backend.** Every
   public `*.bjk.ai` hostname terminates TLS at nginx and reverse-proxies to a
   backend on `127.0.0.1:<port>`. It does not matter whether that backend is a
   Docker container or a bare systemd process — from nginx's perspective they're
   identical. Never expose an app port directly to the internet; never bind an
   app to `0.0.0.0`.
3. **Every long-running thing is supervised.** A container (with `restart:
   unless-stopped` or equivalent) or a systemd unit. **Never a bare `nohup ... &`
   or a detached `tmux`/`screen` session as "production."** Supervision means it
   restarts on crash, starts on boot, and is visible to Komodo/Cockpit/`systemctl`
   — an unsupervised process is invisible to all of that.
4. **Every scheduled thing is a systemd timer or a `cron.d` entry, and it is
   tracked in the manifest.** No ad hoc scheduling. See §7 for the double-start
   anti-pattern to avoid.

---

## 2. Standard layout

Everything for an app lives under `/apps/<name>/`:

```
/apps/<name>/
├── docker-compose.yml        # Docker apps: compose file lives at the app root
├── .env                       # secrets/config, NOT committed, referenced by compose or unit
├── data/                      # persistent bind-mounted data (DB files, uploads, etc.)
├── config/                    # app config files
└── logs/                      # optional; many apps just use journald/docker logs instead
```

Rules:
- The app's canonical directory is always `/apps/<name>`. No apps outside `/apps`
  (aside from the OS-level units that point at them).
- `.env` holds secrets and environment-specific config; it is read by
  `docker-compose.yml` (`env_file: .env`) or by the systemd unit
  (`EnvironmentFile=/apps/<name>/.env`). Never put secrets directly in the
  compose file, the systemd unit, or the nginx vhost.
- Persistent data goes in `/apps/<name>/data` (or a named Docker volume scoped to
  that app) — not in `/tmp`, not in the repo checkout, not in a shared directory
  another app might touch.
- If the app is a git checkout, the working tree itself normally *is*
  `/apps/<name>` (no extra nesting).

---

## 3. Port allocation

**Internal vs external port — know the difference:**
- *Internal* port: whatever the container/process listens on inside itself
  (often `80`, `3000`, `8080`, whatever the upstream image defaults to). This
  never needs to be unique across apps — it's not reachable from outside its own
  container/process.
- *External* (host) port: the `127.0.0.1:<port>` nginx actually proxies to. This
  **must** be unique per app and is the number that matters for this server's
  bookkeeping.

Rules:
- Bind the external port to `127.0.0.1` only — never `0.0.0.0` and never a bare
  port publish like `"8016:80"` (which Docker defaults to all interfaces). Docker
  Compose: always `"127.0.0.1:<port>:<internal_port>"`. Systemd: bind the
  app/uvicorn/etc. with `--host 127.0.0.1`.
- One host port per public-facing service. If an app needs multiple internal
  routes (e.g. DEL's docs preview at `8072`/`8073` alongside its main app at
  `8075`), that's fine — each gets its own port and its own nginx `location`
  block, but each is still `127.0.0.1`-only and each should be listed in the
  manifest.
- **DEL is the port registry.** Before assigning a new port, check what's
  already in use — either via the DEL web UI (https://del.bjk.ai) inventory
  view, or directly:
  ```bash
  grep -rhoE '127\.0\.0\.1:[0-9]+' /etc/nginx/sites-available/*.bjk.ai | sort -u
  ```
- **Range in active use is roughly 8000–8102** (with some later stragglers up to
  ~8195 and a handful of unrelated high ports for non-web services like
  Cockpit `9091` or Netdata `19999`). For a new *web-facing* app, pick the next
  free number in the 8000s (currently the low 8100s and gaps like 8009/8011/8015
  are open — always re-check live state, don't hardcode a number from this doc).
  Record whatever you pick in the app's DEL manifest and nginx vhost so the next
  person doesn't collide with it.

---

## 4. Recipe A — deploying a Docker Compose app

1. **Directory:** `mkdir -p /apps/<name>` (git clone or scaffold there).
2. **Compose file:** `/apps/<name>/docker-compose.yml`, publish the port bound
   to loopback only:
   ```yaml
   services:
     <name>:
       image: ...           # or build: .
       container_name: <name>
       restart: unless-stopped
       env_file: .env
       ports:
         - "127.0.0.1:<host_port>:<internal_port>"
       volumes:
         - ./data:/data      # adjust to the image's data path
   ```
3. **Bring it up:** `docker compose up -d` from `/apps/<name>`.
4. **Register the stack in Komodo** (the Docker control-plane UI/API for this
   server, at `/apps/komodo`) so it shows up alongside every other stack for
   start/stop/logs/updates instead of being a compose file only `docker compose`
   on the CLI knows about.
5. **nginx vhost** from the template in §6, `proxy_pass` to
   `http://127.0.0.1:<host_port>`.
6. **Create the DNS record:** `isbd <name>.bjk.ai` (see below — this only points
   DNS at the server; it does not touch nginx).
7. **`nginx -t && systemctl reload nginx`.**
8. **Write the DEL manifest** (`/apps/del/manifests/<name>.yaml`, §8) listing the
   compose file, host path, domain, and nginx config paths.

---

## 5. Recipe B — deploying a non-Docker (systemd) app

1. **Directory:** `/apps/<name>` — code, `.env`, `data/`, `config/` as in §2.
2. **Systemd unit** at `/etc/systemd/system/<name>.service`, from the template:
   ```ini
   [Unit]
   Description=<Name> service
   After=network.target

   [Service]
   Type=simple
   User=bjkai
   Group=bjkai
   WorkingDirectory=/apps/<name>
   EnvironmentFile=/apps/<name>/.env
   ExecStart=/apps/<name>/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port <host_port> --workers 1
   Restart=on-failure
   RestartSec=3
   NoNewPrivileges=yes
   PrivateTmp=yes
   ProtectKernelModules=yes
   ProtectKernelTunables=yes
   ProtectControlGroups=yes

   [Install]
   WantedBy=multi-user.target
   ```
   (Swap `ExecStart` for whatever the app actually is — a `start.sh` wrapper is
   fine, as in `flixapp.service`, if the runtime needs env setup a one-liner
   can't express. Keep `--host 127.0.0.1` / equivalent loopback bind either
   way.)
3. **Enable + start:**
   ```bash
   systemctl daemon-reload
   systemctl enable --now <name>.service
   ```
4. **View/control it day-to-day via Cockpit** (`https://<server>:9091`, the
   systemd/services web UI) — status, logs, start/stop/restart — instead of
   SSHing in for routine checks.
5. **nginx vhost** from the template in §6, `proxy_pass` to
   `http://127.0.0.1:<host_port>`.
6. **`isbd <name>.bjk.ai`** for DNS.
7. **`nginx -t && systemctl reload nginx`.**
8. **Write the DEL manifest** listing the systemd unit, host path, domain, and
   nginx config paths.

---

## 6. Subdomain / nginx standard

Every vhost is a plain HTTP→HTTPS redirect server block plus a TLS server block
using the shared wildcard cert — **no per-subdomain certificate work is ever
needed**, the cert already covers `*.bjk.ai`.

`/etc/nginx/sites-available/<name>.bjk.ai` (fill in `<name>` and `<port>`):

```nginx
server {
    listen 80;
    server_name <name>.bjk.ai;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl;
    server_name <name>.bjk.ai;

    ssl_certificate /etc/letsencrypt/live/bjk.ai/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/bjk.ai/privkey.pem;

    client_max_body_size 500M;   # tune down for apps with no upload needs

    location / {
        proxy_pass http://127.0.0.1:<port>;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 86400;
    }
}
```

The `Upgrade`/`Connection "upgrade"` pair is always included even for apps that
don't use websockets — it's cheap and saves a round-trip later if the app grows
a websocket feature. Enable and validate:

```bash
ln -s /etc/nginx/sites-available/<name>.bjk.ai /etc/nginx/sites-enabled/<name>.bjk.ai
nginx -t                       # ALWAYS before reload — never skip this
systemctl reload nginx
```

**Never `systemctl reload nginx` without a preceding `nginx -t`.** A syntax
error in one vhost can take down every other vhost on the box.

**Creating the DNS record — `isbd`:** `/usr/local/bin/isbd` is a one-purpose
script: `isbd <subdomain>` (e.g. `isbd foo.bjk.ai`) looks up this server's
current public IPv4 (`curl -4 ifconfig.me`) and PATCHes an `A` record for that
name to IONOS's DNS API for the `bjk.ai` zone, TTL 3600. It only touches DNS —
it does **not** create the nginx vhost, does not reload anything, and does not
touch certificates (the wildcard cert already covers any new label under
`bjk.ai`, so nothing else is needed there). Run it once per new subdomain,
any time before or after the nginx vhost exists.

---

## 7. Scheduled tasks

- Prefer a **systemd timer** (`<name>.timer` + `<name>.service`, `Type=oneshot`
  in the service) — it gets the same `systemctl`/Cockpit/journald visibility as
  every other unit. See `bjkflix-dizipal-cf.timer` / `bjkflix-ios-refresh.timer`
  for the pattern on this box.
- A `/etc/cron.d/<name>` entry is acceptable when a timer is overkill (simple
  one-liners), but it must be **listed in the app's DEL manifest** (`cron:`
  field) — an untracked cron entry is invisible to DEL's correlation and to
  anyone doing an audit.
- **Anti-pattern to avoid — the `xtr` double-start:** this server has (as of
  writing) both `xtr.service` (a supervised systemd unit) *and* a `@reboot`
  cron entry independently launching the same `/apps/xtr/app.py`. That means
  two independent things believe they own starting xtr, which on every reboot
  races to double-start the process and leaves an orphaned/duplicate one
  running outside systemd's supervision (not restart-managed, not visible to
  `systemctl status`, and easy to end up manually `kill`ing the wrong PID).
  **A given app must be started by exactly one mechanism.** If it has a
  systemd unit, it must not also have an `@reboot` cron line (or vice versa).
  When you find this pattern, it needs cleanup — don't add a third launcher on
  top of it.

---

## 8. The DEL manifest — the authoritative record

Location: `/apps/del/manifests/<id>.yaml`. `correlate.py` treats a manifest as
**authoritative** ("confirmed"/"manual" confidence) — it overrides whatever DEL's
own discovery/inference would otherwise guess from scanning nginx/systemd/docker.

Schema (`del_app/manifests.py`):

```yaml
id: <name>                       # required; slug, matches the filename (<id>.yaml)
name: <Display Name>              # optional human label
status: active                    # active | retired | unknown, etc.
domains:
  - <name>.bjk.ai                 # every public hostname this app owns
compose:
  - /apps/<name>/docker-compose.yml   # Docker apps: path(s) to compose file(s)
repositories:
  - /apps/<name>                  # git checkout(s), if any
host_paths:
  - /apps/<name>                  # every filesystem path this app owns (data, config...)
systemd_units:
  - <name>.service                # non-Docker apps: unit name(s), no path, just the unit name
nginx:
  - /etc/nginx/sites-available/<name>.bjk.ai
  - /etc/nginx/sites-enabled/<name>.bjk.ai
cron:
  - /etc/cron.d/<name>             # if applicable
notes: |
  Free-text: what it is, anything a future operator needs to know before
  touching it.
shared: []                        # resources this app uses but does NOT own
                                   # exclusively (e.g. a shared Postgres instance) —
                                   # DEL will never delete these on this app's behalf
excluded: []                      # paths/units that look related but are explicitly
                                   # NOT part of this app (keeps discovery from
                                   # false-positiving them in)
```

Real example, `/apps/del/manifests/del.yaml`:

```yaml
id: del
name: DEL (App Inventory & Uninstaller)
status: active
domains:
  - del.bjk.ai
repositories:
  - /apps/del
host_paths:
  - /apps/del
systemd_units:
  - del-web.service
  - del-helper.service
  - del-docs.service
nginx:
  - /etc/nginx/sites-available/del.bjk.ai
  - /etc/nginx/sites-enabled/del.bjk.ai
notes: |
  DEL itself. Protected application — must never be removable through DEL.
  DNS for del.bjk.ai is managed externally in IONOS.
```

**What each field is for, correlation-wise:** DEL's discovery pass scans
running containers, systemd units, nginx sites, and cron entries and tries to
*infer* which ones belong to which app by name/path proximity — that inference
comes with a confidence level (`possible`/`probable`/`high`). A manifest entry
short-circuits that guessing: anything listed here is `confirmed`/`manual`
confidence and is used as-is when DEL builds an app's resource graph, its
connection map (what talks to what), and — critically — the removal plan when
someone asks to decommission the app (§9). `shared` tells DEL "don't ever
attribute sole ownership of this to me, even if discovery thinks it looks like
mine" (protects things like a shared DB from being deleted as collateral).
`excluded` is the opposite escape hatch: "discovery keeps matching this to me,
it's wrong, ignore it."

**Every new app deployed under §4/§5 must get a manifest.** It's the difference
between DEL being able to answer "what does this box run and how is it wired"
accurately, versus best-effort guessing.

---

## 9. Decommissioning — always through DEL

**Never manually `docker compose down -v`, `rm -rf /apps/<name>`, `systemctl
disable --now` + `rm` the unit, or `rm` the nginx site by hand for something
that's supposed to go away.** Manual removal has no dry-run, no backup, and no
check for shared resources — exactly the mistakes DEL's lifecycle exists to
prevent (see `/apps/del/docs/REMOVAL-LIFECYCLE.md` for the full 9-stage
engine). Always go through DEL:

1. **Dry-run** — build a plan for the app in the DEL UI; review every step,
   warning, and what's marked preserved/blocked before touching anything.
   Dry-run is the default mode; nothing executes at this stage.
2. **Backup** — DEL takes content-addressed backups (`sha256`-tracked, in
   `/apps/del/backups`) of every resource the plan will touch, before any
   mutation — automatically, as part of the plan execution, not a separate
   manual step you have to remember.
3. **Live execute** — only once the dry-run looks right, explicitly execute in
   `live` mode. DEL quiesces, then removes runtime resources, then host
   integrations (systemd/cron/nginx — nginx removal itself backs up first,
   runs `nginx -t`, and auto-restores on failure), then files, in that order,
   halting immediately on any safety-validation failure.
4. **Validate** — DEL's own post-removal validation confirms the targeted
   resources are actually gone and nothing else broke (e.g. `nginx -t` still
   passes for everything else).

DEL preserves anything the app's manifest lists under `shared`, and it refuses
outright to touch protected roots (`/`, `/etc`, `/apps` itself, `/apps/del`,
etc.) or to remove a volume/network still referenced by another container. This
is exactly why keeping the manifest (§8) accurate matters — it's what makes an
automated decommission for a 192-app server safe instead of terrifying.

---

## 10. Operating tools cheat-sheet

| Tool | What it's for | Where |
|---|---|---|
| **DEL** | Cross-cutting inventory, connection map, correlated resource discovery, and the only sanctioned decommission path (dry-run → backup → live → validate) | https://del.bjk.ai |
| **Komodo** | Docker Compose stack control plane — start/stop/update/logs for containerized apps | `/apps/komodo` |
| **Cockpit** | Web UI for systemd services/logs on non-Docker apps — status, start/stop/restart, journal viewing | `https://<server>:9091` |
| **journalctl** | Raw log inspection for any systemd unit: `journalctl -u <name>.service -f` | CLI |
| **`nginx -t`** | Config syntax validation — run before every `reload`, no exceptions | CLI |
| **`isbd <name>.bjk.ai`** | Create/update the DNS `A` record for a new subdomain (IONOS) | CLI |

---

## 11. Checklists

### Pre-flight — adding a new app

- [ ] `/apps/<name>` created; `.env` holds secrets (not committed, not in
      compose/unit/nginx directly)
- [ ] Picked a free `127.0.0.1:<port>` by checking current nginx vhosts / DEL
      inventory (not reused from another app)
- [ ] **Docker:** compose file at app root, port published as
      `"127.0.0.1:<port>:<internal>"`, `restart: unless-stopped`, stack
      registered in Komodo
- [ ] **Non-Docker:** systemd unit at `/etc/systemd/system/<name>.service`,
      bound to `127.0.0.1:<port>`, `enable --now`'d, visible in Cockpit
- [ ] Exactly one supervision mechanism for the process — no unsupervised
      `nohup`/detached-terminal "production," no duplicate cron `@reboot` on
      top of a systemd unit (see §7 xtr anti-pattern)
- [ ] nginx vhost from the §6 template, `nginx -t` passes, site symlinked into
      `sites-enabled`, nginx reloaded
- [ ] `isbd <name>.bjk.ai` run
- [ ] Any scheduled work is a systemd timer or a tracked `cron.d` entry
- [ ] DEL manifest written at `/apps/del/manifests/<name>.yaml` covering
      domains, compose/units, host paths, nginx configs, cron, and `shared`
      resources if any

### Decommissioning an app

- [ ] Manifest is up to date (so DEL's plan is complete, not a guess)
- [ ] Built a plan in DEL and reviewed the full dry-run: steps, warnings,
      preserved/blocked resources, estimated reclaimed space
- [ ] Confirmed nothing else on the server depends on this app's resources
      (check `shared` on *other* manifests too, not just this one)
- [ ] Executed live only after the dry-run review; let DEL run
      backup → quiesce → remove runtime → remove host integrations → remove
      files → validate, in order
- [ ] Confirmed DEL's post-removal validation passed (including `nginx -t`
      still green for the rest of the server)
- [ ] Did **not** manually `rm`/`systemctl disable`/`docker compose down -v`
      anything outside of DEL's own execution
