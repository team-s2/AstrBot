"""Privacy mode must discard content without breaking ordinary persistence."""

import asyncio
import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from sqlalchemy import text

from astrbot.core import privacy
from astrbot.core.agent.message import Message
from astrbot.core.agent.tool_image_cache import tool_image_cache
from astrbot.core.astr_main_agent import _get_session_conv
from astrbot.core.conversation_mgr import ConversationManager
from astrbot.core.db.sqlite import SQLiteDatabase
from astrbot.core.log import LogBroker, LogQueueHandler, _LoguruInterceptHandler
from astrbot.core.pipeline.process_stage.method.agent_sub_stages.internal import (
    InternalAgentSubStage,
)
from astrbot.core.pipeline.scheduler import PipelineScheduler
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.platform import Platform
from astrbot.core.provider.entities import LLMResponse, ProviderRequest
from astrbot.core.utils.astrbot_path import get_astrbot_temp_path
from astrbot.core.utils.trace import TraceSpan
from astrbot.dashboard.api.app import PrivacyMiddleware


@pytest_asyncio.fixture
async def private_db(tmp_path, monkeypatch):
    monkeypatch.setattr(privacy, "enabled", True)
    db = SQLiteDatabase(str(tmp_path / "private.db"))
    await db.initialize()
    try:
        yield db
    finally:
        await db.engine.dispose()


@pytest.mark.asyncio
async def test_private_conversation_lifecycle_and_no_disk_records(private_db):
    manager = ConversationManager(private_db)
    umo = "napcat:GroupMessage:test"
    with privacy.run_scope():
        cid = await manager.new_conversation(umo)
        await manager.update_conversation(
            umo, cid, history=[{"role": "user", "content": "PRIVATE_SENTINEL"}]
        )
        conversation = await manager.get_conversation(umo, cid)
        assert "PRIVATE_SENTINEL" in conversation.history
        assert await manager.get_curr_conversation_id(umo) == cid
        row = await private_db.get_conversation_by_id(cid)
        await private_db.insert_platform_message_history(
            "napcat", umo, {"text": "PRIVATE_SENTINEL"}
        )
        # The dashboard does not run in this context and cannot enumerate it.
        assert await manager.get_conversations() == []
        assert await private_db.get_platform_message_history("napcat", umo) == []
    assert row.content == []
    assert await manager.get_conversation(umo, cid) is None
    assert await manager.get_curr_conversation_id(umo) is None
    with privacy.run_scope():
        assert await manager.get_curr_conversation_id(umo) is None
        new_cid = await manager.new_conversation(umo)
        assert new_cid != cid
        assert (await manager.get_conversation(umo, new_cid)).history == "[]"
    async with private_db.engine.connect() as connection:
        for table in ("conversations", "platform_message_history", "preferences"):
            assert (
                await connection.execute(text(f"SELECT count(*) FROM {table}"))
            ).scalar() == 0


@pytest.mark.asyncio
async def test_agent_history_save_is_visible_only_in_its_private_run(private_db):
    manager = ConversationManager(private_db)
    context = SimpleNamespace(conversation_manager=manager)
    stage = object.__new__(InternalAgentSubStage)
    stage.conv_manager = manager
    event = MagicMock()
    event.unified_msg_origin = "napcat:GroupMessage:example"
    event.get_platform_id.return_value = "napcat"
    event.get_extra.return_value = None

    with privacy.run_scope():
        conversation = await _get_session_conv(event, context)
        request = ProviderRequest(prompt="PRIVATE_SENTINEL", conversation=conversation)
        await stage._save_to_history(
            event,
            request,
            LLMResponse(role="assistant", completion_text="private reply"),
            [
                Message(role="user", content="PRIVATE_SENTINEL"),
                Message(role="assistant", content="private reply"),
            ],
            runner_stats=None,
        )
        active = await _get_session_conv(event, context)
        assert active.cid == conversation.cid
        assert "PRIVATE_SENTINEL" in active.history
        assert "private reply" in active.history

    with privacy.run_scope():
        following = await _get_session_conv(event, context)
        assert following.cid != conversation.cid
        assert following.history == "[]"
    async with private_db.engine.connect() as connection:
        assert (
            await connection.execute(text("SELECT count(*) FROM conversations"))
        ).scalar() == 0


