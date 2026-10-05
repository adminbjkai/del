# DEL web UI — conventions

How the server-rendered UI is put together: the visual language, the shell,
the component classes templates compose from, the table engine's `data-*`
contract, keyboard shortcuts and what the browser remembers.

Files (all in `backend/del_app/web/static/`, served from `/static/` under CSP
`script-src 'self'; style-src 'self'` — no inline scripts, `on*=` handlers or
`style=""` in markup; JS sets sizes through the CSSOM, which CSP allows):

| File | Role |
|---|---|
| `app.css` | The only stylesheet (sections 1–7 listed at its top), including the assistant and gallery. |
| `theme-init.js` | Blocking `<head>` script: sets `data-theme` / `data-density` before first paint. |
| `app.js` | Shell, tables, palette, shortcuts, scan stamp, toasts, small behaviours. Exposes `window.DEL`. |
| `assistant.js` | Ask dock and `/assistant` page. The dock fetches prompts and targets when Ask is opened, not on every page. |
| `removal.js` | Plan and job pages only: plan presets, the execute gate, the live job poller. |
| `gallery.js` | View Apps only: search, categories, favourites, layout, drag order. |
| `fonts/` | Barlow Semi Condensed 400/500/600 and IBM Plex Mono 400 (woff2 subsets, OFL licences alongside). |
| `favicon.svg` | Survey-marker icon. |

Templates link every file through `asset('name')`, which adds a content hash
(`/static/app.css?v=1a2b3c4d5e`); a request carrying the current hash is cached
for a year as immutable, so a deploy changes the URL and a page view costs no
asset round trips. Fonts keep plain URLs (the stylesheet names them) and are
always immutable — ship a changed font under a new file name. See
`static_routes.py`.

## Look and feel: the survey sheet

DEL surveys one host and plans demolitions on it, so the UI borrows from
survey sheets and site plans rather than a generic admin kit.

- **Two papers.** Dark (default) is a cyanotype: Prussian-blue paper
  `--paper #0f1f30`, chalk-blue accents `--chalk #8fd0ff`. Light is drafting
  film: cool vellum `#edf1f4`, Prussian ink `#0e2235`, accent `#1658a0`. Both
  carry a faint 24px measuring grid on the page background.
- **Type.** Barlow Semi Condensed (a signage grotesque) for every word;
  IBM Plex Mono only for literal machine values — paths, ids, ports, keys.
  Numbers use tabular figures.
- **Colour is state, never decoration.** Tone pairs `--ok`, `--warn`, `--bad`,
  `--info`, `--idle` (each with a `-bg`) back badges, dots and callouts; text
  and every pair are ≥ 4.5:1 on their surfaces in both themes.
- **Status is a dot plus a word** (`.badge.status-running` renders a green dot
  and "running"), so it reads without colour.
- **One loud element: the hazard stripe.** Yellow/black tape appears only where
  something is about to be deleted for real — the `LIVE` tag, the execute panel
  in live mode, the complete-removal box. Dry runs stay calm.
- **Motion** answers actions (sheets, popovers, the scan sweep). Moving between
  pages is a cross-document view transition (`@view-transition { navigation: auto }`):
  the content fades and settles while the sidebar and right rail stay put
  (they carry their own `view-transition-name`); browsers without support
  just navigate. `prefers-reduced-motion` turns all of it off.
- **Themes.** `data-theme` on `<html>`, set before paint from
  `localStorage["del.theme"]`, else `prefers-color-scheme`, else dark; a
  no-JS `@media (prefers-color-scheme: light)` block covers the rest. Toggle:
  the half-moon button in the sidebar, `t`, or the command palette.
- **Icons.** Inline SVG from `templates/_icons.html`
  (`{% import "_icons.html" as i %}` then `{{ i.icon('scan') }}`), stroke
  `currentColor`; `app.js` keeps a small matching set for JS-built controls.

## Shell (`base.html`)

- **Sidebar**: brand (marker, `DEL`, host name), theme and collapse buttons,
  the command-palette button, grouped nav (Inventory: Dashboard, View Apps,
  Applications, Resources, Orphans · Removal: Jobs · Help: Assistant), then the
  **scan stamp**, Settings and Log out. Each nav link's tooltip names its
  `g` shortcut. Collapses to icons on desktop; below 900px it is a drawer
  behind the menu button with a backdrop.
- **Scan stamp** (`#scan-block`): the inventory's completed scan number, its
  age and duration, and a run button. It is marked stale (a warn dot and the
  word "stale") when that scan is older than `scan_interval_hours`, or older
  than a day when automatic scans are off. Starting a scan here (or from Settings)
  posts `/scan` with `Accept: application/json`, shows a toast, and the stamp
  polls every 2.5 s while discovery runs (sweep bar + elapsed time), then
  reloads the page when the new inventory is published.
