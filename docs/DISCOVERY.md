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
| confirmed | 95–100 | included in a plan automatically (unless excluded, or shared and unapproved) |
| high | 80–94 | included in a plan automatically (unless excluded, or shared and unapproved) |
| probable | 60–79 | **never** included; always preserved with a warning. Raise it via a manifest entry to make it removable |
| possible | 30–59 | **always blocked**; never auto-removable |
| unrelated | <30 | not associated |
| manual | `source = 'manual'` | treated exactly like confirmed/high |

Example evidence weights: a Compose project label match is `confirmed`; an nginx
`proxy_pass` port matching a container's published port is `high`; name-similarity
alone is `possible` and is never sufficient by itself for automated removal.

### What "requires approval" actually means

`planner._classify` returns `(level, is_step=False, "requires per-resource
approval")` for **every** `probable` association, unconditionally — it never looks
at `approved_by_user` for them. So approving a `probable` row on the app detail
page does not turn it into a plan step; it stays in *Preserved* with that warning.
To make a `probable` association removable, declare the resource in the app's
manifest, which raises it to confidence 100.

The per-row **approve** action does exactly two things:

1. It unblocks a `confirmed`/`high`/`manual` association that is flagged `shared`.
   Shared rows are otherwise held back with "shared resource requires per-resource
   approval"; approving clears that for this app's plan.
2. It approves an individual named **volume** for `volume_rm` (equivalently, the
   per-volume checkbox on the plan form does the same thing).

It also clears `excluded` on the row it touches.

### Manifest entries display as `confirmed`, not `manual`

`correlate.py` sets `level = "manual"` on a manifest-declared association in
memory, but `scanner.py` writes the literal string `"correlate"` into
`associations.source` for every row it persists — the in-memory `level` is not
stored at all. Both `_level()` functions (`planner.py`, `web/routes.py`) derive
`manual` solely from `source == 'manual'`, so a manifest entry — confidence 100 —
reads and classifies as `confirmed`. Behaviourally identical for removal; only the
badge differs.

## Correlation rules (`correlate.py`)

```python
def build_apps(resources: list[Resource], manifests: dict[str, Manifest]) -> list[tuple[AppRecord, list[Association]]]
```

- **Grouping seed**: the Compose project label. Every resource carrying a given
  compose project label seeds one application.
- **Attachment rules**, applied after seeding:
  - systemd units attach via `WorkingDirectory`/`ExecStart` path matching the app's
    directory, followed immediately by the cgroup pass that attributes listening
    ports and processes to their owning unit.
  - nginx sites attach via `proxy_pass` port → the app's published container port
    (or, for a systemd app, the port the pass above just registered).
  - directories attach via the compose `working_dir` or bind mounts referenced by
    the app's containers.
  - cron entries attach via command path matching the app's directory.
- **Pass order matters and is fixed.** The systemd-unit pass (7c) and the
  unit/cgroup port+process pass (7d) run *before* nginx matching (8, 8b). Nginx
  matching compares `proxy_pass` against `app.ports`, and for a non-Docker,
  systemd-managed app the only thing that populates that set is pass 7d — so when
  those passes ran after nginx matching, a systemd app could never win its own
  vhost unless its `ExecStart` happened to spell the port out, and the vhost went
  to whichever container claimed the port first.
- **Compose files in a sub-directory attach to the enclosing app.** Compose
  projects are processed shallowest working-directory first, so a parent project
  has already created its app before a nested compose file is considered; a nested
  file then attaches to the deepest matching app directory
  (`/apps/karakeep/docker/compose.yml` → `karakeep`). A directory basename that
  names a *layout role* rather than an application — `docker`, `deploy`, `compose`,
  `server`, `backend`, `config`, `stack`, and so on — is never used as an app name;
  correlation anchors on the owning project directory instead. Without these two
  rules, phantom apps called `docker`, `deploy` and `compose-project` accumulated
  many unrelated projects' compose roots at confidence 95 and reported them safe to
  delete.
