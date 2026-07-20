"""Correlation engine: groups discovered Resources into applications and scores
the evidence for each app<->resource association, per docs/ARCHITECTURE.md
"Confidence scoring" and docs/INTERFACES.md correlate.py contract.

Grouping seed = compose project label (from docker_src containers), then
compose_src's own compose_project resources fill in stopped/orphaned projects.
Non-compose containers become their own single-container apps. Everything else
(nginx, systemd, cron, directories, volumes, images, networks, bind mounts) is
attached by matching evidence. Manifest entries always win (level=manual) and
can exclude/mark-shared any resource. Finally, any resource associated with
>=2 apps is marked shared=True on every one of those associations.
"""
from __future__ import annotations

import difflib
import logging
import re

from del_app.config import get_settings
from del_app.manifests import Manifest
from del_app.models import Association, AppRecord, Evidence, Resource

logger = logging.getLogger("del_app.correlate")

NAME_SIMILARITY_THRESHOLD = 0.72


def _slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s or "app"


def _is_broad_root(path: str) -> bool:
    """True for paths too broad to be ownership evidence (a container
    bind-mounting /apps does not own every project under /apps)."""
    try:
        roots = set(get_settings().scan_roots)
    except Exception:
        roots = {"/apps", "/data/apps", "/opt", "/srv", "/var/www"}
    broad = roots | {"/", "/home", "/etc", "/var", "/usr", "/data", "/root", "/tmp", "/mnt", "/media"}
    p = path.rstrip("/") or "/"
    return p in {r.rstrip("/") or "/" for r in broad}


def _level_for_confidence(confidence: int) -> str:
    if confidence >= 95:
        return "confirmed"
    if confidence >= 80:
        return "high"
    if confidence >= 60:
        return "probable"
    if confidence >= 30:
        return "possible"
    return "unrelated"


def _removal_eligible(level: str, shared: bool, excluded: bool) -> str:
    if excluded:
        return "blocked"
    if shared:
        return "blocked"
    if level in ("confirmed", "high", "manual"):
        return "safe"
    if level == "probable":
        return "uncertain"
    return "blocked"


def _recommended_action(resource_type: str) -> str:
    return {
        "container": "stop_and_remove_container",
        "image": "remove_image",
        "volume": "remove_volume",
        "network": "remove_network",
        "bind_mount": "review_bind_mount_data",
        "compose_project": "compose_down",
        "nginx_site": "remove_nginx_site",
        "systemd_unit": "disable_and_remove_unit",
        "systemd_timer": "disable_and_remove_unit",
        "cron_entry": "remove_cron_entry",
        "directory": "delete_directory",
        "git_repo": "delete_directory",
        "env_file": "delete_directory",
        "process": "terminate_process",
        "tmux_session": "kill_tmux_session",
        "port": "no_action",
    }.get(resource_type, "review_manually")


class _AppBuilder:
    """Mutable accumulator for one app's evidence, keyed by resource_key so
    repeated evidence merges instead of duplicating associations."""

    def __init__(self, slug: str, name: str, kind: str):
        self.slug = slug
        self.name = name
        self.kind = kind
        self.status = "unknown"
        self.domains: set[str] = set()
        self.ports: set[int] = set()
        self.assocs: dict[tuple[str, str], Association] = {}  # (resource_type, resource_key)
        self.dir_paths: set[str] = set()  # working dirs / bind-mount sources associated so far

    def add(
        self,
        resource: Resource,
        confidence: int,
        ownership: str,
        data_loss_risk: str,
        evidence: list[Evidence],
        excluded: bool = False,
    ) -> None:
        level = _level_for_confidence(confidence)
        existing = self.assocs.get((resource.type, resource.key))
        if existing and existing.confidence >= confidence:
            # keep the stronger evidence; still merge evidence list for audit trail
            existing.evidence.extend(evidence)
            return
        self.assocs[(resource.type, resource.key)] = Association(
            resource_key=resource.key,
            resource_type=resource.type,
            confidence=confidence,
            level=level,
            ownership=ownership,
            shared=False,
            data_loss_risk=data_loss_risk,
            removal_eligible=_removal_eligible(level, False, excluded),
            recommended_action=_recommended_action(resource.type),
            evidence=evidence,
            excluded=excluded,
        )


