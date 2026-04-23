import json
import weakref
from types import SimpleNamespace

from gateway import status
from gateway.run import GatewayRunner, _ensure_gateway_stderr_handler
from gateway.config import Platform


def test_write_runtime_status_records_last_inbound_and_outbound(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    status.write_runtime_status(
        gateway_state="running",
        activity_direction="inbound",
        activity_platform="telegram",
        activity_chat_id="123",
        activity_thread_id="840",
        activity_user_id="237757106",
        activity_chat_type="group",
        activity_message_id="111",
    )
    status.write_runtime_status(
        activity_direction="outbound",
        activity_platform="telegram",
        activity_chat_id="123",
        activity_thread_id="840",
        activity_message_id="222",
    )

    payload = status.read_runtime_status()
    assert payload["last_inbound"]["platform"] == "telegram"
    assert payload["last_inbound"]["chat_id"] == "123"
    assert payload["last_inbound"]["thread_id"] == "840"
    assert payload["last_inbound"]["user_id"] == "237757106"
    assert payload["last_inbound"]["chat_type"] == "group"
    assert payload["last_inbound"]["message_id"] == "111"
    assert payload["last_outbound"]["platform"] == "telegram"
    assert payload["last_outbound"]["message_id"] == "222"


def test_write_runtime_status_preserves_previous_outbound_identifier_when_new_one_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    status.write_runtime_status(
        activity_direction="outbound",
        activity_platform="telegram",
        activity_chat_id="123",
        activity_thread_id="840",
        activity_message_id="222",
    )
    status.write_runtime_status(
        activity_direction="outbound",
        activity_platform="telegram",
        activity_chat_id="123",
        activity_thread_id="840",
        activity_message_id=None,
    )

    payload = status.read_runtime_status()
    assert payload["last_outbound"]["message_id"] == "222"


async def _fake_send_success(chat_id, content, *args, **kwargs):
    return SimpleNamespace(success=True, message_id="msg-1")


class _FakeAdapter:
    async def send(self, chat_id, content, *args, **kwargs):
        return await _fake_send_success(chat_id, content, *args, **kwargs)

    async def send_document(self, chat_id, file_path, *args, **kwargs):
        return await _fake_send_success(chat_id, file_path, *args, **kwargs)


def test_instrument_adapter_send_records_outbound(monkeypatch):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._health_wrapped_adapters = weakref.WeakSet()
    recorded = []
    runner._record_message_activity = lambda direction, **kwargs: recorded.append((direction, kwargs))

    adapter = _FakeAdapter()
    runner._instrument_adapter_send(Platform.TELEGRAM, adapter)

    import asyncio
    result = asyncio.run(adapter.send("123", "oi", metadata={"thread_id": "840"}))

    assert result.success is True
    assert recorded == [(
        "outbound",
        {
            "platform": "telegram",
            "chat_id": "123",
            "thread_id": "840",
            "message_id": "msg-1",
        },
    )]


class _FallbackAdapter(_FakeAdapter):
    async def send_document(self, chat_id, file_path, *args, **kwargs):
        return await self.send(chat_id, f"fallback:{file_path}", *args, **kwargs)


def test_instrument_adapter_send_records_media_methods(monkeypatch):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._health_wrapped_adapters = weakref.WeakSet()
    recorded = []
    runner._record_message_activity = lambda direction, **kwargs: recorded.append((direction, kwargs))

    adapter = _FakeAdapter()
    runner._instrument_adapter_send(Platform.TELEGRAM, adapter)

    import asyncio
    result = asyncio.run(adapter.send_document("123", "/tmp/file.pdf", metadata={"thread_id": "840"}))

    assert result.success is True
    assert recorded == [(
        "outbound",
        {
            "platform": "telegram",
            "chat_id": "123",
            "thread_id": "840",
            "message_id": "msg-1",
        },
    )]


def test_instrument_adapter_send_dedupes_media_fallback_paths(monkeypatch):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._health_wrapped_adapters = weakref.WeakSet()
    recorded = []
    runner._record_message_activity = lambda direction, **kwargs: recorded.append((direction, kwargs))

    adapter = _FallbackAdapter()
    runner._instrument_adapter_send(Platform.TELEGRAM, adapter)

    import asyncio
    result = asyncio.run(adapter.send_document("123", "/tmp/file.pdf", metadata={"thread_id": "840"}))

    assert result.success is True
    assert len(recorded) == 1


def test_log_turn_route_decision_emits_structured_line(caplog):
    runner = GatewayRunner.__new__(GatewayRunner)
    source = SimpleNamespace(platform=Platform.TELEGRAM, chat_id="-100123")

    with caplog.at_level("INFO"):
        runner._log_turn_route_decision(
            route={
                "model": "anthropic/claude-sonnet-4.6",
                "reason": "primary",
                "runtime": {"provider": "openrouter", "base_url": "https://openrouter.ai/api"},
            },
            source=source,
            session_key="telegram:-100123:840",
            request_kind="message",
        )

    assert "routing decision:" in caplog.text
    assert "kind=message" in caplog.text
    assert "decider=hermes-gateway" in caplog.text
    assert "executor=aiagent" in caplog.text
    assert "reviewer=none" in caplog.text
    assert "model=anthropic/claude-sonnet-4.6" in caplog.text
    assert "platform=telegram" in caplog.text
    assert "provider=openrouter" in caplog.text
    assert "reason=primary" in caplog.text
    assert "chat=-100123" not in caplog.text
    assert "session=telegram:-100123:840" not in caplog.text


def test_internal_events_do_not_record_last_inbound(monkeypatch):
    runner = GatewayRunner.__new__(GatewayRunner)
    recorded = []
    runner._record_message_activity = lambda direction, **kwargs: recorded.append((direction, kwargs))
    runner._is_user_authorized = lambda source: True
    runner._session_key_for_source = lambda source: "session:test"
    runner._update_prompt_pending = {}
    runner._running_agents_ts = {}
    runner._running_agents = {}
    runner._cancel_pending_restart_for_session = lambda *args, **kwargs: False

    async def _fake_handle_message_with_agent(event, source, quick_key, run_generation=None):
        return None

    runner._handle_message_with_agent = _fake_handle_message_with_agent

    source = SimpleNamespace(
        platform=Platform.TELEGRAM,
        chat_id="-100123",
        thread_id="840",
        user_id="237757106",
        chat_type="group",
        user_name="Glauco",
    )
    event = SimpleNamespace(
        source=source,
        internal=True,
        message_id="5010",
        text="background completion",
        get_command=lambda: None,
    )

    import asyncio
    asyncio.run(runner._handle_message(event))

    assert recorded == []


def test_unauthorized_events_still_record_last_inbound(monkeypatch):
    runner = GatewayRunner.__new__(GatewayRunner)
    recorded = []
    runner._record_message_activity = lambda direction, **kwargs: recorded.append((direction, kwargs))
    runner._is_user_authorized = lambda source: False
    runner._get_unauthorized_dm_behavior = lambda platform: "ignore"

    source = SimpleNamespace(
        platform=Platform.TELEGRAM,
        chat_id="-100123",
        thread_id="840",
        user_id="237757106",
        chat_type="group",
        user_name="Glauco",
    )
    event = SimpleNamespace(
        source=source,
        internal=False,
        message_id="5010",
        text="oi",
        get_command=lambda: None,
    )

    import asyncio
    assert asyncio.run(runner._handle_message(event)) is None
    assert recorded and recorded[0][0] == "inbound"


def test_ensure_gateway_stderr_handler_dedupes(monkeypatch):
    root = __import__("logging").getLogger()
    before = list(root.handlers)
    original_level = root.level
    try:
        _ensure_gateway_stderr_handler(20)
        _ensure_gateway_stderr_handler(30)
        marked = [h for h in root.handlers if getattr(h, "_hermes_gateway_stderr", False)]
        assert len(marked) == 1
        assert marked[0].level == 30
    finally:
        for handler in list(root.handlers):
            if handler not in before:
                root.removeHandler(handler)
                try:
                    handler.close()
                except Exception:
                    pass
        root.setLevel(original_level)