- **A compose file matched by name is checked against its own project root
  before being merged.** Step 2 first tries to match a compose project to an
  existing app by its declared name or directory basename. Before trusting
  that match, `_foreign_project_root()` resolves the compose file's *own*
  top-level project directory (`{scan_root}/{first-component}`) and checks
  whether that directory already belongs to a **different** app. If it does —
  an archived clone at `/apps/agyinstall/boxy/docker-compose.yml` matching the
  real `/apps/boxy` app purely by name — the compose project is **not** merged
  into the same-named app at full confidence. Instead it is attached to the
  owning tree's app (`agyinstall`, seeding a placeholder application for it if
  one doesn't exist yet) at confidence 55 / `possible` / `shared`, with an
  evidence statement naming both the foreign owner and the same-named app it
  did not merge into.
- **An app claims its own project root — and only its own.** When an app's compose
  file or directory sits one level down, the enclosing project root is attributed to
  that app directly, instead of falling through to the name-similarity fallback at
  50 (below the removable threshold) and being left on disk after a removal. The
  parent's basename must **slugify to the app's own slug** for this to apply. That
  restriction matters: an app with an archived clone inside another project's tree
  (`/apps/agyinstall/banban`) would otherwise walk up and claim that unrelated
  180 GB archive root, marking it shared and blocking its real owner from ever being
  removed cleanly. The slug comparison also makes the match case-insensitive, so
  `/apps/2FAuth` resolves to app `2fauth`. This directory-walk-up guard and the
  name-match guard above (`_foreign_project_root`) cover the two different ways a
  nested clone could otherwise get absorbed into the wrong app.
- **Docker's built-in networks are never associated.** `bridge`, `host` and `none`
  always exist and cannot be removed; associating them would put
  `network_rm bridge` in a plan as soon as one app happened to be attached.
- **Non-Docker (pure-systemd) apps are seeded as first-class applications too**,
  not just Docker/Compose ones — a custom (`is_custom`) systemd unit whose
  `WorkingDirectory`/`ExecStart` resolves to a directory directly under a scan
  root (e.g. `/apps/xtr`) seeds its own application (`kind="systemd"`) when no
  Docker app already claims that slug. Its dir path then feeds the ordinary
  directory/git-repo/env-file/nginx-by-port/cron attachment rules above, so the
  rest of the app (config, nginx site, cron entries) is picked up automatically
  from that one seed. This is what makes systemd-only services like `xtr`,
  `htmls`, or `ppv` show up as ordinary applications alongside Docker apps
  instead of being invisible or appearing only as orphaned resources.
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
  traces a listening port's pid back to its owning container by matching the
  socket's inode against `/proc/<pid>/fd` (using the `list_listeners` helper op to
  read `ss -lntp` as root, since `del-web` runs `NoNewPrivileges` with no sudo); an
  nginx site proxying to that port is then attached to the resolved container's app
  at `high` confidence, with evidence naming the container and noting
  "(host network)". **The inode match is exact and there is no fallback.** An
  earlier version fell back to "this uid owns exactly one host-network container,
  so give it every port that uid listens on" — on a host where uid 1000 runs every
  systemd-managed app, that gave one container the whole box, including DEL's own
  port 8075, and marked other apps' live vhosts safe to delete. Unresolved is now
  the correct answer.
- **Nginx config debris matched by exact `server_name`**: once an app's containers
  are stopped, proxy-port matching has nothing left to match against. Any nginx
  config file — enabled or not, including differently-named/`.bak`/disabled
  `sites-available` copies — whose first `server_name` label slugifies to
  *exactly* the app's slug is still attached to that app, so removal deletes the
  leftover config file too and doesn't leave stale debris. This is an exact-slug
  match only; fuzzy/partial matches are never used here. An exact `server_name`
  match **upgrades** a prior weak port-match claim on the same file (e.g. a
  `sites-available` copy the app already held at `probable`/60 via port
  guessing) rather than being skipped because an association already exists —
  this closes the gap where an app's own stale `sites-available` copy used to
  survive removal as leftover debris; it is now always removal-eligible along
  with the rest of the app.
