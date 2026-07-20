"""Docker discovery source: containers, images, volumes, networks, bind mounts.

Entirely read-only: ``docker ps -aq`` / ``docker images`` / ``docker volume ls`` /
``docker network ls`` to enumerate, then ``docker inspect`` (batched) for detail.
No `docker start/stop/rm/...` is ever called from this module. Env VAR VALUES are
stripped; only var names are kept.
"""
from __future__ import annotations

import json
import logging
import subprocess

from del_app.models import Resource

logger = logging.getLogger("del_app.discovery.docker_src")

TIMEOUT = 30
BATCH_SIZE = 50


def _run(args: list[str], timeout: int = TIMEOUT) -> str:
    """Run a read-only docker CLI command, return stdout text or "" on failure."""
    try:
        proc = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout, check=False
        )
        if proc.returncode != 0:
            logger.warning("docker command failed: %s: %s", args, proc.stderr[:500])
            return ""
        return proc.stdout
    except Exception:
        logger.exception("docker command errored: %s", args)
        return ""


def _env_var_names(env_list: list[str] | None) -> list[str]:
    """Strip VALUES from KEY=VALUE env strings, keep only KEY names."""
    names = []
    for item in env_list or []:
        name = item.split("=", 1)[0]
        names.append(name)
    return names


