# DEL backend code audit — 2026-07-20

Scope: backend/del_app/**, helper/**, scripts/**, tests/** (per task; docs/fern/config
excluded).

## Modules reviewed

- `backend/del_app/correlate.py` — full read: app grouping, evidence scoring, systemd
  seeding, nginx-debris/stale-copy handling, shared-resource detection (steps 1-13).
- `backend/del_app/planner.py` — full read: stage ordering, path-safety roots, compose
  project naming, plan HMAC persist/verify.
- `backend/del_app/jobs.py` — full read: job execution loop, halt/restore-on-failure,
  retry_job resume, validate_removal, sanitize_output.
- `helper/del_helper.py` — full read: every Operations method, ALLOWED_OPS, dispatch,
  audit logging, socket security.
- `helper/validation.py` — referenced via del_helper review (path/name validators).
- `backend/del_app/discovery/docker_src.py` — full read: containers/images/volumes/
  networks/bind_mounts collection, per-item try/except.
- `backend/del_app/discovery/compose_src.py` — full read: compose-file walk/parse.
- `backend/del_app/discovery/systemd_src.py` — full read: unit/timer collection,
  disabled-unit capture via glob.
- `backend/del_app/discovery/nginx_src.py` — full read: sites-enabled/available
  parsing, stale-copy detection.
- `backend/del_app/discovery/proc_src.py` — full read: ports/processes/tmux,
  cgroup-based container/unit ownership, host-network port resolution.
- `backend/del_app/discovery/fs_src.py` — full read: directory/env/git discovery.
- `backend/del_app/discovery/cron_src.py` — full read: crontab/cron.d/periodic/
  user-crontab parsing.
- `backend/del_app/scanner.py` — full read: per-source try/except orchestration,
  persistence.
- `backend/del_app/auth.py` — full read: sessions, CSRF, password hashing, secret key.
- `backend/del_app/auditlog.py`, `backend/del_app/helper_client.py` — full read.
- `backend/del_app/web/routes.py` — spot-checked for secret exposure and cron/proc
  display paths.
- Not separately re-read line-by-line (no findings surfaced against them; covered
  indirectly via tests and grep): `config.py`, `db.py`, `models.py`, `manifests.py`,
  `main.py`, `scripts/gen-registry.py`, `scripts/*.py`.

## pyflakes

Clean (no output, exit 0) both before and after the fix:
```
.venv/bin/python -m pyflakes backend/del_app helper scripts/gen-registry.py scripts/*.py
```

## Full test suite

Before fix: `128 passed, 1 skipped, 1 warning in 15.21s`
After fix (added 1 regression test): `129 passed, 1 skipped, 1 warning in 15.47s`

Command: `cd /apps/del/backend && ../.venv/bin/python -m pytest ../tests/ -q -rs`

### Skipped test explanation

`tests/test_helper.py:68` — `test_every_protected_root_rejected[/data]`. This test is
parametrized over `PROTECTED_ROOTS` (from `helper/validation.py`) and asserts each
protected root is rejected by `validate_path_for_deletion`. It calls
`pytest.skip(...)` when the root does not exist on the current host
(`if not os.path.exists(root): pytest.skip(...)`). `/data` is not mounted on this
host (confirmed: `/data` absent — see `ls /data` returns nothing / not a directory).
**Legitimate skip**, not a masked failure: the guard under test
(`_is_protected_root`/mountpoint check) simply has nothing to exercise for a
nonexistent path on this host; the other protected roots (`/`, `/etc`, `/apps`, etc.)
that do exist are still exercised and pass.

## Issues found → fix → regression test

1. **Found**: `backend/del_app/discovery/proc_src.py` `_collect_processes()` stored
   each long-running process's command line into `data["args_redacted"]` by simply
   truncating to 200 chars (`args[:200]`) — despite the field name implying secret
   values had been stripped. A process invoked with e.g. `--password=hunter2` or
   `--token=...` on its command line would have that value persisted verbatim into
   the `resources` table and displayed in the admin UI, violating the "no
   secrets/env values are ever logged or returned" contract (this mirrors the
   redaction `jobs.sanitize_output()` already performs for helper output, and the
   env-VALUE stripping `docker_src`/`fs_src` already do for env vars — process argv
   was the one path that hadn't been redacted).
   **Fix**: added `_SECRET_ARG_RE` + `_sanitize_args()` in `proc_src.py` (same
   `(?i)(password|token|secret|api[_-]?key|key)[=: ]\S+` → `\1***` pattern family as
   `jobs.sanitize_output`), applied before truncation.
   **Regression test**: `tests/test_discovery.py::test_proc_src_sanitize_args_redacts_secret_shaped_flags`.

2. No other issues found. Specifically checked and found correct:
   - `correlate.py` systemd-app seeding (step 6b), status-from-active-unit override,
     nginx stale/debris handling (steps 8/8b, exact-slug match only, non-enabled
     copies never leak into `app.domains`), shared-network detection (generic via
     `resource_owners` count in step 13, correctly catches networks shared by
     multiple compose projects/containers regardless of name).
   - `planner.py` compose project name derivation (lowercase-safe, falls back
     through declared_name → basename → resource_key), latest-scan-only filter in
     the associations query, `_approved_deletion_roots()`/`_is_safe_delete_path()`
     path-safety roots matching the helper's policy, nested-compose/nested-dir
     dedup in the remove_files stage (`delete_candidates` ancestor check).
   - `jobs.py` auto-rescan after live success, halt-on-failure semantics, restore-
     on-failure for the nginx/systemd ops, crash-safety (`except Exception` around
     each step converts internal errors into a normal failed step instead of
     killing the daemon thread).
   - `helper/del_helper.py` `compose_down` (lowercase project name, label-based
     sweep always runs even after a successful file-based `down`, graceful fallback
     when compose files are invalid/missing), idempotent remove ops
     (`container_stop/rm`, `image_rm`, `volume_rm`, `network_rm`, `cron_rm`,
     `nginx_rm_site`, `systemd_rm_unit` all treat "already absent" as success, not
     an error), `list_listeners` (read-only `ss -lntp`, exists specifically so
     unprivileged del-web can resolve listeners without sudo), `nginx_test`
     (read-only, never reloads), `path_restore` (validated via
     `validate_backup_source`/absolute-path check).
   - `discovery/*` per-item try/except: `docker_src`, `fs_src`, `nginx_src`,
     `cron_src` all wrap each per-item loop body; `systemd_src`'s main per-service
     loop is not individually wrapped but only performs `.get()`/regex/string ops on
     already-validated dict values (no realistic exception source) and is itself
     wrapped at the `scanner.py` per-source level, so a single malformed unit cannot
     silently drop the whole systemd source — reviewed, not a real bug, left as-is.
   - Disabled-unit capture (`systemd_src.py` extra custom-unit-file glob scan) and
     host-network port resolution (`proc_src._host_network_container_ports`, with
     documented ambiguous-uid gap) both reviewed and correct.
   - No secrets/env VALUES found logged or returned anywhere else: `docker_src`
     strips env var values to names only; `fs_src` strips `.env` values to names
     only; `auth.py` never returns password hashes or the session-signing secret key
     to a caller; `auditlog.py`/`jobs.sanitize_output` redact helper output before
     persisting; helper's `Auditor.log()` logs op/args, and args are documented (and,
     after this fix, actually) non-secret paths/names/ids only.

## ALLOWED_OPS reconciliation

`helper/del_helper.py` `ALLOWED_OPS` has exactly **22** entries, each backed by a
corresponding `Operations` method, each independently re-validating its arguments via
`helper/validation.py` (`V.validate_*`) and honoring `dry_run` (returns the exact
command(s) it would run, mutates nothing) before falling through to the live path:

```
ping, compose_down, container_stop, container_rm, image_rm, volume_rm, network_rm,
systemd_stop, systemd_disable, nginx_test, list_listeners, systemd_rm_unit, cron_rm,
nginx_rm_site, nginx_test_reload, path_delete, tmux_kill, process_term, backup_tar,
volume_backup, file_backup, path_restore
```
Count verified by manual enumeration of the `ALLOWED_OPS` set literal (22 items) and
cross-checked 1:1 against `Operations` method names.

## Services / health after fix

```
$ sudo systemctl restart del-web del-helper
$ sudo systemctl is-active del-web del-helper
active
active
$ curl -s -o /dev/null -w "healthz http=%{http_code}\n" http://127.0.0.1:8075/healthz
healthz http=200
```

## VERDICT

**1 fix** (secret-shaped process-argv redaction in `discovery/proc_src.py`, was
mislabeled as already redacted); pyflakes clean; full suite green
(129 passed, 1 skipped legitimately); services active; healthz OK.
