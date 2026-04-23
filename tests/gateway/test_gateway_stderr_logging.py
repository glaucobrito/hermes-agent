import logging

from gateway.run import _ensure_gateway_stderr_handler, _resolve_gateway_stderr_level, _sanitize_base_url_for_logging


def test_resolve_gateway_stderr_level_defaults_to_warning_on_tty(monkeypatch):
    monkeypatch.setattr("sys.stderr.isatty", lambda: True)
    assert _resolve_gateway_stderr_level(0) == logging.WARNING


def test_resolve_gateway_stderr_level_defaults_to_info_without_tty(monkeypatch):
    monkeypatch.setattr("sys.stderr.isatty", lambda: False)
    assert _resolve_gateway_stderr_level(0) == logging.INFO


def test_resolve_gateway_stderr_level_respects_verbose_and_quiet(monkeypatch):
    monkeypatch.setattr("sys.stderr.isatty", lambda: True)
    assert _resolve_gateway_stderr_level(1) == logging.INFO
    assert _resolve_gateway_stderr_level(2) == logging.DEBUG
    assert _resolve_gateway_stderr_level(None) is None


def test_ensure_gateway_stderr_handler_removes_handler_on_quiet():
    root = logging.getLogger()
    gateway_logger = logging.getLogger("gateway")
    before = list(root.handlers)
    original_level = root.level
    original_gateway_level = gateway_logger.level
    try:
        _ensure_gateway_stderr_handler(logging.DEBUG)
        marked = [h for h in root.handlers if getattr(h, "_hermes_gateway_stderr", False)]
        assert len(marked) == 1
        assert gateway_logger.getEffectiveLevel() == logging.DEBUG

        _ensure_gateway_stderr_handler(None)
        marked = [h for h in root.handlers if getattr(h, "_hermes_gateway_stderr", False)]
        assert marked == []
        assert gateway_logger.level == original_gateway_level
    finally:
        for handler in list(root.handlers):
            if handler not in before:
                root.removeHandler(handler)
                try:
                    handler.close()
                except Exception:
                    pass
        root.setLevel(original_level)
        gateway_logger.setLevel(original_gateway_level)


def test_gateway_stderr_filter_keeps_non_gateway_warnings():
    root = logging.getLogger()
    before = list(root.handlers)
    original_level = root.level
    try:
        _ensure_gateway_stderr_handler(logging.INFO, gateway_info_only=True)
        marked = [h for h in root.handlers if getattr(h, "_hermes_gateway_stderr", False)]
        assert len(marked) == 1
        handler = marked[0]

        info_gateway = logging.LogRecord("gateway.run", logging.INFO, __file__, 1, "msg", (), None)
        info_tool = logging.LogRecord("tools.mcp_tool", logging.INFO, __file__, 1, "msg", (), None)
        warn_tool = logging.LogRecord("tools.mcp_tool", logging.WARNING, __file__, 1, "warn", (), None)

        assert all(f.filter(info_gateway) for f in handler.filters)
        assert not all(f.filter(info_tool) for f in handler.filters)
        assert all(f.filter(warn_tool) for f in handler.filters)
    finally:
        for handler in list(root.handlers):
            if handler not in before:
                root.removeHandler(handler)
                try:
                    handler.close()
                except Exception:
                    pass
        root.setLevel(original_level)


def test_gateway_stderr_handler_verbose_mode_has_no_info_filter():
    root = logging.getLogger()
    before = list(root.handlers)
    original_level = root.level
    try:
        _ensure_gateway_stderr_handler(logging.INFO, gateway_info_only=False)
        marked = [h for h in root.handlers if getattr(h, "_hermes_gateway_stderr", False)]
        assert len(marked) == 1
        handler = marked[0]
        info_tool = logging.LogRecord("tools.mcp_tool", logging.INFO, __file__, 1, "msg", (), None)
        assert all(f.filter(info_tool) for f in handler.filters)
    finally:
        for handler in list(root.handlers):
            if handler not in before:
                root.removeHandler(handler)
                try:
                    handler.close()
                except Exception:
                    pass
        root.setLevel(original_level)


def test_sanitize_base_url_for_logging_drops_userinfo_and_query():
    assert _sanitize_base_url_for_logging(
        "https://user:pass@example.com/v1/chat?api_key=secret#frag"
    ) == "https://example.com/v1/chat"
