import shutil

import pytest

from del_app.correlate import build_apps
from del_app.discovery import docker_src, nginx_src
from del_app.models import Resource


# --------------------------------------------------------------------------
# correlate.py: synthetic-resource unit tests
# --------------------------------------------------------------------------


def _container(name, compose_project=None, compose_working_dir=None, published_ports=None):
    return Resource(
        type="container",
        key=name,
        display=name,
        path=compose_working_dir,
        state="running",
        data={
            "compose_project": compose_project,
            "compose_service": name,
            "compose_working_dir": compose_working_dir,
            "published_ports": published_ports or [],
            "state": "running",
        },
    )


def test_compose_label_groups_containers_into_one_app():
    resources = [
        _container("myapp_web", compose_project="myapp", compose_working_dir="/apps/myapp"),
        _container("myapp_db", compose_project="myapp", compose_working_dir="/apps/myapp"),
    ]
    apps = build_apps(resources, {})
    assert len(apps) == 1
    record, assocs = apps[0]
    assert record.slug == "myapp"
    assert record.kind == "compose"
    assert {a.resource_key for a in assocs} == {"myapp_web", "myapp_db"}
    for a in assocs:
        assert a.level == "confirmed"
        assert a.confidence == 100
        assert a.removal_eligible == "safe"


def test_standalone_container_becomes_its_own_app():
    resources = [_container("netmuxd")]
    apps = build_apps(resources, {})
    assert len(apps) == 1
    record, assocs = apps[0]
    assert record.slug == "netmuxd"
    assert record.kind == "container"
    assert assocs[0].level == "confirmed"


def test_nginx_port_match_creates_high_confidence_association():
    container = _container("myapp_web", compose_project="myapp", compose_working_dir="/apps/myapp",
                            published_ports=[9205])
    nginx_site = Resource(
        type="nginx_site",
        key="/etc/nginx/sites-enabled/myapp.bjk.ai",
        display="myapp.bjk.ai",
        path="/etc/nginx/sites-enabled/myapp.bjk.ai",
        state="enabled",
        data={
            "server_names": ["myapp.bjk.ai"],
            "upstreams": [{"location": "/", "proxy_pass": "http://127.0.0.1:9205", "port": 9205}],
        },
    )
    apps = build_apps([container, nginx_site], {})
    record, assocs = apps[0]
    nginx_assoc = next(a for a in assocs if a.resource_type == "nginx_site")
    assert nginx_assoc.level == "high"
    assert 80 <= nginx_assoc.confidence <= 94
    assert "myapp.bjk.ai" in record.domains


def test_shared_resource_across_two_apps_is_flagged_and_blocked():
    net = Resource(
        type="network", key="shared_net", display="shared_net", path=None, state="custom",
        data={"attached_containers": ["appa_web", "appb_web"]},
    )
    resources = [
        _container("appa_web", compose_project="appa", compose_working_dir="/apps/appa"),
        _container("appb_web", compose_project="appb", compose_working_dir="/apps/appb"),
        net,
    ]
    apps = build_apps(resources, {})
    by_slug = {r.slug: assocs for r, assocs in apps}
    a_net = next(a for a in by_slug["appa"] if a.resource_type == "network")
    b_net = next(a for a in by_slug["appb"] if a.resource_type == "network")
    assert a_net.shared is True
    assert b_net.shared is True
    assert a_net.removal_eligible == "blocked"
    assert b_net.removal_eligible == "blocked"


def test_name_similarity_only_reaches_possible_level_capped_at_50():
    container = _container("myapp_web", compose_project="myapp", compose_working_dir="/apps/myapp")
    unrelated_dir = Resource(
        type="directory", key="/srv/apps/myapp2", display="myapp2",
        path="/srv/apps/myapp2", state="found", data={},
    )
    apps = build_apps([container, unrelated_dir], {})
    record, assocs = apps[0]
    dir_assoc = next((a for a in assocs if a.resource_type == "directory"), None)
    assert dir_assoc is not None
    assert dir_assoc.level == "possible"
    assert dir_assoc.confidence <= 50
    assert dir_assoc.removal_eligible == "blocked"


def test_bind_mount_gets_confirmed_level_with_data_loss_risk():
    container = _container("myapp_web", compose_project="myapp", compose_working_dir="/apps/myapp")
    bind_mount = Resource(
        type="bind_mount",
        key="/apps/myapp/data->myapp_web:/data",
        display="/apps/myapp/data -> myapp_web:/data",
        path="/apps/myapp/data",
        state="rw",
        data={"container": "myapp_web", "compose_project": "myapp", "destination": "/data"},
    )
    apps = build_apps([container, bind_mount], {})
    record, assocs = apps[0]
    bm_assoc = next(a for a in assocs if a.resource_type == "bind_mount")
    assert bm_assoc.level == "confirmed"
    assert bm_assoc.data_loss_risk == "data"


