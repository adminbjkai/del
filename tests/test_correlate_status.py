"""Regression tests for correlate.py status/kind, host bind mounts and
phantom compose apps (audit of scan 332)."""
from del_app.correlate import build_apps
from del_app.manifests import Manifest
from del_app.models import Resource


def _container(name, project, wd, state="running"):
    return Resource(
        type="container", key=name, display=name, path=wd, state=state,
        data={"compose_project": project, "compose_working_dir": wd, "state": state},
    )


def _unit(name, wd, state):
    return Resource(
        type="systemd_unit", key=name, display=name, path=None, state=state,
        data={"is_custom": True, "working_directory": wd, "exec_start": f"{wd}/run"},
    )


def _compose(wd, display=None, declared_name=None):
    return Resource(
        type="compose_project", key=wd, display=display or wd.rsplit("/", 1)[-1],
        path=wd, state="found",
        data={"working_dir": wd, "declared_name": declared_name, "config_files": []},
    )


def _by_slug(apps):
    return {record.slug: (record, assocs) for record, assocs in apps}


# 1. status -------------------------------------------------------------------

def test_inactive_owned_unit_makes_app_stopped_not_unknown():
    apps = _by_slug(build_apps([_unit("glmflix.service", "/apps/glmflix", "inactive")], {}))
    assert apps["glmflix"][0].status == "stopped"


def test_compose_project_without_containers_is_stopped():
    apps = _by_slug(build_apps([_compose("/apps/showdoc")], {}))
    assert apps["showdoc"][0].status == "stopped"


def test_active_unit_or_running_container_is_running():
    apps = _by_slug(build_apps([
        _unit("xtr.service", "/apps/xtr", "active"),
        _container("web_1", "web", "/apps/web"),
    ], {}))
    assert apps["xtr"][0].status == "running"
    assert apps["web"][0].status == "running"


def test_exited_container_makes_app_stopped():
    apps = _by_slug(build_apps([_container("old_1", "old", "/apps/old", state="exited")], {}))
    assert apps["old"][0].status == "stopped"


def test_manifest_status_is_only_a_fallback_and_active_maps_to_running():
    unit = _unit("del-web.service", "/apps/del", "inactive")
    dirs = [Resource(type="directory", key=f"/apps/{n}", display=n, path=f"/apps/{n}",
                     state="found", data={}) for n in ("notes", "blank")]
    manifests = {
        "del": Manifest(id="del", status="active", systemd_units=["del-web.service"]),
        "notes": Manifest(id="notes", status="stopped", host_paths=["/apps/notes"]),
        "blank": Manifest(id="blank", status="active", host_paths=["/apps/blank"]),
    }
    apps = _by_slug(build_apps([unit, *dirs], manifests))
    assert apps["del"][0].status == "stopped"  # host evidence wins over the manifest
    assert apps["notes"][0].status == "stopped"  # no runtime evidence: manifest value
    assert apps["blank"][0].status == "running"  # "active" is not UI vocabulary


# 2. kind ---------------------------------------------------------------------

def test_stopped_compose_app_run_by_systemd_unit_has_kind_systemd():
    apps = _by_slug(build_apps([
        _compose("/apps/boxy"),
        _unit("boxy.service", "/apps/boxy", "active"),
    ], {}))
    record = apps["boxy"][0]
    assert (record.status, record.kind) == ("running", "systemd")


# 3. host system bind mounts --------------------------------------------------

def test_host_system_bind_mounts_are_shared_and_blocked_not_safe():
    c = _container("portainer", "portainer", "/apps/portainer")
    mounts = [
        Resource(type="bind_mount", key=f"{src}->portainer:{src}", display=src, path=src,
                 state="rw", data={"container": "portainer"})
        for src in ("/var/run/docker.sock", "/etc/localtime", "/etc/nginx", "/apps")
    ]
    own = Resource(type="bind_mount", key="/apps/portainer/data->portainer:/data",
                   display="/apps/portainer/data", path="/apps/portainer/data",
                   state="rw", data={"container": "portainer"})
    _record, assocs = _by_slug(build_apps([c, *mounts, own], {}))["portainer"]
    by_key = {a.resource_key: a for a in assocs}
    for m in mounts:
        a = by_key[m.key]
        assert a.ownership != "exclusive", m.path
        assert a.removal_eligible != "safe", m.path
    assert by_key[own.key].removal_eligible == "safe"


# 4. phantom / duplicate compose apps -------------------------------------------

