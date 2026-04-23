import sys
from types import SimpleNamespace

from gateway import run as gateway_run


class _OneShotStopEvent:
    def __init__(self):
        self._set = False

    def is_set(self):
        return self._set

    def wait(self, timeout=None):
        self._set = True
        return True


def test_cron_ticker_emits_runtime_status_heartbeat(monkeypatch):
    calls = []

    monkeypatch.setitem(
        sys.modules,
        "cron.scheduler",
        SimpleNamespace(tick=lambda **kwargs: calls.append(("cron_tick", kwargs))),
    )
    monkeypatch.setitem(
        sys.modules,
        "gateway.platforms.base",
        SimpleNamespace(
            cleanup_image_cache=lambda max_age_hours=24: 0,
            cleanup_document_cache=lambda max_age_hours=24: 0,
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "gateway.status",
        SimpleNamespace(
            write_runtime_status=lambda **kwargs: calls.append(("write_runtime_status", kwargs))
        ),
    )

    stop_event = _OneShotStopEvent()
    gateway_run._start_cron_ticker(stop_event, adapters=None, loop=None, interval=0)

    assert ("write_runtime_status", {}) in calls
