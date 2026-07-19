"""Pure validation logic for the DEL privileged helper daemon.

Every function here is side-effect free (aside from filesystem *reads* such as
``os.path.realpath`` / ``os.path.exists`` / ``os.path.ismount``) and raises
``ValidationError`` on any policy violation. They are imported directly by the
daemon and exercised in isolation by the test-suite.

Security model notes
--------------------
* Path safety is anchored on ``os.path.realpath``: the canonical, symlink-resolved
  absolute path. All containment checks are performed against the *realpath*, so a
  symlink that points outside an approved root cannot escape — after resolution the
  realpath simply will not start with an approved root and is rejected. This single
  containment rule therefore covers the "no symlink escape" requirement.
* A path is deletable only if its realpath is strictly *deeper* than an approved
  root (root + at least one path component) AND is not itself (nor resolves to) a
  protected root AND is not on the never-delete list AND is not a mountpoint.
"""

from __future__ import annotations

import os
import re
from typing import Iterable


class ValidationError(ValueError):
    """Raised when an argument fails a helper policy check."""


# ---------------------------------------------------------------------------
# regexes
# ---------------------------------------------------------------------------
# systemd unit / timer names
_UNIT_RE = re.compile(r"\A[A-Za-z0-9@_.\-]+\.(service|timer)\Z")
# docker object identifiers (container / volume / network / image tag component)
_DOCKER_NAME_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.\-]*\Z")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _norm_root(root: str) -> str:
    """Normalise an approved/protected root to a realpath with no trailing slash."""
    r = os.path.realpath(root)
    if r != "/" and r.endswith("/"):
        r = r.rstrip("/")
    return r


def _is_under(realpath: str, root: str) -> bool:
    """True if ``realpath`` is strictly inside ``root`` (deeper by >=1 component)."""
    root = _norm_root(root)
    if realpath == root:
        return False
    if root == "/":
        return realpath.startswith("/") and realpath != "/"
    return realpath.startswith(root + os.sep)


def _matched_root(realpath: str, roots: Iterable[str]) -> str | None:
    for root in roots:
        if _is_under(realpath, root):
            return _norm_root(root)
    return None


# ---------------------------------------------------------------------------
# path deletion
# ---------------------------------------------------------------------------
def validate_path_for_deletion(path: str, policy: dict) -> str:
    """Validate ``path`` for deletion and return its canonical realpath.

    Rules enforced (in order):
      1. must be a non-empty absolute path string
      2. realpath must exist
      3. realpath must live strictly under an approved deletion root
         (root + at least one deeper component)
      4. realpath must NOT be, nor resolve to, any protected root
      5. realpath must NOT be, nor be under, any never-delete entry
      6. realpath must NOT be a mountpoint
    Symlink escape is impossible because every check uses the realpath: a link
    pointing outside an approved root resolves to a realpath that fails rule 3.
    """
    if not isinstance(path, str) or not path:
        raise ValidationError("path must be a non-empty string")
    if not os.path.isabs(path):
        raise ValidationError(f"path must be absolute: {path!r}")

    realpath = os.path.realpath(path)

    if not os.path.isabs(realpath):
        raise ValidationError(f"resolved path is not absolute: {realpath!r}")
    if not os.path.exists(realpath):
        raise ValidationError(f"path does not exist: {realpath!r}")

    approved = policy.get("approved_deletion_roots", [])
    if _matched_root(realpath, approved) is None:
        raise ValidationError(
            f"path is not under an approved deletion root: {realpath!r}"
        )

    protected = {_norm_root(p) for p in policy.get("protected_roots", [])}
    if realpath in protected:
        raise ValidationError(f"path is a protected root, refusing: {realpath!r}")

    for entry in policy.get("never_delete", []):
        n = _norm_root(entry)
        if realpath == n or _is_under(realpath, n):
            raise ValidationError(
                f"path is on the never-delete list, refusing: {realpath!r}"
            )

    if os.path.ismount(realpath):
        raise ValidationError(f"path is a mountpoint, refusing: {realpath!r}")

    # defence-in-depth: never operate on a top-level path
    if realpath.count("/") < 2:
        raise ValidationError(f"path is too shallow, refusing: {realpath!r}")

    return realpath


# ---------------------------------------------------------------------------
# systemd unit names
# ---------------------------------------------------------------------------
def validate_unit_name(name: str, policy: dict | None = None,
                       require_unit_file: bool = False) -> str:
    """Validate a systemd unit/timer name.

    Rejects anything that is not ``^[A-Za-z0-9@_.\\-]+\\.(service|timer)$`` — which
    excludes shell metacharacters, spaces, slashes and injection attempts such as
    ``foo.service; rm -rf /``.  When ``require_unit_file`` is set (unit removal),
    the corresponding file must exist under the configured systemd unit dir.
    """
    if not isinstance(name, str) or not _UNIT_RE.match(name):
        raise ValidationError(f"invalid systemd unit name: {name!r}")

    if require_unit_file:
        unit_dir = _norm_root((policy or {}).get(
            "systemd_unit_dir", "/etc/systemd/system"))
        candidate = os.path.join(unit_dir, name)
        realpath = os.path.realpath(candidate)
        if not _is_under(realpath, unit_dir):
            raise ValidationError(
                f"unit file escapes {unit_dir}: {realpath!r}")
        if not os.path.isfile(realpath):
            raise ValidationError(f"unit file not found: {candidate!r}")
        return realpath

    return name


