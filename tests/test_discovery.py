import os
import shutil

import pytest

from del_app.correlate import build_apps
from del_app.discovery import docker_src, fs_src, nginx_src, proc_src, systemd_src
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


def _compose_project(display, working_dir, declared_name=None):
    return Resource(
        type="compose_project",
        key=working_dir,
        display=display,
        path=working_dir,
        state="stopped",
        data={"working_dir": working_dir, "declared_name": declared_name, "config_files": []},
    )


def test_nested_compose_in_another_apps_tree_is_not_a_safe_same_name_match():
    # /apps/boxy is the real app; /apps/agyinstall/boxy is an archived clone
    # inside a DIFFERENT project's tree (agyinstall). Both compose files slug
    # to "boxy" by directory basename — the nested one must NOT be merged
    # into app "boxy" at full (95, safe) confidence.
    resources = [
        _compose_project("boxy", "/apps/boxy"),
        _compose_project("agyinstall", "/apps/agyinstall"),
        _compose_project("boxy", "/apps/agyinstall/boxy"),
    ]
    apps = build_apps(resources, {})
    by_slug = {record.slug: (record, assocs) for record, assocs in apps}
    assert "boxy" in by_slug
    boxy_record, boxy_assocs = by_slug["boxy"]
    nested = next(
        (a for a in boxy_assocs if a.resource_key == "/apps/agyinstall/boxy"), None
    )
    # Either it was kept out of app "boxy" entirely, or it was attached at a
    # confidence/level that is not a safe 95 same-name association.
    if nested is not None:
        assert not (nested.confidence == 95 and nested.removal_eligible == "safe")
    # And it must not have vanished silently — it should land somewhere
    # (its own path key associated to SOME app), most sensibly agyinstall.
    all_keys = {
        a.resource_key for _, assocs in apps for a in assocs
        if a.resource_key == "/apps/agyinstall/boxy"
    }
    assert "/apps/agyinstall/boxy" in all_keys


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


def test_nginx_parser_ignores_proxy_ssl_server_name_directive(tmp_path):
    # `proxy_ssl_server_name on;` ends in the substring "server_name" — the
    # parser must not mistake it for a `server_name` directive and capture
    # its value ("on") as a bogus extra domain.
    conf = """
server {
    listen 443 ssl;
    server_name real.bjk.ai;
    location /proxy {
        proxy_ssl_server_name on;
        proxy_pass http://127.0.0.1:9205;
    }
}
"""
    conf_path = tmp_path / "real.bjk.ai"
    conf_path.write_text(conf)
    resource = nginx_src._resource_from_file(str(conf_path), enabled=True)
    assert resource is not None
    assert resource.data["server_names"] == ["real.bjk.ai"]


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


def test_parse_systemctl_show_does_not_mix_execstart_across_units():
    """Batched `systemctl show` emits ExecStart before Id= and separates
    units with a blank line. The parser must keep each unit's ExecStart."""
    raw = (
        "ExecStart={ path=/apps/glmflix/start.sh ; argv[]=/apps/glmflix/start.sh ; ignore_errors=no }\n"
        "WorkingDirectory=/apps/glmflix\n"
        "Id=glmflix.service\n"
        "FragmentPath=/etc/systemd/system/glmflix.service\n"
        "\n"
        "ExecStart={ path=/apps/url2/target/release/url-shortener ; argv[]=/apps/url2/target/release/url-shortener ; ignore_errors=no }\n"
        "WorkingDirectory=/apps/url2\n"
        "Id=url-shortener.service\n"
        "FragmentPath=/etc/systemd/system/url-shortener.service\n"
        "\n"
        "ExecStart={ path=/usr/bin/gpu-manager ; argv[]=/usr/bin/gpu-manager --log /var/log/gpu-manager.log ; ignore_errors=no }\n"
        "WorkingDirectory=\n"
        "Id=gpu-manager.service\n"
        "FragmentPath=/lib/systemd/system/gpu-manager.service\n"
    )
    parsed = systemd_src._parse_systemctl_show(raw)
    assert set(parsed) == {
        "glmflix.service", "url-shortener.service", "gpu-manager.service",
    }
    assert "/apps/glmflix/start.sh" in parsed["glmflix.service"]["ExecStart"]
    assert parsed["glmflix.service"]["WorkingDirectory"] == "/apps/glmflix"
    assert "/apps/url2/target/release/url-shortener" in parsed["url-shortener.service"]["ExecStart"]
    assert "gpu-manager" in parsed["gpu-manager.service"]["ExecStart"]
    assert "/apps/glmflix" not in parsed["gpu-manager.service"].get("ExecStart", "")