# --------------------------------------------------------------------------
# nginx_src: regex parser on a synthetic config string
# --------------------------------------------------------------------------

SAMPLE_NGINX_CONFIG = """
server {
    listen 80;
    listen 443 ssl;
    server_name example.bjk.ai;
    ssl_certificate /etc/letsencrypt/live/bjk.ai/fullchain.pem;
    client_max_body_size 50m;

    location / {
        proxy_pass http://127.0.0.1:9205;
        proxy_set_header Upgrade $http_upgrade;
    }

    location /progress/ {
        proxy_pass http://127.0.0.1:9205;
    }
}
"""


def test_nginx_parser_extracts_server_names_listens_upstreams(tmp_path):
    conf_path = tmp_path / "example.bjk.ai"
    conf_path.write_text(SAMPLE_NGINX_CONFIG)
    resource = nginx_src._resource_from_file(str(conf_path), enabled=True)
    assert resource is not None
    assert resource.data["server_names"] == ["example.bjk.ai"]
    assert "443 ssl" in resource.data["listens"]
    assert resource.data["ssl_cert"] == "/etc/letsencrypt/live/bjk.ai/fullchain.pem"
    assert resource.data["client_max_body_size"] == "50m"
    ports = {u["port"] for u in resource.data["upstreams"]}
    assert ports == {9205}
    assert resource.data["websocket"] is True


def test_nginx_parser_tolerates_snippet_with_no_server_block(tmp_path):
    conf_path = tmp_path / "snippet.conf"
    conf_path.write_text("auth_basic \"Restricted\";\nauth_basic_user_file /etc/nginx/.htpasswd;\n")
    resource = nginx_src._resource_from_file(str(conf_path), enabled=True)
    assert resource is not None
    assert resource.data["server_names"] == []
    assert resource.data["upstreams"] == []


# --------------------------------------------------------------------------
# docker_src: live smoke test against this host's real docker daemon
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# docker_src: image <-> container matching by normalized tag / short id
# --------------------------------------------------------------------------


def test_image_containers_using_matches_by_normalized_tag_without_explicit_tag(monkeypatch):
    # Container reports an untagged image ref ("b64pdf-app"); the image list
    # entry is "b64pdf-app:latest". Must still be recognized as in-use.
    container_index = {
        "b64pdf-app-1": {"image": "b64pdf-app", "image_id": "sha256:" + "a" * 64},
    }
    raw = '{"ID":"deadbeefcafe","Repository":"b64pdf-app","Tag":"latest","Size":"10MB","CreatedSince":"1 day ago"}\n'
    monkeypatch.setattr(docker_src, "_run", lambda args, timeout=docker_src.TIMEOUT: raw)
    images = docker_src._collect_images(container_index)
    assert len(images) == 1
    assert images[0].data["containers_using"] == ["b64pdf-app-1"]
    assert images[0].state == "in-use"


def test_image_containers_using_matches_by_short_image_id(monkeypatch):
    # Container's image_id is a full sha256 digest; image list ID is the
    # short 12-char form. Must match on the common prefix.
    full_sha = "sha256:" + "b" * 64
    container_index = {
        "myctr-1": {"image": "myrepo/myimg:1.0", "image_id": full_sha},
    }
    short_id = full_sha.split(":", 1)[1][:12]
    raw = f'{{"ID":"{short_id}","Repository":"myrepo/myimg","Tag":"1.0","Size":"5MB","CreatedSince":"2 days ago"}}\n'
    monkeypatch.setattr(docker_src, "_run", lambda args, timeout=docker_src.TIMEOUT: raw)
    images = docker_src._collect_images(container_index)
    assert images[0].data["containers_using"] == ["myctr-1"]


def test_image_with_no_matching_container_stays_orphan(monkeypatch):
    container_index = {"other-1": {"image": "other:latest", "image_id": "sha256:" + "c" * 64}}
    raw = '{"ID":"unused123456","Repository":"unused-img","Tag":"latest","Size":"1MB","CreatedSince":"3 days ago"}\n'
    monkeypatch.setattr(docker_src, "_run", lambda args, timeout=docker_src.TIMEOUT: raw)
    images = docker_src._collect_images(container_index)
    assert images[0].data["containers_using"] == []
    assert images[0].state == "unused"


# --------------------------------------------------------------------------
# nginx_src: stale sites-available copies must not leak domains
# --------------------------------------------------------------------------


