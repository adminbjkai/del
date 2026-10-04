# 2026-10-04 refinement pass — validation

Branch `pass-2026-10-04` (worktree), verified before merge on two preview
servers reading the same snapshot of the live database (scan #345):
old code (master `ec0f21c`) on :8298, new code on :8299, no privileged helper.

## Automated
- `pytest ../tests -q -W error`: 477 passed, 2 skipped (459 before the pass).
- `pyflakes backend helper scripts tests`: clean. `node --check` on every static JS file: clean.
- `tests/conftest.py` used to put `/apps/del/backend` on `sys.path`, so a run
  from any other checkout tested the deployed code; it now tests its own tree.

## Accuracy parity (old vs new, same snapshot) — PASS
Row counts identical on /apps (92), /jobs (200), all 9 /orphans tables, all 13
on /orphans?show=all and all 16 /resources/<type> tables; subnav counts,
dashboard figures, orphan buckets (182 actionable / 307 system / 39 expected /
528 no probable owner) and View Apps online/unavailable counts equal. /apps
rows identical cell for cell. Only intended presentation changes differ
(Ports column, "no check", duplicate Key/Path columns hidden, cron schedules).

## Console and links (new code, production CSP enforced) — PASS
48 pages crawled (every sidebar page, all resource types, 14 apps, jobs,
plan, manifests, settings, assistant, view-apps, orphans?show=all), every tab
clicked: 0 console errors/warnings, 0 page errors, 0 responses >= 400; all 568
unique same-origin links 200. Re-crawled after the last fixes: 0 errors
desktop and 390px.

## New features checked against the database — PASS
- Status by scan: ticks for all 92 apps match each scan's recorded
  `app_status` (0 mismatches); "since" sentences correct.
- Wiring lanes: totals (shown + "+N more") match an independent derivation
  for all 92 apps (0 mismatches); excluded rows left out.
- Scan trend: 90 points per chart (#255–#345), every point equals the DB.
- Account: sessions listed, current marked, "Sign out other sessions" removed
  10 of 11 and kept the current one.
- Tables: 0 horizontal overflow at 1440 and 1280 px on the 11 pages tested
  (old code overflowed at 1440, e.g. orphans 1630 vs 1079 px).

## Defects found by the verifier and fixed
- Mobile navigation drawer links could not be tapped (backdrop above the
  drawer's stacking context) — present before this pass.
- Glossary still said "no healthcheck"; Help sheet stayed open after widening
  the window; step durations showed raw seconds; settings scan table cramped
  at 390 px.

## Review
Fresh-context code review of the full diff: no must-fix issues; three
suggestions applied (duplicate listening ports, name fallback, torn icon-cache
file treated as a miss) plus a guard so "sign out others" needs a current
session. Icon-link restriction (same host, https) checked against all 88
gallery domains: none used an off-site icon link.