def test_host_monitor_bind_mounts_do_not_claim_foreign_systemd_units():
    """Netdata bind-mounts /, /sys, /proc, /var/log for host monitoring.
    Those must not become ownership paths that absorb glmflix / url-shortener."""
    netdata = _container(
        "netdata", compose_project="netdata", compose_working_dir="/apps/netdata",
    )
    binds = []
    for src, dest in (
        ("/", "/host/root"),
        ("/sys", "/host/sys"),
        ("/proc", "/host/proc"),
        ("/var/log", "/host/var/log"),
        ("/etc/passwd", "/host/etc/passwd"),
        ("/var/run/docker.sock", "/var/run/docker.sock"),
    ):
        binds.append(Resource(
            type="bind_mount",
            key=f"{src}->netdata:{dest}",
            display=f"{src} -> netdata:{dest}",
            path=src,
            state="ro",
            data={"container": "netdata", "source": src, "destination": dest},
        ))
    glmflix = Resource(
        type="systemd_unit", key="glmflix.service", display="glmflix.service",
        path="/etc/systemd/system/glmflix.service", state="active",
        data={
            "is_custom": True,
            "working_directory": "/apps/glmflix",
            "exec_start": "/apps/glmflix/start.sh",
        },
    )
    # Scrambled ExecStart as stored by the old batched-show parser — contains
    # /var/log, which netdata also bind-mounts.
    scrambled = Resource(
        type="systemd_unit", key="url-shortener.service", display="url-shortener.service",
        path="/etc/systemd/system/url-shortener.service", state="active",
        data={
            "is_custom": True,
            "working_directory": None,
            "exec_start": "/usr/bin/gpu-manager --log /var/log/gpu-manager.log",
        },
    )
    own_unit = Resource(
        type="systemd_unit", key="netdata.service", display="netdata.service",
        path="/etc/systemd/system/netdata.service", state="active",
        data={
            "is_custom": True,
            "working_directory": "/apps/netdata",
            "exec_start": "/apps/netdata/run.sh",
        },
    )
    apps = build_apps([netdata, *binds, glmflix, scrambled, own_unit], {})
    by_slug = {r.slug: assocs for r, assocs in apps}
    assert "netdata" in by_slug
    net_units = {
        a.resource_key for a in by_slug["netdata"] if a.resource_type == "systemd_unit"
    }
    assert "glmflix.service" not in net_units
    assert "url-shortener.service" not in net_units
    assert "netdata.service" in net_units
    assert "glmflix" in by_slug


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


@pytest.mark.skipif(
    os.environ.get("DEL_HOST_INTEGRATION") != "1" or shutil.which("docker") is None,
    reason="set DEL_HOST_INTEGRATION=1 on the DEL host to run live Docker assertions",
)
def test_docker_src_collect_returns_many_resources_on_this_host():
    resources = docker_src.collect()
    assert len(resources) > 100
    assert all(isinstance(r, Resource) for r in resources)
    # env values must never leak: only KEY names, never KEY=VALUE, in env_var_names
    for r in resources:
        if r.type == "container":
            for name in r.data.get("env_var_names", []):
                assert "=" not in name


def test_proc_src_sanitize_args_redacts_secret_shaped_flags():
    """A process command line can carry a secret-shaped flag; the value must
    be stripped before args_redacted is stored/displayed (mirrors
    del_app.jobs.sanitize_output's helper-output redaction)."""
    raw = "myapp --password=hunter2 --token=abc123 --api-key=xyz --other=fine"
    cleaned = proc_src._sanitize_args(raw)
    assert "hunter2" not in cleaned
    assert "abc123" not in cleaned
    assert "xyz" not in cleaned
    assert "--other=fine" in cleaned


def test_fs_src_epoch_to_iso_and_directory_timestamps(tmp_path, monkeypatch):
    """Directory resources must carry mtime/ctime ISO timestamps for the
    Installed column (no birthtime required on Linux)."""
    assert fs_src._epoch_to_iso(None) is None
    assert fs_src._epoch_to_iso(0).startswith("1970-01-01")

    project = tmp_path / "myproj"
    project.mkdir()
    (project / "docker-compose.yml").write_text("services: {}\n")

    # Point scan_roots at tmp via settings
    from del_app.config import get_settings

    config_path = tmp_path / "del.toml"
    config_path.write_text(
        f"""
port = 8075
db_path = "{tmp_path}/del.db"
manifests_dir = "{tmp_path}/manifests"
backups_dir = "{tmp_path}/backups"
logs_dir = "{tmp_path}/logs"
scan_roots = ["{tmp_path}"]
helper_socket = "{tmp_path}/helper.sock"
protected_apps = ["del"]
"""
    )
    monkeypatch.setenv("DEL_CONFIG_PATH", str(config_path))
    get_settings.cache_clear()
    try:
        resources = fs_src.collect()
    finally:
        get_settings.cache_clear()

    dirs = [r for r in resources if r.type == "directory" and r.display == "myproj"]
    assert len(dirs) == 1
    data = dirs[0].data
    assert data.get("mtime") and data["mtime"].endswith("Z")
    assert data.get("ctime") and data["ctime"].endswith("Z")
    assert "size_kb" in data


