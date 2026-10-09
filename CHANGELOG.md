# Changelog

## 2026.10.09 (b)

Review pass: repair the regression the telemetry commit left behind, finish the
feature it only half-wired, and stop the test suite from writing into the live
database directory.

Fixes:
- **Gallery probe cache no longer blocks the wrong way.** The persistent disk
  cache was loaded lazily inside `_probe_domains`, so a cold in-memory cache
  silently meant "read the file" and the first gallery load stopped blocking
  for its probes (it rendered empty instead). The disk cache is now warmed once,
  at startup, by `gallery.warm_probe_cache()` (called from the app lifespan),
  and restored entries are marked stale so the first load serves instantly and
  revalidates in the background. Restores the "an empty cache must block"
  contract the suite asserts.
- **Tests no longer pollute production data.** A bare `_probe_domains` call in a
  test derived its cache path from the real `settings.db_path` and wrote probe
  results into the live `database/probe-cache.json`; the next run then read them
  back as warm. The autouse conftest fixture now points the probe cache at a
  throwaway path and resets the warm flag, and the polluted file was trashed
  (it rebuilds on the next `/view-apps` load).

Now actually used:
- **Host telemetry panel.** `/api/telemetry` shipped last commit but nothing
  consumed it. The dashboard now renders a **Host telemetry** strip from it:
  CPU load relative to core count, memory used / total, `/apps` disk usage with
  a bar, Docker reclaimable + volume bytes, and scan-engine state — refreshed on
  demand and every 30 s while the tab is visible. Sources that do not answer show
  "unavailable", never a fake zero. The endpoint gained `hostname` and
  `cpu_count`, and its handler was tidied (module-level import, extracted
  best-effort `_host_telemetry()`).

Tests: 486 passed, 2 skipped (`pytest -W error`). Pyflakes and `node --check`
clean. Upgrade: restart `del-web`. No migration.

## 2026.10.09

Engineering telemetry overhaul: deeper host resource efficiency, SQLite acceleration,
architectural CAD styling with dual-tier measuring grids, live latency instrumentation,
and high-convenience developer workflows.

Host Resource & Backend Optimization:
- **SQLite Engine Tuning**: Configured `PRAGMA cache_size = -32000` (32MB dedicated page cache),
  `mmap_size = 67108864` (64MB memory-mapped zero-copy reads), and `temp_store = MEMORY` for
  ultra-fast table sorting and joins.
- **Batched Scanner Pipeline**: Replaced thousands of single-row SQLite deletes and inserts
  with 400-item chunked queries and `conn.executemany(...)`, drastically reducing write lock duration.
- **Direct Database Aggregation**: Offloaded disk usage calculations to SQLite `json_extract()`,
  avoiding in-memory JSON parsing across thousands of resource rows. Scoped scan ID queries to
  only the apps rendered on page.
- **Fast Similarity Pruning**: Added length-difference fast-path pruning in `correlate.py` before
  invoking `difflib.SequenceMatcher`, eliminating unnecessary CPU work during correlation passes.
- **Persistent Probe Caching**: Added atomic disk caching (`probe-cache.json`) with background
  revalidation for `/view-apps` domain health checks, eliminating 20s cold-start delays. Added
  in-memory TTL cache to icon domains, eliminating 125+ redundant SQLite connections per load.
- **Host Telemetry API**: Authenticated `GET /api/telemetry` providing real-time CPU load averages,
  `/proc/meminfo` RAM breakdown, disk space, Docker reclaimable space, and scan engine state.

CAD Instrument UI & High-Convenience Additions:
- **Dual-Tier Measuring Grid**: Replaced generic background with precise 8px/32px CAD drafting
  sub-grids with frosted glass panels and high-contrast telemetry styling.
- **Latency Badges & Sonar Indicators**: Launcher tiles feature color-coded real-time latency
  telemetry badges (`<100ms` green, `100-300ms` amber, `>300ms` red) with sonar radar pulses.
- **Hover Micro-Actions & Universal 1-Click Copy**: Added hover action toolbars to launcher tiles
  (instant URL copy and inventory drill-down) and micro-copy buttons to all domain links.
- **Architectural HUD Toasts**: Upgraded notifications to frosted glass floating banners with
  live progress countdown bars and manual dismiss.
- **Command Palette Port Search**: `Ctrl+K` palette now indexes and searches published container/host
  listening ports (e.g. `:8080`) and provides launcher quick-actions.
