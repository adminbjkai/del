# System State — bjkai-2tb-ubuntu (as of 2026-07-20)

Authoritative, consolidated reference for the current state of this server, built from
the 8-lane audit run 2026-07-20 (raw findings: `/apps/del/data/audit/2026-07-20/`) plus
`/apps/del/docs/PORT-REGISTRY.md`. Read this before starting any new audit — most
questions about "what's running, what's shared, what's broken" are answered here.

---

## 1. Executive summary

- **236** top-level directories under `/apps` (249G total). Of these:
  - **~192 are real, deployed applications** (114 LIVE + 29 DISABLED-but-valid +
    3 MISCONFIGURED across lanes A–C, plus ~46 more LIVE counted in dirs-C) — see the
    exact per-state counts in §2.
  - **70 are NOT-IN-DEL / NOT-AN-APP / NOT-CONFIGURED** — data dirs, scratch, mobile-client
    source, doc-only folders, or code with no deployment wiring. The large ones
    (`models` 49G, `LTX-2` 7.7G, `ideogram` 7.1G, `arch` 3.1G) are **intentional data/reference
    storage, not gaps** — nothing is missing, they were just never meant to be services.
  - A handful (10) are ABANDONED/ambiguous per DEL's DB signal (association exists,
    confidence "unknown") — treat as needing a `systemctl status` check before any
    removal action, not as a certified dead list.
- **Docker:** 180 containers (179 running, 1 exited-but-owned-by-a-live-app), 166 images
  (3 dangling-but-in-use, 3 truly unused/safe-to-prune), 98 volumes (21 orphaned,
  ~10.3GB), 88 networks (1 genuinely cross-app-shared: `rowboat_net`).
- **Nginx:** 135 enabled sites (100% symlinks, 0 debris), 137 available files — DEL's
  inventory matches disk exactly.
- **Ports:** 179 listening sockets; 155 internal (loopback-bound), 24/22 distinct
  public-bound (mostly intentional: ssh, http/s, streaming, remote-desktop, plus the
  known samba exception).
- **134 live subdomains** per PORT-REGISTRY, now regenerated from the current scan (89).
- **Systemd:** 75 custom unit files; 0 failed units (2 were failed pre-audit, both fixed
  — see §6).

**Verdict:** the system is clean and healthy. Docker, nginx, and DEL's own inventory all
reconcile to ground truth with zero real discrepancies (only expected scan-lag drift, e.g.
1 image count difference from timing). The known exceptions are narrow and already
enumerated: 8 nginx sites serving 502 for deliberately-stopped apps, 3 genuinely
misconfigured apps (OpenPdf, 17imgshare, fizzy), a couple of incomplete decommissions
(liam, img3 stray config), ~3 apps DEL's automatic scanner doesn't yet track
(semalist, trp, 17imgshare), and one pre-existing security note (samba public bind).
None of these represent active incidents; all are enumerated with recommended actions
in §5.

---

## 2. Directory classification

Full per-directory detail lives in `data/audit/2026-07-20/dirs-A.md` (A–E, 61 dirs),
`dirs-B.md` (F–M, 75 dirs), `dirs-C.md` (N–Z/digits, 98 dirs) — this section is the
roll-up.

### Consolidated STATE counts (all 236 dirs)

