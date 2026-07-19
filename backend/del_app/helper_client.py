"""Unix-socket JSON client to del-helper (the root-privileged daemon)."""
from __future__ import annotations

import json
import socket

from del_app.config import get_settings


class HelperError(Exception):
    """Raised on transport failure talking to del-helper."""


def call(op: str, args: dict, dry_run: bool = True, timeout: int = 300,
         plan_id: int | None = None, step_id: int | None = None) -> dict:
    """Send {op, args, dry_run, plan_id?, step_id?} as JSON over the helper
    unix socket and return the parsed JSON response
    {ok, dry_run, output, error, changed}.

    Raises HelperError on any transport failure (socket missing, refused,
    timeout, malformed response).
    """
    request = {"op": op, "args": args, "dry_run": dry_run}
    if plan_id is not None:
        request["plan_id"] = plan_id
    if step_id is not None:
        request["step_id"] = step_id

    settings = get_settings()
    payload = json.dumps(request).encode() + b"\n"

    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(settings.helper_socket)
            sock.sendall(payload)
            sock.shutdown(socket.SHUT_WR)
            chunks = []
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
    except OSError as e:
        raise HelperError(f"helper transport failure: {e}") from e

    raw = b"".join(chunks)
    if not raw:
        raise HelperError("empty response from helper")
    try:
        return json.loads(raw.decode())
    except json.JSONDecodeError as e:
        raise HelperError(f"malformed helper response: {e}") from e
