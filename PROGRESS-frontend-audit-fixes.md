# PROGRESS — frontend audit fixes (P0/P1/P2)

Goal: apply the 38 numbered audit findings + P2 dead-code removals to
`backend/del_app/web/{templates/*.html,static/app.css,static/app.js}`.
No Python touched (routes.py was being edited concurrently by the user).

## Status: complete

- P0 1–7: done.
- P1 a11y 8–21: done, two deliberate deviations (13, 16) — see Decisions.
- P1 layout 22–38: done except 31 (blocked on a Python-side parser change).
- P2 dead code: all removed after re-grepping each one.

## Evidence
- `node --check app.js` → OK.
- Jinja parse of all 15 templates → OK.
- `/tmp/del-ui/render.py` → all 15 pages HTTP 200; every page larger than a
  same-moment baseline rendered from the pre-change templates (`/tmp/render_old.py`,
  copies at `/tmp/old-tpl`); word-level diff shows zero lost content (only
  timestamps/scan ids drift, because a scanner runs continuously).
- `pytest tests/` → 170 passed, 1 skipped.
- `node /tmp/check-sort.js` → size/duration/date sort orders assert green.
- `node /tmp/smoke-app.js` → app.js executes top-level against a DOM stub with
  no ReferenceError.

## Decisions
- `--danger-bg` was already taken (dark tint for badges/boxes/rows), so the new
  solid button fill is `--danger-solid: #cf2f34`; `--accent-bg: #2f6fe0` as asked.
  Measured white-on-fill contrast: 4.70:1 and 5.11:1.
- Item 13: `aria-sort` stays on the `<th>` (columnheader) and the click target is
  a real nested `<button class="th-sort-btn">`. `role="button"` on the `<th>`
  itself would have removed the columnheader role that makes `aria-sort` valid.
- Item 16: `role="status"` went directly on `#gallery-visible-count`; its only
  wrapper is the `<h1>`, and a live region there would cost the heading role.
- Item 31 (tmux Created) is not implementable from the template: tmux emits a
  ctime string (`Fri Jun  5 01:34:01 2026`) that `_parse_dt` cannot read, so
  `format_dt` returns `—`. Value now renders in the shared `.dt` style only.
- Item 6 needed more than the stated fix: `parseSize` cannot read `"42s"` either,
  so a `parseDuration` step was added to the shared `sortValue()`; both paths
  now sort Duration numerically instead of matching each other while both wrong.
