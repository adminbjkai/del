-- Secondary indexes for the hot read paths.
--
-- 001_init.sql declared foreign keys but no indexes. SQLite does not create
-- indexes for foreign keys, so every page load made it build AUTOMATIC
-- COVERING INDEXes over `associations` on the fly — a transient index over
-- ~3k rows constructed, used once and discarded, twice per dashboard render.
--
-- Measured on a copy of the production DB (14 hot queries, 50 iterations):
-- 16.6 ms -> 6.9 ms total (2.4x); per-type resource counts 8.2x; the orphan
-- anti-join 3.6x; the /apps aggregate 3.7x. DB grows ~4% (6.25 -> 6.52 MB).
--
-- Two deliberate omissions, both measured:
--   * NO standalone `resources(last_seen)`. It makes the planner flip the
--     dashboard "uncertain" join to outer-first and the whole set regresses
--     (6.92 -> 7.38 ms). The composite (type, last_seen) below is strictly
--     better and still serves last_seen-only lookups for a given type.
--   * NO `ANALYZE`. With sqlite_stat1 present the "uncertain" query picks a
--     skip-scan and degrades ~10x. The query was rewritten as an EXISTS
--     subquery in routes.py, which is plan-stable either way, but there is
--     no reason to add the statistics that caused the regression.
--
-- `audit_log` is intentionally NOT indexed: nothing in the codebase reads it.
-- It needs a retention policy, not an index.

-- associations: the single hottest table. resource_id drives the orphan
-- anti-join and the resource->owner map; (app_id, excluded) drives the /apps
-- aggregate, app detail, and the per-app DELETE the scanner runs 326 times.
CREATE INDEX IF NOT EXISTS idx_assoc_resource          ON associations(resource_id);
CREATE INDEX IF NOT EXISTS idx_assoc_app_excluded      ON associations(app_id, excluded);
CREATE INDEX IF NOT EXISTS idx_assoc_removal_eligible  ON associations(removal_eligible);
CREATE INDEX IF NOT EXISTS idx_assoc_shared            ON associations(shared) WHERE shared = 1;

-- resources: every inventory page filters by type within the latest scan.
CREATE INDEX IF NOT EXISTS idx_resources_type_lastseen ON resources(type, last_seen);

-- applications: scan-scoping on the apps list, gallery and dashboard.
CREATE INDEX IF NOT EXISTS idx_applications_lastseen   ON applications(last_seen);

-- jobs / job_steps: job detail, the execution loop, and the concurrency guard
-- that refuses a second live job for the same plan.
CREATE INDEX IF NOT EXISTS idx_job_steps_job_seq       ON job_steps(job_id, seq);
CREATE INDEX IF NOT EXISTS idx_jobs_plan_status        ON jobs(plan_id, status);

-- backups: read by the rollback path once per failed removal.
CREATE INDEX IF NOT EXISTS idx_backups_job             ON backups(job_id);

-- scans: latest_done_scan_id() runs on essentially every request.
CREATE INDEX IF NOT EXISTS idx_scans_status_id         ON scans(status, id);

-- sessions: looked up on every authenticated request, and swept on login.
CREATE INDEX IF NOT EXISTS idx_sessions_expires        ON sessions(expires);