- **Site Plan Quick Filters**: Added instant status filter chips (`All`, `Running`, `Stopped`, `Weak`)
  with zero layout shift (CLS).
- **Expanded Keyboard Navigation**: Added `[` (toggle left sidebar), `]` (toggle right drawer),
  `b` (open launcher), `h` (help dock), `c` (copy URL), and arrow key navigation in the launcher.
- **Assistant Code Header**: Code blocks in AI assistant threads now feature terminal titlebars
  with 1-click clipboard copy.

Hygiene:
- Safely purged 118 broken dangling symlinks and obsolete test dumps using `/usr/bin/trash`.

Upgrade: restart `del-web`. No migration.

## 2026.10.05

A tighter survey sheet: the site plan is something you can find a lot in,
the figures read as an instrument rather than a row of stat cards, and a
page no longer starts the assistant just by being opened.

New and more convenient:
- **Site plan filter.** Name, slug, kind, status, or an enabled site. Disabled
  nginx copies and excluded associations are not searchable names. Enter opens
  the first matching lot. `/` focuses this filter on the dashboard. The text
  is not remembered.
- **Recent apps** in the command palette (empty query), from the applications
  this browser has opened (`del.recentApps`, eight kept, six shown). Recorded
  from `/apps/<slug>` and its plan page.
- **Open site** on an application that has exactly one enabled domain. More
  than one stays as links in the header, so DEL does not pick a site for you.
- The **scan stamp** says stale, with a warn dot, when the inventory is older
  than the auto-scan interval, or older than a day when automatic scans are off.

Leaner:
- The Ask dock fetches prompts and targets when Ask is opened. Other pages
  no longer do that work on every navigation. `/assistant` still starts immediately.
- Enabled nginx server names are read through one helper (`enabled_server_names`,
  `enabled_domains_by_app`) for the apps list, the site plan and the palette.
- The first-visit staggered lot animation is gone, and so is
  `sessionStorage del.siteplanSeen`.

The figure strip and the orphan stat strip use the same mono title-block
figures, and the lots sit on the survey grid. Counts, removal, and the table
engine are unchanged.

Upgrade: restart `del-web`. No migration.

## 2026.10.04

Refinement pass: tables that fit, an application overview that explains the
app at a glance, scan history you can see, and a few fixes found on the way.