def test_compose_file_at_running_project_working_dir_merges_into_that_app():
    # Docker project "immich" runs from /apps/immich-app, whose compose file
    # declares `name: immich`. No second "immich-app" app may appear.
    apps = _by_slug(build_apps([
        _container("immich_server", "immich", "/apps/immich-app"),
        _compose("/apps/immich-app", declared_name="immich"),
    ], {}))
    assert set(apps) == {"immich"}
    cp = next(a for a in apps["immich"][1] if a.resource_type == "compose_project")
    assert cp.removal_eligible == "safe" and not cp.shared


def test_compose_nested_in_another_apps_tree_does_not_create_an_app():
    apps = _by_slug(build_apps([
        _container("karakeep-web-1", "karakeep", "/apps/karakeep/docker"),
        _compose("/apps/karakeep/packages/benchmarks"),
    ], {}))
    assert set(apps) == {"karakeep"}
    cp = next(a for a in apps["karakeep"][1]
              if a.resource_key == "/apps/karakeep/packages/benchmarks")
    assert cp.removal_eligible == "blocked"


def test_compose_in_sample_or_backup_tree_does_not_create_an_app():
    apps = _by_slug(build_apps([
        _compose("/apps/knowledgebase/samples/deploy"),
        _compose("/apps/system-diagnostics-2026-09-27/backups/notecapai"),
    ], {}))
    assert apps == {}


def test_docker_labels_redact_secret_values_only():
    from del_app.discovery.docker_src import _redact_labels

    out = _redact_labels({
        "opensandbox.io/egress-auth-token": "7pbcB1nt",
        "traefik.http.middlewares.a.basicauth.users": "u:$apr1$x",
        "org.opencontainers.image.authors": "someone",
        "com.docker.compose.project": "surfsense",
    })
    assert out["opensandbox.io/egress-auth-token"] == "<redacted>"
    assert out["traefik.http.middlewares.a.basicauth.users"] == "<redacted>"
    assert out["org.opencontainers.image.authors"] == "someone"
    assert out["com.docker.compose.project"] == "surfsense"


def test_opensandbox_session_containers_belong_to_the_server_app():
    """Per-session sandbox + egress containers join the app running opensandbox/server."""
    from del_app.correlate import build_apps
    from del_app.models import Resource

    server = Resource(type="container", key="surfsense-opensandbox-server-1", display="s", state="running",
                      data={"compose_project": "surfsense", "image": "opensandbox/server:v0.2.2", "state": "running"})
    sandbox = Resource(type="container", key="sandbox-abc", display="sandbox-abc", state="running",
                       data={"image": "surfsense-sandbox:skills", "state": "running",
                             "labels": {"opensandbox.io/id": "abc"}})
    egress = Resource(type="container", key="sandbox-egress-abc", display="e", state="running",
                      data={"image": "opensandbox/egress:v1", "state": "running",
                            "labels": {"opensandbox.io/egress-sidecar-for": "abc"}})
    lone = Resource(type="container", key="lonely", display="lonely", state="running",
                    data={"image": "nginx", "state": "running"})
    apps = {rec.slug: assocs for rec, assocs in build_apps([server, sandbox, egress, lone], {})}
    assert "sandbox-abc" not in apps and "sandbox-egress-abc" not in apps
    keys = {a.resource_key for a in apps["surfsense"]}
    assert {"sandbox-abc", "sandbox-egress-abc"} <= keys
    assert "lonely" in apps


def test_manifest_app_with_nothing_on_host_is_absent():
    from del_app.correlate import build_apps
    from del_app.manifests import Manifest

    m = Manifest(id="gone", status="stopped", host_paths=["/apps/gone"])
    apps = {rec.slug: rec for rec, _ in build_apps([], {"gone": m})}
    assert apps["gone"].status == "absent"


def test_manifest_declared_excluded_rows_still_date_the_app():
    from del_app.web.apps import _dates_from

    manifest_ev = '[{"source": "manifest", "statement": "manually declared in manifest", "weight": 100}]'
    backup_ev = '[{"source": "backups", "statement": "inside backups_dir", "weight": 100}]'
    assert _dates_from({"excluded": 0, "shared": 0})
    assert not _dates_from({"excluded": 0, "shared": 1})
    assert _dates_from({"excluded": 1, "shared": 0, "evidence_json": manifest_ev})
    assert not _dates_from({"excluded": 1, "shared": 0, "evidence_json": backup_ev})
