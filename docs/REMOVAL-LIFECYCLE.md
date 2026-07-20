# DEL — Removal Lifecycle

A removal job executes a previously-built, operator-approved `Plan` through nine
ordered stages. Each stage's steps are recorded in `job_steps` (state
`pending`→`running`→`done`/`failed`) before and after execution; **any
safety-validation failure halts the job before any downstream deletion runs.**

## The 9 stages

1. **Analyze** — resolve the app's associations against current live state.
   Nothing from scan time is assumed still true; the job re-checks reality before
   acting.
2. **Preview (dry-run by default)** — render the full plan: steps, warnings,
   preserved/blocked resources, estimated reclaimed bytes, for operator review.
   Nothing executes. A job's `mode` is `dry_run` unless the operator explicitly
   chooses `live` at execution time (`POST /plans/{id}/execute`) — dry-run is the
   default in both the UI and `helper_client.call()` (`dry_run: bool = True`).
3. **Backup** — `file_backup`, `volume_backup`, `backup_tar` for every resource the
   plan will touch, written to `/apps/del/backups`, before any mutation. Backups
   are content-addressed (`sha256`, `size`) and tracked in the `backups` table
   linked to the `job_id`.
4. **Quiesce** — stop the running workload without deleting anything yet:
   `container_stop`, `systemd_stop`, `tmux_kill`, `process_term`. A failure here is
   trivially reversible (nothing has been removed).
5. **Remove (runtime)** — `compose_down`, `container_rm`, `image_rm` (refused if
   still referenced by another container), `volume_rm` (see volume gate below),
   `network_rm` (refuses `bridge`/`host`/`none` and any network with foreign
   containers still attached).
6. **Remove (host integrations)** — `systemd_disable`/`systemd_rm_unit`,
   `cron_rm`, `nginx_rm_site` (backs up first, runs `nginx -t`, reloads only on
   pass, restores automatically on failure). Because correlation attaches an
   app's stale/disabled `sites-available` config copies by exact `server_name`
   match (docs/DISCOVERY.md), this step removes those alongside the live
   `sites-enabled` file, so a completed removal leaves no nginx config debris
   behind.
7. **Remove (files)** — `path_delete` for project directories/bind data;
   canonicalized (`realpath`) and re-checked against protected roots and the
   specific approved plan on every call.
8. **Validate** — post-removal checks confirm the targeted resources are actually
   gone and nothing else broke (e.g. `nginx -t` still passes, no orphaned
   dependents appeared). Implemented as `jobs.validate_removal()`.
9. **Report** — final job status, reclaimed bytes, and a durable audit-log
   record. Failed jobs are **resumable**: retrying re-enters at the failed step
   rather than from the beginning, without re-running already-`done` steps.

## Safety gates

- **HMAC plan integrity** — an approved plan is signed with an HMAC (key readable
  only by root and the `bjkai` user) when written to the `plans` table. The helper
  does not rely on the signature alone; it independently re-validates every
  argument against `helper-policy.json` regardless of what the plan claims.
- **Volume double-confirmation** — live volume deletion requires three
  independent things to all be true: the plan option `remove_named_volumes`
  enabled, the specific volume individually checked by the operator, **and** a
  typed confirmation phrase (`DELETE VOLUMES`) entered at execution time — not
  just at plan-build time. This is the one irreversible-by-default operation in
  the allowlist, so it is the only one with a second, explicit, typed
  confirmation gate.
- **Protected roots** — never deletable regardless of plan contents: `/`, `/bin`,
  `/boot`, `/dev`, `/etc`, `/home`, `/lib`, `/lib64`, `/opt`, `/proc`, `/root`,
  `/run`, `/sbin`, `/srv`, `/sys`, `/tmp`, `/usr`, `/var`, `/apps`, `/data`, and
  `/apps/del` itself. Enforced independently by the helper on every `path_delete`
  call, not just at plan-build time.
- **Halt-on-failure + automatic rollback** — any safety-validation failure halts
  the job before further deletions in that run. Nginx and systemd removal steps
  specifically restore automatically from their stage-3 backup if a later step in
  the same job fails (e.g. `nginx -t` fails after a site removal) — this is a
  built-in job-engine behavior, not a manual runbook step.
- **Resumable jobs** — a failed job can be retried from the failed step once the
  underlying cause is fixed, without re-running completed steps, so an operator
  never has to restart a partially-successful removal from scratch.
- **Confidence gating (inherited from discovery/correlation)** — only
  `confirmed`/`high`/`manual` associations are eligible for automatic inclusion in
  a plan; `probable` requires explicit per-resource approval; `possible` is always
  blocked and rendered as a warning/preserved entry rather than a step. See
  docs/DISCOVERY.md.

See also `docs/server-audit.md §16` for the original design writeup of this
lifecycle and `§17` for the backup/recovery strategy it depends on.
