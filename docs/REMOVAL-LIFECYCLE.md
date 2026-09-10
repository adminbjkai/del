# DEL — Removal Lifecycle

A removal job executes a previously-built, operator-approved `Plan` through six
ordered stages. Each stage's steps are recorded in `job_steps` (state
`pending`→`running`→`done`/`failed`) before and after execution; **any step failure
halts the job before any downstream deletion runs.**

Two things bracket those six but are not stages: **analysis and preview** happen
at plan-build time, and the **report** is the job's terminal status plus its
audit-log records. Neither produces a `job_steps` row.

Two routes build and run this: the step-by-step `/apps/{slug}/plan` →
`/plans/{id}` preview → `/plans/{id}/execute` flow (dry-run or live, operator
chooses each option), and the one-click `POST /apps/{slug}/remove` ("Remove
app now" on the app detail page), which builds a complete-removal plan with
every option on and runs it live immediately, with no preview or dry-run
step. Both call the same `planner.build_plan()`/`persist_plan()` and the same
job engine described below, so every rule in this document applies equally to
both.

## Before the stages: building the plan

`planner.build_plan()` resolves the app's associations against the latest scan
whose status is `done`, decides which become steps, and renders the rest as
warnings and preserved resources with an estimated reclaim figure. Nothing
executes. A job's `mode` is `dry_run` unless the operator explicitly chooses `live`
at execution time (`POST /plans/{id}/execute`) — dry-run is the default in both the
UI and `helper_client.call()` (`dry_run: bool = True`).

**Plan building refuses rather than producing an empty plan.** If the app has
associations but none of them appear in the latest *completed* scan, `build_plan`
raises `PlanError` asking the operator to re-scan. Previously the query used
`MAX(id)` with no status filter, so while any scan was running the builder saw zero
resources and emitted an empty plan that then executed "successfully" having
removed nothing. Three live removals hit exactly that.

## The six stages

`planner.STAGE_ORDER` is exactly: `backup`, `quiesce`, `remove_runtime`,
`remove_host`, `remove_files`, `validate`. Those six strings are the only values
`job_steps.stage` ever holds. `_check_stage_order()` enforces that a plan's steps
appear in non-decreasing `STAGE_ORDER` rank, raising `PlanError` if they don't;
both `build_plan()` and `persist_plan()` call it, so a stage-ordering bug is
caught at build/persist time rather than surfacing mid-execution.

1. **`backup`** — `file_backup`, `volume_backup`, `backup_tar` for the resources
   the plan will touch, written under `/apps/del/backups`, before any mutation.
   Only runs when the plan's backup mode is *Config* or *Full*; the default is
   *None*, which produces no backup steps at all. On a **live** job each completed
   backup step also writes a `backups` row (`job_id`, `kind`, `src`, `dest`) — that
   row is what the rollback path below reads. The `sha256`/`size` columns exist but
   are not populated; these are plain copies, not content-addressed archives.
2. **`quiesce`** — stop the running workload without deleting anything yet:
   `systemd_stop` (timers before services), `tmux_kill`, `process_term`,
   `container_stop`. A failure here is trivially reversible; nothing has been
   removed.
3. **`remove_runtime`** — `compose_down` (or `container_rm` when there is no
   compose project), `network_rm` (skipping `bridge`/`host`/`none` and shared
   networks), `volume_rm` (see the volume gate below), `image_rm` (refused if still
   referenced by another container). Before emitting a `compose_down` step, the
   planner checks whether another app's compose project would resolve to the
   *same* Docker Compose project label under a different path; if so it adds a
   plan **warning** (not a hard error) naming both apps and paths, since
   `compose_down`'s label sweep is host-wide and could otherwise remove the
   other app's containers too.
4. **`remove_host`** — `systemd_disable`/`systemd_rm_unit`, `cron_rm`,
   `nginx_rm_site` then `nginx_test_reload`. Every nginx path for the app goes into
   one `nginx_rm_site` step, ordered `sites-enabled` first so removing a
   `sites-available` target never leaves a dangling symlink that fails `nginx -t`.
   Because correlation attaches an app's stale/disabled `sites-available` copies by
   exact `server_name` match (docs/DISCOVERY.md), this stage removes those
   alongside the live file, so a completed removal leaves no nginx config debris.
5. **`remove_files`** — `path_delete` for project directories and bind-mount data;
   canonicalized (`realpath`) and re-checked by the helper against the protected
   roots, the never-delete list, and the approved deletion roots on every call.
   Paths nested inside another path already being deleted are dropped, so the
   child step cannot fail with "path does not exist" after its parent went first.
6. **`validate`** — one `validate_removal` step running post-removal checks:
   container/volume/network absent, unit inactive, `nginx -t` still passes.
   Implemented as `jobs.validate_removal()`, as direct read-only subprocess checks
   independent of the helper. In a dry run the checks still run but their failures
   are reported as informational, since nothing was actually removed.

Afterwards, a successful **live** job triggers a rescan so the UI reflects the new
state immediately; a rescan failure is audited as `post_removal_rescan_failed`
rather than silently leaving a stale inventory behind.

## Safety gates

- **HMAC plan integrity** — a plan is signed with an HMAC over the canonical JSON
  of its steps (key `/apps/del/config/secret.key`, mode `0600`) when written to the
  `plans` table, and `planner.verify_plan()` recomputes and compares it before a job
  is created and again before it runs. **This check is web-side only.** The helper
  has no concept of a plan — it is sent an op, args and a `dry_run` flag, records
  `plan_id`/`step_id`/`job_id`/`requested_by` in its audit line without acting on
  them, and independently re-validates every argument against
  `/etc/del/helper-policy.json`. The two controls are independent, not layered:
  tampering with `steps_json` is caught by the HMAC; an argument the helper
  disallows is refused whether or not a plan vouches for it. `POST
  /plans/{id}/execute` distinguishes the two failure modes at the HTTP layer:
  an unknown plan id (`planner.PlanNotFoundError`, a subclass of `PlanError`)
  returns 404; any other `PlanError` (e.g. a failed HMAC check) returns 409.
- **Volume double-confirmation** — live volume deletion requires three
  independent things to all be true: the plan option `remove_named_volumes`
  enabled, the specific volume individually checked by the operator, **and** a
  typed confirmation phrase (`y`) entered at execution time — not
  just at plan-build time. This is the one irreversible-by-default operation in
  the allowlist, so it is the only one with a second, explicit, typed
  confirmation gate.
- **Protected roots** — never deletable regardless of plan contents: `/`, `/bin`,
  `/boot`, `/dev`, `/etc`, `/home`, `/lib`, `/lib64`, `/opt`, `/proc`, `/root`,
  `/run`, `/sbin`, `/srv`, `/sys`, `/tmp`, `/usr`, `/var`, `/apps`, `/data`, and
  `/apps/del` itself. Enforced independently by the helper on every `path_delete`
  call, not just at plan-build time.
- **Protected units** — the helper refuses to stop, disable or remove DEL's own
  `del-*` units, `sshd`, `nginx`, `docker`, `systemd-*`, `cron` and the rest of
  `protected_units` in the policy, whatever a plan says.
- **Halt-on-failure + rollback** — any step failure halts the job before further
  deletions in that run. Two distinct restore mechanisms exist, and it is worth
  knowing which is which:
  - *Inside `nginx_rm_site`*: the helper backs up each site file, removes them,
    runs `nginx -t`, and copies them straight back if the test fails. Entirely
    self-contained, no database involvement — this one has always worked.
  - *Job-level*: when an `nginx_rm_site`, `nginx_test_reload`, `systemd_disable`
    or `systemd_rm_unit` step fails, the engine reads this job's `backups` rows
    newest-first and calls `path_restore` for each, then audits the real
    per-backup outcome. Volume archives are skipped and reported as
    manual-restore, because a `volume_backup` is a tar of contents rather than a
    filesystem path. **This path only has anything to restore if the plan's
    backup mode was Config or Full and the job ran live.** Before 2026-08-24
    nothing wrote the `backups` table at all, so it silently iterated an empty set
    while still auditing that a restore had been attempted.
- **Resume is engine-only and not exposed.** `jobs.retry_job()` resets the first
  failed step and everything after it and re-executes from there without re-running
  completed steps — but nothing calls it. There is no `/jobs/{id}/retry` route, no
  button in the UI and no `del-admin` subcommand. In practice, recovering a failed
  job today means fixing the cause and either invoking `retry_job` from a Python
  shell or building a fresh plan.
- **Confidence gating (inherited from discovery/correlation)** — only
  `confirmed`/`high`/`manual` associations become steps, and only when not excluded
  and not shared-and-unapproved. `probable` is **always** preserved with a warning,
  regardless of per-resource approval — raise it with a manifest entry to make it
  removable. `possible` is always blocked. See docs/DISCOVERY.md.
- **Per-resource approvals are not durable.** `approve`, `exclude` and
  `mark-shared` write to an association row, and the next scan deletes and
  re-inserts that row. Set them immediately before building the plan, or record the
  correction in the app's manifest instead.

The original design writeup of this lifecycle is `docs/server-audit.md` §16, with
the backup/recovery strategy in §17 — that file is gitignored and present on the
deployment host only.

## Robust compose teardown (2026-07-20)
Backup destinations are unique per *source* file, not per basename:
`{backups_dir}/{slug}/{resource_type}/{flattened-parent-dir}/{filename}`. Two
config files sharing a name (`server/compose.yaml` and
`server/config/compose.yaml`) therefore get separate destinations. The filename
itself is preserved because `path_restore` requires a restore to keep the
backup's basename — that is what makes the restore target unambiguous.

The `remove_runtime` stage's `compose_down` is resilient to broken project state,
so removals no
longer get stuck on: (a) an uppercase project name (Docker Compose forces
lowercase — the helper lowercases before calling compose); (b) a missing or
unparseable compose file (falls back to a label-based teardown that removes
containers by their `com.docker.compose.project` label, needing no compose
file); (c) a compose file that no longer matches what is running (after any
compose-down attempt, the helper always sweeps remaining containers carrying
the project label, so stragglers can't keep a volume "in use" and block the
rest of the removal).