- **Manifest override**: entries in `/apps/del/manifests/*.yaml` override or augment
  automatic correlation at confidence 100. They display as `confirmed` (see
  "Manifest entries display as `confirmed`" above).
- **Shared-resource detection**: if a resource is associated with more than one
  application, `shared=True` is set on all of its associations. Shared resources
  are **blocked from removal until explicitly approved** per-application — removing
  one app's plan will never silently take a resource another app depends on. This
  is the one case where the per-row *approve* action changes what a plan contains.

## Persistence: what a scan rewrites

`scanner.run_scan()` does not merge into the association table — for every app it
re-correlated, it runs `DELETE FROM associations WHERE app_id = ?` and re-inserts
the freshly correlated set. It then deletes the associations of every application
whose `last_seen` is older than this scan.

<a id="approvals-are-not-durable"></a>
**Consequence, and it is important: per-resource `approve`, `exclude` and
`mark-shared` do not survive a scan.** Those actions write `approved_by_user`,
`excluded` and `shared` onto an association row, and the next scan replaces that
row. Use them immediately before building a plan. For a correction that must
persist, edit the app's manifest — `shared:` and `excluded:` are manifest fields
precisely because correlation re-derives everything else from scratch.

The stale-owner purge exists because an app removed from the host used to keep its
associations forever. Those rows point at resources that are still live, so they
went on claiming ownership: real leftovers were hidden from Orphans, and surviving
apps looked "shared" with a ghost, which blocks a clean removal. The first run of
the purge removed 1,171 rows and took stale-owner associations from 152 to 0.

## Orphans: what counts as "no owner"

A resource is an orphan candidate when **no** association on it satisfies all of:
the association is not excluded, its confidence is **at least 60** (`probable` or
better), and the owning application is itself present in the latest completed
scan.

Both extra conditions were added deliberately:

- Without the "owner still exists" condition, a resource whose only owner was an
  app removed several scans ago stayed permanently hidden from the page whose
  entire purpose is to surface exactly that leftover.
- Without the confidence floor, a sub-60 `difflib` name-similarity guess (the Step
  11 fallback) was enough to hide a resource while being far too weak to make it
  removable anywhere — a dead zone where the resource was neither actionable nor
  cleanable.

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
associations regardless of what correlation finds).

A manifest entry is the only way to make a `probable` or `possible` association
removable — correlation never promotes them on its own, and neither does the
per-row *approve* button (see "What 'requires approval' actually means"). It is
also the only correction that survives a rescan, since a scan rewrites every
association row it touches.

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

## Port registry and port-conflict detection

`scripts/gen-registry.py` reads the latest scan straight out of `del.db` (SQLite,
read-only) and writes `docs/PORT-REGISTRY.md` — the transparent map of every
enabled subdomain to its host port, correlated backend app, deploy type
(`compose` vs `systemd`), and container port mapping. It is **gitignored** and
regenerated on demand, never hand-edited:

```bash
/apps/del/scripts/gen-registry.py     # or scripts/gen-registry.sh
```

The generated file has three sections:
- The main table — every live subdomain with its backend and deploy type.
- **Needs attention** — subdomains with no backend attributed, or a dead
  upstream (nothing actually listening on the proxied port).
- **Port CONFLICTS** vs **Shared ports (aliases)** — a host port proxied by
  more than one *distinct* app is a real conflict (only one of them can
  actually be served; the others are misconfigured leftovers competing for the
  same port). A host port proxied by multiple subdomains that all resolve to
  the *same* app is a normal alias, not a conflict, and is reported separately.

## Disabled/inactive unit capture (2026-07-20)
systemd discovery now captures custom unit *files* under `/etc/systemd/system`
even when the unit is disabled/inactive (not in `systemctl list-units`), by
scanning the unit-file directories and showing each individually (batched
`systemctl show` intermittently drops inactive records). This makes non-Docker
apps whose service is stopped (e.g. `htmls`, `ppv`) first-class instead of
invisible.