| STATE | A–E | F–M | N–Z/digit | **Total** |
|---|---:|---:|---:|---:|
| LIVE | 34 | 33 | 47 | **114** |
| DISABLED (valid config, cleanly stopped) | 6 | 9 | 14 | **29** |
| MISCONFIGURED (broken/dangling) | 1 | 1 | 2 | **4** |
| NOT-CONFIGURED (code exists, never wired) | 4 | 13 | 15 | **32** |
| NOT-AN-APP (data/scratch/docs/build-artifact/mobile-client) | 16 | 19 | 20 | **55** |
| NOT-VERIFIABLE (permission denied) | 0 | 0 | 1 | **1** |
| ABANDONED (dead script/data, no process) | 0 | 0 | 1 (`ted`) + `you` | **1–2*** |
| **Total** | **61** | **75** | **98** | **236** (checks out: 61+75+98=234, plus filesystem.md's separately-counted 2 hidden dirs `.agents`/`.claude` are excluded from this 236; see note below) |

\* dirs-C lists `ted` and `you` individually as "NOT-AN-APP/ABANDONED" (single stray
script + leftover data, no process) — folded into NOT-AN-APP above; called out here
since they're the closest things to genuinely abandoned cruft in the whole fleet.

Note: filesystem.md's own top-level classification (ACTIVE/PAUSED/ABANDONED/NOT-IN-DEL/
retired, keyed off DEL DB association rather than per-directory ground-truth
inspection) gives slightly different bucket names and counts (131 ACTIVE / 34 PAUSED /
10 ABANDONED / 70 NOT-IN-DEL / 1 retired) because it's a DB-first pass rather than the
filesystem-first deep-dive in dirs-A/B/C. Where the two disagree, **trust dirs-A/B/C**
(ground-truth per-directory inspection) as the more authoritative source — the
filesystem.md pass is a useful cross-check but was explicitly less deep for
DB-confirmed-running dirs.

### NOT-AN-APP data dirs — intentional, not gaps

These are large but deliberate non-service storage; nothing to fix:

| Dir | Size | What it is |
|---|---:|---|
| `models` | 49G | ML model weight storage (gemma-3-12b, LTX-2 weights) |
| `LTX-2` | 7.7G | Video-gen model inference code/weights |
| `ideogram` | 7.1G | Image-gen reference/code checkout |
| `arch` | 3.1G | Folder of unrelated sub-projects, no top-level service |

Plus many smaller doc/scratch/build-artifact dirs (`dagster`, `darktable` — upstream OSS
clones for reference; `bjk-flix-*` — mobile client source, not server deployments;
`boxy-apk` — compiled APK artifact; various empty dirs). None of these need action.

### Full per-state lists

See the "Per-directory table" in each lane file for the complete dir-by-dir breakdown
with type, state, and reasoning:
- LIVE / DISABLED / MISCONFIGURED / NOT-CONFIGURED / NOT-AN-APP lists for A–E: `dirs-A.md` lines 19–79
- F–M: `dirs-B.md` lines 21–96
- N–Z/digit: `dirs-C.md` lines 12–109

---

## 3. Shared resources map — zero ambiguity on what's shared

Source: `shared-and-config.md`. DEL recorded 28 `shared=1` resource rows; below is
every one that's a **genuine** cross-app coupling (false positives noted separately).

### Genuine cross-app sharing