- **Main**: `.content > .content-inner` (max 1440px).
- **Right rail** (≥ 1280px): **Help** (glossary filtered to the page via
  `<body data-glossary>`, computed by `glossary_ctx()` in `render.py`) and
  **Ask** (assistant dock). It starts collapsed on every page; "Ask" links
  anywhere open the Ask tab with a drafted question. Below 1280px Help and Ask
  are floating buttons that open bottom sheets (Esc or the close button
  dismisses them). The glossary exists once per page: opening the Help sheet
  moves it from the rail into the sheet and closing puts it back.
- **Resize handles** beside the sidebar and rail on desktop: drag, or focus
  and use Left/Right (Shift for bigger steps), double-click to reset. Widths
  persist; handles hide when their panel is collapsed and on mobile.
- **Toasts** (`#toast-region`, `DEL.toast(msg, "ok"|"error"|"info")`) and
  **flash banners** from `?flash=` / `?error=` (success banners auto-dismiss;
  both query parameters are stripped from the URL once shown).

## Page anatomy

```html
<nav class="crumbs" aria-label="Breadcrumb">…</nav>          <!-- detail pages -->
<div class="page-header">
  <h1>Title <span class="count-pill">171</span></h1>
  <div class="page-actions">…links / buttons…</div>
</div>
<div class="meta-row"><span class="meta"><span class="meta-k">Kind</span> compose</span></div>
<p class="page-lede">One or two sentences: what this page is for.</p>
<!-- the dashboard puts a dl.title-block (scan, taken, took, auto-scan,
     next scan) where other pages have .page-actions -->
<details class="explain"><summary>How rows relate</summary><div class="explain-body">…</div></details>
…panels / sections / tables…
```

## Components

| Class | Use |
|---|---|
| `.panel` (+ `.panel-head`) | Default content container. |
| `details.section` > `summary` (`h2`, `.count-pill`, `.section-hint`) > `.section-body` | Collapsible block. Add `id` + `data-remember` to persist open state per page; a `#id` link opens it. |
| `[data-expand-all="open\|close"]` (+ `data-scope`) | Opens/closes every `details.section` in scope. |
| `details.explain` > `.explain-body` | Inline "what does this mean" disclosure. |
| `.callout` + `-info` / `-warn` / `-danger` (+ `.callout-title`) | Notices. `.flash-ok` / `.flash-error` for banners. |
| `.meta-row` / `.meta` / `.meta-k` | Compact facts under a title. |
| `.kv` (`<dl>`) | Key/value grid. |
| `dl.title-block` > `div` > `dt` + `dd` | Dashboard drawing title block: ruled cells, small-caps labels, mono values. |
| `.status-strip` > `li.tick.tick-<status>` | App Overview "Status by scan": one bar per recorded scan, newest outlined. |
| `.wiring` > `.wiring-lane.wiring-<key>` > `.wiring-item` (`.is-data`) | App Overview "How it is wired": four lanes (reached at, listens on, runs as, keeps data in) joined by chalk connectors; built by `apps._wiring`. |
| `.trend-grid` > `figure.trend` > `svg.trend-chart` | Settings scan trend small multiples (inline SVG: `.trend-line`, `.trend-bar`, hover `.trend-hit` with a `<title>` per scan). |
| `.siteplan` > `a.lot` (`.is-running` etc., `.has-warning`, `.is-protected`) | Dashboard site plan tiles. `#siteplan-filter` narrows them by `data-find` (name, slug, kind, status, enabled domains). Enter opens the first match in the visible grouping. |
| `.figures` | Dashboard figure strip; `.stat-grid` > `a.stat-card` on Orphans. |
| `.subnav`, `.jump-nav` | Sibling views (resource types); in-page anchor chips. |
| `.tabs` > `[role=tablist]` > `[role=tab]` + `[role=tabpanel]` | ARIA tabs: roving tabindex, arrows/Home/End, `#tab-id` deep links. |
| `.relation-list` | "this → that" relationship rows. |
| `.btn` + `-primary` / `-danger` / `-danger-quiet` / `-ghost` / `-sm`, `.icon-btn`, `.btn-group` | Buttons. One primary per view; `-danger` only for real deletion. |
| `.badge` + `badge-ok/-amber/-grey` / `status-*` / `badge-confidence-*` / `badge-danger-*`; `.tag-live` | Labels. `m.state(value)` and `m.mode_tag(mode)` in `_macros.html` render the common ones. |
| `.dot` + `-ok` / `-warn` / `-info` / `-idle` | Small state dot. |
| `.choice` (`.choice-title`, `.choice-desc`) | Radio/checkbox cards (dry run vs live). |
| `.execute-panel` (`.is-live-mode`), `.danger-zone` | Removal controls; hazard stripe in live mode. |
| `.chip`, `.filter-chip`, `.count-pill` | Link chips, filter toggles, counts. |

