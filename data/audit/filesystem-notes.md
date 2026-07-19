# Filesystem / Compose / Git / Storage Audit — 2026-07-19

Read-only inventory. Full machine-readable data: `/apps/del/data/audit/filesystem.json`.

## Counts

| Metric | Value |
|---|---|
| Compose files found (/apps, /opt, /srv, /var/www, /usr/local, /home, /root; maxdepth 4) | 277 |
| Project directories inventoried (/apps/* top-level + /srv/apps, /var/www, /opt project dirs) | 271 |
| Dirs with compose file(s) at top level | 128 |
| Dirs with a `.env` (names/counts only recorded, never contents) | 53 |
| Git repositories | 166 |
| Git repos with uncommitted changes (dirty) | 106 |
| Running docker compose projects | 111 |
| Abandoned candidates (compose dir, no matching running project) | 28 |
| Dirs > 5 GB | 9 |

## Storage

- Root filesystem `/dev/nvme1n1p1`: 1.8T total, 812G used (47%), 929G available. Healthy headroom.
- `/var/lib/docker`: **51.3 GiB**.
- Total `/apps`: **~254 GiB** across 271 top-level entries.
- Largest /apps dirs: immich-app 68.4G, models 48.9G, fileshare2 14.6G, LTX-2 7.6G, netdata 7.4G, ideogram 7.0G, dl 7.0G, flixapp 6.4G, boxy 5.2G, liam 4.8G.
- Logs are modest: /var/log/journal 503 MiB, nginx 63 MiB, audit 35 MiB — nothing runaway.

## Notable findings

1. **`/data` does not exist on this host**, yet docker has a registered compose project `fireshare_migrated` whose config file is `/data/apps/migrated/fireshare/docker-compose.generated.yml` and it reports `running(1)`. The container runs but its compose file path is gone — a stale/broken migration remnant alongside the live `fireshare` project at `/apps/fireshare` (duplicate deployment, exactly the pattern flagged in the task).
2. **28 abandoned candidates** — dirs in /apps with compose files but no matching running compose project, including: baserow, flagsmith, kestra, wanwu, zabbix, netdata (7.4G), poco-claw, dockhand, nginx-ui, monitoring, notes-dashboard, and the whole Cap family (Cap, cap3, cap5, cap_v2). Some may run under a different project name; treat as candidates, not verdicts.
3. **Cap sprawl**: 7 similarly-named dirs (Cap, cap3, cap4, cap42, cap4l, cap5, cap_v2) — only `cap4` has a running compose project. Other duplicate clusters: b64pdf/b64pdf2 (both running), fileshare/fileshare2 (14.6G in fileshare2), scrcpy/scrcpy2, /apps/ui vs /apps/glmflix/ui (identical compose, both present; `ui` is the running one), zublo in /apps (running) vs /srv/apps/zublo (data dir), Flussonic-2 + flussonic2403 + /opt/flussonic, img2/img3/zimg/g3img.
4. **106 of 166 git repos are dirty** — nearly two-thirds of repos have uncommitted changes. Full list in `flags.dirty_git_repos` in the JSON.
5. `/apps/models` (48.9G) and `/apps/LTX-2` (7.6G) are model-weight storage — biggest reclaim targets after immich media if space is ever needed.
6. Compose projects `docker` (from /apps/kanbu/docker + /apps/karakeep/docker — two unrelated apps sharing one project name because of the generic dir name) and `compose` (/apps/komodo/compose) have collision-prone project names.
7. /opt holds non-compose deployments: flussonic, affine, postgres, typesense, pb_data, kanbu-backups. /var/www holds static sites (aidocs.bjk.ai, bjk-dashboard, cap-static, certbot, html). /home/bitwarden is empty; bitwarden actually lives at /apps/bitwarden/bwdata (running, 11 containers).