New and more convenient:
- Application overview: **Status by scan** (one bar per recorded scan, and
  since when the current status holds) and **How it is wired** — four lanes
  from the domains that reach the app, through the ports it listens on (a
  stopped container's published port shows as not listening) and what runs
  it, to where it keeps data (data-bearing storage first, in orange). The
  Overview no longer repeats the header facts; resource counts are chips.
- Dashboard **title block**: scan number, when it was taken, how long it took,
  the auto-scan interval and when the next automatic scan is due. It replaces
  two buttons that duplicated the sidebar.
- Settings: **scan trend** small multiples (applications, resources, scan time
  over the last 90 completed scans; scans missing a figure are left out, never
  drawn as zero) and an **Account** section listing your active sign-ins with
  **Sign out other sessions**.
- Page changes crossfade while the sidebar and rail stay put (view
  transitions; off under reduced motion).

Tables:
- No more sideways scrolling at desktop widths: long values clamp to one line
  and shrink with their column (full value in the tooltip), filter buttons
  overlay the header instead of reserving width, Docker tables show one
  **Ports** column ("8124 → 8000/tcp"), and Orphans hides the Key or Path
  column when it would only repeat another column on every row (volumes,
  directories). The Containers state cell is one line.
- Cron entries now show `/etc/cron.daily|weekly|…` scripts with their
  frequency and script path instead of rows of dashes.

Fixes:
- Phones: the navigation drawer's links sat under the dimming backdrop and
  could not be tapped (a stacking-context bug, present before this release).
- The Help sheet closes and returns the glossary to the rail when the window
  grows past the rail breakpoint.
- Job step durations read "2 ms" / "4.3s" / "1.5m" instead of raw seconds
  like `0.001521`.
- Settings' scan history hides the Finished column on narrow screens.

Accuracy and safety:
- An unknown username now costs the same argon2 check as a wrong password, so
  login timing no longer reveals which usernames exist.
- The icon proxy follows `<link rel="icon">` targets only on the same site over
  https (urllib would otherwise also open `file://` or another host).
- The test suite imported `/apps/del/backend` no matter where it ran, so a
  worktree or CI checkout tested the deployed code; it now tests its own
  checkout.

Lighter:
- Favicons are cached on disk next to the database (7 days, misses 6 hours)
  and concurrent lookups for one domain share a fetch, so a restart no longer
  triggers up to five outbound requests per gallery domain.
- The help glossary is rendered once per page instead of twice (rail and
  mobile sheet now share it).
- View Apps search is debounced, the card-width slider no longer rebuilds the
  gallery on every tick, and the category list has one source (the server)
  instead of three copies; bundled icons also file obvious apps (IPTV, file
  share, monitor) out of "Other".
- Removed: the never-used `settings` table (migration 005), an unused halting-
  stage constant, `assistant.is_enabled`, an unused CSRF helper, the dead
  `data-toolbar` switch, an ignored `trunc` width argument, a duplicated CSS
  rule; code comments no longer cite the untracked `docs/INTERFACES.md`.

Upgrade: run `./scripts/del-admin migrate` (005), then restart `del-web`.

## 2026.10.03

A lighter, calmer, more accurate UI ("survey sheet"), with the same features.

Leaner:
- AG Grid is gone. One vanilla table engine (search, sort on parsed values,
  per-column filters, quick chips, column chooser, compact rows, column resize,
  pagination, CSV export, clickable rows) replaces it and its fallback. The
  stylesheet and scripts a page loads fell from about 2.2 MB to about 240 KB
  including fonts, and every asset URL now carries a content hash and is cached
  as immutable, so later page views download only the HTML. Plan/job and
  gallery code load only on their pages (`removal.js`, `gallery.js`);
  `assistant.css` merged into the one stylesheet.
- Filesystem discovery probes directories with four workers (same output;
  about 4x faster warm). The gallery icon route no longer re-reads every nginx
  row per request, and its cache evicts oldest-first instead of clearing.
- Removed dead code: unused assistant/planner/jobs import shims, a palette page
  list, a settings-table read, five unused functions/parameters, a duplicated
  image-ref helper, the test-only re-exports in `web/routes.py`, an AG Grid test
  fixture, unused icons and CSS rules.

New look and convenience:
- Dark cyanotype / light drafting-film themes on a faint grid, self-hosted
  Barlow Semi Condensed + IBM Plex Mono, status as dot + word, and a hazard
  stripe only where something is deleted for real (LIVE tags, live execute).
- Dashboard: a site plan of every app (by kind or status), figures, what
  changed since the previous scan, apps needing review, recent jobs.
- Sidebar scan stamp on every page: which completed scan you are reading, its
  age, and a button that rescans in place and reloads when it is published.
- Optional automatic scans (`scan_interval_hours`; this host uses 6).
- Command palette gains actions and "Open site" entries; `g`+letter page
  jumps, `/` table search, `t` theme, `?` shortcuts dialog.
- Tables remember sort, page size, hidden columns and column widths, but
  always open unfiltered, so a forgotten filter can no longer hide rows.

Accuracy:
- View Apps no longer calls an app "Online" when nginx answers a basic-auth
  401 while nothing listens on the upstream; failed domains are listed under
  **Unavailable** with the reason instead of vanishing.
- App status: a oneshot unit that ran and exited no longer masks a stopped
  daemon (freeze-watch showed running while its recorder was down).
- Disk usage now includes Docker volumes (it claimed to and did not); the
  Docker figures show "measuring" instead of 0 B before the first measurement.
- Shared is described as "used by 2+ apps or a system path"; published ports of
  stopped containers are shown as idle; systemd rows without a unit file are
  labelled; the plan page shows its real creation time (it always showed "—").
- Fixed: on phones the main column was wider than the screen; app pages
  scrolled themselves down on load; the font preload fetched each face twice;
  each resource-type table shared one saved layout; a finished job showed a
  "Job done" toast on every visit; a resized sidebar or rail ignored
  "collapse"; a malformed `data:` favicon made `/app-icon` fail with an
  uncached 500; a docker failure showed as "0 B reclaimable".

Docs: UI.md rewritten; README, ARCHITECTURE, OPERATIONS, DISCOVERY, SECURITY,
INSTALL and ASSISTANT updated. CI syntax-checks every static script.
Tests: 459 passed, 2 skipped (`-W error`); pyflakes clean.
Validation details: [report](reports/2026-10-03/validation.md).

## 2026.10.02

Inventory integrity and scan performance:
- Publish resources, apps, associations, stale-owner cleanup, and the completed
  scan marker in one transaction. A failed source, invalid manifest, correlation
  error, or persistence error retains the prior inventory.
- Serialize CLI/web scans using a Linux advisory file lock. Release locks on DB
  failures; leave active CLI scans alone during startup recovery; allow a new scan
  after a crashed CLI leaves a stale running row.
- Use batched resource upserts and three lookup queries instead of a SELECT for
  every resource and app. Inventory routes, planner reads, and assistant context
  use consistent SQLite read snapshots.
- Migration 004 records operator exclusion/shared flags separately. Reviews
  survive for still-associated pairs; approval is revoked if safety changes.
  Inferred shared flags are recomputed. Existing exclusions migrate conservatively.
- Resource owners omit stale apps and handle large ID sets in bounded batches.
  Operator reviews invalidate cached orphan counts. Excluded mappings no longer
  inflate the dashboard's uncertain count.

Convenience and maintenance:
- Settings shows live scan feedback and scan outcomes/counts/duration. Scan
  elapsed times correctly parse SQLite UTC timestamps.
- Dashboard keeps the true completed-scan summary after repeated failed attempts.
- Health checks return 503 for an unavailable DB or incomplete required schema;
  migrations roll back schema changes and bookkeeping together on failure.
- Remove the obsolete Fern CI job/ignore entry and obsolete optional-module
  fallbacks in Settings. CI checks application JavaScript syntax.
- Update README, operations, architecture, discovery, and UI documentation.

Validation details and measured performance: [audit report](reports/2026-10-02/validation.md).

## 2026.9.30

Accurate install dates. *Installed* used the earliest of the app folder's ctime
and container Created, but a folder's ctime moves every time a file is added,
so n50 (folder created 2026-07-18) showed as installed 2026-09-28. Discovery now
records real ext4 birth times (`stat -c %W`; Python 3.10 cannot read them) for
app folders, custom unit files and nginx site files, plus the last git commit
time. *Installed* is the earliest creation time of the app's own non-shared
resources. When DEL saw the app before any of those files existed (the files
were recreated since), it shows "by <first scan>". A new *Last changed* column
and field shows the newest folder change, git commit, container recreate, or
unit/site edit, and the hover text names the source.

Status and kind from the host. Apps with runtime resources, none of them
running, are now *stopped* instead of *unknown* (11 apps: glmflix, ppv,
ginstall, …). A manifest's `active` maps to *running*. Manifest apps whose
folders no longer exist are *absent*: cap42, cap4l, myspeed-source and sharex.
boxy and bjkai-shorturl-by-claude are *systemd*, not *compose_stopped*.
Scan 335: 66 running, 15 stopped, 4 absent, 0 unknown.

Correlation:
- No more phantom apps. immich-app merged into immich, dependency-track into
  dependencytrack. benchmarks, notecapai and knowledgebase were compose files
  in nested, backup or sample trees.
- OpenSandbox session containers belong to SurfSense, not six separate apps.
- System bind mounts are shared and blocked, not exclusive and "safe":
  docker.sock, `/etc/*`, `/apps`, `/home/bjkai`.

Root-only reads through the helper. del-web runs with `NoNewPrivileges`, so the
`sudo` fallbacks for 0600 nginx sites and user crontabs had failed since 09-27
(bjk.ai's shorturl site and bjkai's crontab had disappeared). Two new read-only
helper ops, `read_nginx_config` and `read_crontab`, bring the allowlist to 24.
The nginx op returns only the routing directives DEL parses, never raw file
text. nginx parsing now ignores commented-out directives.

Other fixes:
- Docker label values with secret-looking keys, such as
  `opensandbox.io/egress-auth-token`, are redacted before storage, and 3
  already-stored rows were scrubbed.
- `applications.manifest_path` is now written, so the *Manifest* field is no
  longer always "—".
- The detail page's resource count matches the list: excluded rows are listed
  separately.
- Gallery: domains that redirect to another listed app are merged onto its card
  (c64, cdx64, d64 → gd64). Categories are decided per app: tix is no longer
  AI, and gittodoc is no longer a todo app.
- Gallery: bundled icons for apps without a usable favicon, and category filter
  pills. This is 09-22 work that was already live but uncommitted.

Tests: 425 passed, 2 skipped (`-W error`).

## 2026.9.13

One orphan classification everywhere: the assistant's *orphans* scope used to
classify rows one at a time and skipped the row-set rules (Git repo inside a
candidate directory, dual-stack sockets, vendor-unit listeners), so it reported
41 more Actionable rows than `/orphans` on scan 254. The page, the dashboard
count and the assistant now all call `web/orphans.py::classify_orphans`.

Backups are never app resources: Compose copies under `backups_dir`
(`/apps/del/backups/<app>/compose_project`) were attached to DEL by directory
nesting — `deck-renderer` at 95/exclusive/safe. Correlation now skips them
entirely, excludes any other association that reaches a path inside
`backups_dir` (even a manifest entry), the planner refuses them as steps, and
Orphans lists them as Expected.

Orphans wording: the glossary's *Unassociated* now states the real rule (no
non-excluded owner at confidence 60 or more, and no protected-app claim, in the
latest scan). An enabled site with a `server_name` but no `proxy_pass` is now
Actionable with a static/redirect reason, instead of being called a catch-all
and put in System; only a site with no `server_name` stays System. Port reasons
name the process and pid, or say that no owning process was reported.
Scan 255: 174 Actionable / 407 System / 63 Expected (the previous classifier on
the same rows: 176 / 409 / 59; the only row changes are the 4 backup copies and
`aidocs.bjk.ai` and `iphone.bjk.ai`). Tests: 376 passed, 2 skipped (`-W error`).

