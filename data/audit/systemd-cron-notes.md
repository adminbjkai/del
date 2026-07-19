# systemd + cron audit notes — 2026-07-19 (read-only)

## Counts
- System services loaded (`list-units --type=service --all`): **213**
- System service unit files: **354**
- User (bjkai) services loaded: **70**; user service unit files: **107**; user timers: **0**
- Custom units in /etc/systemd/system (real files, snap/vendor-symlinks excluded): **56** (52 services + 2 timers + 1 socket + 1 templated service)
- Custom user units: **3** (bjkai: openclaw-gateway.service, vertex-proxy.service — both inactive/disabled; root: openclaw-gateway.service — root user manager not running)
- System timers: **19** (2 custom: bjkflix-dizipal-cf.timer, bjkflix-ios-refresh.timer)
- Failed units: **2**
- Cron entries recorded: **12** (4 /etc/crontab stock, 4 /etc/cron.d active, 4 in bjkai's crontab)

## Failed units
| Unit | State | Note |
|---|---|---|
| notecapai-doc-worker.service | failed, disabled | Host doc-worker for /apps/notecapai; unit header says it retries until the compose postgres (:9103) is up — currently failed and NOT enabled, so it will not start at boot |
| onlook-web.service | failed, enabled | `docker compose up -d` in /apps/onlook; Requires supabase-onlook.service, which does not exist on this host (likely the failure cause) |

## Custom units → likely apps (evidence = WorkingDirectory/ExecStart paths)
Nearly all custom units map 1:1 to directories under /apps:

- **Streaming/flix stack**: flixapp (/apps/flixapp), glmflix (/apps/glmflix), trflix (/apps/trflix, disabled), xtr + xtr-dashboard (/apps/xtr), shows (/apps/shows, enabled but inactive), ppv (/apps/ppv, disabled), hls-manager (/apps/m3u8_antigravity, gunicorn :8099), bdl-bjk + bjkflix-ios-refresh timer (/apps/bjkflixdl), bjkflix-dizipal-cf service+timer (/apps/bjk-ai-flix, Cloudflare-clearance refresh every 12 min), 17tube (/apps/alltube_custom)
- **File/note sharing**: boxy + boxy-docs (/apps/boxy), fileshare (/apps/fileshare), fileshare2 (/apps/fileshare2), vshare (/apps/vshare), img2 (/apps/img2), txtshr (/apps/txtshr), tbl (/apps/tbl), htmls-webapp (/apps/htmls, disabled)
- **URL shorteners (4 variants!)**: bjkai_shorturl_by_claude (/apps/bjkai_shorturl_by_claude, active), url-shortener (/apps/url2, active), urlshortener + urlshortener-dashboard (/apps/urlshortener, both disabled)
- **AI/agents**: openclaw-gateway (system unit, active; also duplicate disabled user units for bjkai and root), openclaw-proxy (/home/bjkai/vertex-proxy.py), claudeclaw-web (/apps/claudeclaw-workspace, bun), notecapai-doc-worker + notecapai-image-daemon (/apps/notecapai), notex-image-bridge (/apps/notex), aionui-webui (/apps/AionUi), comfyui (/apps/ComfyUI, disabled), ollama, n50-runner (/apps/n50/v11-fresh-dir), liam (/apps/liam, disabled)
- **Remotes**: appletv-remote, atv-remote (disabled), astv-remote (shares the appletv-remote venv), samsung-tv-remote (disabled), vncend (/apps/vncend)
- **Misc**: jsonp (/apps/jsonp), sema-shopping (/apps/sema) + semashop (/apps/semashop), next-auth-docs (/apps/next-auth/docs), uld-backend + uld-frontend (/apps/8041, disabled), onlook-web (/apps/onlook, docker compose), neoclaw-forward (neoclaw-upstream.sh, disabled), nginx-ui (disabled), pm2-root (PM2 resurrect as root), docker-events (disabled), checkmk-agent socket :6556

## Timers → services
- bjkflix-dizipal-cf.timer → bjkflix-dizipal-cf.service (boot+2min, every 12 min)
- bjkflix-ios-refresh.timer → bjkflix-ios-refresh.service (every 6 days, 09:00 — AltServer re-sign before 7-day profile expiry)
- Remaining 17 timers are stock distro (anacron, apt-daily*, certbot, fstrim, logrotate, man-db, e2scrub, ua-timer, fwupd, motd-news, update-notifier*, dpkg-db-backup, systemd-tmpfiles-clean, apport-autoreport, snapd.snap-repair)

## Cron
- Root crontab: exists but empty. Only bjkai has entries:
  - `@reboot` /apps/flixapp/.venv/bin/python3 **/apps/xtr/app.py** — overlaps with xtr.service (both active would double-start; xtr.service is active/enabled → potential conflict)
  - `@reboot sleep 30` **/apps/cap4/scripts/doc-worker.sh**
  - `30 4 * * *` /apps/cap4/scripts/prune-raw-originals.sh
  - `15 */6 * * *` /apps/cap4/scripts/health-watch.sh
- /etc/cron.d: anacron, certbot, e2scrub_all are all no-ops under systemd (guarded on /run/systemd/system); sysstat entries are commented out.
- cron.daily notable: google-chrome, samba backup, sysstat; cron.hourly empty.

## Anomalies
1. **Inline secrets in unit files** (values redacted here, only names recorded): /etc/systemd/system/openclaw-gateway.service embeds NOTION_API_TOKEN and GH_TOKEN as Environment= values; xtr-dashboard.service embeds UNDERSTAND_ACCESS_TOKEN. Recommend moving to EnvironmentFile with 0600 perms.
2. **xtr double-start risk**: xtr.service (systemd, active) and bjkai's `@reboot` cron both launch /apps/xtr/app.py — and xtr.service uses /apps/flixapp's venv uvicorn while cron runs app.py directly.
3. **onlook-web.service** Requires the nonexistent supabase-onlook.service → failed but enabled (will fail every boot).
4. **notecapai-doc-worker** failed + disabled while its sibling notex-image-bridge is active; cap4 cron scripts (doc-worker.sh) look like a parallel/successor deployment of the same worker pattern.
5. **Triplicate openclaw-gateway definitions**: system unit (active, with secrets), bjkai user unit (disabled, newer v2026.6.10, uses EnvironmentFile ~/.openclaw/gateway.systemd.env), root user unit (orphaned — no root user manager). Only the system one runs.
6. Three URL-shortener implementations installed, two active (bjkai_shorturl_by_claude on Rust, url-shortener in /apps/url2).
7. shows.service enabled but inactive (not running though wanted at boot — likely exited/stopped manually).
8. Drop-ins: cockpit.socket listens on 9091; docker.service sets DOCKER_MIN_API_VERSION=1.24.
9. EnvironmentFile paths in use (paths only): /apps/bjkai_shorturl_by_claude/.env; /home/bjkai/.openclaw/gateway.systemd.env; stock: /etc/default/{locale,ssh,smartmontools}.

Full detail: /apps/del/data/audit/systemd.json and /apps/del/data/audit/cron.json (both validated with python3 -m json.tool).
