# Changelog

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
