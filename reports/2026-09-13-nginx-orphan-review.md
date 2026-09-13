# Nginx orphan review — scan 249 confidence-60 associations

Date: 2026-09-13. Read-only audit; no files changed.
Scope: 28 associations (26 distinct nginx_site resources; 2 are backup copies) where
app slug ≠ config filename/domain. All evidence verified live on host:
sites-enabled symlinks, server_name/proxy_pass, live listeners (ss/lsof), process
args/cwd, docker port maps, HTTP title probes, systemd units, mtimes.

## Verdict summary

All 28 "remove_nginx_site" recommendations are WRONG (false positives).
Every one of these configs is a live, named-alias site pointing at a healthy
listener owned by the associated app. They are live aliases, not debris.

### A. Definitely LIVE ALIASES — filename/domain ≠ app slug, upstream verified (19)

| Config | server_name | Upstream | Owner (verified) | Status |
|---|---|---|---|---|
| bdl.bjk.ai | bdl.bjk.ai | 6969 | docker `anisette` (0.0.0.0:6969) | enabled, live |
| astv.bjk.ai.conf | astv.bjk.ai | 8083/8767 | /apps/astv-remote/server.py (pid 2809485) | enabled, live |
| atv.bjk.ai.conf | atv.bjk.ai | 8081/8765 | /apps/appletv-remote server.py (pid 1782) | enabled, live |
| flix.bjk.ai | flix.bjk.ai | 8061 | uvicorn → "BJK FLIX" (bjk-ai-flix) | enabled, live |
| bjkai_shorturl_by_claude.conf | bjk.ai | 8009 | systemd bjkai_shorturl_by_claude.service (title "URL Shortener - BJK.AI") | enabled, live |
| api.boxy.bjk.ai | api.boxy.bjk.ai | 8086 | /apps/boxy/target/release/boxy | enabled, live |
| docs.boxy.bjk.ai | docs.boxy.bjk.ai | 3911/3901 | `fern docs dev` (node, fern backend) | enabled, live |
| caprust.bjk.ai | caprust.bjk.ai | 8071 | docker caprust-web-internal-1 | enabled, live |
| dev.bjk.ai.conf | dev.bjk.ai | 8069 | docker dev-code-server | enabled, live |
| infinitv.bjk.ai.conf | infinitv.bjk.ai | 8026/8015 | docker extreme-infinitv / -remux | enabled, live |
| fs2.bjk.ai.conf | fs2.bjk.ai | 7127 | docker-proxy, title "FileShare2" (=fileshare2) | enabled, live |
| fileshare2.bjk.ai.conf | fileshare2.bjk.ai | 7127 | same app as fs2 (intentional alias pair) | enabled, live |
| plutors.bjk.ai | plutors.bjk.ai | 8877 | /apps/flixscrape/pluto/restream.py (pid 3547) | enabled, live |
| homepage.bjk.ai.conf | homepage.bjk.ai | 8049 | docker homepage-lite ("Homepage Lite") | enabled, live |
| libredb.bjk.ai.conf | libredb.bjk.ai | 8046 | docker libredb-studio ("LibreDB Studio") | enabled, live |
| hls.bjk.ai | hls.bjk.ai | 8099 | gunicorn hls_manager:app, cwd /apps/m3u8_antigravity | enabled, live |
| openknowledge.bjk.ai.conf | openknowledge.bjk.ai | 8100 | docker open-knowledge ("OpenKnowledge") | enabled, live |
| passbolt.bjk.ai | passbolt.bjk.ai | 8003 | docker passbolt_api-app-1 | enabled, live |
| pomodist.bjk.ai.conf | pomodist.bjk.ai | 8004 | docker pomodoist-selfhost-web ("pomodoist") | enabled, live |
| ente.bjk.ai | ente.bjk.ai | 8089/8090 | docker ente-museum / ente-web | enabled, live |
| stalkerportal.bjk.ai.conf | stalkerportal.bjk.ai | 8096 | docker stalker-portal ("STALKER PRO") | enabled, live |
| stash.bjk.ai | stash.bjk.ai | 8054 | docker stash-bookmark-app-1 ("Stash — AI Bookmark Manager") | enabled, live |
| tix.bjk.ai | tix.bjk.ai | 8199 | systemd tix-bjk-ai.service (http.server /opt/tix-bjk-ai) | enabled, live |
| txt.bjk.ai.conf | txt.bjk.ai | 7013 | systemd txtshr.service (node /apps/txtshr/index.js) | enabled, live |
| url.bjk.ai.conf | url.bjk.ai | 7007 | systemd url-shortener.service (/apps/url2/target/release/url-shortener) | enabled, live |
| vnce.bjk.ai | vnce.bjk.ai | 9123 | systemd vncend.service (/apps/vncend/vncend_server.py) | enabled, live |
| trbein.bjk.ai | trbein.bjk.ai | 8058 | uvicorn ("trbein — beIN Sports TR") | enabled, live |
| media-centaur.bjk.ai.conf | media-centaur.bjk.ai | 8058 | same upstream as trbein (alias, currently not served — see B) | available only |
| monkeycode.bjk.ai.conf | monkeycode.bjk.ai | 8074 | /apps/streamedpk uvicorn ("StreamedPK — Live Sports") | available only |

