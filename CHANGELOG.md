# Changelog

## 2026.9.12

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