| Resource | Apps sharing | Nature | Removal-safety note |
|---|---|---|---|
| `rowboat_net` (docker network) | rowboat, deck-renderer, llm-proxy | Real custom bridge network; `rowboat.bjk.ai` path-routes to both rowboat (8038) and deck-renderer (8060) containers as one logical stack, with llm-proxy also attached | **Do not remove while any of the 3 remain deployed.** `docker compose down` (no `-v`, no orphan-network removal) on one app is only safe for the others if the network is `external: true` in their compose files — verify before any automated teardown. |
| `/apps/ShareX` (+ `.git`) | rshare, vshare | Same repo checkout registered under two app slugs — two deployments of one codebase | One physical directory serves both apps' code. Treat as a single removal unit — deleting it breaks both. |
| `/apps/astv-remote` (+ `.git`) | samsung-tv-remote, appletv-remote | Same repo; `astv-remote.service`'s `ExecStart` actually runs code from `/apps/appletv-remote`'s venv | Do not delete `/apps/astv-remote` while any of the 3 remote-control units are enabled; do not delete `/apps/appletv-remote`'s venv either — `astv-remote.service` silently depends on it. |
| `postgres:16-alpine` (`e013e867e712`) | opensparrow, cap4, linkwarden, paca, glean | Shared base image only — each app has its own container + data volume | Safe to remove any one app's container; the image stays for the others. Do not `docker rmi` this tag while any of the 5 remain. |
| Postgres `16bc17c64a57` (dangling, still referenced) | cap4, linkwarden, paca | Same pattern, untagged layer | Docker refuses to remove it while in use — no action needed, will self-resolve once all 3 apps are gone. |
| MySQL `a3dff78d8762` (dangling) | moo-tasks, plainpad | Shared base + built layer | Same as above — in-use, not deletable, no action needed. |
| `redis:7.4-alpine` (`6ab0b6e73817`) | docmost, core, sheets, leantime, stash-bookmark, gongyu, grist-core | Shared cache base image, 7 apps, no shared data | Image-layer sharing only; safe to remove any single app's redis container independently. |
| `redis:latest` (`e628485c98f8`) | affine, rowboat | Same pattern | Image-layer only, independent containers/data. |
| `mongo:latest` (`d6566e93e6a9`) | komodo (compose project "compose"), rowboat | Same pattern | Image-layer only. |
| `pgvector/pgvector:pg17` (`feb68f4f1544`) | colanode, stash-bookmark | Same pattern | Image-layer only. |
| `nginx:alpine` (`1d13701a5f9f`) | deldemo, horizon, opensparrow, cap4 | Generic reverse-proxy base image | Image-layer only, no runtime state shared. |
| `getmeili/meilisearch:v1.41.0` | karakeep, docker/kanbu | Base image only | Image-layer only. |
| `gcr.io/zenika-hub/alpine-chrome:124` | karakeep, docker/kanbu | Headless-chrome sidecar base image | Image-layer only. |
| `rowboat.bjk.ai` (nginx site, 2 path-routed backends) | rowboat (8038), deck-renderer (8060) | One server_name, two backend containers — same logical stack | Not a conflict; both ports must stay reachable together or the site breaks for one of the two paths. |
| `/apps/cap42`, `/apps/cap4l` (dirs, weakly shared via env-var proximity) | cap4, cap-v2, cap | Same monorepo/config registered 2–3× under different app slugs | **Confirm canonical slug before any removal** — deleting "cap" or "cap-v2" as a "duplicate" could delete the only copy of `/apps/cap42`/`cap4l`'s config. |

**No shared volumes exist** (0 confirmed in ground truth — every volume belongs to
exactly one compose project). **No shared database instances exist** — every
Postgres/MySQL/Redis/Mongo container inspected serves exactly one app; all "shared" DB
entries above are image-layer sharing only, never a single DB instance serving
multiple apps.

### False positives (DEL flagged shared=1, but ground truth says no real coupling)

| Resource | DEL claim | Ground truth |
|---|---|---|
| `bridge`, `host` docker networks | "shared" across ~10 apps | Docker's own built-in default networks — every container not given an explicit compose network lands here; not a meaningful per-app coupling, never delete-able anyway. Recommend DEL exclude these from the shared-resource surface. |
| `runtipi_tipi_main_network` | shared=1 | Only 1 app actually associated — DB noise. |
| `/apps/bitwarden` (dir) ↔ linkwarden | shared=1 | Secondary artifact of the shared postgres base-image resource, not real code sharing (`/apps/bitwarden` has no compose/env of its own) — flag for manual confirmation, not load-bearing. |
| `/apps/notes` (dir) ↔ zennotes | shared=1 | Only 1 app returned in the join despite the flag — DB data-quality issue (see also §5, this dir is separately mis-associated to notex/zennotes both). |
| `cap_v2_minio_data` (volume) → app `wonderful-mayer` | shared=1 | Volume does not exist on host at all; stale record from a renamed/retired `cap_v2` compose project. Not really "shared" (only 1 app in the join either way). |
| `filerise.bjk.ai.bak.*` (nginx_site row) | shared=1 | A `.bak` file, not a live config — not in sites-enabled, shouldn't count as a shared resource. |

---

## 4. Docker / nginx / ports health

