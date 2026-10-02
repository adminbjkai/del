# DEL integrity and maintenance audit — 2026-10-02

This pass reviewed scan publication, inventory queries, review actions, monitoring,
Settings feedback, dependency consistency, documentation, CI, and live deployment.
It used the existing Python/SQLite/systemd stack; no new runtime dependencies,
service, scheduler, or integration was introduced. The privileged helper and its
allowlist did not change. This is a scoped engineering audit, not a claim that all
possible defects or all host discovery edge cases have been eliminated.

## Findings and changes

- Scan writes committed resources before apps/associations and swallowed
  correlation failures into an empty successful scan. Publication is now one
  transaction; raised source errors, invalid manifests, correlation failures, and
  persistence failures retain the previous inventory and mark the attempt failed.
- The Python scan lock did not cover the admin CLI. A Linux advisory lock beside
  the DB now serializes scans across processes, and startup cleanup respects an
  active CLI scan. A connection error no longer strands the Python lock.
- Replacing association rows discarded manual reviews. Migration 004 stores
  explicit exclusion/shared flags; matching pairs retain protections. Approval is
  retained only while confidence, ownership, effective shared status, data-loss
  risk, and removal eligibility are unchanged.
- Multi-query reads could mix inventories during publication. Inventory pages,
  removal-plan reads, and assistant context now use read transactions.
- Owner lookups included stale apps and built unbounded SQL parameter lists.
  They now scope apps to the requested completed scan and batch IDs. Reviews
  invalidate cached orphan counts; excluded mappings stop inflating uncertainty.
- The last-scan strip could identify a failed attempt as the completed inventory
  after five failures. It now retrieves the actual latest completed scan.
- Settings lacked live scan feedback and outcome detail. It now shows counts,
  durations/failure reasons, a live elapsed indicator, and a disabled scan button
  while discovery is running. The JavaScript elapsed parser treats SQLite times
  as UTC instead of the browser's local zone.
- Health checks swallowed schema errors into HTTP 200. They now return HTTP 503
  without exposing exception details. Migration DDL and bookkeeping roll back
  together on failure.
- CI still ran Fern checks against the removed directory. The obsolete job and
  ignore entry are removed; CI checks all three application JavaScript files.
  Settings' obsolete optional-module/import fallbacks are removed.

## Verification

- Baseline: **425 passed, 2 skipped**.
- Updated suite: **450 passed, 2 skipped**, warnings treated as errors.
- Pyflakes: passed for backend, helper, scripts, and tests.
- Node syntax checks: passed for app.js, assistant.js, and theme-init.js.
- `pip check`: no broken requirements.
- Regression coverage includes failed collection/manifest/correlation/persistence,
  complete inventory rollback, readers during publication, CLI/web file locks,
  stale CLI recovery, review persistence and approval revocation, migration rollback
  and retry, large owner maps, stale owners, orphan cache invalidation, health
  failures, and completed-scan selection after repeated failed attempts.
- Authenticated live Chromium checks: Dashboard, Applications, Containers,
  Orphans, Jobs, Settings, Assistant, and View Apps all returned HTTP 200.
- Browser interactions: table filtering/clearing, theme persistence after reload,
  command palette app search, and nonempty CSV export passed. No JavaScript page
  errors were recorded. Screenshot review covered Settings at 1440px and
  Applications at 390px; mobile body width matched the 390px viewport.
- A scan started from live Settings showed its running indicator and disabled
  button, then completed as scan **341** in **11.1s**, with **87 apps**, **1,365
  resources**, and **879 associations**. Discovery made no host mutations.
- Both local and public `/healthz` returned HTTP 200 with `ok: true`. Web/helper
  units remained active. SQLite quick-check returned `ok`; foreign-key check
  reported zero violations. Migration 004 is applied in production.

## Performance measurement

A consistent copy of the production DB was used to replay the same 1,343 resources
and 87 apps through the previous scanner at commit `4956f12` and the new scanner.
Host discovery and correlation were stubbed with the same captured input; each
version ran three times on fresh DB copies with SQLite statement tracing enabled.

| Measurement | Before | After |
|---|---:|---:|
| Lookup SELECT statements per publication | 1,430 | 3 |
| Median persistence time, final run | 56.81ms | 56.96ms |

The query reduction is substantial, but these short timing samples do **not** show
an end-to-end speedup. The new path includes review validation and stronger
publication guarantees. Host discovery still dominates the full scan duration.

## Deployment and operational limits

The production DB was backed up before migration to the local, gitignored
`backups/del-20261002T071058.db`. Migration 004 was applied, then only del-web was
restarted. No helper redeploy, Nginx edit, service-unit edit, or scheduler change
was needed. Browser screenshots and the test CSV remain under the gitignored
`backups/audit-2026-10-02/`; the temporary verification session was revoked.

Individual collectors still log and skip some unreadable/transient artifacts
internally; the scanner rejects source exceptions, not every internally handled
partial failure. Review flags last only while an app/resource pair remains
associated; manifests are the durable declaration mechanism for rediscovery.
Existing exclusions migrate conservatively because their original source was not
recorded. The old schema cannot distinguish old manual shared flags from inferred
ones; new manual shared reviews are explicit.

Health checks cover the DB and required schema; they do not test the helper,
external providers, or all discovery sources. No destructive removal was run on
production. Removal safety continues to rely on the automated planner/helper/job
suite. Historical reports and operational backups are retained intentionally.