# ---------------------------------------------------------------------------
# docker object names
# ---------------------------------------------------------------------------
def _validate_docker_name(value: str, kind: str) -> str:
    if not isinstance(value, str) or not _DOCKER_NAME_RE.match(value):
        raise ValidationError(f"invalid docker {kind} name: {value!r}")
    return value


def validate_container_id(value: str) -> str:
    return _validate_docker_name(value, "container")


def validate_volume_name(value: str) -> str:
    return _validate_docker_name(value, "volume")


def validate_network_name(value: str) -> str:
    return _validate_docker_name(value, "network")


def validate_image_id(value: str) -> str:
    # image ids may be a sha256:hex digest, a short id, or a name[:tag][@digest]
    if not isinstance(value, str) or not value:
        raise ValidationError(f"invalid image id: {value!r}")
    if not re.match(r"\A[A-Za-z0-9][A-Za-z0-9_.\-:/@]*\Z", value):
        raise ValidationError(f"invalid image id: {value!r}")
    return value


# ---------------------------------------------------------------------------
# compose config files
# ---------------------------------------------------------------------------
def validate_compose_files(files, policy: dict) -> list[str]:
    """Each compose file must exist and resolve under an approved root."""
    if not isinstance(files, list) or not files:
        raise ValidationError("config_files must be a non-empty list")
    approved = policy.get("approved_deletion_roots", [])
    out: list[str] = []
    for f in files:
        if not isinstance(f, str) or not os.path.isabs(f):
            raise ValidationError(f"compose file must be absolute: {f!r}")
        realpath = os.path.realpath(f)
        if not os.path.isfile(realpath):
            raise ValidationError(f"compose file does not exist: {f!r}")
        if _matched_root(realpath, approved) is None:
            raise ValidationError(
                f"compose file not under an approved root: {realpath!r}")
        out.append(realpath)
    return out


# ---------------------------------------------------------------------------
# nginx site files
# ---------------------------------------------------------------------------
def validate_nginx_site_paths(paths, policy: dict) -> list[str]:
    """Each nginx site path must resolve under sites-available/sites-enabled."""
    if not isinstance(paths, list) or not paths:
        raise ValidationError("paths must be a non-empty list")
    roots = policy.get("nginx_site_roots",
                       ["/etc/nginx/sites-available", "/etc/nginx/sites-enabled"])
    out: list[str] = []
    for p in paths:
        if not isinstance(p, str) or not os.path.isabs(p):
            raise ValidationError(f"nginx site path must be absolute: {p!r}")
        literal = os.path.abspath(p)
        # The literal path must sit directly inside a site root: it is what
        # gets removed (an enabled symlink must be removed as a symlink, never
        # dereferenced to its sites-available target).
        if os.path.dirname(literal) not in roots:
            raise ValidationError(
                f"nginx site path not directly under sites-available/enabled: {literal!r}")
        # Its resolution must also stay inside the roots (no symlink escape).
        realpath = os.path.realpath(literal)
        if _matched_root(realpath, roots) is None:
            raise ValidationError(
                f"nginx site path resolves outside sites-available/enabled: {realpath!r}")
        out.append(literal)
    return out


# ---------------------------------------------------------------------------
# cron.d files
# ---------------------------------------------------------------------------
def validate_cron_path(path: str, policy: dict) -> str:
    """A cron.d file must resolve under the configured cron.d dir and exist."""
    cron_dir = policy.get("cron_d_dir", "/etc/cron.d")
    if not isinstance(path, str) or not os.path.isabs(path):
        raise ValidationError(f"cron path must be absolute: {path!r}")
    realpath = os.path.realpath(path)
    if _matched_root(realpath, [cron_dir]) is None:
        raise ValidationError(f"cron path not under {cron_dir}: {realpath!r}")
    if not os.path.isfile(realpath):
        raise ValidationError(f"cron file does not exist: {path!r}")
    return realpath


# ---------------------------------------------------------------------------
# backup destinations / restore sources
# ---------------------------------------------------------------------------
def validate_backup_dest(dest: str, policy: dict) -> str:
    """A backup destination must resolve strictly under the backup dir."""
    backup_dir = policy.get("backup_dir", "/apps/del/backups")
    if not isinstance(dest, str) or not os.path.isabs(dest):
        raise ValidationError(f"backup dest must be absolute: {dest!r}")
    # resolve the *parent* (dest itself will not exist yet)
    realpath = os.path.realpath(dest)
    if not _is_under(realpath, backup_dir):
        raise ValidationError(
            f"backup dest must be under {backup_dir}: {realpath!r}")
    return realpath


def validate_backup_source(backup_path: str, policy: dict) -> str:
    """A restore source (a previously-made backup) must live under the backup dir."""
    backup_dir = policy.get("backup_dir", "/apps/del/backups")
    if not isinstance(backup_path, str) or not os.path.isabs(backup_path):
        raise ValidationError(f"backup_path must be absolute: {backup_path!r}")
    realpath = os.path.realpath(backup_path)
    if not _is_under(realpath, backup_dir):
        raise ValidationError(
            f"backup_path must be under {backup_dir}: {realpath!r}")
    if not os.path.exists(realpath):
        raise ValidationError(f"backup_path does not exist: {realpath!r}")
    return realpath