# --------------------------------------------------------------------------
# Regression tests for the 2026-08-24 correlation-accuracy pass.
# Each of these locks in a fix for a defect that could have caused a WRONG
# removal — an operator deleting one app and taking another app's live
# resources with it.
# --------------------------------------------------------------------------


def test_systemd_app_wins_its_own_nginx_site_over_a_host_network_container():
    """A systemd-run app must claim the vhost that proxies to its own port.

    Regression: the nginx-by-port step used to run before the pass that puts a
    systemd unit's ports into `app.ports`, so a systemd app could never match
    its own site and whichever container claimed the port first took the vhost.

    NOTE the ExecStart deliberately carries no `--port N` flag. The unit-seeding
    step scrapes that flag when it is present, which masked the ordering bug;
    the real-world case is a service that reads its port from a config file or
    environment, where the *only* source of the port is the listener's cgroup
    owner — which is exactly what used to be discovered too late.
    """
    unit = Resource(
        type="systemd_unit", key="astv-remote.service", display="astv-remote.service",
        path="/etc/systemd/system/astv-remote.service", state="active",
        data={
            "is_custom": True,
            "working_directory": "/apps/astv-remote",
            "exec_start": "/apps/astv-remote/.venv/bin/gunicorn -c gunicorn.conf.py astv:app",
        },
    )
    port = _port(8083, systemd_unit="astv-remote.service")
    site = Resource(
        type="nginx_site",
        key="/etc/nginx/sites-enabled/astv.bjk.ai.conf",
        display="astv.bjk.ai",
        path="/etc/nginx/sites-enabled/astv.bjk.ai.conf",
        state="enabled",
        data={
            "server_names": ["astv.bjk.ai"],
            "upstreams": [{"host": "127.0.0.1", "port": 8083}],
            "enabled": True,
        },
    )
    # An unrelated host-network container also exists on this host.
    other = _container("bjkflix", compose_project="bjk-ai-flix",
                       compose_working_dir="/apps/bjk-ai-flix")

    apps = build_apps([unit, port, site, other], {})
    by_slug = {r.slug: assocs for r, assocs in apps}

    owners = [
        slug for slug, assocs in by_slug.items()
        if any(a.resource_type == "nginx_site" and a.resource_key == site.key for a in assocs)
    ]
    assert owners == ["astv-remote"], f"vhost went to {owners}, expected only astv-remote"


def test_nested_compose_file_attaches_to_the_enclosing_app_not_a_phantom():
    """`/apps/karakeep/docker/compose.yml` belongs to karakeep.

    Regression: the compose step slugified by directory basename, inventing a
    phantom app literally named "docker" that then accumulated the nested
    compose roots of every unrelated project and reported them safe to delete.
    """
    container = _container("karakeep_web", compose_project="karakeep",
                           compose_working_dir="/apps/karakeep")
    nested = Resource(
        type="compose_project", key="/apps/karakeep/docker/docker-compose.yml",
        display="docker", path="/apps/karakeep/docker/docker-compose.yml",
        state="present",
        data={"working_dir": "/apps/karakeep/docker", "declared_name": None, "images": []},
    )
    apps = build_apps([container, nested], {})
    slugs = {r.slug for r, _ in apps}
    assert "docker" not in slugs, "a phantom app named after the sub-directory was created"

    by_slug = {r.slug: assocs for r, assocs in apps}
    assert any(
        a.resource_type == "compose_project" and a.resource_key == nested.key
        for a in by_slug["karakeep"]
    ), "nested compose file was not attached to karakeep"


def test_generic_basename_compose_root_does_not_collapse_unrelated_projects():
    """Two unrelated apps each with a `deploy/` compose root stay separate."""
    a = Resource(
        type="compose_project", key="/apps/gongyu/deploy/compose.yml",
        display="deploy", path="/apps/gongyu/deploy/compose.yml", state="present",
        data={"working_dir": "/apps/gongyu/deploy", "declared_name": None, "images": []},
    )
    b = Resource(
        type="compose_project", key="/apps/nocodb/deploy/compose.yml",
        display="deploy", path="/apps/nocodb/deploy/compose.yml", state="present",
        data={"working_dir": "/apps/nocodb/deploy", "declared_name": None, "images": []},
    )
    apps = build_apps([a, b], {})
    slugs = {r.slug for r, _ in apps}
    assert "deploy" not in slugs
    assert {"gongyu", "nocodb"} <= slugs, f"got {slugs}"

    # and neither app claims the other's compose root
    by_slug = {r.slug: assocs for r, assocs in apps}
    gongyu_keys = {x.resource_key for x in by_slug["gongyu"]}
    assert b.key not in gongyu_keys