### B. Duplicate/alias pairs where one side is NOT enabled (2)

- monkeycode.bjk.ai.conf → 8074 (StreamedPK, streamedpk app): valid config but NOT
  in sites-enabled; server_name also differs from streamedpk.bjk.ai which IS enabled.
- media-centaur.bjk.ai.conf → 8058 (trbein app): NOT enabled; trbein.bjk.ai is enabled.
- fireshare.bjk.ai → 8071 (caprust): NOT enabled; caprust.bjk.ai is enabled. This is
  a leftover filename alias for the same live app.

These are "stale but valid app configs" — not debris; they duplicate live domains.

### C. Backup files swept into the same bucket (2)

- bjkai_shorturl_by_claude.conf.bak-20260724-2123 — timestamped backup of the live
  shorturl config. Not debris; belongs with the backup convention
  (backups/nginx-sites-available-*.bak pattern).

## Key correlation facts

1. The DEL matcher scores filename↔slug equality. All 28 are intentional
   renames/aliases where the serving domain predates or differs from the app slug.
2. Every upstream port in the 28 was probed live at audit time (all 200/302/400 on
   loopback; 9123 requires a session, systemd vncend.service running).
3. Naming map (config-name → real app):
   - bdl → anisette · atv → appletv-remote · astv → astv-remote
   - flix → bjk-ai-flix · bjkai_shorturl_by_claude.conf → bjkai-shorturl-by-claude
   - api.boxy / docs.boxy → boxy (docs = fern docs dev)
   - caprust.bjk.ai → caprust · fireshare.bjk.ai → caprust (disabled alias)
   - dev → code-server (docker dev-code-server)
   - infinitv → extreme-infinitv · fs2/fileshare2 → fileshare2
   - plutors → flixscrape · hls → m3u8-antigravity
   - homepage → homepage-lite · libredb → libredb-studio
   - openknowledge → open-knowledge · passbolt → passbolt-api
   - pomodist → pomodoist-selfhost · ente → selfhost (ente containers)
   - stalkerportal → stalker-portal · stash → stash-bookmark
   - monkeycode → streamedpk (disabled alias; streamedpk.bjk.ai live)
   - tix → tix-bjk-ai · media-centaur → trbein (disabled alias)
   - txt → txtshr · url → url2 · vnce → vncend

## Recommendations (evidence-backed; no deletions)

1. Correlation manifest: add `aliases:` support to DEL app manifests (or an
   nginx_alias table) so confidence-60 name-mismatch links can be upgraded to
   approved evidence. Candidate manifest entries:
   - anisette.yaml: nginx alias bdl.bjk.ai (upstream 6969)
   - code-server.yaml: nginx alias dev.bjk.ai (8069, docker dev-code-server)
   - fileshare2.yaml: fs2.bjk.ai + fileshare2.bjk.ai (both 7127)
   - caprust.yaml: caprust.bjk.ai (live) + fireshare.bjk.ai (disabled)
   - streamedpk.yaml: monkeycode.bjk.ai.conf (disabled alias)
   - trbein.yaml: media-centaur.bjk.ai.conf (disabled alias)
   - docs.boxy: attach to boxy as `fern docs dev` node service (3911/3901)
   - selfhost → ente.bjk.ai (ente-museum/ente-web containers)
2. Matcher fix: before scoring, resolve `server_name`/`proxy_pass` and compare
   against live listener ownership (process cwd/args, docker port map) — this
   alone would reclassify all 28 from 60 → high confidence.
3. Tests: for each row in the alias map, assert (a) upstream port has a live
   listener, (b) owner maps to the app slug, (c) enabled symlink state matches
   expectation (2 aliases + 1 backup intentionally not enabled).
4. Docs: add the naming map above to docs/PORT-REGISTRY.md or
   docs/DISCOVERY.md as "domain↔slug alias registry"; mark fireshare/
   monkeycode/media-centaur as intentional non-served aliases.
5. No removal action is justified by current evidence for any of the 28.