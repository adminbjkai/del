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
- **Manifest override**: entries in `/apps/del/manifests/*.yaml` override or augment
  automatic correlation, and are recorded at `level=manual` or `confirmed`.
- **Shared-resource detection**: if a resource is associated with more than one
  application, `shared=True` is set on all of its associations. Shared resources
  are **blocked from removal until explicitly approved** per-application — removing
  one app's plan will never silently take a resource another app depends on.

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
