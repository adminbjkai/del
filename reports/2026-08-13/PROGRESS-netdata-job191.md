# PROGRESS — Fix DEL job 191 (netdata LIVE removal)

**Goal (one line):** Netdata removal must not touch other apps, and a dead pid must not fail the job.
**Started:** 2026-08-13 · **Budget:** this session · **Sandbox:** no (live host recovery)
**Definition of done:** glmflix + url-shortener running again; parser/broad-root/process_term fixed with tests; new netdata plan no longer lists foreign units.

## Assumptions
- User wanted to remove **netdata**, not glmflix / url-shortener.
- Job 191's plan is poisoned (HMAC-signed); do **not** retry it. Operator must build a new plan after rescan.
- Restarting the two wrongly-stopped enabled services is recovery, not a new removal.

## What actually failed
Job 191, step 3 `process_term` for pid 1716 (`/apps/url2/.../url-shortener`): "pid 1716 not running".
That pid belonged to **url-shortener.service**, which step 2 had just stopped.

Root causes:
1. `systemd_src._show_units` mixes ExecStart across batched `systemctl show` records (Id= is not first; parser ignores blank-line separators).
2. Netdata bind-mounts `/`, `/sys`, `/proc`, `/var/log`; `_is_broad_root` did not treat those as non-ownership, so Step 9 attached foreign units at confidence 95.
3. `process_term` is not idempotent when the pid is already gone (systemd_stop already killed it).

## Stages

| # | Stage / deliverable | Acceptance criterion | Status | Evidence |
|---|---|---|---|---|
| 1 | Restore collateral services | glmflix + url-shortener active again | ✅ | `systemctl start` then `is-active` both active (MainPID 377941 / 377942) |
| 2 | Fix systemd show parser | batched show keeps each unit's ExecStart | ✅ | live `_show_units`: glmflix=/apps/glmflix/start.sh, url-shortener=/apps/url2/... |
| 3 | Fix broad-root + Step 9 matching | netdata host mounts do not absorb foreign units | ✅ | scan 197: 0 systemd units on netdata; glmflix/url2 own their units at 95 |
| 4 | process_term + planner safety | dead pid = success; foreign unit/process skipped | ✅ | helper `pid 999999999 already absent`; planner test + new plan has no process_term |
| 5 | Tests + deploy + rescan + new plan check | tests green; helper/web restarted; new plan has no glmflix/url-shortener | ✅ | pytest 149+ then 23 jobs tests; healthz 200; new plan 10 netdata-only steps |
| 6 | Execute-time foreign-unit guard | retry of job 191 cannot systemd_rm glmflix/url-shortener | ✅ | `test_job_skips_foreign_systemd_unit_at_execute_time`; jobs._foreign_step_reason |

## Decisions log
- Do not retry job 191 (would disable+remove glmflix/url-shortener unit files).
- Restart the two enabled services that job 191 stopped.

## Blockers / needs user
- After the fix: build a **new** netdata plan and run it; do not retry #191.