@pytest.mark.asyncio
async def test_concurrent_private_runs_do_not_share_context(private_db):
    manager = ConversationManager(private_db)
    ready = asyncio.Barrier(2)

    async def respond(content):
        with privacy.run_scope():
            cid = await manager.new_conversation("same-group")
            await manager.update_conversation(
                "same-group", cid, history=[{"role": "user", "content": content}]
            )
            await ready.wait()
            assert await manager.get_curr_conversation_id("same-group") == cid
            assert (await manager.get_conversation("same-group", cid)).history == (
                '[{"role": "user", "content": "' + content + '"}]'
            )
            return cid

    first, second = await asyncio.gather(respond("first"), respond("second"))
    assert first != second


@pytest.mark.asyncio
async def test_detached_task_cannot_repopulate_expired_run(private_db):
    proceed = asyncio.Event()

    async def detached(cid):
        await proceed.wait()
        assert await private_db.get_conversation_by_id(cid) is None
        assert await private_db.update_conversation(cid, content=["late"]) is None
        with pytest.raises(RuntimeError, match="active response"):
            await private_db.create_conversation("group", "napcat", content=["late"])
        with pytest.raises(RuntimeError, match="response has ended"):
            get_astrbot_temp_path()

    with privacy.run_scope():
        row = await private_db.create_conversation("group", "napcat")
        task = asyncio.create_task(detached(row.conversation_id))
    proceed.set()
    await task


def test_private_tool_images_and_adopted_media_are_removed(monkeypatch, tmp_path):
    monkeypatch.setattr(privacy, "enabled", True)
    # The singleton is initialized before any response. It must not keep using
    # that persistent cache directory when privacy mode is active.
    with privacy.run_scope():
        cached = tool_image_cache.save_image("c2VjcmV0", "tool-id", "tool")
        path = Path(cached.file_path)
        assert path.is_relative_to(get_astrbot_temp_path())
        assert path.read_bytes() == b"secret"
        event = object.__new__(AstrMessageEvent)
        event._temporary_local_files = [str(path)]
        event.untrack_temporary_local_file(str(path))
        assert event._temporary_local_files == [str(path)]
    assert not path.exists()


@pytest.mark.asyncio
async def test_private_mode_rejects_unscoped_conversations_and_attachments(private_db):
    with pytest.raises(RuntimeError, match="active response"):
        await private_db.create_conversation("group", "napcat", content=["secret"])
    with pytest.raises(RuntimeError, match="Attachment storage"):
        await private_db.insert_attachment("private-file", "image", "image/png")


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "failure", "cancel"])
async def test_pipeline_cleans_private_media_and_state(private_db, outcome):
    scheduler = object.__new__(PipelineScheduler)
    event = MagicMock()
    event.get_platform_id.return_value = "napcat"
    event.get_platform_name.return_value = "aiocqhttp"
    retained = {}

    async def process(_event):
        retained["state"] = privacy.current_run.get()
        retained["file"] = Path(get_astrbot_temp_path()) / "image.txt"
        retained["file"].write_text("PRIVATE_SENTINEL")
        await private_db.create_conversation("group", "napcat", content=["secret"])
        if outcome == "failure":
            raise ValueError("synthetic failure")
        if outcome == "cancel":
            raise asyncio.CancelledError

    scheduler._process_stages = process
    if outcome == "success":
        await scheduler.execute(event)
    else:
        with pytest.raises(
            ValueError if outcome == "failure" else asyncio.CancelledError
        ):
            await scheduler.execute(event)
    assert not retained["file"].exists()
    assert not retained["state"].active
    assert not retained["state"].conversations
    assert privacy.current_run.get() is None
    event.cleanup_temporary_local_files.assert_called_once()


def test_logs_and_trace_never_format_or_publish_content(monkeypatch):
    monkeypatch.setattr(privacy, "enabled", True)
    broker = LogBroker()
    queue = broker.register()
    handler = LogQueueHandler(broker)
    handler.format = MagicMock(side_effect=AssertionError("Must not format content"))
    record = logging.LogRecord(
        "astrbot.plugin.test",
        logging.ERROR,
        __file__,
        1,
        "PRIVATE_SENTINEL %s",
        ("secret",),
        None,
    )
    handler.emit(record)
    intercept = _LoguruInterceptHandler()
    record.getMessage = MagicMock(side_effect=AssertionError("Must not format content"))
    intercept.emit(record)
    monkeypatch.setattr("astrbot.core.utils.trace._cached_log_broker", broker)
    TraceSpan("test", message_outline="PRIVATE_SENTINEL").record(
        "response", text="secret"
    )
    broker.publish({"data": "PRIVATE_SENTINEL"})
    assert not broker.log_cache
    assert queue.empty()