Utilities: `.stack`, `.cluster`, `.muted`, `.faint`, `.mono`, `.text-xs|sm`,
`.mt-0|2|3`, `.mb-0|1|2`, `.sr-only`.

## Tables

Every `table.table` is enhanced by the vanilla engine in `app.js`
(`enhanceTable`); add `table-plain` (or `job-steps`) to keep plain HTML. It
provides a search box, sortable headers, a per-column filter popover
(contains / equals / starts with / greater than / is empty …), quick filter
chips for status-like columns (Status, Kind, State, Class, Active, Type …), a
Columns menu (show/hide columns, compact rows, reset), pagination
(25/50/100/250/All), drag-to-resize columns, CSV export of the filtered rows,
and clickable rows (a click anywhere opens the row's first link;
Ctrl/Cmd-click opens a new tab). Column filter buttons overlay the end of the
header on hover instead of reserving width. Long machine values go through
`m.trunc()` (`.trunc`): one line with an ellipsis and the full value in the
tooltip, able to shrink to 6rem with its column, so tables fit their container;
only a table that still cannot fit scrolls horizontally. Published ports use
`m.ports_cell()` ("host → container", one per line). Tables with ≤ 10 rows get
a minimal toolbar unless they have an export button.

| Attribute | Effect |
|---|---|
| `table[id]` | Storage key for the table's remembered state (see below); give every inventory table a stable id. |
| `data-page-size="25"` | Initial rows per page (default 50). |
| `data-empty="…"` | Message when nothing matches. |
| `data-search-placeholder="…"` | Search box placeholder. |
| `data-prefill="…"` | Initial search text. |
| `th[data-nosort]` | Not sortable or filterable (action columns such as **Ask**). |
| `th[data-priority="low"]` / `td[data-priority="low"]` | Hidden on narrow screens until "Show all columns". |
| `td[data-sort-value]` | Sort key. Dates use `iso_sort()`; numbers, sizes (`1.5 GB`) and durations (`42s`, `1.5m`) are parsed. Columns that are ≥ 85% numeric right-align. |
| `td[data-filter-value]` | Text used for search, filters, chips and CSV instead of the cell text. |
| `[data-export-table="<table id>"]` | Page-level export button; the engine moves it into the table toolbar as **CSV**. |

`—` is treated as blank everywhere.

## Keyboard

| Key | Action |
|---|---|
| Ctrl/Cmd + K | Command palette: pages, applications, "Open site" domains, actions (run scan, theme, help, ask, export, collapse sidebar). With an empty query, apps opened in this browser (`del.recentApps`) are listed first. |
| `g` then `d` `v` `a` `r` `o` `j` `k` `s` | Dashboard, View Apps, Applications, Resources, Orphans, Jobs, Assistant, Settings. |
| `/` | On the dashboard, focus the site-plan filter. Otherwise the gallery search, then the first visible table search. |
| `t` | Toggle theme. |
| `?` | Shortcuts dialog. |
| Esc | Close the open popover, dialog, sheet or Ask panel. |

Shortcuts are ignored while typing in a field.

## What the browser remembers (localStorage)

| Key | What |
|---|---|
| `del.theme`, `del-density` | Theme; compact or comfortable rows. |
| `del.sidebarCollapsed`, `del.sidebarWidth`, `del.rightRailWidth` | Shell layout. |
| `del.table.<table id>` (or `del.table.<path>\|<caption>`) | Sort, page size, hidden columns, column widths. Search text and filters are not kept. |
| `del.sections.<path>` | Open/closed `details.section[data-remember]`. |
| `del.siteplanGroup` | Dashboard site plan grouping (kind or status). The filter text is not kept. |
| `del.recentApps` | Up to eight application slugs opened in this browser, newest first, for the command palette. |
| `del.appGallery.v1` | View Apps favourites, hidden apps, categories, order, view, density, card width, sort. |

Keys from older layouts (`del.agN.colstate.*`, `del.glossaryCollapsed`,
`del.rightRailTab`, and the retired `sessionStorage` key `del.siteplanSeen`)
are deleted on load.

## Accessibility

Skip link, landmarks, `aria-current="page"` on the active nav item, visible
focus rings, keyboard-operable resize separators, ARIA tabs, focus traps in
the drawer, sheets and dialogs, live regions for toasts, the scan stamp and
the gallery count, `prefers-reduced-motion` honoured, text ≥ 4.5:1 in both
themes. Print hides the shell and prints tables in black on white.
