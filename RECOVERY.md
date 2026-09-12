# DEL — Recovery

## If del-web is down: application data is untouched

DEL is **read-only against the rest of the host except for approved, executed
removal jobs**. If `del-web` (or `del-helper`) is stopped, crashed, or being
repaired, every other application on the host — its containers, volumes, nginx
sites, systemd units, cron entries, and files — is completely unaffected. There is
nothing in DEL's normal operation (discovery, correlation, dashboard, plan
building/viewing) that mutates host state; only an operator-approved job's
"quiesce"/"remove" stages do, and only for the one application targeted, per plan.
A dead `del-web` simply means the inventory UI and job engine are unavailable until
it's restarted — nothing else on the host degrades because of it.

To recover `del-web` itself, see OPERATIONS.md for restart, and the venv/DB
procedures below if the cause is deeper than a simple crash.

## Restoring the database from backup

1. Stop `del-web` so nothing writes to the DB during restore:
   ```bash
   sudo systemctl stop del-web.service
   ```
2. Pick a snapshot from `/apps/del/backups/del-<timestamp>.db` (created by
   `del-admin backup-db` or ad hoc via `sqlite3 .backup`).
3. Move the current DB aside and copy the snapshot into place:
   ```bash
   mv /apps/del/database/del.db /apps/del/database/del.db.pre-restore
   cp /apps/del/backups/del-<timestamp>.db /apps/del/database/del.db
   ```
4. Re-run migrations in case the snapshot predates a schema change:
   ```bash
   /apps/del/scripts/del-admin migrate
   ```
5. Restart and verify:
   ```bash
   sudo systemctl start del-web.service
   curl -fsS http://127.0.0.1:8075/healthz
   ```
6. Log in and check the dashboard/recent scans look sane. If a scan or job was
   mid-flight at snapshot time, run a fresh rescan (`del-admin rescan`) to
   reconcile inventory with current host state — the discovery layer always
   re-derives from live state, it never trusts what was in the DB.

Restoring the DB never undoes an already-executed removal job on the host (those
changes were made to real systemd units/nginx sites/containers/files, not just DB
rows) — it only restores DEL's own bookkeeping. To undo an actual removal, use the
job's own backups (below), not a DB restore.

## Undoing a specific removal job's changes

A **live** job whose plan had a backup mode of Config or Full records a `backups`
row per completed backup step (`job_id`, `kind`, `src`, `dest`) before it deletes
anything, with the archives themselves under `/apps/del/backups/`. The `sha256` and
`size` columns exist in the schema but are not populated — treat the archives as
plain copies, not integrity-checked ones. A job planned with backup mode **None**
recorded nothing, and there is nothing to reverse it with.

### What rolls back automatically, and what does not

- **Inside `nginx_rm_site`** — the helper copies each site file to
  `/apps/del/backups/` first, removes them, runs `nginx -t`, and if the test fails
  copies them all straight back before failing the step. This one has always
  worked and does not depend on the `backups` table at all.
- **Job-level restore** — when a step in `nginx_rm_site`, `nginx_test_reload`,
  `systemd_disable` or `systemd_rm_unit` fails, the engine walks this job's
  `backups` rows newest-first and calls the helper's `path_restore` for each, then
  audits the real per-backup outcome as `job_restore_attempted`. Volume archives
  are deliberately skipped — a `volume_backup` is a tar of a volume's *contents*,
  not a filesystem path, so it is reported as "restore manually" rather than
  attempted. Restore anything else by hand.

**If you are looking at a job from before 2026-08-24:** nothing ever wrote the
`backups` table, so the job-level restore above iterated an empty set while still
recording `job_restore_attempted` in the audit log. Those jobs were not rolled
back, whatever the audit says. The `nginx_rm_site` internal restore was unaffected
and did work.

To manually reverse a job:
- **nginx site**: copy the timestamped `.bak.<ts>` config back over the removed
  site file, `sudo nginx -t`, `sudo systemctl reload nginx`. (The job engine does
  this automatically on an in-job failure; this is for reversing an already-`done`
  job after the fact.)
