import sys
import threading

import pytest

sys.path.insert(0, "/apps/del/backend")

from del_app import scanner  # noqa: E402
from del_app.web import gallery  # noqa: E402

# How long a test's leftover threads get to finish once its body returns.
_THREAD_JOIN_TIMEOUT = 10.0


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "real_scanner: run the real scanner.run_scan instead of the test stub",
    )


@pytest.fixture(autouse=True)
def _no_host_work_from_background_threads(request, monkeypatch):
    """Keep DEL's background threads off the real host.

    A successful live job ends with a post-removal scanner.run_scan(), and any
    dashboard render refreshes `docker system df` in a thread. Under test those
    were real host scans that outlived the test, wrote into whichever temp DB
    was current next ("no such table: audit_log") and collided with other
    tests over scanner._scan_lock ("release unlocked lock").
    """
    if request.node.get_closest_marker("real_scanner") is None:
        monkeypatch.setattr(scanner, "run_scan", lambda: 0)
    monkeypatch.setattr(gallery, "_compute_reclaimable_bytes", lambda: 0)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item):
    """Join every thread DEL code started during the test body, before any
    fixture teardown.

    Routes and jobs start daemon threads (jobs.execute_job, /scan, the gallery
    stale-while-revalidate refreshers). Joining here, rather than in a fixture
    finalizer, guarantees they finish while the test's temp DB, config env var
    and monkeypatches are all still in place, whatever the fixture order.
    Only threads started directly from a del_app module are tracked: the
    TestClient's own AnyIO workers live until the client fixture closes.
    """
    started = []
    real_start = threading.Thread.start

    def tracking_start(self):
        if sys._getframe(1).f_globals.get("__name__", "").startswith("del_app."):
            started.append(self)
        real_start(self)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(threading.Thread, "start", tracking_start)
        try:
            result = yield
        except BaseException:
            _join(started)
            raise
        alive = _join(started)
    if alive:
        pytest.fail(f"threads still running {_THREAD_JOIN_TIMEOUT}s after the test: {alive}")
    return result


def _join(threads):
    for t in threads:
        t.join(_THREAD_JOIN_TIMEOUT)
    return [t.name for t in threads if t.is_alive()]
