# DEL — Discovery

Discovery is the read-only data-collection phase of a scan (`del_app.scanner.run_scan`).
Every source module lives in `backend/del_app/discovery/` and implements the same
contract:

```python
def collect() -> list[Resource]   # read-only, never raises on partial failure (log+skip), strips env VALUES
```

## Sources

| Module | Collects | Method |
|---|---|---|
| `docker_src.py` | containers, images, volumes, networks | `docker inspect`/`ps`/`images`/`volume ls`/`network ls` via subprocess JSON (does not require direct socket access) |
| `compose_src.py` | compose projects | scans `settings.scan_roots` for compose files, parses with `yaml` |
| `nginx_src.py` | nginx sites | parses `/etc/nginx/sites-enabled` and `sites-available` |
| `systemd_src.py` | units, timers | `systemctl show` / `list-units` / `list-timers`, plus reads custom unit files |
| `proc_src.py` | listening sockets, processes, sessions | `ss -lntp`, `ps`, `tmux ls` |
| `cron_src.py` | cron entries | `/etc/cron.d` files, user crontabs |
| `fs_src.py` | project directories, git repos | scans `scan_roots`, fast `du` estimate, git info, `.env` variable **names only** |

`scan_roots` (from `del.toml`): `/apps`, `/data/apps`, `/opt`, `/srv`, `/var/www`.

All sources are read-only: no `start`/`stop`/`rm`/`reload` is ever called during
discovery. Environment variable values are stripped at the collection layer —
`fs_src.py` and others record `.env` variable names only, never values.

## Resource types

`container | image | volume | network | compose_project | nginx_site |
systemd_unit | systemd_timer | cron_entry | process | port | directory | git_repo |
env_file | tmux_session | bind_mount`

Each `Resource` has `type`, `key`, `display`, `path`, `state`, `data` (dict).

## Evidence and confidence levels

Every `Association` between an application and a resource carries a list of
`Evidence` items (`source`, `statement`, `weight`) that justify its confidence
score.

| Level | Score range | Removal eligibility |
|---|---|---|
| confirmed | 95–100 | eligible for automated inclusion in a plan |
| high | 80–94 | eligible for automated inclusion in a plan |
| probable | 60–79 | requires explicit user approval per-resource before inclusion |
| possible | 30–59 | **always blocked** until manually confirmed by the operator; never auto-removable |
| unrelated | <30 | not associated |
| manual | (user-assigned) | set by a manifest entry; treated as confirmed/high depending on entry |

Example evidence weights: a Compose project label match is `confirmed`; an nginx
`proxy_pass` port matching a container's published port is `high`; name-similarity
alone is `possible` and is never sufficient by itself for automated removal.

## Correlation rules (`correlate.py`)

```python
def build_apps(resources: list[Resource], manifests: dict[str, Manifest]) -> list[tuple[AppRecord, list[Association]]]
```

- **Grouping seed**: the Compose project label. Every resource carrying a given
  compose project label seeds one application.
- **Attachment rules**, applied after seeding:
  - nginx sites attach via `proxy_pass` port → the app's published container port.
  - systemd units attach via `WorkingDirectory`/`ExecStart` path matching the app's
    directory.
  - directories attach via the compose `working_dir` or bind mounts referenced by
    the app's containers.
  - cron entries attach via command path matching the app's directory.
- **Broad-root bind mounts are excluded from ownership evidence** — a bind mount of
  a shared root (e.g. `/apps` or `/data` itself, rather than a specific app
  subdirectory) does not count as evidence that an app owns that path; this
  prevents one over-broad mount from making everything under it look "owned."
- **Networks attach by compose label or by attached-container name** — a Docker
  network is seeded by its compose project label (confirmed) or by the **names**
  of the containers attached to it (high, `docker_src.py` stores container names,
  not just ids, so the mapping survives container recreation). A network attached
  to containers from more than one application is `shared=True` and preserved
  unless explicitly approved for a given app's removal.
