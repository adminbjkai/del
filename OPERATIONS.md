# DEL — Operations

## Start / stop / restart / status

```bash
# status
systemctl status del-web.service
systemctl status del-helper.service

# restart (e.g. after an update)
sudo systemctl restart del-helper.service
sudo systemctl restart del-web.service       # restart helper first; del-web Wants= it

# stop
sudo systemctl stop del-web.service
sudo systemctl stop del-helper.service

# start
sudo systemctl start del-helper.service
sudo systemctl start del-web.service
```

Both units are `enable`d, so they come back on boot. `del-web` has
`Restart=on-failure` (3s backoff); `del-helper` has `Restart=on-failure` (2s
backoff).

## Logs

```bash
# systemd journal (primary source for both units)
journalctl -u del-web.service -f
journalctl -u del-helper.service -f
journalctl -u del-web.service -u del-helper.service --since "1 hour ago"

# on-disk logs
ls /apps/del/logs/
tail -f /apps/del/logs/helper-audit.log     # append-only, every helper request
```

`del-web` logs its own app-level audit trail to the `audit_log` table (see
`del_app/auditlog.py`) and to `/apps/del/logs/`. `del-helper` additionally writes an
independent, append-only `helper-audit.log` line for every request it receives,
regardless of what `del-web` believes happened — the two logs are meant to be
cross-checked. Each helper line carries `op`, `args`, `dry_run`, `ok`, `error` and,
when the caller supplied them, `plan_id`, `step_id`, `job_id` and `requested_by`, so
a root-level action can be traced to the DEL job, plan step and user that asked for
it instead of being matched to `audit_log` by timestamp. Secrets and environment
variable values are never written to either log (see SECURITY.md).

## Update procedure

`/apps/del` is the production checkout of the GitHub repository. Keep local
runtime files out of Git, review the incoming commits, and never overwrite a
dirty worktree.

1. Check and update the source:
   ```bash
   cd /apps/del
   git status --short --branch
   git pull --ff-only
   ./.venv/bin/python -m pip install -r requirements-dev.txt
   ```
2. **Run tests before restarting anything:**
   ```bash
   cd /apps/del/backend && ../.venv/bin/python -m pytest ../tests/ -q -W error
   ```
3. If a migration was added, apply it from the project root:
   `./scripts/del-admin migrate`.
4. **If you changed `helper/` or `config/helper-policy.json`, redeploy them before
   restarting.** `del-helper.service` runs the root-owned copy at
   `/usr/local/lib/del-helper/` with policy at `/etc/del/helper-policy.json`, not
   the repo working tree — a bare restart re-runs the old code:
   ```bash
   cd /apps/del && ./scripts/install.sh        # idempotent; re-installs both, restarts the helper
   # or, by hand:
   sudo install -o root -g root -m 0644 helper/del_helper.py /usr/local/lib/del-helper/del_helper.py
   sudo install -o root -g root -m 0644 helper/validation.py /usr/local/lib/del-helper/validation.py
   sudo install -o root -g root -m 0644 config/helper-policy.json /etc/del/helper-policy.json
   ```
5. Restart the affected unit(s):
   ```bash
   sudo systemctl restart del-helper.service   # after the redeploy in step 4
   sudo systemctl restart del-web.service      # if backend/del_app changed
   ```
6. Confirm health: `curl -fsS http://127.0.0.1:8075/healthz`, and
   `journalctl -u del-web -n 20` — a broken import now makes `del-web` fail to
   start rather than serving 404 for the whole UI with a green `/healthz`.
   The health endpoint also returns HTTP 503 if the DB or required schema is
   unavailable; it does not check the helper or external providers.
7. If `config/nginx-del.bjk.ai.conf` changed, back it up, copy it over the installed
   site file, `sudo nginx -t`, then `sudo systemctl reload nginx` (never reload
   before `nginx -t` passes). Edit the `sites-available` copy only and leave the
   `sites-enabled` entry as a symlink to it — `nginx.conf`'s
   `include sites-enabled/*;` has no filename filter, so any stray regular file
   or backup left in `sites-enabled` (not just `.conf` files) is parsed as a
   vhost on the next reload and can break every site on the box. Put any saved
   copy outside `sites-enabled` (e.g. `/apps/del/backups/`), never inside it.
   See `docs/DEPLOYMENT-CONVENTION.md` §6 for the full server-wide convention.

## Assistant (inventory Q&A)

Read-only chat at `/assistant` and in the right-rail **Ask** tab on every
authenticated page, using Ollama Cloud `glm-5.3-flash`. It cannot run helper
ops or start removal jobs. Key file `/apps/del/config/ollama-api-key.txt`
must be mode `0600` (refused if wider).

Enable:

```bash
install -m 600 /dev/stdin /apps/del/config/ollama-api-key.txt <<< "<ollama-cloud-key>"
# [assistant] enabled = true is the default in config/del.toml
sudo systemctl restart del-web
```

