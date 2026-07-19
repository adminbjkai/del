# Docker Audit Notes — 2026-07-19

Read-only inventory. Full data: `/apps/del/data/audit/docker.json`.
Raw outputs: `raw-docker-version.txt`, `raw-docker-info.txt`, `raw-docker-system-df.txt` (same dir).

## Counts

| Resource | Count | Notes |
|---|---|---|
| Containers | 212 (212 running, 0 stopped) | 1 health `starting` (docker-api-1), 93 healthy, 118 no healthcheck |
| Images | 302 repo:tag rows (299 unique per df) | 3 dangling `<none>` |
| Volumes | 160 | 65 with 0 containers attached (orphan candidates) |
| Networks | 109 | incl. default bridge/host/none; 0 empty non-default networks |
| Compose projects | 111 | 3 containers NOT compose-managed |

## Disk (docker system df)

- Images: 323.5 GB total, 76.97 GB reclaimable (23%)
- Containers: 12.08 GB writable layers
- Local volumes: 52.42 GB, 19.69 GB reclaimable (37%)
- Build cache: 101.6 GB across 879 entries, 0 active — largest single reclaim target
- Largest volumes: cap4_minio_data 25.83 GB, cap_v2_minio_data 6.66 GB, openshell-cluster-nemoclaw 4.74 GB, netdata_netdatacache 3.40 GB

## Non-compose containers (manually run)

- `netmuxd` (netmuxd-local, restart=always, host network)
- `anisette` (dadoum/anisette-v3-server, restart=always)
- `2fauth` (2fauth/2fauth, restart=unless-stopped)

## Compose labels pointing at missing config files

- Project `bitwarden` -> `/apps/bitwarden/bwdata/docker/docker-compose.yml` (missing)
- Project `fireshare_migrated` -> `/data/apps/migrated/fireshare/docker-compose.generated.yml` (missing)

These containers cannot be recreated with `docker compose up` from their labeled paths.

## Shared resources across projects

**Networks shared by >1 compose project (4):**
- `rowboat_net` — deck-renderer, llm-proxy, rowboat (6 containers)
- `runtipi_tipi_main_network` — fireshare_migrated, runtipi
- default `bridge` — jsoncrack, mtxt (+ non-compose anisette, 2fauth)
- `host` — beszel, bjk-ai-flix, filebrowser, memos, notesnook (+ netmuxd)

**Images shared by >1 container** (17 repo:tags), mostly common bases reused across projects: mariadb:10.11, redis:latest, redis:7.4-alpine, postgres:14, pgvector/pgvector:pg17, mongo:8, mongo:latest, nginx:alpine, meilisearch v1.41.0, plus app images fireshare, opensparrow, gongyu, glean-backend, alpine-chrome.

**Volumes shared across projects:** none.

## Orphan candidates

- 65 volumes mounted by 0 containers (list in docker.json, `orphan_candidate: true`). 13 are anonymous 64-hex volumes; named examples: `2_pg_db_data`, `2_pg_nc_data`, `appwrite_appwrite-*` — some look like data from removed/renamed stacks. Verify before any cleanup (this audit made no changes).
- 3 dangling images, and notably **all 3 dangling images are still in use by running containers**:
  - `16bc17c64a57` -> linkwarden-postgres-1, paca-postgres-1, cap4-postgres-1
  - `20edbde7749f` -> glean-postgres
  - `a3dff78d8762` -> moo-tasks-db, plainpad-mysql-1
  These containers run on untagged image layers (their tag was pulled over, e.g. postgres/mysql updated). A recreate would silently jump versions; an image prune would NOT remove them while containers exist, but this is a drift signal.

## Other observations

- All 212 containers are running; zero stopped/exited containers (unusually clean).
- Restart policies: 165 unless-stopped, 46 always, 1 `no` (`blinko-postgres` — running and healthy, but it will not come back automatically after a daemon restart or reboot).
- `docker-api-1` health is `starting` at audit time (still warming up or flapping its healthcheck).
- Build cache (101.6 GB, 0 active) dominates reclaimable space.
- No secrets recorded anywhere: env vars stored as names only.