- **Host-network containers correlate via listener ownership** — a container run
  with `--network host` publishes no distinct container port, so `proc_src.py`
  traces a listening port's pid back to its owning container via
  `/proc/<pid>/cgroup` (falling back to the `list_listeners` helper op under
  `NoNewPrivileges`, since sudo isn't available there); an nginx site proxying to
  that port is then attached to the resolved container's app at `high` confidence,
  with evidence naming the container and noting "(host network)".
- **Nginx config debris matched by exact `server_name`**: once an app's containers
  are stopped, proxy-port matching has nothing left to match against. Any nginx
  config file — enabled or not, including differently-named/`.bak`/disabled
  `sites-available` copies — whose first `server_name` label slugifies to
  *exactly* the app's slug is still attached to that app, so removal deletes the
  leftover config file too and doesn't leave stale debris. This is an exact-slug
  match only; fuzzy/partial matches are never used here.
- **Manifest override**: entries in `/apps/del/manifests/*.yaml` override or augment
  automatic correlation, and are recorded at `level=manual` or `confirmed`.
- **Shared-resource detection**: if a resource is associated with more than one
  application, `shared=True` is set on all of its associations. Shared resources
  are **blocked from removal until explicitly approved** per-application — removing
  one app's plan will never silently take a resource another app depends on.

## Latest-scan-only views

The **Applications** list and an application's **detail page** only show
applications/resources present as of the *most recent* scan by default (filtered
on `last_seen`/`state` against the latest `scans.id`) — a resource or app removed
in an earlier scan does not linger in the UI forever. Add `?show=removed` to the
Applications URL to see history including apps no longer present. An app's detail
page shows a "not present in the latest scan" banner instead of hiding it
outright, so a completed removal is discoverable but not confused with a
currently-installed app. A successful **live** removal job automatically triggers
a rescan (`jobs.py`) so this reflects reality immediately, without the operator
needing to remember to rescan by hand.

## Manifest format

Manifests are YAML files in `/apps/del/manifests/`, one per application, loaded by
`del_app.manifests.load_all()` / written by `save()` / seeded from a scan by
`generate_from_app()`. Schema (fields per the `Manifest` pydantic model):

```yaml
id: del
name: DEL (App Inventory & Uninstaller)
status: active
domains:
  - del.bjk.ai
repositories:
  - /apps/del
host_paths:
  - /apps/del
systemd_units:
  - del-web.service
  - del-helper.service
nginx:
  - /etc/nginx/sites-available/del.bjk.ai
  - /etc/nginx/sites-enabled/del.bjk.ai
notes: |
  Free-text notes. E.g. mark protected apps, note external DNS management, etc.
```

Additional fields supported by the schema (per `models.Manifest`): `compose`
(compose file paths), `cron`, `shared: []` (resource keys explicitly marked
shared), `excluded: []` (resource keys explicitly excluded from this app's
associations regardless of what correlation finds). A manifest entry is the only
way to force `possible`-level or excluded evidence into an eligible association —
correlation itself never promotes `possible` on its own.

## Adding a new detector

1. Create `backend/del_app/discovery/<name>_src.py` implementing:
   ```python
   def collect() -> list[Resource]:
       ...
   ```
   Follow the existing modules' pattern: read-only, catch and log partial
   failures rather than raising, strip any secret/env values before building
   `Resource.data`.
2. Register it in `scanner.py` alongside the other source modules so
   `run_scan()` calls it and folds its output into the resource set passed to
   `correlate.build_apps()`.
3. If the new resource type needs correlation rules beyond generic path/name
   matching, add them to `correlate.py`.
4. Add a `resources: types` entry to `models.py` if the detector introduces a new
   resource `type` string.
5. Add a unit test under `tests/test_discovery.py` following the existing
   per-source test pattern (mock the subprocess/file calls, assert on the
   returned `Resource` list).

## Disabled/inactive unit capture (2026-07-20)
systemd discovery now captures custom unit *files* under `/etc/systemd/system`
even when the unit is disabled/inactive (not in `systemctl list-units`), by
scanning the unit-file directories and showing each individually (batched
`systemctl show` intermittently drops inactive records). This makes non-Docker
apps whose service is stopped (e.g. `htmls`, `ppv`) first-class instead of
invisible.

## Port-conflict detection (registry)
`scripts/gen-registry.py` flags **port conflicts** — one host port proxied by
subdomains of two or more *different* apps (only one backend can actually
serve; the others are misconfigured). Multiple domains resolving to a single
app on one port are reported separately as normal **aliases**, not conflicts.