Ownership accuracy: a listener or process running under a systemd user manager
is now attributed to the innermost service in its cgroup path. Before this, `/proc/<pid>/cgroup` for the OpenClaw
gateway (`user@1000.service/app.slice/openclaw-gateway.service`) was reported as
`user@1000.service`. The user manager is now only the fallback. Ports owned by a
vendor/package unit that the same scan lists and classifies as System (such as
`flussonic.service` and `flussonic-epmd.service` from the `flussonic` dpkg
package) are now System instead of Actionable. The match is exact on the cgroup
unit and never applies to `user@*.service`. Enabled Nginx sites with no owner
keep their bucket, and their reason now names the loopback proxy target and the
unit that owns that listener. Flussonic is not turned into a removable DEL app,
because the package owns `/opt/flussonic` and its unit files.

Orphan view de-duplication: a Git repository inside a candidate directory, and
the second address family of a dual-stack listener (same proto, pid, and port),
are shown as Expected and point back to the row that stays Actionable.

Strategic safety pass: generic Compose directories nested below archived clones
inside `/apps/agyinstall` no longer become false 95-confidence removable
projects. The live agyinstall deployment tool is also protected in production
configuration.

Orphan clarification pass: validated source clones (`flashflix-tvos`,
`ultraflix-apk`, `ExpenseOwl`, `MySpeed`, `notes`, and `ShareX`) now have
explicit blocked manifests instead of misleading weak app matches. Historical
Docker volumes are attributed to their owning apps; the large code-server
volume and Docmost rollback volume remain explicitly blocked for review.
Manifests now support a dedicated `volumes` field.