def _batched(items: list[str], size: int) -> list[list[str]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def _inspect_many(ids: list[str]) -> list[dict]:
    """docker inspect a list of ids/names, batched, tolerant of individual failures."""
    out: list[dict] = []
    for batch in _batched(ids, BATCH_SIZE):
        if not batch:
            continue
        raw = _run(["docker", "inspect", *batch])
        if not raw:
            continue
        try:
            out.extend(json.loads(raw))
        except json.JSONDecodeError:
            logger.warning("docker inspect returned invalid JSON for batch of %d", len(batch))
    return out


def _collect_containers() -> tuple[list[Resource], dict[str, dict]]:
    """Returns (container resources, bind-mount resources merged in), plus a map
    of container name -> compose info used by other collectors (image usage etc.)."""
    resources: list[Resource] = []
    ids_raw = _run(["docker", "ps", "-aq"])
    ids = [i for i in ids_raw.splitlines() if i.strip()]
    if not ids:
        return resources, {}

    containers = _inspect_many(ids)
    container_index: dict[str, dict] = {}

    for c in containers:
        try:
            name = c.get("Name", "").lstrip("/")
            state = c.get("State", {}) or {}
            config = c.get("Config", {}) or {}
            host_config = c.get("HostConfig", {}) or {}
            labels = config.get("Labels") or {}
            compose_project = labels.get("com.docker.compose.project")
            compose_service = labels.get("com.docker.compose.service")
            compose_working_dir = labels.get("com.docker.compose.project.working_dir")
            compose_config_files_raw = labels.get("com.docker.compose.project.config_files")
            compose_config_files = (
                compose_config_files_raw.split(",") if compose_config_files_raw else []
            )

            mounts = c.get("Mounts", []) or []
            bind_mounts = []
            volume_mounts = []
            for m in mounts:
                mtype = m.get("Type")
                if mtype == "bind":
                    bind_mounts.append(
                        {
                            "source": m.get("Source"),
                            "destination": m.get("Destination"),
                            "rw": m.get("RW", True),
                        }
                    )
                elif mtype == "volume":
                    volume_mounts.append(
                        {
                            "name": m.get("Name"),
                            "destination": m.get("Destination"),
                            "rw": m.get("RW", True),
                        }
                    )

            networks = list(((c.get("NetworkSettings") or {}).get("Networks") or {}).keys())

            published_ports = []
            port_mappings = []  # [{"host": 8002, "container": "80/tcp"}, ...]
            port_bindings = (host_config.get("PortBindings") or {}) or (
                (c.get("NetworkSettings") or {}).get("Ports") or {}
            )
            for container_port, bindings in (port_bindings or {}).items():
                for b in bindings or []:
                    host_port = b.get("HostPort")
                    if host_port:
                        try:
                            published_ports.append(int(host_port))
                            port_mappings.append({"host": int(host_port), "container": container_port})
                        except (TypeError, ValueError):
                            pass

            data = {
                "id": c.get("Id", "")[:12],
                "image": config.get("Image"),
                "image_id": c.get("Image"),
                "state": state.get("Status"),
                "health": (state.get("Health") or {}).get("Status"),
                "restart_policy": host_config.get("RestartPolicy", {}).get("Name"),
                "compose_project": compose_project,
                "compose_service": compose_service,
                "compose_working_dir": compose_working_dir,
                "compose_config_files": compose_config_files,
                "networks": networks,
                "published_ports": sorted(set(published_ports)),
                "port_mappings": sorted(port_mappings, key=lambda p: (p["host"], p["container"])),
                "env_var_names": _env_var_names(config.get("Env")),
                "labels": {k: v for k, v in labels.items() if not k.startswith("com.docker.compose.project.environment")},
                "bind_mounts": bind_mounts,
                "volume_mounts": [v["name"] for v in volume_mounts if v.get("name")],
                "created": c.get("Created"),
            }

            resources.append(
                Resource(
                    type="container",
                    key=name,
                    display=name,
                    path=compose_working_dir,
                    state=state.get("Status", "unknown"),
                    data=data,
                )
            )
            container_index[name] = data

            for bm in bind_mounts:
                src = bm.get("source")
                if not src:
                    continue
                key = f"{src}->{name}:{bm.get('destination')}"
                resources.append(
                    Resource(
                        type="bind_mount",
                        key=key,
                        display=f"{src} -> {name}:{bm.get('destination')}",
                        path=src,
                        state="rw" if bm.get("rw") else "ro",
                        data={
                            "container": name,
                            "compose_project": compose_project,
                            "destination": bm.get("destination"),
                        },
                    )
                )
        except Exception:
            logger.exception("failed to process container %s", c.get("Id"))
            continue

    return resources, container_index


def _normalize_image_ref(ref: str | None) -> str:
    """Normalize a container's `Config.Image` value for tag matching: append
    ':latest' when there is no explicit tag or digest, since docker itself
    treats an untagged reference as implicitly ':latest'."""
    if not ref:
        return ""
    if "@" in ref:  # pinned by digest, e.g. repo@sha256:...
        return ref
    # a ':' after the last '/' means an explicit tag is present
    tail = ref.rsplit("/", 1)[-1]
    if ":" in tail:
        return ref
    return f"{ref}:latest"


def _short_id(value: str | None) -> str:
    """Strip a leading 'sha256:' prefix and return the first 12 hex chars,
    matching docker CLI's short image id convention."""
    if not value:
        return ""
    v = value.split(":", 1)[1] if value.startswith("sha256:") else value
    return v[:12]


def _collect_images(container_index: dict[str, dict]) -> list[Resource]:
    resources: list[Resource] = []
    raw = _run(["docker", "images", "--all", "--format", "{{json .}}"])
    if not raw:
        return resources

    # Build lookup indices keyed by (a) normalized repo:tag and (b) short
    # image id, so containers match images regardless of whether
    # Config.Image carries a tag or a full sha256 id.
    users_by_tag: dict[str, list[str]] = {}
    users_by_short_id: dict[str, list[str]] = {}
    for name, c in container_index.items():
        tag_ref = _normalize_image_ref(c.get("image"))
        if tag_ref:
            users_by_tag.setdefault(tag_ref, []).append(name)
        short_id = _short_id(c.get("image_id"))
        if short_id:
            users_by_short_id.setdefault(short_id, []).append(name)

    seen_ids = set()
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            img = json.loads(line)
        except json.JSONDecodeError:
            continue
        image_id = img.get("ID")
        if not image_id or image_id in seen_ids:
            continue
        seen_ids.add(image_id)
        repo_tag = f"{img.get('Repository')}:{img.get('Tag')}"
        dangling = img.get("Repository") == "<none>"
        users = dict.fromkeys(
            users_by_tag.get(repo_tag, []) + users_by_short_id.get(_short_id(image_id), [])
        )
        containers_using = list(users)
        resources.append(
            Resource(
                type="image",
                key=image_id,
                display=repo_tag if not dangling else image_id,
                path=None,
                state="dangling" if dangling else "in-use" if containers_using else "unused",
                data={
                    "repo_tag": repo_tag,
                    "size": img.get("Size"),
                    "created_since": img.get("CreatedSince"),
                    "dangling": dangling,
                    "containers_using": containers_using,
                    "shared": len({c.split(":")[0] for c in containers_using}) > 1
                    if containers_using
                    else False,
                },
            )
        )
    return resources


def _collect_volumes(container_index: dict[str, dict]) -> list[Resource]:
    resources: list[Resource] = []
    raw = _run(["docker", "volume", "ls", "--format", "{{json .}}"])
    if not raw:
        return resources

    names = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            v = json.loads(line)
        except json.JSONDecodeError:
            continue
        names.append(v.get("Name"))

    volume_details = {v.get("Name"): v for v in _inspect_volumes(names)}

    users_by_volume: dict[str, list[str]] = {}
    for name, c in container_index.items():
        for vol_name in c.get("volume_mounts", []) or []:
            users_by_volume.setdefault(vol_name, []).append(name)

    for name in names:
        detail = volume_details.get(name, {})
        labels = detail.get("Labels") or {}
        containers_using = users_by_volume.get(name, [])
        projects_using = sorted(
            {
                container_index.get(c, {}).get("compose_project")
                for c in containers_using
                if container_index.get(c, {}).get("compose_project")
            }
        )
        resources.append(
            Resource(
                type="volume",
                key=name,
                display=name,
                path=detail.get("Mountpoint"),
                state="orphan" if not containers_using else "attached",
                data={
                    "driver": detail.get("Driver"),
                    "labels": labels,
                    "compose_project": labels.get("com.docker.compose.project"),
                    "containers_using": containers_using,
                    "projects_using": projects_using,
                    "orphan_candidate": not containers_using,
                },
            )
        )
    return resources


def _inspect_volumes(names: list[str]) -> list[dict]:
    out: list[dict] = []
    for batch in _batched(names, BATCH_SIZE):
        if not batch:
            continue
        raw = _run(["docker", "volume", "inspect", *batch])
        if not raw:
            continue
        try:
            out.extend(json.loads(raw))
        except json.JSONDecodeError:
            logger.warning("docker volume inspect returned invalid JSON")
    return out


def _collect_networks(container_index: dict[str, dict]) -> list[Resource]:
    resources: list[Resource] = []
    raw = _run(["docker", "network", "ls", "--format", "{{json .}}"])
    if not raw:
        return resources

    names = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            n = json.loads(line)
        except json.JSONDecodeError:
            continue
        names.append(n.get("Name"))

    projects_by_network: dict[str, set[str]] = {}
    for cname, c in container_index.items():
        proj = c.get("compose_project")
        if not proj:
            continue
        for net in c.get("networks", []) or []:
            projects_by_network.setdefault(net, set()).add(proj)

    for detail in _inspect_networks(names):
        name = detail.get("Name")
        labels = detail.get("Labels") or {}
        driver = detail.get("Driver")
        projects_using = sorted(projects_by_network.get(name, set()))
        resources.append(
            Resource(
                type="network",
                key=name,
                display=name,
                path=None,
                state="default" if name in ("bridge", "host", "none") else "custom",
                data={
                    "driver": driver,
                    "scope": detail.get("Scope"),
                    "labels": labels,
                    "compose_project": labels.get("com.docker.compose.project"),
                    "projects_using": projects_using,
                    "shared_across_projects": len(projects_using) > 1,
                    # store container NAMES (correlation matches by name); the
                    # Containers dict is keyed by id -> {"Name": ...}.
                    "attached_containers": [
                        (info or {}).get("Name") or cid
                        for cid, info in (detail.get("Containers") or {}).items()
                    ],
                    "attached_container_ids": list((detail.get("Containers") or {}).keys()),
                },
            )
        )
    return resources


def _inspect_networks(names: list[str]) -> list[dict]:
    out: list[dict] = []
    for batch in _batched(names, BATCH_SIZE):
        if not batch:
            continue
        raw = _run(["docker", "network", "inspect", *batch])
        if not raw:
            continue
        try:
            out.extend(json.loads(raw))
        except json.JSONDecodeError:
            logger.warning("docker network inspect returned invalid JSON")
    return out


def collect() -> list[Resource]:
    """Collect containers, images, volumes, networks, and bind-mount resources.
    Read-only; never raises; partial failures are logged and skipped."""
    resources: list[Resource] = []
    container_index: dict[str, dict] = {}

    try:
        container_resources, container_index = _collect_containers()
        resources.extend(container_resources)
    except Exception:
        logger.exception("docker_src: container collection failed")

    try:
        resources.extend(_collect_images(container_index))
    except Exception:
        logger.exception("docker_src: image collection failed")

    try:
        resources.extend(_collect_volumes(container_index))
    except Exception:
        logger.exception("docker_src: volume collection failed")

    try:
        resources.extend(_collect_networks(container_index))
    except Exception:
        logger.exception("docker_src: network collection failed")

    return resources
