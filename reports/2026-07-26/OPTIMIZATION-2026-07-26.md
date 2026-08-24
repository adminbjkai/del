# DEL optimization pass — 2026-07-26

How changes were identified, what landed, how each was validated. Companion to
`PROGRESS.md` (task scratchpad).

## Goal

Make DEL's inventory UI accurate and operator-friendly (especially **Installed**
dates and all timestamps), remove friction that did not buy real safety, keep
removal workflows productive, and leave docs/code in sync with live behavior.

## How issues were identified

| Finding | How found | Evidence |
|---|---|---|
| Apps list had no install-time column; detail showed raw scan IDs for first/last seen | Code review of `apps.html`, `app_detail.html`, `applications.first_seen` schema | `first_seen`/`last_seen` are scan IDs; UI printed the integers |
| Host install signals existed but unused | `docker_src` already stores `created`; directories had no mtime | Live DB: container `Created` like `2026-05-13T11:31:00Z` for 2fauth |
| UTC / 24h display misread for an Eastern operator | Operator request | Storage is UTC; display was ISO-like without timezone label |
| Dashboard blocked on `docker system df` | Working-tree WIP + code read of `_reclaimable_bytes` | Sync subprocess on request path |
| In-progress rescan emptied the apps list | Working-tree WIP + `_latest_scan_id` used `MAX(id)` | Running scan has no app rows yet |
| Confirm phrase `DELETE VOLUMES` / full backup default | Working-tree WIP + docs drift | Operator productivity preference already in uncommitted code |
| Docs basic auth unnecessary | Operator request | Nginx `auth_basic` on `/docs`, `/_next` |
| Admin password rotation | Operator request | `del-admin change-password --username admin` |
| Flaky cookie tamper test | Full suite intermittently failed `test_session_cookie_sign_and_verify` | Last base64 sig char can decode to same bytes |

## What changed

### UI / product

1. **Installed column** on Applications (sortable), plus human **Installed / First seen by DEL / Last seen by DEL** on app detail.
2. **Install time resolution** (read-time, no schema migration): earliest of
   container `data_json.created` → directory `birthtime`/`ctime`/`mtime` → first_seen scan `started`.
3. **All UI datetimes** via `format_dt` / `relative_dt` / `iso_sort`:
   - Timezone: `America/New_York` (zoneinfo; EST/EDT correct per date)
   - Format: `Mar 15, 2026 4:00 AM ET` (12-hour AM/PM); date-only: `Mar 15, 2026`
   - Sort keys stay UTC ISO for chronological correctness
4. Surfaces using `format_dt`: dashboard (scan strip, recent scans/jobs), apps list/detail, jobs list, settings scans, plan created.
5. **Show removed too** toggle, CSV export of filtered rows, copy-slug, `/` focuses filter.
6. Status badge colors for common app states (`running`, `stopped`, `active`, …).
7. Execute gate phrase **`y`**; complete-removal preset backup default **none** (still overridable).
8. Non-blocking reclaimable docker df (stale-while-revalidate); latest scan = latest **`status='done'`** only.

### Discovery

9. `fs_src` records directory `mtime`/`ctime`/`birthtime` as UTC ISO-Z (needs a rescan before older directory rows gain them; container `created` already present).

### Security / deploy

10. **Docs basic auth removed** from Nginx `/docs`, `/docs/`, `/_next/`, `/_local`. Inventory `/miscwork.html` remains basic-auth.
11. **Admin password** rotated via `del-admin change-password` (not stored in this doc).
12. Repo copy `config/nginx-del.bjk.ai.conf` is the source of truth; deployed to `sites-available/del.bjk.ai`.

### Docs sync

13. README, SECURITY, OPERATIONS, ARCHITECTURE, REMOVAL-LIFECYCLE, fern browsing/removing/backups/architecture/helper-ops updated for open docs, confirm phrase `y`, backup default, Installed column, Eastern display.

### Tests / hygiene

14. New/extended tests for date helpers (including Eastern day-boundary), apps Installed column, show-removed, latest-scan ignores `running`, dashboard scan strip, fs_src timestamps.
15. Cookie-tamper test flips first signature character (not last base64 char).
16. `routes` uses `jobs.CONFIRM_VOLUMES_PHRASE`; parameterized `last_seen = ?`.
17. **Job retry reliability:** `retry_job` now clears job status to `pending`
    (was leaving stale `failed` visible while the retry thread started). Retry
    test waits for both job `success` and step `done` (independent verifier
    found a race under full-suite load).

## Validation (executed this session)

| Check | Result |
|---|---|
| `pytest ../tests/ -q` | **136 passed**, 1 skipped (final full run this session) |
| `pyflakes backend/del_app helper` | clean |
| Live `auth.verify("admin", …)` after password change | ok |
| `https://del.bjk.ai/docs/overview` without credentials | **200** (after nginx reload) |
| `https://del.bjk.ai/miscwork.html` without credentials | **401** (inventory still gated) |
| `nginx -t` + reload | ok |
| `del-web` restart + `/healthz` | `{"ok":true,...}` |
| Live DB sample formats | e.g. 2fauth installed → `May 13, 2026 7:31 AM ET` from Docker Created `2026-05-13T11:31:00Z` (EDT = UTC−4) |

## Operator notes

- **Rescan** (`Settings → Run scan` or `del-admin rescan`) to populate directory mtime/ctime on resources discovered before this change. Docker apps already show Installed from container Created without a rescan.
- Display always says **ET**; offsets auto-adjust for daylight saving via `zoneinfo`.
- Do not commit real passwords. Rotate with `del-admin change-password --username admin --password-stdin`.

## Explicit non-goals this pass

- Host remediation from `AUDIT-2026-07-21.md` (kanbu, semalist, ufw, etc.) — needs separate owner decisions for live services.
- Removing inventory basic auth.
- Schema migration for a stored `installed_at` column (computed is enough and self-heals on better discovery data).