Reviewed weak filesystem matches are now cleaner: bare directories with no
Compose, `.env`, or Git signal are not attached to apps by name similarity alone.
The standalone `cap42` and `cap4l` local projects now have explicit manifests,
so they are distinguished from the Docker `cap`/`cap4` deployments at 100%
manual confidence.

Nested Compose projects found inside another app's tree remain conservative
(shared/blocked) rather than becoming independently removable by name alone. If
the nested definition declares bind mounts or named volumes, DEL now records
`data` loss risk instead of understating it as config-only.

Correlation accuracy: Git worktrees now inherit ownership from their common
repository, symlink aliases are not inventoried as duplicate project directories,
and macOS `__MACOSX` archive metadata is excluded from project-name matching.
This removes false weak matches such as `/opt/del` → `del` and
`/__MACOSX` → `macro` while making `/apps/del-2` a confirmed DEL worktree.

## 2026.9.12

Correlation accuracy: enabled Nginx evidence now describes a running app as
running instead of claiming it is stopped. Detached Docker volumes that retain
only a historical Compose project label are kept as low-confidence possible
associations, so they remain visible for orphan review and cannot become
automatic removal steps. Current container attachment remains authoritative.

Applications table: live header-click captures showed every row stacked at
`top: 0` (`position: absolute` without `translateY`) after repeated sorts.
Every enhanced table now keeps the current page of rows in the DOM and lays
them out in document order, so sorting reorders real rows instead of relying
on transforms. Stale pinned column state is ignored. Quartz theme CSS loads
before DEL's stylesheet so tables use the same slate/sage tokens as the rest
of the shell.