def test_nginx_collect_marks_stale_copy_and_enabled_flags(tmp_path, monkeypatch):
    enabled_dir = tmp_path / "sites-enabled"
    available_dir = tmp_path / "sites-available"
    enabled_dir.mkdir()
    available_dir.mkdir()

    live_conf = (
        "server {\n  listen 443 ssl;\n  server_name bytestash.bjk.ai;\n"
        "  location / { proxy_pass http://127.0.0.1:8002; }\n}\n"
    )
    (available_dir / "bytestash.bjk.ai").write_text(live_conf)
    (enabled_dir / "bytestash.bjk.ai").write_text(live_conf)  # regular file stand-in for symlink

    stale_conf = (
        "server {\n  listen 443 ssl;\n  server_name focalboard.bjk.ai;\n"
        "  location / { proxy_pass http://127.0.0.1:8002; }\n}\n"
    )
    (available_dir / "focalboard.bjk.ai.bak").write_text(stale_conf)

    monkeypatch.setattr(nginx_src, "SITES_ENABLED", str(enabled_dir))
    monkeypatch.setattr(nginx_src, "SITES_AVAILABLE", str(available_dir))

    resources = nginx_src.collect()
    by_key = {r.key: r for r in resources}

    enabled_res = by_key[str(enabled_dir / "bytestash.bjk.ai")]
    assert enabled_res.data["enabled"] is True
    assert enabled_res.data["stale_copy"] is False

    stale_res = by_key[str(available_dir / "focalboard.bjk.ai.bak")]
    assert stale_res.data["enabled"] is False
    assert stale_res.data["stale_copy"] is True


def test_correlate_excludes_stale_nginx_domains_but_still_associates():
    container = _container("bytestash", compose_project="bytestash", compose_working_dir="/apps/bytestash",
                            published_ports=[8002])
    live_site = Resource(
        type="nginx_site",
        key="/etc/nginx/sites-enabled/bytestash.bjk.ai",
        display="bytestash.bjk.ai",
        path="/etc/nginx/sites-enabled/bytestash.bjk.ai",
        state="enabled",
        data={
            "server_names": ["bytestash.bjk.ai"],
            "upstreams": [{"location": "/", "proxy_pass": "http://127.0.0.1:8002", "port": 8002}],
            "enabled": True,
            "stale_copy": False,
        },
    )
    stale_site = Resource(
        type="nginx_site",
        key="/etc/nginx/sites-available/focalboard.bjk.ai.bak",
        display="focalboard.bjk.ai",
        path="/etc/nginx/sites-available/focalboard.bjk.ai.bak",
        state="available",
        data={
            "server_names": ["focalboard.bjk.ai"],
            "upstreams": [{"location": "/", "proxy_pass": "http://127.0.0.1:8002", "port": 8002}],
            "enabled": False,
            "stale_copy": True,
        },
    )
    apps = build_apps([container, live_site, stale_site], {})
    record, assocs = apps[0]
    # Only the enabled site's server_name reaches app.domains.
    assert record.domains == ["bytestash.bjk.ai"]
    assert "focalboard.bjk.ai" not in record.domains
    # The stale copy still shows up as an association (removable debris),
    # just at a lower, "probable" confidence with a clear evidence trail.
    stale_assoc = next(a for a in assocs if a.resource_key == stale_site.key)
    assert stale_assoc.level == "probable"
    assert stale_assoc.confidence <= 70
    assert any("stale sites-available copy, not enabled" in e.statement for e in stale_assoc.evidence)


def _port(port, container=None, systemd_unit=None, addr="127.0.0.1"):
    return Resource(
        type="port",
        key=f"tcp:{addr}:{port}",
        display=f"{addr}:{port}",
        path=None,
        state="listen",
        data={"proto": "tcp", "addr": addr, "port": port, "pid": 12345,
              "process": "app", "container": container, "systemd_unit": systemd_unit},
    )


def test_host_network_container_port_gets_attached_via_cgroup_and_shows_in_host_ports():
    # memos-style container: network_mode=host, so no published_ports, but
    # proc_src resolved the listening port's owning container via cgroup.
    container = _container("memos")  # standalone, no compose project, no published ports
    port = _port(8014, container="memos")
    apps = build_apps([container, port], {})
    record, assocs = apps[0]
    assert 8014 in record.ports
    port_assoc = next(a for a in assocs if a.resource_type == "port")
    assert port_assoc.confidence == 90
    assert "cgroup match" in port_assoc.evidence[0].statement