Then Settings → Assistant → Test connection. Disable by setting
`[assistant] enabled = false` or removing the key file and restarting.
The key is never logged; the file is gitignored (`config/*-api-key.txt`).

## Rescan (refresh the application inventory)

Any of:
- **UI**: the scan stamp at the foot of the sidebar (its refresh icon), or
  Settings → Scanning → **Run scan now**. Both issue `POST /scan` (auth +
  CSRF), which starts `run_scan()` on a background thread and answers at once:
  JSON `{"started": true}` for the UI's fetch, or a redirect with
  `flash=Scan+started` for a plain form post. If a scan is already running
  (`scanner.scan_state()` reports `running: true`) it refuses (409 /
  an error flash) instead of starting a second one. The stamp polls
  `GET /scan/status` — the current `scan_state()` (running/scan_id/started)
  plus the most recent `scans` row — and reloads the page when the new
  inventory is published.
- **CLI**:
  ```bash
  /apps/del/scripts/del-admin rescan
  ```
- **Automatically**: set `scan_interval_hours` in `config/del.toml` (0 = off,
  the code default; this host uses 6) and restart `del-web`. Every ten minutes
  `del-web` checks the age of the latest completed scan and scans when it is
  older than the interval; after a failed attempt it waits min(interval, 1 h)
  before retrying, and it never scans while a removal job runs (a live removal
  ends with its own rescan).

All of them call `del_app.scanner.run_scan()`, which re-runs all discovery sources
(docker, compose, nginx, systemd, proc, cron, fs), re-correlates, and persists a new
scan row plus refreshed `applications`/`resources`/`associations`. The inventory and
completed marker publish in one transaction; scan failures retain the previous
inventory. Settings shows scan outcomes and live progress, including scans started
from the CLI. A Linux file lock beside the DB prevents CLI/web overlap. Per-row
review decisions survive while the app/resource pair remains associated; approvals
are revoked if the safety classification changes. Invalid manifests now fail scans
instead of silently omitting their safety rules. Collectors still log and skip some
individual unreadable artifacts internally. Scanning is
entirely read-only against the host — it does not stop, start, or modify anything.

## Regenerating the port registry

```bash
/apps/del/scripts/gen-registry.py     # or scripts/gen-registry.sh
```

Reads the latest scan out of `del.db` (read-only) and writes
`docs/PORT-REGISTRY.md` — every enabled subdomain, its host port, correlated
backend app, and deploy type, plus a **Port CONFLICTS** section (one host port
claimed by more than one *distinct* app — misconfigured) separate from normal
**Shared ports (aliases)** (one app, several domains, same port). The output
file is gitignored and gets stale as soon as anything changes on the server —
regenerate it after adding/removing/re-porting an app, rather than trusting an
old copy.

## Whole-server inventory export

A self-contained, browsable export of the full inventory (built from `del.db` plus
the `/apps/del/miscwork/` build scripts, gitignored) is served at
`https://del.bjk.ai/miscwork.html` (aliased at `/inventory`), basic-auth protected
via `/etc/nginx/.del-docs-htpasswd`.
It is a point-in-time snapshot for browsing/handoff, not a live-refreshing view —
regenerate it manually from `/apps/del/miscwork/extract.py` (and the other scripts
in that directory) when
it goes stale; there is no scheduled job that rebuilds it.

## Backup

```bash
/apps/del/scripts/del-admin backup-db
```
Writes a consistent SQLite snapshot (via `sqlite3.Connection.backup`) to
`/apps/del/backups/del-<timestamp>.db`. The timestamp is **host local time**
(`datetime.now()`, format `YYYYMMDDTHHMMSS`), not UTC — so on this host it reads as
Eastern. Run this before any manual DB surgery and on whatever cadence the host's
general backup routine uses — `del.db` is a single file, so it fits into existing
host backup jobs with no special handling.

Per-removal-job backups (config files, volumes, tarred directories) are taken
automatically by the job engine during the `backup` stage of a removal and also land
under `/apps/del/backups/`. A row is written to the `backups` table for each one —
`job_id`, `kind` (`file_backup` / `volume_backup` / `backup_tar`), `src` and
`dest` — and that is what the in-job rollback path reads. Two caveats worth knowing:

- The `backups` table has `sha256` and `size` columns, but the job engine does not
  populate them. Backups are **not** content-addressed or integrity-checked; they
  are plain `cp -a` / `tar` copies. Verify by hand if that matters to you.
- Rows are only written for **live** jobs. A dry run creates no backups (it changes
  nothing), so it also records none.

## Restore

See RECOVERY.md for the full DB-restore and nginx-rollback procedures.

## Changing the admin password

```bash
/apps/del/scripts/del-admin change-password --username admin
```
Prompts for and confirms a new password, re-hashed with Argon2id. Use
`--password-stdin` for non-interactive/scripted password rotation.
