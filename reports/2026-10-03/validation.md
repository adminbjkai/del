# DEL "survey sheet" overhaul — validation, 2026-10-03

Release `v2026.10.3` (commit `6a35fb6`). This pass rebuilt the web UI, removed
AG Grid and dead code, and fixed the accuracy problems an audit of the live host
turned up. The privileged helper and its allowlist did not change; no migration
was added. This is a scoped engineering pass, not a claim that every defect is gone.

## Method

- A preview copy of the app (copied database, no helper socket) ran beside
  production. Every page was loaded in Chromium with the production nginx CSP
  injected, at 1440px and 390px, in both themes.
- A fresh-context verifier walked the 400-line feature checklist written
  before the redesign (every page, table feature, shortcut, gallery control,
  plan/job behaviour) and reported 4 defects; all were fixed and re-checked.
- A separate reviewer read the backend Python diff and found 2 bugs (an
  uncaught malformed `data:` favicon, a docker failure shown as 0 B); both
  fixed with regression tests.
- Accuracy: the old code (`a2506c1`) and the new code were served side by side
  from two identical snapshots of the same database and their pages compared.

## Results

| Check | Result |
|---|---|
| Tests | 459 passed, 2 skipped (`-W error`); pyflakes clean; `node --check` on every static script; GitHub CI green |
| Console / CSP | 0 errors and 0 warnings on 15 desktop pages (dark), 5 mobile pages (light), and the verifier's 30-URL sweep |
| Links | 435 internal links, all 200 |
| Asset weight per page (uncompressed, first visit) | about 2.2 MB before (AG Grid 1.65 MB of it) → about 240 KB including fonts; later visits fetch only HTML (immutable, content-hashed URLs) |
| Same-database comparison, old vs new | 87 apps with identical status, resources, warnings, dates and domains (only `compose_stopped` now reads "compose stopped"); orphans 164 / 298 / 40 / 502 and every per-type count identical; dashboard 87 / 164 / 17 / 12 / 86.8 GB identical; every resource table's row count identical |
| Live after deploy | `/healthz` ok on scan 342; public `/login` 200; `/static/*?v=` served gzip + `immutable`; 113 authenticated pages (every app page) render 200 on a snapshot of the live database |
| Live scan | scan 342: 88 apps (57 running, 27 stopped, 4 absent), 1434 resources, 6.8 s (scan 341: 11.1 s) |

## Accuracy fixes, with the evidence that prompted them

- View Apps showed n50, deeptutor and querypad as "Online, HTTP 401": nginx
  answers basic auth itself while nothing listened on ports 8055/8043/8059.
  Such domains, and every failing domain, are now listed as Unavailable with
  the reason.
- freeze-watch showed running because a oneshot setup unit is `active
  (exited)`, while `freeze-watch-recorder.service` is inactive. The status rule
  now ignores finished oneshots when a daemon is down; scan 342 records
  freeze-watch as stopped. (The recorder itself is still down on the host;
  DEL does not start services.)
- "Disk usage" said it included Docker volumes and did not: 86.8 GB of
  project directories, plus 149.8 GB of local volumes per `docker system df`.
- The inventory was 30 hours old with no indication on most pages: every page
  now shows the scan it reads, and `scan_interval_hours = 6` keeps it fresh.

## Incident during the pass

`del-web` serves templates from the working tree it runs from. While the new
templates were on disk but the service still ran the old code, `GET /login`
returned 500 (missing `asset()` helper) from 13:23 to 13:58 EDT, until
`del-web` was restarted. Future UI work should happen in a separate git
worktree, or the service should be restarted as soon as templates change.