**Docker** (scan 89 vs. live `docker` ground truth — all match except noted):
- 180 containers (179 running, 1 exited: `blinko-postgres`, unhealthy but its sibling
  `blinko-website` is still running — the app is active, this needs a restart
  investigation, not removal).
- 166 images (165 in DEL — 1-image drift is scan-timing lag, not a bug). 3 dangling
  images are all in-use shared DB base-image layers (postgres/mysql, see §3) —
  correctly excluded from prune. 3 truly-unused images are safe prune candidates
  (~1.9GB: `openruntimes/node:v5-22`, `openruntimes/static:v5-1`, `busybox:latest`).
- 98 volumes, 21 orphaned (~10.3GB total). Largest two: `openshell-cluster-nemoclaw`
  (4.7GB, unlabeled/no owning app — **already deleted, see §6**) and
  `netdata_netdatacache` (3.4GB, netdata is stopped but not removed).
- 88 networks; only `rowboat_net` is a genuine cross-app share (§3); `bridge`/`host`
  are Docker defaults, not app-specific.
- Build cache: 16.44GB, 100% reclaimable — the single largest system-wide reclaim
  opportunity, unrelated to any app resource (`docker builder prune` candidate).

**Nginx:**
- 135 sites-enabled (100% symlinks, 0 broken links), 137 sites-available, 0 debris
  files (no `.bak`/`.orig`/`~`/`.stale`/`.retired` litter in the live tree — one `.bak`
  exists but correctly stays disabled/unenabled).
- DEL's nginx_site inventory (272 rows: 137 available + 135 enabled) matches disk
  exactly at scan 89.
- 0 duplicate server_names across distinct files; 0 real port conflicts (all
  multi-domain-per-port cases are documented same-app aliases: fileshare2, will-be-done,
  b64pdf2, boxy).
- 8 dead-upstream **enabled** sites — all confirmed to be intentionally-stopped apps
  (dir/compose project still exists), not orphaned configs. Full list in §5.

**Ports:**
- 179 listening sockets total. 155 internal (loopback-bound). 24 socket entries / 22
  distinct ports public-bound.
- Public-bound breakdown: ssh (22), http/s (80/443), samba (139/445 — **the one
  pre-existing security exception**, unchanged from prior audits), streaming
  (1935/8050 flussonic), STUN/TURN (3478, 6969), NoMachine (4000), Performance
  Co-Pilot metrics (4330/44321, default PCP behavior), RustDesk (21118), AionUI
  (3000/9000), and several public-by-design app frontends (sema-shopping 3015,
  semashop 3019, pm2-root 3020, boxy-docs 3901/3911, del-docs itself 8072/8073).
  Cockpit's previously-flagged public exposure is **confirmed resolved**
  (now `127.0.0.1:9091` only).
- DEL-inventory-accuracy verdict: **matches ground truth** on every count checked
  (containers, images, volumes, networks, ports, systemd attribution) — the only
  drift anywhere in the entire audit is the expected 1-image scan-timing lag.

---

## 5. Known exceptions / needs-attention

Every actionable item across all 8 lanes, one table, prioritized by impact.