def test_docker_builtin_networks_are_never_associated_to_an_app():
    """bridge/host/none always exist and can never be removed, so they must
    not appear in any app's resource set (a plan would emit network_rm)."""
    container = _container("solo", compose_project="solo", compose_working_dir="/apps/solo")
    nets = [
        Resource(type="network", key=name, display=name, path=None, state="active",
                 data={"attached_containers": ["solo"], "driver": driver})
        for name, driver in (("bridge", "bridge"), ("host", "host"), ("none", "null"))
    ]
    user_net = Resource(
        type="network", key="solo_default", display="solo_default", path=None,
        state="active", data={"attached_containers": ["solo"], "compose_project": "solo"},
    )
    apps = build_apps([container, *nets, user_net], {})
    _record, assocs = apps[0]
    network_keys = {a.resource_key for a in assocs if a.resource_type == "network"}
    assert network_keys == {"solo_default"}, f"built-ins leaked in: {network_keys}"


def test_app_claims_its_project_root_when_compose_lives_in_a_subdirectory():
    """`/apps/karakeep` belongs to karakeep even though its compose file is at
    `/apps/karakeep/docker/`.

    Regression: the directory step only matched an app's own working dir or a
    path *nested* under it, never the parent. The project root therefore fell
    to the 50-point name-similarity fallback, which is below the removable
    threshold — so removing the app left its entire directory on disk.
    """
    container = _container("karakeep_web", compose_project="karakeep",
                           compose_working_dir="/apps/karakeep/docker")
    project_root = Resource(
        type="directory", key="/apps/karakeep", display="karakeep",
        path="/apps/karakeep", state="present", data={"size_kb": 4096},
    )
    apps = build_apps([container, project_root], {})
    by_slug = {r.slug: assocs for r, assocs in apps}
    assoc = next(
        a for a in by_slug["karakeep"]
        if a.resource_type == "directory" and a.resource_key == "/apps/karakeep"
    )
    assert assoc.confidence >= 90, f"project root only reached {assoc.confidence}"
    assert assoc.removal_eligible == "safe"


def test_project_root_rule_stays_within_one_component_of_a_scan_root():
    """The rule must claim at most `{scan_root}/{one component}` — never the
    scan root itself, and never a sibling.

    The original version of this test only checked the sibling case, which the
    buggy rule never violated; it passed against the defect it was meant to
    catch. The real failure was reaching an ANCESTOR, so assert that directly.
    """
    a = _container("foo_web", compose_project="foo", compose_working_dir="/apps/foo/docker")
    sibling = Resource(
        type="directory", key="/apps/bar", display="bar",
        path="/apps/bar", state="present", data={},
    )
    scan_root = Resource(
        type="directory", key="/apps", display="apps",
        path="/apps", state="present", data={},
    )
    apps = build_apps([a, sibling, scan_root], {})
    by_slug = {r.slug: assocs for r, assocs in apps}
    claimed = {
        x.resource_key for x in by_slug["foo"]
        if x.resource_type == "directory" and x.confidence >= 90
    }
    assert "/apps" not in claimed, "claimed the scan root itself"
    assert "/apps/bar" not in claimed, "claimed a sibling"


def test_project_root_rule_does_not_claim_another_projects_tree():
    """An app with a clone inside someone else's tree must not claim that
    tree's root.

    Regression found in production: `banban` had a copy at
    /apps/agyinstall/banban, and the parent-directory rule then gave it
    /apps/agyinstall at confidence 92 — marking an unrelated project's root
    as shared and blocking its real owner from being removed cleanly.
    """
    banban = _container("banban", compose_project="banban",
                        compose_working_dir="/apps/banban/docker")
    clone = Resource(
        type="compose_project", key="/apps/agyinstall/banban/compose.yml",
        display="banban", path="/apps/agyinstall/banban/compose.yml", state="present",
        data={"working_dir": "/apps/agyinstall/banban", "declared_name": None, "images": []},
    )
    foreign_root = Resource(
        type="directory", key="/apps/agyinstall", display="agyinstall",
        path="/apps/agyinstall", state="present", data={},
    )
    own_root = Resource(
        type="directory", key="/apps/banban", display="banban",
        path="/apps/banban", state="present", data={},
    )
    apps = build_apps([banban, clone, foreign_root, own_root], {})
    by_slug = {r.slug: assocs for r, assocs in apps}
    claimed = {
        a.resource_key for a in by_slug["banban"]
        if a.resource_type == "directory" and a.confidence >= 90
    }
    assert "/apps/agyinstall" not in claimed, "claimed an unrelated project's root"
    assert "/apps/banban" in claimed, "should still claim its own project root"