- **systemd unit**: copy the backed-up unit file back to
  `/etc/systemd/system/`, `sudo systemctl daemon-reload`, `sudo systemctl
  enable --now <unit>`.
- **volume/container/compose data**: restore from the `volume_backup`/`backup_tar`
  archive under `/apps/del/backups/` with `tar` (or `docker run --rm -v
  vol:/dest tar` in reverse) into a recreated volume, then re-run the app's
  compose project.
- **files/directories**: extract the `backup_tar` archive back to its original
  path.

## Helper socket troubleshooting

Symptoms: plan execution / job actions fail; `del-web` logs a `HelperError`.

```bash
systemctl status del-helper.service          # is it even running?
ls -l /run/del/helper.sock                   # expect srw-rw---- root bjkai
sudo journalctl -u del-helper.service -n 100 # recent helper errors
```

Common causes and fixes:
- **Socket missing** — `del-helper.service` isn't running or `/run/del` wasn't
  created: `sudo systemctl restart del-helper.service` (the unit's
  `RuntimeDirectory=del` recreates `/run/del` on start; `/run` is tmpfs so this
  directory does not survive a reboot without the unit re-running).
- **Permission denied connecting to the socket** — `del-web` runs as `bjkai`;
  confirm the socket is group `bjkai` and mode `0660`, and that `bjkai` has not
  been removed from the group.
- **Helper rejects a valid-looking operation** — check the **deployed** policy at
  `/etc/del/helper-policy.json` (the repo copy at
  `/apps/del/config/helper-policy.json` is the source, but the running daemon reads
  the deployed one) for the approved/protected root lists, `protected_units`, and
  the never-delete list; then `/apps/del/logs/helper-audit.log` for the exact
  rejection reason — the helper logs every request including denials.
- **Helper is running stale code after an edit** — `del-helper.service` executes
  `/usr/local/lib/del-helper/del_helper.py`, not `/apps/del/helper/del_helper.py`.
  Re-run `/apps/del/scripts/install.sh` (or re-`install` the files as root) and
  then restart; a bare `systemctl restart` re-runs the old deployed copy. Confirm
  with `sudo diff /apps/del/helper/del_helper.py /usr/local/lib/del-helper/del_helper.py`.
- **Helper down entirely** — `sudo systemctl start del-helper.service`; `del-web`
  itself keeps running (dashboard/browsing/plan-building still work), only actions
  that require the helper (plan execution, scans that need privileged reads — none
  currently do, discovery is unprivileged) are blocked until it's back.

## Restoring nginx from a timestamped .bak

`install.sh` and any subsequent site update back up the previous
`sites-available`/`sites-enabled` files as `<name>.bak.<YYYYMMDD-HHMMSS>` before
overwriting. To roll back:

```bash
ls -la /etc/nginx/sites-available/del.bjk.ai.bak.*
sudo cp /etc/nginx/sites-available/del.bjk.ai.bak.<ts> /etc/nginx/sites-available/del.bjk.ai
sudo nginx -t && sudo systemctl reload nginx
```

Never reload nginx after a config swap without `nginx -t` passing first — that
matches how the helper's own `nginx_rm_site`/`nginx_test_reload` operations behave
(test, then reload only on pass, restore on failure).

## Rebuilding the venv

If `/apps/del/.venv` is corrupted or missing:

```bash
cd /apps/del
python3.10 -m venv .venv
./.venv/bin/pip install fastapi uvicorn jinja2 pydantic argon2-cffi pyyaml python-multipart itsdangerous tomli pytest
cd backend && ../.venv/bin/python -m pytest ../tests/ -q   # confirm before restarting the unit
sudo systemctl restart del-web.service
```

`del-helper.service` does not use the venv — it runs under the system
`/usr/bin/python3` with stdlib only, so a broken `.venv` never affects the helper.