def test_privacy_redacts_dashboard_platform_errors(monkeypatch):
    monkeypatch.setattr(privacy, "enabled", True)
    platform = MagicMock()
    platform._errors = []
    Platform.record_error(platform, "PRIVATE_SENTINEL", "PRIVATE_SENTINEL")
    assert "PRIVATE_SENTINEL" not in platform._errors[0].message
    assert platform._errors[0].traceback is None


def test_startup_setting_suppresses_console_file_and_trace(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "cmd_config.json").write_text(
        json.dumps(
            {
                "privacy_mode": True,
                "log_file_enable": True,
                "trace_enable": True,
                "trace_log_enable": True,
            }
        )
    )
    env = dict(os.environ, ASTRBOT_ROOT=str(tmp_path))
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
from astrbot.core import logger, privacy
from astrbot.core.log import _loguru
from astrbot.core.utils.trace import TraceSpan
assert privacy.enabled is True
logger.error('PRIVATE_SENTINEL')
_loguru.bind(payload='PRIVATE_SENTINEL').error('PRIVATE_SENTINEL')
TraceSpan('test', message_outline='PRIVATE_SENTINEL').record('response')
print('privacy-started')
""",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "privacy-started" in result.stdout
    assert "PRIVATE_SENTINEL" not in result.stdout + result.stderr
    for log in data.rglob("*.log"):
        assert "PRIVATE_SENTINEL" not in log.read_text()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path,kind",
    [
        ("/api/chat/send", "http"),
        ("/api/v1/chat/sessions/new", "http"),
        ("/api/v1/files/upload/chunk", "http"),
        ("/api/backup/export", "http"),
        ("/api/v1/backups", "http"),
        ("/api/unified_chat/ws", "websocket"),
        ("/api/v1/live-chat/ws", "websocket"),
    ],
)
async def test_privacy_rejects_persistent_routes_before_body(monkeypatch, path, kind):
    monkeypatch.setattr(privacy, "enabled", True)
    app = AsyncMock()
    receive = AsyncMock(side_effect=AssertionError("Must not read uploaded content"))
    send = AsyncMock()
    await PrivacyMiddleware(app)({"type": kind, "path": path}, receive, send)
    app.assert_not_awaited()
    receive.assert_not_awaited()
    first = send.await_args_list[0].args[0]
    assert first.get("status", first.get("code")) == (403 if kind == "http" else 1008)


@pytest.mark.asyncio
async def test_privacy_keeps_bot_delivery_and_settings_routes(monkeypatch):
    monkeypatch.setattr(privacy, "enabled", True)
    app = AsyncMock()
    middleware = PrivacyMiddleware(app)
    for path in (
        "/api/v1/im/messages",
        "/api/v1/config-profiles/default",
        "/api/v1/auth/login",
    ):
        await middleware({"type": "http", "path": path}, AsyncMock(), AsyncMock())
    assert app.await_count == 3


@pytest.mark.asyncio
async def test_disabled_mode_preserves_existing_persistence(private_db, monkeypatch):
    monkeypatch.setattr(privacy, "enabled", False)
    row = await private_db.create_conversation("group", "napcat", content=["ordinary"])
    await private_db.insert_platform_message_history(
        "napcat", "group", {"text": "ordinary"}
    )
    assert (await private_db.get_conversation_by_id(row.conversation_id)).content == [
        "ordinary"
    ]
    assert await private_db.count_platform_message_history("napcat", "group") == 1
    # Enabling privacy hides old data but does not destructively erase it.
    monkeypatch.setattr(privacy, "enabled", True)
    assert await private_db.get_conversation_by_id(row.conversation_id) is None
    assert await private_db.get_platform_message_history("napcat", "group") == []
    monkeypatch.setattr(privacy, "enabled", False)
    assert (await private_db.get_conversation_by_id(row.conversation_id)).content == [
        "ordinary"
    ]
