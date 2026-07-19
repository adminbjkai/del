# DEL — Installation

## Prerequisites

- Host: bjkai-2tb-ubuntu (Ubuntu), Docker present, Nginx present, systemd present.
- User `bjkai` with sudo (install.sh calls `sudo` for the systemd/nginx steps).
- `/apps/del` project root already checked out with:
  - `.venv/` — Python 3.10 virtualenv with `uvicorn`, `fastapi`, `jinja2`, `pydantic`,
    `argon2-cffi`, `pyyaml`, `python-multipart`, `itsdangerous` installed.
  - `backend/del_app` — the application package.
  - `helper/del_helper.py` — the privileged helper daemon (stdlib only, no venv needed
    since it's invoked with the system `/usr/bin/python3`).
  - `config/` — `del.toml`, `del-web.service`, `del-helper.service`,
    `nginx-del.bjk.ai.conf`, `helper-policy.json`.
- DNS: **the `del.bjk.ai` A/AAAA record must already exist in IONOS**, pointing at this
  host's public IP (same record set the rest of `*.bjk.ai` uses, since TLS is served
  from the existing wildcard cert). If it does not exist, HTTPS access will 404 at
  the CDN/DNS layer even though Nginx and the app are healthy locally — check this
  first if `https://del.bjk.ai` fails from outside but `curl` from the host succeeds.
- TLS cert: `/etc/letsencrypt/live/bjk.ai/fullchain.pem` and `privkey.pem` (the
  existing bjk.ai wildcard cert) must be present; DEL does not provision its own cert.

## Running the installer

```bash
cd /apps/del
./scripts/install.sh
```

`install.sh` is idempotent and does, in order:

1. **venv check** — fails fast if `.venv/bin/uvicorn` is missing.
2. **DB migrate + dirs** — creates `database/`, `logs/`, `backups/`, `manifests/` and
   runs `./scripts/del-admin migrate` (applies `backend/del_app/migrations/NNN_*.sql`).
3. **Install systemd units** — copies `config/del-helper.service` and
   `config/del-web.service` to `/etc/systemd/system/`, `daemon-reload`, then
   `systemctl enable --now` both units (helper first, since del-web `Wants=`/`After=`
   it).
4. **Wait for local health** — polls `http://127.0.0.1:8075/healthz` for up to 30s,
   then confirms it.
5. **Nginx site** — backs up any existing
   `/etc/nginx/sites-{available,enabled}/del.bjk.ai` with a timestamp suffix
   (`.bak.YYYYMMDD-HHMMSS`), installs `config/nginx-del.bjk.ai.conf`, symlinks it into
   `sites-enabled`, runs `nginx -t`, then `systemctl reload nginx`.
6. **HTTPS check** — `curl -fsSI https://del.bjk.ai/login` and prints the status line.

The script does not touch any other application, container, compose project, nginx
site, systemd unit, or cron entry on the host — installing DEL is additive only.

## Creating the admin account

DEL ships with no default credentials. After install:

```bash
/apps/del/scripts/del-admin create-admin
```

This prompts for a username and password (or accepts `--username` and
`--password-stdin` for scripted/non-interactive use) and creates the one admin user
via Argon2id hashing. If an initial password was generated for you and left at
`/apps/del/config/admin-initial-password.txt` (mode `0600`), **log in, change it
immediately with `del-admin change-password`, then delete that file** — see
SECURITY.md.

## Verifying health

```bash
curl -fsS http://127.0.0.1:8075/healthz          # local, no auth required
curl -fsSI https://del.bjk.ai/login               # through nginx + TLS
systemctl status del-web.service del-helper.service
```

`/healthz` is the only unauthenticated route and returns JSON `ok` from `del-web`
directly — it does not exercise the helper socket, so a healthy `/healthz` does not
by itself confirm `del-helper` is reachable (check `systemctl status
del-helper.service` and `ls -l /run/del/helper.sock` separately).

Log in at https://del.bjk.ai, then run an initial scan from the Settings page (or
`POST /scan`, or `del-admin rescan`) to populate the application inventory.