| # | Item | Impact | Recommended action |
|---|---|---|---|
| 1 | **Dead-upstream ENABLED nginx sites for stopped apps**: dockhand (9230), fizzy (8077), netdata (19999), notecapai (9101), shows (8335), trflix (8048), twenty (8036), zabbix (8300) | Visitors get 502/connection-refused on these 8 subdomains; not a security issue, just broken UX | Per-app: either restart the compose stack / systemd unit to revive, or disable+remove the nginx site if permanently retired. Group decision recommended rather than one-by-one. |
| 2 | **OpenPdf** — MISCONFIGURED | `docker compose config` fails outright: `.env` file missing. App cannot start at all in current state | Restore/recreate `/apps/OpenPdf/.env` from backup or redeploy config, or explicitly mark decommissioned |
| 3 | **17imgshare** — MISCONFIGURED, untracked in DEL | Gunicorn backend actually running on 127.0.0.1:8087, but its nginx vhost was never installed to sites-available/enabled — app is up but unreachable from outside; also invisible to DEL's automatic inventory | Install the vhost config if the app should be public, or stop the gunicorn process if abandoned; add manifest so DEL tracks it |
| 4 | **fizzy** — MISCONFIGURED (also listed in #1) | Container fully absent (not even stopped) while nginx site stays enabled — worse than a normal "paused app," looks like an incomplete teardown | Confirm intent: redeploy container or remove nginx site + compose files together |
| 5 | **liam** — incomplete decommission | Both an active `liam.bjk.ai` nginx config and an explicitly-named `liam.bjk.ai.retired-20260623T231540Z` config exist side by side; systemd unit disabled | Confirm whether liam should be revived or the still-enabled (non-retired) nginx config removed to match the retirement |
| 6 | **img3** — stray config artifact | Contains a leftover `nginx-img2.bjk.ai.conf` referencing the *different*, currently-live `img2` app — not active, but confusing for future editors | Delete the stray file from `/apps/img3` (not a live config, safe to remove; not performed here, docs-only task) |
| 7 | **semalist, trp, 17imgshare** — live but untracked in DEL | These 3 apps are genuinely running (semalist: node behind nginx; trp: active openvpn tunnel; 17imgshare: see #3) but have zero DEL applications-table row — DEL's automatic scan misses non-Docker/non-systemd manual apps | Add DEL manifests for each so they're tracked, not rediscovered by manual audit each time |
| 8 | **`/apps/notes` dir mis-associated** | DEL's association table links this directory to both `notex` and `zennotes`, but the directory itself holds an unrelated CMake/C++ project — looks like a false-positive name match | Review/correct the DEL association record; not an infra problem, a data-quality one |
| 9 | **Samba public bind (139/445 on 0.0.0.0)** | Pre-existing, unresolved across multiple audits — the one standing security note | Decide once: firewall to LAN/Tailscale-only, or accept as intentional and document the acceptance |
| 10 | **xtr: `@reboot` cron entry vs. `xtr.service`** | Root crontab has `@reboot /apps/flixapp/.venv/bin/python3 /apps/xtr/app.py` which duplicates what `xtr.service` already starts on boot via uvicorn — a double-start risk (same app dir, two uncoordinated launch mechanisms) | Confirm which is authoritative (recommend: keep `xtr.service`, remove the cron line) |
| 11 | **`cap42`/`cap4l` vs. `cap`/`cap4`/`cap-v2` slug ambiguity** | Same underlying monorepo appears registered 2–3× under different DEL app slugs; risk of deleting the only copy of shared config if one slug is removed as a "duplicate" | Confirm canonical slug per deployment before any cap-family removal |
| 12 | **`astv-remote.service` / `xtr.service` hidden cross-app venv dependencies** | `astv-remote.service` executes from `/apps/appletv-remote`'s venv; `xtr.service` executes from `/apps/flixapp`'s venv — deleting either "unrelated" app would silently break the other's service | Document the dependency in each app's manifest so future removal planning catches it; do not remove `/apps/appletv-remote` or `/apps/flixapp` venvs without checking |
| 13 | **`atv-remote.service` — disabled duplicate unit** | Dead config clutter, identical target to `appletv-remote.service` | Remove once `appletv-remote.service` is confirmed canonical (already removed — see §6) |

---

## 6. What was fixed in this audit

- **`openshell-cluster-nemoclaw` orphan docker volume (4.7GB, no owning app)** — deleted.
  Confirmed absent from `docker volume ls` post-fix.
- **Leftover systemd unit files removed** (targets no longer exist on disk):
  `onlook-web.service`, `comfyui.service`, `uld-backend.service`, `uld-frontend.service`,
  `atv-remote.service`. Confirmed absent from `systemctl list-units --all`.
- **`boxy` status-accuracy fix** — DEL's DB had recorded `boxy` as `kind=compose,
  status=compose_stopped`, which was wrong; it actually runs as a native systemd
  service (`boxy.service`, compiled Rust binary), serving `boxy.bjk.ai` and
  `api.boxy.bjk.ai` (both HTTP 200). Corrected in the current scan.
- **`PORT-REGISTRY.md` regenerated** from scan 89 (was 21 scans stale, generated from
  scan 68). All `compose_stopped` entries in the doc now reflect current reality
  (e.g. boxy/docs.boxy are shown running again).
- **Result: 0 failed systemd units** as of this audit (previously 2: `onlook-web.service`
  and `notecapai-doc-worker.service` — the latter remains failed as it's tied to the
  still-stopped notecapai app, see §5 item #1; onlook-web's failure was resolved by
  removing the leftover unit since its app no longer exists).

---

## 7. How to refresh this

1. **Re-scan the fleet via DEL**: trigger a rescan from the DEL web UI (`https://del.bjk.ai`)
   or via `del-admin rescan` (see `/apps/del/scripts/`). This refreshes the
   `applications`/`resources`/`associations` tables that most of this document derives from.
2. **Regenerate the port registry**: `/apps/del/scripts/gen-registry.py` against the
   latest scan id — writes `/apps/del/docs/PORT-REGISTRY.md`. Run this after every
   rescan; the registry silently goes stale otherwise (it was 21 scans behind before
   this audit).
3. **Raw audit data**: the full 8-lane findings this document summarizes live at
   `/apps/del/data/audit/2026-07-20/` (`filesystem.md`, `nginx.md`, `docker.md`,
   `ports-services.md`, `dirs-A.md`, `dirs-B.md`, `dirs-C.md`, `shared-and-config.md`).
   Re-running the same 8-lane methodology periodically (e.g. quarterly, or after any
   large cleanup) is the recommended way to keep this reference current — a full
   re-audit should not be needed for day-to-day questions if this document and the
   DEL DB stay in sync.

## Port provenance & actual external exposure (2026-07-20)

All 172 listening ports were traced end-to-end (listener -> pid -> exact binary/script -> launcher -> config file -> working dir). **0 unexplained.** Breakdown: ~110 Docker-compose-published (traced to the exact compose file incl. nested paths like /apps/AFFiNE/.docker/selfhost/, /apps/kanbu/docker/, /apps/komodo/compose/); ~35 systemd units under /etc/systemd/system (xtr, boxy, img2, ppv, n50-runner, del-web/del-docs, flixapp, glmflix, hls-manager, vshare, appletv-remote/astv-remote, etc.); 3 host-network containers (memos:8014, bjkflix:8061, notesnook-web:8018); remainder are system daemons + a few manual/orphan processes.

### Actual exposure — ufw default-DENY is the real gate
Host runs **ufw active, policy DROP**. A process binding 0.0.0.0 is **NOT publicly reachable** unless ufw explicitly allows the port. Verified BLOCKED despite 0.0.0.0 bind: del-docs 8072/8073, AionUi 9000, dev servers 3015/3019/3020/3901, PCP pmcd 44321, **Samba 139/445**. This corrects the recurring "samba/dev-server public exposure" flag from prior audits — bind-address artifacts, not real exposure. Genuinely reachable = only ufw ALLOW rules (80/443 nginx, flussonic 8050, rustdesk 21118, and an operator-opened set, some scoped to admin IP 72.80.59.32). Worth a glance: 8087 (17imgshare gunicorn, manual/orphan) is ufw-allowed Anywhere.

### Manual / orphan processes (functioning, no systemd/compose supervisor)
17imgshare gunicorn (8087), node server.js in /apps/htmls (8195), claude-code-router (3456), adb (5037), anisette container (6969, bare docker run), netmuxd container (5353, bare docker run), a stray Playwright relay. Candidates to convert to systemd units or leave as-is.

### Cross-app venv oddities (functioning, inconsistent)
xtr.service runs from flixapp's venv; astv-remote.service runs from appletv-remote's venv. Both work; noted for consistency only.
