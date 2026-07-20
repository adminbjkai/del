# Fern docs site audit — 2026-07-20

Scope: `/apps/del/fern/**` only. No code/config/other-docs changes made.

## 1. Nav tree (docs.yml)

```
Getting Started
  Overview                     -> pages/overview.mdx                          (slug: overview)
  Logging In                   -> pages/guides/logging-in.mdx                 (slug: guides/logging-in)
Using DEL
  Scanning Your Server         -> pages/guides/scanning.mdx                   (slug: guides/scanning)
  Browsing the Inventory       -> pages/guides/browsing.mdx                   (slug: guides/browsing)
  Understanding Confidence     -> pages/guides/confidence.mdx                 (slug: guides/confidence)
  Removing an Application      -> pages/guides/removing-an-application.mdx    (slug: guides/removing-an-application)
  Orphan Review                -> pages/guides/orphans.mdx                    (slug: guides/orphans)
  Backups & Restore            -> pages/guides/backups.mdx                    (slug: guides/backups)
Administration
  Installation                 -> pages/installation.mdx                      (slug: installation)
  Operations                   -> pages/guides/operations.mdx                 (slug: guides/operations)
  Recovery                     -> pages/guides/recovery.mdx                   (slug: guides/recovery)
  Uninstall DEL                -> pages/reference/uninstall.mdx               (slug: reference/uninstall)
Reference
  Architecture                 -> pages/reference/architecture.mdx            (slug: reference/architecture)
  Security Model               -> pages/reference/security.mdx                (slug: reference/security)
  Helper Operations            -> pages/reference/helper-operations.mdx       (slug: reference/helper-operations)
  Deployment Convention        -> pages/reference/deployment-convention.mdx   (slug: reference/deployment-convention)
  System State                 -> pages/reference/system-state.mdx            (slug: reference/system-state)
```

17 nav entries, 17 `.mdx` files under `fern/pages/`, 1:1 match — every page file is in the nav, every nav entry points to a real, existing file. No orphan pages, no dangling nav entries.

## 2. Page-render matrix (curl, `admin:<password>` basic auth, del-docs.service was already active — up 10h, no restart needed since no content changes were required)

| Slug | HTTP code |
|---|---|
| overview | 200 |
| guides/logging-in | 200 |
| guides/scanning | 200 |
| guides/browsing | 200 |
| guides/confidence | 200 |
| guides/removing-an-application | 200 |
| guides/orphans | 200 |
| guides/backups | 200 |
| installation | 200 |
| guides/operations | 200 |
| guides/recovery | 200 |
| reference/uninstall | 200 |
| reference/architecture | 200 |
| reference/security | 200 |
| reference/helper-operations | 200 |
| reference/deployment-convention | 200 |
| reference/system-state | 200 |

**17/17 pages return 200 with auth.**

No-auth check: `curl https://del.bjk.ai/docs/overview` (no credentials) → **401**, confirmed basic-auth gate is active as expected.

`del-docs.service`: confirmed `active (running)`, up ~10h, listening on 8072/8073 as designed, proxied by nginx `/docs`, `/_next/`, `/_local`.

## 3. Image-resolution matrix

All `<img src="...">` references across `fern/pages/**/*.mdx` are relative paths into `fern/assets/`. The Fern dev server rewrites these to an absolute `/_local/apps/del/fern/assets/<file>` URL served **at nginx root** (not under `/docs`), confirmed by inspecting the rendered HTML of `overview` (`src="/_local/apps/del/fern/assets/02-dashboard.png"`).

