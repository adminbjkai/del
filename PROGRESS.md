# DEL — Progress

## ROUND 8 CLOSED 2026-07-19 — post-removal UI staleness fixed (cap3 confusion)
- Successful LIVE jobs now auto-trigger a rescan (jobs.py) so the UI reflects reality immediately.
- Apps list hides apps not seen in the latest scan (add ?show=removed to see history); app detail shows a "not present in latest scan — removed" banner for stale apps.
- Verified live: /apps?search=cap3 → 0 rows; /apps/cap3 → banner; resources/volume shows 0 cap3 (latest scan 43). Suite: test_web 18 passed; full suite 115 passed earlier this round.

## ROUND 7 CLOSED 2026-07-19 — cap3 volume-deletion bug fixed, cap3 fully removed
- Bug: plan-form per-volume checkboxes stored options.approved_volumes but planner only honored the DB approved_by_user flag → volumes always "not approved, preserving" for form users. Fixed: planner accepts either.
- Bug: planner included stale resources from old scans (deleted cap3 compose file still got a compose_down step). Fixed: plans only include latest-scan resources (tolerant when scans table empty for unit tests).
- cap3: 3 volumes backed up (/apps/del/backups/cap3/volumes/*.tar) then deleted via DEL job 52, validation ALL OK, docker volume ls shows 0 cap3. Suite 115 passed / 1 skipped.

## ROUND 6 CLOSED 2026-07-19 — real production removals (bewcloud, filerise) + robustness fixes; session wrapped per user
- bewcloud: FULLY removed via DEL (job 40: config+volume backups → compose down → volume_rm → /apps/bewcloud deleted → validation ALL OK). Verified gone (dir + volume).
- filerise: FULLY removed via DEL across jobs 43/45/47 (backups incl. 3 bind-mount tars; containers/network/dir/nginx removed; leftover image + one stale .bak removed via audited helper ops). Verified: no docker/nginx/dir remnants; nginx -t pass.
- Robustness fixes shipped during these removals (all tested, 115 passed/1 skipped): planner backs up compose config FILES not the dir (stopped projects); compose_down project-name fallback declared_name→dir basename; image_rm gets allowed_container_ids; helper rm-ops now idempotent (absent network/container/volume/image = success); correlate Step 8b attaches ENABLED nginx sites to stopped apps by EXACT server_name slug match.
- Backups live under /apps/del/backups/{bewcloud,filerise}/.
- NOT DONE (user deferred, next session): removals of runtipi, refetch, thirtybees, tesserae — script ready at scratchpad remove_app.py (per-app: approve volumes → full-backup plan → dry-run → live w/ phrase). Note runtipi_tipi_main_network is shared with running fireshare_migrated and will be preserved.

## ROUND 5 CLOSED 2026-07-19 — post-removal leftover handling (bewcloud case)
- Bug 1: _AppBuilder.assocs keyed by resource key only → directory '/apps/bewcloud' collided with compose_project '/apps/bewcloud' (assoc silently merged, directory lost; cross-type key overlap also produced bogus shared flags). Fixed: keyed by (type, key) everywhere incl. Step 13 shared detection + manifest indexing.
- Bug 2: weak name-similarity claims (cloud@0.77 on /apps/bewcloud) survived vs confirmed owners → shared/blocked pollution. Fixed: Step 13a drops <60-confidence claims when another app holds ≥80.
- Enhancement: Step 7b attaches git_repo/env_file resources inside an owned app dir at confirmed 95 (was name-similarity 48-50/possible/blocked).
- Verified live (scan 31): /apps/bewcloud shows all 5 leftovers confirmed/safe, zero blocked, no phantom 'cloud' claims. Suite 115 passed / 1 skipped.

## ROUND 4 CLOSED 2026-07-19 — hardening round (memos/orphans/UI/docs-link), all verified
- Host-network correlation fixed: memos (memos.bjk.ai + 8014), bjk-ai-flix, filebrowser, notesnook all attributed via cgroup/listener ownership. netmuxd/beszel-agent have no proxied domains — nothing user-facing missing.
- NoNewPrivileges sudo-block solved architecturally: helper gained read-only `list_listeners` op (ss -lntp as root); proc_src falls back sudo→helper→plain ss. UI scan 29 verified using helper (audit log) with 4 container-owned ports.
- Orphans: reasons now precise (compose-referenced vs unreferenced images; stale nginx copies); 10/10 random orphan images sample-verified truly unused; owned ports/processes excluded (30 noise rows removed).
- Apps page single filter bar; sidebar Docs link; fern pages updated (browsing/orphans/architecture). Suite 115 passed / 1 skipped.

## ROUND 3 CLOSED 2026-07-19 — all user feedback items done, all validated
- Correlation fixes (sonnet lane): stale nginx .bak files no longer pollute domains (bytestash/blinko verified clean); image orphan false-positives fixed (297→110 true orphans; b64pdf-app:latest shows its container); Host/Container port columns; clickable domains (target=_blank); orphan "why" reasons. 110 tests passed.
- Fern docs redesign (opus lane): user-first nav (Getting Started / Using DEL with flagship "Removing an Application" walkthrough incl. "Removing everything" checklist / Administration / Reference), 13 real dark-theme screenshots of the final UI, 3 codex-generated infographics, 3 mermaid diagrams, helper-ops accordions. Fresh e2e via deldemo: dry-run 26/26, live success, zero remnants. make-demo-app.sh kept in scripts/.
- I added nginx location /_local → 127.0.0.1:8073 (backup del.bjk.ai.bak.*, nginx -t pass, reloaded; tracked copy synced) — all doc images now 200 image/png.
- I fixed two lane-surfaced bugs: helper image_rm allowed-container matching by name+id (was ids-only, failed dry-runs); planner remove_files dedupes paths and skips children nested under an ancestor being deleted (was failing live jobs on nested bind mounts).
- Final matrix (evidence in session): helper tests 54 passed; full suite 110 passed/1 skipped; app pages /,apps,app-detail,resources/image+container,orphans,jobs,settings all 200 authed; docs pages 200 with 7 inline /_local screenshots on the removal guide; all 4 services active; healthz ok (scan 25).

## FINAL STATUS 2026-07-19: COMPLETE — all acceptance criteria met
- DNS: user added del.bjk.ai A record (72.80.59.32, verified at 1.1.1.1 + 8.8.8.8); https://del.bjk.ai/login → 200 via public IP. /etc/hosts override removed.
- All deliverables done and validated. Test suite: 97 passed, 1 skipped.
- End-to-end removal of disposable app "deltest" through DEL's real HTTPS UI/API: dry-run 25/25 steps, live removal completed across multiple jobs (intentionally exercising halt-on-failure + auto-restore), final state verified clean (no containers/volume/network/unit/timer/cron/nginx/dir remnants), production untouched (212 containers, nginx -t pass, spot checks up).
- Bugs found & fixed during e2e: network_rm dry-run semantics (allowed_container_ids, id/name matching), validate_removal informational in dry-run, confirmed_twice forwarding for volume_rm, backup ops missing makedirs, nginx path validation dereferencing symlinks, nginx multi-path single-step ordering (enabled first), backup filename collision, planner timer units + absent-unit skip, concurrent live job guard, nginx_test helper op for NoNewPrivileges validation.
- /etc/hosts has "127.0.0.1 del.bjk.ai" (backup /etc/hosts.bak.*) so server-local HTTPS works pre-DNS.

## Goal
Build DEL (https://del.bjk.ai): self-hosted app-inventory + safe-uninstall system per spec in the kickoff prompt. Read-only audit first, then report, then build, then validate with a disposable test app.

## Key environment facts (verified 2026-07-19)
- Host: bjkai-2tb-ubuntu, Ubuntu, up 56 days, 62G RAM, 1.8T disk 47% used.
- User bjkai: sudo NOPASSWD, docker group.
- Docker 29.5.2, Compose v5.1.4, socket unix:///var/run/docker.sock.
- ~115 running Compose projects; apps live under /apps/<name> (262 dirs in /apps). Some also under /data/apps (fireshare_migrated).
- /apps/del pre-created and empty → project root. /opt/del is a symlink → /apps/del (created, verified) so spec paths hold.
- Listening ports captured; 8000–8102 range densely used. Candidate free ports for DEL: 8036, 8048, 8065, 8072–8077 (must re-verify before binding).

## Round 2 (user feedback 2026-07-19): UI/UX overhaul + docs consistency
- User: tables not interactive/clear, filtering inconvenient, wants smooth+friendly UI, docs fully accurate, no orphaned docs/code.
- Verified defects: resources tables only 4 generic columns, no owner/shared/size context; no sort/pagination; sidebar links plural /resources/containers but DB types singular → empty page.
- [DONE 2026-07-19] UI overhaul lane (opus): reusable data-enhanced table engine (click-sort numeric/size/date-aware, instant text + per-column dropdown filters, 25/50/100/All pagination with "X–Y of N (filtered from M)", empty states, id truncate+copy-on-click) in static/app.js (~470 lines). Resources: tab bar of all 16 singular types with latest-scan counts, per-type meaningful columns from data_json + owner-map join (owner apps link to /apps/{slug}, amber shared / grey unassigned badges); legacy plural URLs (/resources/containers) alias to singular (no empty page). Apps list, app detail (collapsible sections w/ counts, evidence, shared warning box, protected-aware Plan removal), dashboard (clickable stat cards, human sizes, enhanced recent tables), orphans (grouped by type + review-only explainer), jobs (mode/status badges, duration, app+plan links), job detail (stage grouping, step duration, collapsible output, autoscroll). CSS: badge palette, light-theme via prefers-color-scheme, <900px responsive sidebar, focus-visible, table horizontal scroll. Evidence: pytest ../tests/ 105 passed/1 skipped (test_web.py extended: tab counts, singular/plural, owner join, orphans, shared). systemctl restart del-web OK; curl login+matrix all 200 incl /resources/containers non-empty; grep confirms zero external URLs.
- [DONE 2026-07-19] Dashboard real storage stats (sonnet): `web/routes.py` `_disk_usage_bytes()` (single-pass sum over latest scan's directory `data_json.size_kb*1024` + volume size fields where present) and `_reclaimable_bytes()` (`docker system df --format '{{json .}}'`, read-only subprocess, parses Reclaimable column, in-process 5-minute cache). Evidence: `pytest tests/ -q` → 105 passed/1 skipped (pre-existing unrelated flaky `test_session_cookie_sign_and_verify` reran green 3/3); after `systemctl restart del-web`, curl login flow through both 127.0.0.1:8075 and https://del.bjk.ai showed dashboard stat cards "190.7 GB" (disk usage) and "103.7 GB" (reclaimable), both previously hardcoded 0.
- [DONE 2026-07-19] Docs/orphan sweep lane (sonnet): fixed ARCHITECTURE.md + fern/pages/reference/architecture.mdx (Python 3.12→3.10, backend/del/→backend/del_app/, helper op table added path_restore + nginx_test, new "Deployment units" para covering del-docs.service/8072-8073/basic-auth); INTERFACES.md (tomllib→tomli, check_csrf(request, submitted) signature, execute_job(job_id, confirm_phrase=None) signature, added persist_plan/load_plan/verify_plan signatures, added plans.options_json `_meta` persistence note, completed the web routes list — was missing rescan-approve/manifests/job-status); SECURITY.md + fern/pages/reference/security.mdx (added `nginx_test`+`path_restore` to allowlist summary, ~15-op→~21-op, new "Documentation site auth (/docs, /_next)" section documenting the Nginx-basic-auth bypass of app session auth and the two password-file locations, del-docs hardening note); OPERATIONS.md + fern/pages/guides/operations.mdx (del-docs status/start/stop/restart commands and journal, new "Updating the documentation site content" section — verified by live-editing operations.mdx, confirming journalctl showed `[change]`/`Reload completed` but the new heading/body text was absent from `curl 127.0.0.1:8072/docs/guides/operations` until `systemctl restart del-docs` was run, so documented an honest restart requirement rather than an unverified hot-reload claim; test edit reverted before commit); UNINSTALL.md + fern/pages/reference/uninstall.mdx (added del-docs.service disable/rm, `/etc/nginx/.del-docs-htpasswd` removal, explicit note that removing the nginx site file removes both the app and /docs+/_next blocks, fern/ called out in the tree-removal step, step numbering 2 units→3 units); README.md + fern/pages/overview.mdx (added del-docs Docs-unit row to quick-facts table); fern/pages/overview.mdx also got a new "Interface" section describing the sortable/filterable/paginated tables, resources owner/shared-badge tabs, and clickable dashboard cards added in the Round-2 UI overhaul (previously undocumented on the docs site). Orphan hunt: grepped all *.md + fern/pages/**/*.mdx for compose.yaml-at-root, .env.example, DNS-pending, /etc/hosts-workaround mentions — none found (already clean); path-existence sweep over every /apps|/etc|/run path referenced in docs found zero real dead references (regex false positives on templated paths like `.bak.<ts>` were manually checked and confirmed fine); module cross-reference check over every file in backend/del_app found no orphaned/unimported Python modules. `docs/server-audit.md` intentionally left untouched — it's an explicitly-labeled historical Phase-2 design doc, not a claim about current reality. Evidence: full `pytest tests/ -q` 105 passed/1 skipped; `sudo nginx -t` pass; `systemctl is-active del-web del-helper del-docs` all active; curl matrix — `/` (authed) 200 with non-zero human sizes, `https://del.bjk.ai/docs` with basic auth → 307→200 (401 without auth, confirmed), `/login` 200; del-docs restarted and mdx edits confirmed live via curl (e.g. architecture page shows "Python 3.10", "path_restore", "nginx_test").
- [ ] Round 3 (user request, expanded): Fern docs REDESIGN on opus after sweep lands — USER-GUIDE-FIRST: how to use the UI to remove apps/projects/repos, with REAL screenshots (Playwright headless chromium — verified available: /usr/local/bin/playwright + ~/.cache/ms-playwright chromium builds). Plan: recreate a disposable demo app, screenshot the full removal flow (app page → volume approval → plan preview → dry-run → DELETE VOLUMES confirm → live job → validation), remove it via DEL (doubles as fresh e2e), store shots in /apps/del/fern/assets/. Structure: Using DEL (scan/browse/badges/remove-walkthrough/orphans/backups) as centerpiece; Admin (install/ops/recovery/uninstall); Reference (architecture w/ mermaid, security, helper ops) condensed. Fern MDX components: Steps, Tabs, Accordions, Callouts, Cards, Frame.
- [ ] Final re-validation (tests + HTTPS page matrix incl /docs) and report.

## Staged plan
1. [DONE 2026-07-19] Phase 1 read-only audit — parallel subagent lanes writing JSON to /apps/del/data/audit/:
   - docker (containers/images/volumes/networks/df)
   - nginx sites
   - systemd + cron/timers
   - processes/ports/tmux/screen
   - filesystem: compose files, git repos, storage
2. [ ] Phase 2 audit report /opt/del/docs/server-audit.md + /opt/del/data/initial-inventory.json
3. [ ] Architecture + threat model docs
4. [ ] Build backend (discovery engine, correlation, manifests, planner, jobs, auth, audit log)
5. [ ] Privileged helper (allowlist, path validation, dry-run)
6. [ ] Frontend UI
7. [ ] Deploy: compose, localhost port, nginx del.bjk.ai, HTTPS validation
8. [ ] Disposable test app + end-to-end removal test through DEL
9. [ ] Automated tests, docs, final report

## Decisions
- Project root /apps/del with /opt/del symlink (server convention; spec paths preserved).
- DEL must be marked protected (cannot remove itself).

## Audit results (all lanes completed, outputs in /apps/del/data/audit/)
- docker.json: 212 containers (all running), 302 images (77G reclaimable), 160 volumes (65 orphan candidates), 109 networks (4 shared), 111 compose projects, 3 non-compose containers; 101.6G stale build cache; bitwarden + fireshare_migrated labels point at missing compose files.
- nginx.json: 219 available / 162 enabled sites, nginx -t PASS, 13 enabled sites with dead upstreams, 70 orphaned available-only files, del.bjk.ai unclaimed. Wildcard bjk.ai cert used almost everywhere.
- systemd.json + cron.json: 56 custom units (2 failed: notecapai-doc-worker, onlook-web), 19 timers (2 custom), 12 cron entries; xtr double-start risk (service + @reboot cron).
- processes.json: 276 listeners (140 systemd/125 docker/8 manual), 39 public-bound sockets on 24 ports, 3 nohup orphans, 1 tmux session.
- filesystem.json: 277 compose files, 271 project dirs, 166 git repos (106 dirty), 28 abandoned-candidate dirs, /apps=254G.

## Build lanes (launched, sonnet; helper=opus)
- core backend (config/db/models/auth/main/CLI) — running
- discovery+correlate+scanner+manifests — running
- planner+jobs — running
- discovery+correlate+scanner+manifests — DONE 2026-07-19: backend/del_app/discovery/{docker,compose,nginx,systemd,proc,cron,fs}_src.py + correlate.py + manifests.py + scanner.py + tests/test_discovery.py. `pytest tests/test_discovery.py` 9 passed; full repo `pytest tests/` 97 passed/1 skipped. Live dry-run collection (no db writes) on this host: docker 958, compose 196, nginx 381, systemd 232, proc 276, cron 29, fs 497 = 2569 resources in 15.6s. Full scanner.run_scan() against a scratch sqlite db (schema from migrations/001_init.sql): 196 apps, 2568 resources, 2076 associations persisted in 16.8s. Correlation validated against the completed audit numbers: 111 compose apps (matches "111 compose projects"), 3 standalone-container apps (matches "3 non-compose containers"), 162 enabled nginx sites (exact match), 166 git repos / 53 .env files (exact match to filesystem audit). All sources are read-only (docker inspect/ps/images/volume/network ls, sudo ss -lntp, systemctl show/list-*, file reads); no start/stop/rm/reload ever called. Env var VALUES stripped everywhere (only names kept) — verified by a live smoke test.
- privileged helper daemon — DONE 2026-07-19: helper/del_helper.py + helper/validation.py + config/helper-policy.json + config/del-helper.service (not installed) + tests/test_helper.py; pytest 54 passed, 1 skipped. Full op allowlist + path_restore implemented, stdlib only. Smoke-tested with real policy: ping ok, /etc and /apps/del rejected.
- web UI (routes/templates/static) — DONE 2026-07-19: backend/del_app/web/{routes.py,templates/*.html,static/{app.css,app.js}} + tests/test_web.py; `pytest tests/test_web.py` 9 passed, full repo suite `pytest tests/` 88 passed/1 skipped. Router wired through main.create_app() and smoke-tested (GET /login 200, GET / unauth -> 303 /login).
- Port decision: 8075 (confirmed absent from listener port list).

## Integration + deployment (2026-07-19, all evidenced by tool output)
- All 5 build lanes done; full suite 97 passed / 1 skipped after my plan-persistence fix (plan _meta stored in options_json).
- Deployed: del-helper.service + del-web.service installed/enabled/active; /run/del/helper.sock 0660 root:bjkai; RuntimeDirectoryMode fixed 0750→0755.
- nginx del.bjk.ai installed, nginx -t PASS, reloaded; GET /login over TLS via --resolve → 200.
- BLOCKER (user): del.bjk.ai has NO public DNS record (checked 1.1.1.1 + 8.8.8.8; no wildcard). Must be added in IONOS.
- Admin account 'admin' created; initial password at /apps/del/config/admin-initial-password.txt (0600).
- Scan id=1: 196 apps / 2574 resources / 2079 associations in ~18s. Fixed correlation bug (code-server bind-mounting /apps claimed all /apps/* dirs) via _is_broad_root guard; scan id=2 verified: DEL protected+owns its resources, code-server dir associations 0.
- planner.build_plan('del') → PlanError "protected" (verified).
- Launched: deltest disposable app lane (sonnet) + docs lane (sonnet).

## Done (with evidence)
- /opt/del symlink created (ls -ld shows /opt/del -> /apps/del).
- Directory skeleton created under /apps/del.

## Blockers
- None.