def test_host_network_container_nginx_domain_attaches_via_cgroup_port_ownership():
    container = _container("memos")
    port = _port(8014, container="memos")
    nginx_site = Resource(
        type="nginx_site",
        key="/etc/nginx/sites-enabled/memos.bjk.ai",
        display="memos.bjk.ai",
        path="/etc/nginx/sites-enabled/memos.bjk.ai",
        state="enabled",
        data={
            "server_names": ["memos.bjk.ai"],
            "upstreams": [{"location": "/", "proxy_pass": "http://127.0.0.1:8014", "port": 8014}],
            "enabled": True,
            "stale_copy": False,
        },
    )
    apps = build_apps([container, port, nginx_site], {})
    record, assocs = apps[0]
    assert "memos.bjk.ai" in record.domains
    assert 8014 in record.ports
    nginx_assoc = next(a for a in assocs if a.resource_type == "nginx_site")
    assert nginx_assoc.level == "high"
    assert any("host network" in e.statement and "memos" in e.statement for e in nginx_assoc.evidence)


def test_port_owned_by_systemd_unit_attaches_to_units_app():
    container = _container("myapp", compose_project="myapp", compose_working_dir="/apps/myapp")
    unit = Resource(
        type="systemd_unit", key="myapp.service", display="myapp.service",
        path="/etc/systemd/system/myapp.service", state="active",
        data={"working_directory": "/apps/myapp", "exec_start": "/apps/myapp/run.sh"},
    )
    port = _port(9090, systemd_unit="myapp.service")
    apps = build_apps([container, unit, port], {})
    by_slug = {r.slug: assocs for r, assocs in apps}
    assocs = by_slug["myapp"]
    port_assoc = next(a for a in assocs if a.resource_type == "port")
    assert port_assoc.confidence == 85
    assert "myapp.service" in port_assoc.evidence[0].statement


def test_unowned_port_process_get_no_association():
    """Ports/processes proc_src couldn't tie to a container or systemd unit
    still correctly produce no association (they remain real orphans)."""
    container = _container("otherapp")
    port = _port(9999)  # no owner at all
    apps = build_apps([container, port], {})
    record, assocs = apps[0]
    assert not any(a.resource_type == "port" for a in assocs)


def test_pure_systemd_app_seeded_with_unit_nginx_dir_and_port():
    """A service deployed without Docker (unit WorkingDirectory under a scan
    root + nginx proxying its port + the project dir) must become a
    first-class kind=systemd app with everything attached."""
    unit = Resource(
        type="systemd_unit", key="foo.service", display="foo.service",
        path="/etc/systemd/system/foo.service", state="active",
        data={
            "is_custom": True,
            "working_directory": "/apps/foo",
            "exec_start": "/apps/foo/.venv/bin/uvicorn app:app --port 8099",
        },
    )
    nginx_site = Resource(
        type="nginx_site",
        key="/etc/nginx/sites-enabled/foo.bjk.ai",
        display="foo.bjk.ai",
        path="/etc/nginx/sites-enabled/foo.bjk.ai",
        state="enabled",
        data={
            "server_names": ["foo.bjk.ai"],
            "upstreams": [{"location": "/", "proxy_pass": "http://127.0.0.1:8099", "port": 8099}],
            "enabled": True,
            "stale_copy": False,
        },
    )
    directory = Resource(
        type="directory", key="/apps/foo", display="foo",
        path="/apps/foo", state="present", data={},
    )
    apps = build_apps([unit, nginx_site, directory], {})
    assert len(apps) == 1
    record, assocs = apps[0]
    assert record.slug == "foo"
    assert record.kind == "systemd"
    assert record.status == "running"
    assert 8099 in record.ports
    assert "foo.bjk.ai" in record.domains
    by_type = {a.resource_type for a in assocs}
    assert by_type == {"systemd_unit", "nginx_site", "directory"}
    unit_assoc = next(a for a in assocs if a.resource_type == "systemd_unit")
    assert unit_assoc.confidence == 95
    assert unit_assoc.level == "confirmed"


def test_system_unit_not_under_scan_root_does_not_seed_an_app():
    """A vendor/system unit (sshd) never resolves to a scan-root project dir,
    so it must not seed a phantom app."""
    unit = Resource(
        type="systemd_unit", key="ssh.service", display="ssh.service",
        path="/usr/lib/systemd/system/ssh.service", state="active",
        data={
            "is_custom": False,
            "working_directory": None,
            "exec_start": "/usr/sbin/sshd -D",
        },
    )
    apps = build_apps([unit], {})
    assert apps == []


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker not available on this host")
def test_docker_src_collect_returns_many_resources_on_this_host():
    resources = docker_src.collect()
    assert len(resources) > 100
    assert all(isinstance(r, Resource) for r in resources)
    # env values must never leak: only KEY names, never KEY=VALUE, in env_var_names
    for r in resources:
        if r.type == "container":
            for name in r.data.get("env_var_names", []):
                assert "=" not in name