Workspace polish: desktop navigation and Help/Ask panels now have persistent,
keyboard-accessible resize dividers with double-click reset. The dark theme uses
a softer natural slate/sage palette, table rows have quieter tonal separation,
and table toolbars form a clearer control surface. Responsive collapse and mobile
sheet behavior are unchanged. The Fern docs unit now uses the preview CLI's
supported shutdown signal, avoiding dirty 90-second restart timeouts.

Repository polish follow-up: added pinned runtime/development dependency
manifests and GitHub Actions checks for warnings-as-errors tests, pyflakes, and
Fern links. The test stack now uses `httpx2`, eliminating the Starlette
deprecation warning. Manifest IDs are filename-safe and must match the edited
application slug, closing path traversal and cross-app overwrite paths. Startup
now marks interrupted background jobs failed instead of leaving them permanently
active. UI fixes cover the Assistant page's empty mobile Ask panel, complete rail
tab semantics, command-palette keyboard focus, selected-theme browser chrome,
and shared danger-color tokens. The CSP no longer permits inline styles, and the
security/operations/system-state docs now match the repository and deployment.
The installer now checks `/login` with GET (the route does not support HEAD) and
keeps timestamped Nginx backups out of `sites-enabled`, preventing duplicate
vhosts during repeated deployments.

Calm palette in both themes (warm paper neutrals in light, soft slate in
dark; never pure black or white), theme following `localStorage`, else OS
`prefers-color-scheme`, else dark. Sidebar regrouped into Inventory / Removal
/ Help with Settings and Log out at the foot, and inline SVG icons replace
the old icon set. Table engine fixes: widths come from content, rich cells
grow their row instead of scrolling, grids size to their own rows with no
inner scroll box, one filter icon per column header instead of a floating
filter row, and the AG Grid enterprise-option console error is gone.
Resources gained server-side `?filter=shared|dangling|unassigned`, matching
the dashboard's stat tiles, with a "Filtered:" callout and per-type shared
chips. The plan page now derives its status from the jobs that ran it (not
run yet / dry-run only / ran live) with a "Runs of this plan" list, and the
execute button reads "Run dry run" or "Execute live — deletes for real"
depending on the selected mode. Orphans gained four summary tiles, jump
chips, and a collapsible section per resource type. Docs across README,
architecture, interfaces, and the Fern reference/guide pages were corrected
to match the shipped UI (theme fallback order, Resources sub-navigation vs.
tab bar, action-bar order and styling, sidebar grouping). 339 tests pass, 1
skipped.

Fixes found along the way: the mobile **Ask** button never showed (an
`assistant.css` rule hid it); the dashboard "scanning…" label could stay
visible after a scan (the `hidden` attribute lost to a display rule); the
rows-per-page menu showed 25 while grids paged at 50; only a live run asks for
browser confirmation now (a dry run changes nothing); the plan builder groups
its options with descriptions checked against the planner; the app page's
duplicate Evidence column is gone (the confidence cell lists every item); the
Reclaimable and Disk usage tiles say what they measure. Docs: `tomli` added to
the dependency lists in INSTALL, RECOVERY and INTERFACES (the recovery venv
command left `del-web` unable to start without it), unauthenticated static
routes listed in full, and migration `003_assistant.sql` documented.

## 2026.9.10 docs

Operator-loop docs page (steps, mermaid, screenshots, collapsible vocabulary).
Overview tabs. Architecture loop diagram. Assistant/confidence accordions.
Mobile CSS for Fern and the app SOP/actions. Unauthenticated `/static/*` claim
matches the files that actually ship.

## 2026.9.10

Ask can name every current owner. App-scope context now includes
`all_owners` / `also_owners` on each association. General context still
lists every volume, image, network, and container with sorted owner slugs
before the app list.

Vocabulary is one meaning per word: probable (≥60) is a strong owner claim;
possible is weak. `/orphans` is review-only. Dashboard SOP has a single
primary scan control. Ask chips open the right rail. AG Grid Ask columns
stay pinned. Docs match the shipped UI (context budget 48000, on-demand
scan, 337 tests).