| Image | Referenced in | URL tested | Code |
|---|---|---|---|
| 01-login.png | guides/logging-in.mdx | /_local/apps/del/fern/assets/01-login.png | 200 |
| 02-dashboard.png | overview.mdx | /_local/apps/del/fern/assets/02-dashboard.png | 200 |
| 03-apps-list.png | guides/browsing.mdx | /_local/apps/del/fern/assets/03-apps-list.png | 200 |
| 04-app-detail.png | guides/browsing.mdx, guides/removing-an-application.mdx | /_local/apps/del/fern/assets/04-app-detail.png | 200 |
| 05-volume-approval.png | guides/confidence.mdx, guides/removing-an-application.mdx | /_local/apps/del/fern/assets/05-volume-approval.png | 200 |
| 06-plan-form.png | guides/removing-an-application.mdx | /_local/apps/del/fern/assets/06-plan-form.png | 200 |
| 07-plan-preview.png | guides/removing-an-application.mdx | /_local/apps/del/fern/assets/07-plan-preview.png | 200 |
| 08-dryrun-job.png | guides/removing-an-application.mdx | /_local/apps/del/fern/assets/08-dryrun-job.png | 200 |
| 09-live-execute-gate.png | guides/removing-an-application.mdx | /_local/apps/del/fern/assets/09-live-execute-gate.png | 200 |
| 10-live-job.png | guides/removing-an-application.mdx | /_local/apps/del/fern/assets/10-live-job.png | 200 |
| 11-resources-volume.png | guides/browsing.mdx | /_local/apps/del/fern/assets/11-resources-volume.png | 200 |
| 12-orphans.png | guides/orphans.mdx | /_local/apps/del/fern/assets/12-orphans.png | 200 |
| 13-jobs-list.png | guides/browsing.mdx | /_local/apps/del/fern/assets/13-jobs-list.png | 200 |
| infographic-backup.png | guides/backups.mdx | /_local/apps/del/fern/assets/infographic-backup.png | 200 |
| infographic-confidence.png | guides/confidence.mdx | /_local/apps/del/fern/assets/infographic-confidence.png | 200 |
| infographic-flow.png | overview.mdx | /_local/apps/del/fern/assets/infographic-flow.png | 200 |

**16/16 images resolve 200.** No broken image references found; no fixes needed.

## 4. Content accuracy spot-checks

- **Helper operations (22)**: `fern/pages/reference/helper-operations.mdx` has exactly 22 `<Accordion>` entries in the allowlist. Cross-checked against `helper/del_helper.py`'s `ALLOWED_OPS` set — also exactly 22 entries (`ping`, `compose_down`, `container_stop`, `container_rm`, `image_rm`, `volume_rm`, `network_rm`, `systemd_stop`, `systemd_disable`, `nginx_test`, `list_listeners`, `systemd_rm_unit`, `cron_rm`, `nginx_rm_site`, `nginx_test_reload`, `path_delete`, `tmux_kill`, `process_term`, `backup_tar`, `volume_backup`, `file_backup`, `path_restore`). Names, order-independent, match 1:1. **No drift.**
- **compose_down robustness**: doc text explicitly covers lowercasing project names, label-based fallback teardown when the compose file is missing/unparseable, and sweeping stragglers — matches current behavior. Already accurate, no fix needed.
- **boxy status-accuracy fix**: referenced in `reference/system-state.mdx` ("`boxy` status-accuracy fix — corrected DEL's stale `compose_stopped` record"). Present and accurate.
- **Non-docker apps**: `reference/deployment-convention.mdx` includes an explicit "Non-Docker: systemd unit at `/etc/systemd/system/<name>.service`" checklist item, reflecting the non-compose deployment path.
- **Removal flow / discovery rules / system-state**: `guides/removing-an-application.mdx`, `reference/architecture.mdx`, and `reference/system-state.mdx` are consistent with current app behavior (172 traced ports, 0 unexplained, 0 failed systemd units) as recorded in the source-of-truth `docs/SYSTEM-STATE.md`.

No content drift found; no edits were necessary.

## 5. MDX health

- Components used across all pages: `Accordion`, `AccordionGroup`, `Callout`, `Card`, `CardGroup`, `Frame`, `Steps`, `Tabs` — all within the approved renderable set. (`AccordionGroup`/`CardGroup` are the standard Fern wrapper components paired with `Accordion`/`Card`.)
- All pages compiled and rendered 200 (a broken/unknown component would 500), confirming no broken components anywhere.
- Internal markdown links (`](/slug)`) audited across all pages: every target resolves to one of the 17 valid slugs in `docs.yml`. No broken internal links found.

## Fixes made

**None.** The site passed all checks as-is: nav/page/slug mapping correct, all 17 pages 200, all 16 images 200, helper-operations count (22) verified against source, no broken links, no broken components. No edits to `fern/**` were required.

## del-docs active confirmation

`systemctl status del-docs` → `active (running)`, PID 1719580, up ~10h at time of audit, listening on 8072 (frontend proxy target for `/docs`, `/_next/`) and 8073 (backend, proxy target for `/_local`).

## VERDICT: PASS

The Fern docs site is fully healthy: 17/17 nav pages render 200 with auth (401 without), 16/16 images resolve, helper-operations content matches the live `ALLOWED_OPS` set exactly (22/22), no broken internal links, no broken MDX components. No fixes were needed.