def build_apps(
    resources: list[Resource], manifests: dict[str, Manifest]
) -> list[tuple[AppRecord, list[Association]]]:
    try:
        protected_apps = set(get_settings().protected_apps)
    except Exception:
        protected_apps = {"del"}

    by_type: dict[str, list[Resource]] = {}
    for r in resources:
        by_type.setdefault(r.type, []).append(r)

    containers = by_type.get("container", [])
    images = by_type.get("image", [])
    volumes = by_type.get("volume", [])
    networks = by_type.get("network", [])
    bind_mounts = by_type.get("bind_mount", [])
    compose_projects = by_type.get("compose_project", [])
    nginx_sites = by_type.get("nginx_site", [])
    systemd_units = by_type.get("systemd_unit", [])
    systemd_timers = by_type.get("systemd_timer", [])
    cron_entries = by_type.get("cron_entry", [])
    directories = by_type.get("directory", [])
    git_repos = by_type.get("git_repo", [])
    env_files = by_type.get("env_file", [])
    ports = by_type.get("port", [])
    processes = by_type.get("process", [])

    apps: dict[str, _AppBuilder] = {}
    container_slug: dict[str, str] = {}  # container name -> slug

    # --- Step 1: seed apps from compose project labels ---------------------
    project_containers: dict[str, list[Resource]] = {}
    standalone_containers: list[Resource] = []
    for c in containers:
        proj = c.data.get("compose_project")
        if proj:
            project_containers.setdefault(proj, []).append(c)
        else:
            standalone_containers.append(c)

    for proj_name, conts in project_containers.items():
        slug = _slugify(proj_name)
        app = apps.setdefault(slug, _AppBuilder(slug, proj_name, "compose"))
        working_dirs = set()
        for c in conts:
            container_slug[c.key] = slug
            app.add(
                c,
                confidence=100,
                ownership="exclusive",
                data_loss_risk="none",
                evidence=[Evidence(source="docker", statement=f"compose project label = {proj_name}", weight=100)],
            )
            for p in c.data.get("published_ports", []) or []:
                app.ports.add(p)
            wd = c.data.get("compose_working_dir")
            if wd:
                working_dirs.add(wd)
                app.dir_paths.add(wd)
            if any((c.data.get("state") or "").lower() == "running" for _ in [c]):
                app.status = "running"

    for c in standalone_containers:
        slug = _slugify(c.key)
        app = apps.setdefault(slug, _AppBuilder(slug, c.key, "container"))
        container_slug[c.key] = slug
        app.add(
            c,
            confidence=100,
            ownership="exclusive",
            data_loss_risk="none",
            evidence=[Evidence(source="docker", statement="standalone container, no compose project label", weight=100)],
        )
        for p in c.data.get("published_ports", []) or []:
            app.ports.add(p)
        if (c.data.get("state") or "").lower() == "running":
            app.status = "running"

    # --- Step 1b: ports/processes owned by a container via cgroup match ----
    # (needed for host-network containers, which have no published port
    # mapping: their listening port is only discoverable by tracing the
    # listening pid's cgroup back to the owning container). Attaching the
    # port resource here both adds it to app.ports (Host ports column) and
    # gives it an association so it doesn't show up as an orphan.
    port_owner_container: dict[int, str] = {}
    for p in ports:
        owner = p.data.get("container")
        if not owner or owner not in container_slug:
            continue
        port_owner_container[p.data.get("port")] = owner
        slug = container_slug[owner]
        app = apps[slug]
        app.ports.add(p.data.get("port"))
        app.add(
            p,
            confidence=90,
            ownership="exclusive",
            data_loss_risk="none",
            evidence=[Evidence(
                source="proc_src",
                statement=f"listening port owned by container {owner} (cgroup match)",
                weight=90,
            )],
        )

    for pr in processes:
        owner = pr.data.get("container")
        if not owner or owner not in container_slug:
            continue
        slug = container_slug[owner]
        apps[slug].add(
            pr,
            confidence=90,
            ownership="exclusive",
            data_loss_risk="none",
            evidence=[Evidence(
                source="proc_src",
                statement=f"process owned by container {owner} (cgroup match)",
                weight=90,
            )],
        )

    # --- Step 2: compose_src projects not matched by a running container's
    #     project label become their own (stopped/orphaned) app -------------
    running_project_slugs = {_slugify(p) for p in project_containers}
    for cp in compose_projects:
        working_dir = cp.data.get("working_dir")
        name_candidates = [cp.data.get("declared_name"), cp.display]
        matched_slug = None
        for cand in name_candidates:
            if cand and _slugify(cand) in apps:
                matched_slug = _slugify(cand)
                break
        if matched_slug is None and working_dir:
            for slug, app in apps.items():
                if working_dir in app.dir_paths:
                    matched_slug = slug
                    break
        if matched_slug is None:
            slug = _slugify(cp.display)
            if slug in running_project_slugs or slug in apps:
                matched_slug = slug if slug in apps else None
            if matched_slug is None:
                app = apps.setdefault(slug, _AppBuilder(slug, cp.display, "compose_stopped"))
                app.dir_paths.add(working_dir) if working_dir else None
                matched_slug = slug
        app = apps[matched_slug]
        app.add(
            cp,
            confidence=95,
            ownership="exclusive",
            data_loss_risk="config",
            evidence=[Evidence(source="compose_src", statement=f"compose file found at {working_dir}", weight=95)],
        )
        if working_dir:
            app.dir_paths.add(working_dir)

    # --- Step 3: images (used by app containers; shared handled globally) --
    for img in images:
        containers_using = img.data.get("containers_using", []) or []
        slugs_using = {container_slug[c] for c in containers_using if c in container_slug}
        for slug in slugs_using:
            apps[slug].add(
                img,
                confidence=95,
                ownership="shared" if len(slugs_using) > 1 else "exclusive",
                data_loss_risk="none",
                evidence=[Evidence(source="docker", statement="image used by this app's container(s)", weight=95)],
            )

    # --- Step 4: volumes (compose label -> confirmed; attached container ---
    # -> confirmed) ------------------------------------------------------
    for vol in volumes:
        target_slugs: set[str] = set()
        label_project = vol.data.get("compose_project")
        if label_project and _slugify(label_project) in apps:
            target_slugs.add(_slugify(label_project))
        for c in vol.data.get("containers_using", []) or []:
            if c in container_slug:
                target_slugs.add(container_slug[c])
        for slug in target_slugs:
            apps[slug].add(
                vol,
                confidence=95,
                ownership="shared" if len(target_slugs) > 1 else "exclusive",
                data_loss_risk="data",
                evidence=[Evidence(source="docker", statement="volume labeled/attached to this app's container(s)", weight=95)],
            )

    # --- Step 5: bind mounts (attached to a specific container) -------------
    for bm in bind_mounts:
        c_name = bm.data.get("container")
        slug = container_slug.get(c_name)
        if not slug:
            continue
        apps[slug].add(
            bm,
            confidence=95,
            ownership="exclusive",
            data_loss_risk="data",
            evidence=[Evidence(source="docker", statement=f"bind mount used by container {c_name}", weight=95)],
        )
        if bm.path and not _is_broad_root(bm.path):
            apps[slug].dir_paths.add(bm.path)

    # --- Step 6: networks (compose label -> confirmed; attached container ---
    # -> high, since infra networks are commonly shared) ---------------------
    for net in networks:
        target_slugs: set[str] = set()
        label_project = net.data.get("compose_project")
        if label_project and _slugify(label_project) in apps:
            target_slugs.add(_slugify(label_project))
        for c in net.data.get("attached_containers", []) or []:
            if c in container_slug:
                target_slugs.add(container_slug[c])
        for slug in target_slugs:
            confidence = 95 if label_project and _slugify(label_project) == slug else 85
            apps[slug].add(
                net,
                confidence=confidence,
                ownership="shared" if len(target_slugs) > 1 else "exclusive",
                data_loss_risk="none",
                evidence=[Evidence(source="docker", statement="network labeled/attached to this app's container(s)", weight=confidence)],
            )

    # --- Step 7: directories -------------------------------------------------
    matched_directory_keys: set[str] = set()
    for d in directories:
        for slug, app in apps.items():
            if d.path and d.path in app.dir_paths:
                app.add(
                    d,
                    confidence=95,
                    ownership="exclusive",
                    data_loss_risk="data" if any(d.path == bm_path for bm_path in app.dir_paths) else "config",
                    evidence=[Evidence(source="fs_src", statement="directory matches compose working_dir / bind mount source", weight=95)],
                )
                matched_directory_keys.add(d.key)
            elif d.path and any(d.path.startswith(dp.rstrip("/") + "/") for dp in app.dir_paths):
                app.add(
                    d,
                    confidence=90,
                    ownership="exclusive",
                    data_loss_risk="data",
                    evidence=[Evidence(source="fs_src", statement="directory nested under a known app path", weight=90)],
                )
                matched_directory_keys.add(d.key)

    # --- Step 7b: git repos and env files inside an owned app directory -----
    for pool in (git_repos, env_files):
        for r in pool:
            rp = (r.path or r.key).rstrip("/")
            for slug, app in apps.items():
                for dp in app.dir_paths:
                    d = dp.rstrip("/")
                    if rp == d or rp.startswith(d + "/"):
                        app.add(
                            r,
                            confidence=95,
                            ownership="exclusive",
                            data_loss_risk="config" if r.type == "env_file" else "data",
                            evidence=[Evidence(source="correlate", statement=f"located inside app directory {dp}", weight=95)],
                        )
                        break

    # --- Step 8: nginx sites: proxy port == published host port ------------
    for site in nginx_sites:
        upstream_ports = {u.get("port") for u in site.data.get("upstreams", []) or [] if u.get("port")}
        if not upstream_ports:
            continue
        server_names = site.data.get("server_names", []) or []
        enabled = bool(site.data.get("enabled", site.state == "enabled"))
        stale_copy = bool(site.data.get("stale_copy", False))
        for slug, app in apps.items():
            if not (upstream_ports & app.ports):
                continue
            if not enabled:
                # Non-enabled sites-available files (stale backups, retired
                # copies, or otherwise not serving) are debris worth removing
                # alongside the app, but they must never leak their
                # server_names into app.domains and only reach "probable".
                statement = (
                    "stale sites-available copy, not enabled"
                    if stale_copy
                    else "sites-available copy, not enabled"
                )
                app.add(
                    site,
                    confidence=60,
                    ownership="possible",
                    data_loss_risk="config",
                    evidence=[Evidence(source="nginx_src", statement=statement, weight=60)],
                )
                continue
            matched_ports = sorted(upstream_ports & app.ports)
            host_network_owner = next(
                (port_owner_container[p] for p in matched_ports if p in port_owner_container),
                None,
            )
            confidence = 90 if host_network_owner else 85
            if host_network_owner:
                evidence = [Evidence(
                    source="nginx_src",
                    statement=(
                        f"nginx proxies to 127.0.0.1:{matched_ports[0]} whose listener runs in "
                        f"container {host_network_owner} (host network)"
                    ),
                    weight=90,
                )]
            else:
                evidence = [Evidence(source="nginx_src", statement=f"proxy_pass port {matched_ports} matches published container port", weight=85)]
            name_hit = any(
                _slugify(app.name) in _slugify(sn) or _slugify(sn).split(".")[0] == slug
                for sn in server_names
            )
            if name_hit:
                confidence = 90
                evidence.append(Evidence(source="nginx_src", statement="server_name also matches app name", weight=5))
            app.add(site, confidence=confidence, ownership="exclusive", data_loss_risk="config", evidence=evidence)
            app.domains.update(server_names)

    # --- Step 8b: nginx configs matched by exact server_name -----------------
    # After an app's containers are removed, port matching has nothing to match.
    # Any nginx config file (enabled OR a differently-named / .conf / .bak
    # available copy) whose first server_name label slugifies to EXACTLY the
    # app slug is that app's config — include it so removal is complete and no
    # stale config file is left behind. Exact match only (no fuzzy similarity,
    # which previously caused cross-app pollution). Only ENABLED sites
    # contribute to app.domains; non-enabled copies are config debris.
    for site in nginx_sites:
        enabled = bool(site.data.get("enabled"))
        for sn in site.data.get("server_names", []) or []:
            label = _slugify(sn.split(".")[0])
            app = apps.get(label)
            if app is None or (site.type, site.key) in app.assocs:
                continue
            if enabled:
                conf = 85
                stmt = f"enabled site server_name {sn} exactly matches app slug (no live upstream — app stopped)"
            else:
                conf = 80
                stmt = f"nginx config (not enabled) with server_name {sn} exactly matching app slug — config debris to remove with the app"
            app.add(
                site,
                confidence=conf,
                ownership="exclusive",
                data_loss_risk="config",
                evidence=[Evidence(source="nginx_src", statement=stmt, weight=conf)],
            )
            if enabled:
                app.domains.add(sn)

    # --- Step 9: systemd units: WorkingDirectory/ExecStart under app dir ----
    unit_slug: dict[str, str] = {}
    for unit in systemd_units:
        wd = unit.data.get("working_directory")
        exec_start = unit.data.get("exec_start") or ""
        for slug, app in apps.items():
            hit_path = None
            if wd and any(wd == dp or wd.startswith(dp.rstrip("/") + "/") for dp in app.dir_paths):
                hit_path = wd
            elif app.dir_paths and any(dp in exec_start for dp in app.dir_paths):
                hit_path = next((dp for dp in app.dir_paths if dp in exec_start), None)
            if hit_path:
                app.add(
                    unit,
                    confidence=95,
                    ownership="exclusive",
                    data_loss_risk="config",
                    evidence=[Evidence(source="systemd_src", statement=f"WorkingDirectory/ExecStart path under {hit_path}", weight=95)],
                )
                unit_slug[unit.key] = slug
                break

    for timer in systemd_timers:
        activates = timer.data.get("activates")
        slug = unit_slug.get(activates)
        if slug:
            apps[slug].add(
                timer,
                confidence=95,
                ownership="exclusive",
                data_loss_risk="config",
                evidence=[Evidence(source="systemd_src", statement=f"timer activates {activates}", weight=95)],
            )

    # --- Step 9b: ports/processes owned by a systemd-managed process whose --
    # unit is already matched to an app (cgroup match) ------------------------
    for p in ports:
        unit = p.data.get("systemd_unit")
        slug = unit_slug.get(unit)
        if not slug or (p.type, p.key) in apps[slug].assocs:
            continue
        apps[slug].ports.add(p.data.get("port"))
        apps[slug].add(
            p,
            confidence=85,
            ownership="exclusive",
            data_loss_risk="none",
            evidence=[Evidence(source="proc_src", statement=f"listening port owned by systemd unit {unit} (cgroup match)", weight=85)],
        )

    for pr in processes:
        unit = pr.data.get("systemd_unit")
        slug = unit_slug.get(unit)
        if not slug or (pr.type, pr.key) in apps[slug].assocs:
            continue
        apps[slug].add(
            pr,
            confidence=85,
            ownership="exclusive",
            data_loss_risk="none",
            evidence=[Evidence(source="proc_src", statement=f"process owned by systemd unit {unit} (cgroup match)", weight=85)],
        )

    # --- Step 10: cron entries: command path under app dir ------------------
    for entry in cron_entries:
        referenced = entry.data.get("referenced_paths", []) or []
        for slug, app in apps.items():
            hit = next(
                (dp for dp in app.dir_paths for rp in referenced if rp == dp or rp.startswith(dp.rstrip("/") + "/")),
                None,
            )
            if hit:
                apps[slug].add(
                    entry,
                    confidence=80,
                    ownership="exclusive",
                    data_loss_risk="config",
                    evidence=[Evidence(source="cron_src", statement=f"cron command references path under {hit}", weight=80)],
                )

    # --- Step 11: name-similarity fallback for anything unmatched so far ----
    for pool in (directories, git_repos, env_files):
        for r in pool:
            base = r.display
            for slug, app in apps.items():
                if (r.type, r.key) in app.assocs:
                    continue
                ratio = difflib.SequenceMatcher(None, _slugify(base), slug).ratio()
                if ratio >= NAME_SIMILARITY_THRESHOLD:
                    confidence = min(50, int(ratio * 60))
                    app.add(
                        r,
                        confidence=confidence,
                        ownership="possible",
                        data_loss_risk="data" if r.type != "env_file" else "config",
                        evidence=[Evidence(source="correlate", statement=f"name similarity {ratio:.2f} to app '{slug}' (unconfirmed)", weight=confidence)],
                    )

    # --- Step 12: manifests always win --------------------------------------
    for slug, manifest in manifests.items():
        app = apps.get(slug)
        if app is None:
            app = apps.setdefault(slug, _AppBuilder(slug, manifest.name or slug, "manifest"))
        if manifest.name:
            app.name = manifest.name
        if manifest.status:
            app.status = manifest.status
        app.domains.update(manifest.domains)

        by_key = {r.key: r for r in resources}
        for key_list in (
            manifest.compose, manifest.host_paths, manifest.systemd_units,
            manifest.nginx, manifest.cron, manifest.repositories,
        ):
            for key in key_list:
                r = by_key.get(key)
                if r is None:
                    continue
                app.add(
                    r,
                    confidence=100,
                    ownership="exclusive",
                    data_loss_risk=app.assocs[(r.type, r.key)].data_loss_risk if (r.type, r.key) in app.assocs else "config",
                    evidence=[Evidence(source="manifest", statement="manually declared in manifest", weight=100)],
                )
                app.assocs[(r.type, r.key)].level = "manual"

        for key in manifest.shared:
            for (t, k), a in app.assocs.items():
                if k == key:
                    a.shared = True
        for key in manifest.excluded:
            for (t, k), a in app.assocs.items():
                if k == key:
                    a.excluded = True

    # --- Step 13: global shared-resource detection --------------------------
    # 13a: a weak name-similarity claim (<60) loses outright to another app's
    # strong (>=80) claim on the same resource — drop it instead of letting it
    # pollute the strong owner with a bogus "shared/blocked" state.
    strongest: dict[tuple[str, str], int] = {}
    for slug, app in apps.items():
        for key, assoc in app.assocs.items():
            if assoc.confidence > strongest.get(key, 0):
                strongest[key] = assoc.confidence
    for slug, app in apps.items():
        weak = [key for key, assoc in app.assocs.items()
                if assoc.confidence < 60 and strongest.get(key, 0) >= 80
                and assoc.confidence < strongest[key]]
        for key in weak:
            del app.assocs[key]

    resource_owners: dict[tuple[str, str], set[str]] = {}
    for slug, app in apps.items():
        for key in app.assocs:
            resource_owners.setdefault(key, set()).add(slug)

    for slug, app in apps.items():
        for key, assoc in app.assocs.items():
            if len(resource_owners.get(key, set())) > 1:
                assoc.shared = True
            assoc.removal_eligible = _removal_eligible(assoc.level, assoc.shared, assoc.excluded)

    # --- Build final AppRecord + Association list per app -------------------
    result: list[tuple[AppRecord, list[Association]]] = []
    for slug, app in apps.items():
        record = AppRecord(
            slug=slug,
            name=app.name,
            status=app.status,
            kind=app.kind,
            protected=slug in protected_apps,
            domains=sorted(app.domains),
            ports=sorted(app.ports),
        )
        result.append((record, list(app.assocs.values())))

    return result
