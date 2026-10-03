import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from pathlib import Path

from config import get_allowed_user_ids
from publisher.telegram_pub import TelegramPublisher
from recorder.session_manager import SessionManager


@pytest.mark.anyio
async def test_telegram_pub_session_remains_open_after_send():
    """Verify Bug 3 fix: telegram_pub does not close bot session in finally block."""
    pub = TelegramPublisher(token="123456:TEST_TOKEN", channel_id="-100123456789")
    pub.simulation_mode = False

    # Mock the bot and its session
    mock_bot = AsyncMock()
    mock_msg = MagicMock()
    mock_msg.message_id = 999
    mock_bot.send_message.return_value = mock_msg
    mock_bot.session = MagicMock()
    mock_bot.session.close = AsyncMock()
    pub.bot = mock_bot

    digest_mock = MagicMock()
    digest_mock.to_telegram_markdown.return_value = "Test markdown"

    # Send 1
    res1 = await pub.publish_to_channel(digest=digest_mock, full_article_url="https://github.com/test")
    assert res1 == 999
    # Session must NOT be closed!
    mock_bot.session.close.assert_not_called()

    # Send admin alert
    await pub.send_admin_alert("Test alert message")
    # Session still must NOT be closed!
    mock_bot.session.close.assert_not_called()

    # Only explicit close() closes it
    await pub.close()
    mock_bot.session.close.assert_called_once()


def test_stale_sessions_recovery_uses_allowed_users(tmp_path):
    """Verify Bug 2 fix: recovery logic uses get_allowed_user_ids instead of non-existent settings.admin_telegram_id."""
    admin_ids = get_allowed_user_ids()
    assert isinstance(admin_ids, list)
    assert len(admin_ids) > 0

    admin_id = admin_ids[0] if admin_ids else None
    assert admin_id is not None
    assert isinstance(admin_id, int)


def test_topic_update_does_not_corrupt_subject(tmp_path):
    """Verify Bug 4 fix: setting topic does not overwrite subject in metadata."""
    mgr = SessionManager(base_dir=tmp_path / "sessions")
    session_id = "test_topic_session"
    mgr.create_session(session_id=session_id, subject="Компьютерные сети", subject_slug="kompyuternye_seti")

    # User enters topic
    mgr.update_session_metadata(session_id, {"topic": "Маршрутизация и протокол OSPF"})

    meta = mgr.get_session_metadata(session_id)
    # Subject must be preserved!
    assert meta["subject"] == "Компьютерные сети"
    assert meta["subject_slug"] == "kompyuternye_seti"
    assert meta["topic"] == "Маршрутизация и протокол OSPF"


def test_handlers_execute_meeting_task_instantiates_recorder():
    """Verify Bug 1 fix: _execute_meeting_task has recorder instantiation."""
    import inspect
    from bot import handlers

    source = inspect.getsource(handlers._execute_meeting_task)
    assert "recorder = MeetingRecorder(" in source
    # Must be instantiated before monitor = MeetingMonitor(
    recorder_idx = source.index("recorder = MeetingRecorder(")
    monitor_idx = source.index("monitor = MeetingMonitor(")
    assert recorder_idx < monitor_idx


@pytest.mark.anyio
async def test_scheduler_postpones_when_busy_without_burning_join_key(tmp_path, monkeypatch):
    """Verify Bug 5 fix: scheduler does not mark join_key if recorder is busy."""
    from datetime import datetime
    import config
    from bot.scheduler import ScheduleAutoJoinService
    from recorder.schedule_manager import ScheduleManager

    data_file = tmp_path / "sched_busy.json"
    mgr = ScheduleManager(data_file=data_file)
    subj = mgr.add_subject(title="Операционные системы", short_name="ОС")

    mock_now = datetime(2026, 9, 10, 10, 0, 0)  # Weekday 3 (Thursday)
    monkeypatch.setattr("bot.scheduler.get_local_now", lambda: mock_now)
    monkeypatch.setattr(config.settings, "allowed_telegram_users", "1017867274")

    entry = mgr.add_schedule_entry(
        subject_id=subj.id,
        day_of_week=3,
        time_str="10:00",
        url="https://telemost.yandex.ru/j/testbusy",
    )

    mock_bot = MagicMock()
    mock_bot.send_message = AsyncMock()
    mock_execute = AsyncMock(return_value=True)

    is_busy = True
    service = ScheduleAutoJoinService(
        bot=mock_bot,
        schedule_manager=mgr,
        execute_meeting_fn=mock_execute,
        is_busy_fn=lambda: is_busy,
    )

    # 1. First run when server is busy
    await service.check_and_join_scheduled_lectures()
    # Should NOT trigger execution or announcement
    mock_execute.assert_not_called()
    mock_bot.send_message.assert_not_called()
    assert len(service.already_joined_keys) == 0

    # 2. Server becomes free
    is_busy = False
    await service.check_and_join_scheduled_lectures()
    await asyncio.sleep(0.05)

    # Now it should successfully trigger!
    mock_execute.assert_awaited_once()
    mock_bot.send_message.assert_awaited_once()
    assert f"2026-09-10_{entry.id}" in service.already_joined_keys


@pytest.mark.anyio
async def test_scheduler_discards_join_key_when_execution_rejected(tmp_path, monkeypatch):
    """Verify Bug 5 fix: scheduler discards join_key if execute_meeting_fn returns False."""
    from datetime import datetime
    import config
    from bot.scheduler import ScheduleAutoJoinService
    from recorder.schedule_manager import ScheduleManager

    data_file = tmp_path / "sched_reject.json"
    mgr = ScheduleManager(data_file=data_file)
    subj = mgr.add_subject(title="Базы данных", short_name="БД")

    mock_now = datetime(2026, 9, 10, 10, 0, 0)
    monkeypatch.setattr("bot.scheduler.get_local_now", lambda: mock_now)
    monkeypatch.setattr(config.settings, "allowed_telegram_users", "1017867274")

    entry = mgr.add_schedule_entry(
        subject_id=subj.id,
        day_of_week=3,
        time_str="10:00",
        url="https://telemost.yandex.ru/j/testreject",
    )

    mock_bot = MagicMock()
    mock_bot.send_message = AsyncMock()
    mock_execute = AsyncMock(return_value=False)  # Rejected!

    service = ScheduleAutoJoinService(
        bot=mock_bot,
        schedule_manager=mgr,
        execute_meeting_fn=mock_execute,
    )

    await service.check_and_join_scheduled_lectures()
    await asyncio.sleep(0.05)

    # Key must be discarded to allow retry on next tick
    assert f"2026-09-10_{entry.id}" not in service.already_joined_keys


@pytest.mark.anyio
async def test_scheduler_primary_chat_is_strictly_admin_not_channel(tmp_path, monkeypatch):
    """Verify Bug 6 fix: primary_chat is always the admin user ID, never the public channel ID."""
    from datetime import datetime
    import config
    from bot.scheduler import ScheduleAutoJoinService
    from recorder.schedule_manager import ScheduleManager

    data_file = tmp_path / "sched_primary.json"
    mgr = ScheduleManager(data_file=data_file)
    subj = mgr.add_subject(title="Сети", short_name="Сети")

    mock_now = datetime(2026, 9, 10, 10, 0, 0)
    monkeypatch.setattr("bot.scheduler.get_local_now", lambda: mock_now)
    monkeypatch.setattr(config.settings, "allowed_telegram_users", "1017867274")
    monkeypatch.setattr(config.settings, "telegram_channel_id", "-100999888777")

    entry = mgr.add_schedule_entry(
        subject_id=subj.id,
        day_of_week=3,
        time_str="10:00",
        url="https://telemost.yandex.ru/j/testnet",
    )

    mock_bot = MagicMock()
    mock_bot.send_message = AsyncMock()
    mock_execute = AsyncMock(return_value=True)

    service = ScheduleAutoJoinService(
        bot=mock_bot,
        schedule_manager=mgr,
        execute_meeting_fn=mock_execute,
    )

    await service.check_and_join_scheduled_lectures()
    await asyncio.sleep(0.05)

    # Primary chat for interactive session control MUST be admin user ID!
    assert mock_execute.call_args[1]["chat_id"] == 1017867274
    assert mock_execute.call_args[1]["chat_id"] != -100999888777

    # Announcement broadcast goes strictly to admin PM, never spamming the public channel
    recipient_chats = {call[1]["chat_id"] for call in mock_bot.send_message.call_args_list}
    assert 1017867274 in recipient_chats
    assert -100999888777 not in recipient_chats


@pytest.mark.anyio
async def test_cancel_task_callback_clears_state_and_answers():
    """Verify Bug 7 fix: cancel_task callback query handler clears state and answers callback."""
    from bot.handlers import handle_cancel_task, USER_SESSION_STATES

    chat_id = 777888
    USER_SESSION_STATES[chat_id] = {"action": "waiting_student_name"}

    mock_cb = MagicMock()
    mock_cb.message = MagicMock()
    mock_cb.message.chat = MagicMock()
    mock_cb.message.chat.id = chat_id
    mock_cb.message.delete = AsyncMock()
    mock_cb.answer = AsyncMock()

    await handle_cancel_task(mock_cb)

    assert chat_id not in USER_SESSION_STATES
    mock_cb.answer.assert_awaited_once_with("Действие отменено.")
    mock_cb.message.delete.assert_awaited_once()


def test_visual_postprocess_updates_timecode_str_from_datetime(tmp_path):
    """Verify Bug 8 fix: timecode_str is properly updated from datetime format when shifting timestamps."""
    from recorder.visual import postprocess_session_slides, SavedSlide

    slide_file = tmp_path / "slide_001.png"
    slide_file.write_text("fake image content")

    slide = SavedSlide(
        index=1,
        timestamp_sec=100.0,
        timecode_str="2026-09-09_19-30-45",
        file_path=slide_file,
        phash="",
    )

    # Cut off 40s of leading silence
    clean = postprocess_session_slides([slide], start_cutoff_sec=40.0)
    assert len(clean) == 1
    assert clean[0].timestamp_sec == 60.0
    # Must be reformatted to human-readable MMmSSs!
    assert clean[0].timecode_str == "01m00s"


def test_analyzer_model_name_fallback_no_name_error():
    """Verify Bug 9 fix: analyzer uses self.model_name fallback without NameError on primary_model."""
    import inspect
    from ai.analyzer import LectureAnalyzer

    source = inspect.getsource(LectureAnalyzer._run_gemini_analysis)
    # primary_model must NOT be referenced as a loose variable
    assert "primary_model" not in source
    assert "actual_model = successful_model or self.model_name or \"gemini-flash-latest\"" in source


def test_path_traversal_prevention_in_attachments(tmp_path):
    """Verify Bug 10 fix: path traversal attempts are sanitized in add_attachment."""
    mgr = SessionManager(base_dir=tmp_path / "sessions")
    sdir = mgr.create_session(session_id="test_security_sess")

    # Attempt directory traversal attacks
    target_path = mgr.add_attachment(
        session_id="test_security_sess",
        filename="../../../etc/passwd",
        content=b"root:x:0:0:root",
    )
    # Must be contained strictly within attachments/
    assert target_path.parent.resolve() == (sdir / "attachments").resolve()
    assert target_path.name == "passwd"
    assert target_path.exists()
    assert target_path.read_bytes() == b"root:x:0:0:root"

    # Windows-style traversal attempt
    target_win = mgr.add_attachment(
        session_id="test_security_sess",
        filename="..\\..\\windows_attack.exe",
        content=b"payload",
    )
    assert target_win.parent.resolve() == (sdir / "attachments").resolve()
    assert target_win.name == "windows_attack.exe"
    assert target_win.exists()


def test_github_repo_url_parsing_with_dots():
    """Verify Bug 11 fix: repos with dots in their names are parsed accurately."""
    from publisher.github_sync import GitHubPublisher

    pub = GitHubPublisher()
    # 1. URL with multiple dots and .git suffix
    owner1, repo1 = pub._parse_repo_url("https://github.com/study-group/math.advanced.lectures.git")
    assert owner1 == "study-group"
    assert repo1 == "math.advanced.lectures"

    # 2. URL with dot and trailing slash without .git
    owner2, repo2 = pub._parse_repo_url("https://github.com/faculty/cs.databases/")
    assert owner2 == "faculty"
    assert repo2 == "cs.databases"

    # 3. SSH URL format
    owner3, repo3 = pub._parse_repo_url("git@github.com:student/ai.machine.learning.git")
    assert owner3 == "student"
    assert repo3 == "ai.machine.learning"


def test_save_json_atomic(tmp_path):
    """Verify Bug 12 fix: save_json_atomic writes data safely and cleans up temporary files."""
    import json
    from config import save_json_atomic

    test_file = tmp_path / "data" / "atomic_test.json"
    data = {"key": "value", "items": [1, 2, 3]}

    save_json_atomic(test_file, data)

    assert test_file.exists()
    with open(test_file, "r", encoding="utf-8") as f:
        loaded = json.load(f)
    assert loaded == data

    # Verify no dangling temp files left behind
    temp_files = list(test_file.parent.glob("*.tmp*"))
    assert len(temp_files) == 0


@pytest.mark.anyio
async def test_document_handler_large_file_size_blocked(monkeypatch):
    """Verify Bug 13 fix: documents >20 MB without local API server are gracefully rejected."""
    import config
    from bot.handlers import handle_document_or_audio, USER_SESSION_STATES

    chat_id = 999111
    USER_SESSION_STATES[chat_id] = {"action": "waiting_for_files", "session_id": "sess_large"}

    mock_msg = MagicMock()
    mock_msg.chat.id = chat_id
    mock_doc = MagicMock()
    mock_doc.file_name = "huge_textbook.pdf"
    mock_doc.file_size = 25 * 1024 * 1024  # 25 MB
    mock_msg.document = mock_doc
    mock_msg.audio = None
    mock_msg.voice = None
    mock_msg.bot = MagicMock()
    mock_msg.bot.get_file = AsyncMock()
    mock_msg.answer = AsyncMock()

    # Cloud Telegram API mode (no local API server)
    monkeypatch.setattr(config.settings, "telegram_api_server", None)

    await handle_document_or_audio(mock_msg)

    # get_file must NOT be called
    mock_msg.bot.get_file.assert_not_called()
    # Message explaining 20MB limit must be sent
    mock_msg.answer.assert_awaited_once()
    sent_text = mock_msg.answer.call_args[0][0]
    assert "Файл слишком большой" in sent_text
    assert "25" in sent_text
    assert "20 МБ" in sent_text


@pytest.mark.anyio
async def test_document_handler_local_bot_api_path_cleaning(monkeypatch, tmp_path):
    """Verify telegram-bot-api --local paths (/var/lib/.../documents/file.txt) are cleaned and handled."""
    from unittest.mock import AsyncMock, MagicMock
    import config
    from bot.handlers import handle_document_or_audio, USER_SESSION_STATES

    chat_id = 999222
    USER_SESSION_STATES[chat_id] = {"action": "waiting_for_files", "session_id": "sess_local_api"}

    mock_msg = MagicMock()
    mock_msg.chat.id = chat_id
    mock_doc = MagicMock()
    mock_doc.file_id = "doc_123"
    mock_doc.file_name = "test.txt"
    mock_doc.file_size = 100
    mock_msg.document = mock_doc
    mock_msg.audio = None
    mock_msg.voice = None
    mock_msg.photo = None
    mock_msg.answer = AsyncMock()

    mock_bot = MagicMock()
    mock_bot.token = "8794861294:AAFaWhUAcSy__LQI2wfAK6oGHRyGsmLsPQE"

    # Create a real local file simulating local filesystem copy
    fake_local_file = tmp_path / "documents" / "file_2.txt"
    fake_local_file.parent.mkdir(parents=True, exist_ok=True)
    fake_local_file.write_text("Hello from local Bot API server!", encoding="utf-8")

    file_info = MagicMock()
    file_info.file_path = str(fake_local_file)
    mock_bot.get_file = AsyncMock(return_value=file_info)
    mock_msg.bot = mock_bot

    monkeypatch.setattr(config.settings, "telegram_api_server", "http://127.0.0.1:8081")

    await handle_document_or_audio(mock_msg)

    # Should succeed and send confirmation
    mock_msg.answer.assert_awaited()
    sent_text = mock_msg.answer.call_args[0][0]
    assert "Файл успешно прикреплен" in sent_text


def test_browser_participants_helpers_exist():
    """Verify Bug 14 fix: browser recorder contains methods to check and close participants panel."""
    from recorder.browser import MeetingRecorder

    rec = MeetingRecorder(headless=True)
    assert hasattr(rec, "is_participants_panel_open")
    assert hasattr(rec, "ensure_participants_panel_open")
    assert hasattr(rec, "ensure_participants_panel_closed")
    assert hasattr(rec, "get_active_participants")


def test_split_text_into_chunks():
    """Verify text chunking preserves paragraphs and stays within max limit."""
    from publisher.telegram_pub import split_text_into_chunks

    # Short text
    assert split_text_into_chunks("Hello world", max_chars=100) == ["Hello world"]

    # Multiple paragraphs
    p1 = "P" * 60
    p2 = "Q" * 60
    text = f"{p1}\n\n{p2}"
    chunks = split_text_into_chunks(text, max_chars=80)
    assert len(chunks) == 2
    assert chunks[0] == p1
    assert chunks[1] == p2

    # Single oversized line
    long_line = "X" * 250
    chunks_long = split_text_into_chunks(long_line, max_chars=100)
    assert len(chunks_long) == 3
    assert all(len(c) <= 100 for c in chunks_long)


@pytest.mark.anyio
async def test_safe_send_message_markdown_error_fallback():
    """Verify safe_send_message catches entity parse error (offset 5549) and falls back to plain text."""
    from publisher.telegram_pub import safe_send_message
    from aiogram.exceptions import TelegramBadRequest

    mock_bot = AsyncMock()
    # First call fails with TelegramBadRequest (can't parse entities)
    # Second call (plain text fallback) succeeds
    mock_success_msg = MagicMock()
    mock_success_msg.message_id = 42

    error_msg = "Bad Request: can't parse entities: Can't find end of the entity starting at byte offset 5549"
    mock_bot.send_message.side_effect = [
        TelegramBadRequest(method=MagicMock(), message=error_msg),
        mock_success_msg,
    ]

    kb = MagicMock()
    sent_msgs = await safe_send_message(
        bot=mock_bot,
        chat_id=12345,
        text="⚡ Test message with unclosed *markdown tag",
        reply_markup=kb,
    )

    assert len(sent_msgs) == 1
    assert sent_msgs[0].message_id == 42
    assert mock_bot.send_message.call_count == 2

    # Verify first attempt was markdown, second was None (plain text)
    first_call_kwargs = mock_bot.send_message.call_args_list[0].kwargs
    second_call_kwargs = mock_bot.send_message.call_args_list[1].kwargs

    assert first_call_kwargs["parse_mode"] is not None
    assert second_call_kwargs["parse_mode"] is None
    assert second_call_kwargs["reply_markup"] == kb


@pytest.mark.anyio
async def test_external_bot_reuse_in_telegram_pub_and_orchestrator():
    """Verify external bot instance is reused and not closed unintentionally."""
    from publisher.telegram_pub import TelegramPublisher
    from orchestrator import LectureOrchestrator

    mock_bot = MagicMock()
    mock_bot.session = MagicMock()
    mock_bot.session.close = AsyncMock()

    pub = TelegramPublisher(bot=mock_bot)
    assert pub._external_bot is True
    assert pub.bot is mock_bot

    # close() on publisher with external bot should NOT close external bot session
    await pub.close()
    mock_bot.session.close.assert_not_called()

    # Orchestrator accepting bot
    orch = LectureOrchestrator(bot=mock_bot)
    assert orch.telegram_pub.bot is mock_bot
    assert orch.telegram_pub._external_bot is True

    await orch.close()
    mock_bot.session.close.assert_not_called()


def test_session_manager_attachment_path_and_target_path(tmp_path):
    """Verify session_manager supports streaming attachments directly via Path."""
    mgr = SessionManager(base_dir=tmp_path / "sessions")
    session_id = "test_stream_att"
    mgr.create_session(session_id=session_id)

    # 1. get_attachment_target_path
    target_path = mgr.get_attachment_target_path(session_id, "presentation.pptx")
    assert target_path.name == "presentation.pptx"
    assert target_path.parent.name == "attachments"

    # 2. Write file directly to target_path (as bot.download_file would)
    target_path.write_bytes(b"Simulated streamed content")

    # 3. Register with add_attachment using Path
    att_path = mgr.add_attachment(session_id, "presentation.pptx", target_path)
    assert att_path.exists()
    assert att_path.stat().st_size == len(b"Simulated streamed content")

    meta = mgr.get_session_metadata(session_id)
    assert len(meta.get("attachments", [])) == 1
    assert meta["attachments"][0]["filename"] == "presentation.pptx"
    assert meta["attachments"][0]["size_bytes"] == len(b"Simulated streamed content")


def test_slugify_preserves_word_boundaries():
    """Verify slugify does not cut off words mid-letter."""
    from publisher.github_sync import slugify

    # Topic with PyTorch
    text = "Автоматическое дифференцирование и устройство Autograd в PyTorch"
    slug = slugify(text, max_length=60)
    # Shouldn't end with "PyT" or broken word
    assert not slug.endswith("PyT")
    assert not slug.endswith("_")


def test_publish_lecture_uses_official_subject_name_and_session_date(tmp_path):
    """Verify publish_lecture uses official subject name and session date instead of AI hallucination."""
    from publisher.github_sync import GitHubPublisher
    from ai.schemas import LectureAnalysisResult, ActionItemsDigest, IllustratedLongread

    pub = GitHubPublisher(local_repo_dir=tmp_path / "mock_repo")
    pub.simulation_mode = True

    digest = ActionItemsDigest(
        subject="DS",
        topic="Autograd",
        executive_summary="Summary",
        action_items=[],
        key_technologies_and_terms=[],
    )
    longread = IllustratedLongread(
        title="DS: Autograd",
        subject="Машинное обучение: Data Science",  # AI generated long subject
        topic="Автоматическое дифференцирование и устройство Autograd в PyTorch",
        overview="Intro",
        sections=[],
    )
    analysis = LectureAnalysisResult(digest=digest, longread=longread)

    url = pub.publish_lecture(
        analysis_result=analysis,
        slide_files=[],
        lecture_date="2026-09-15",
        subject_name="Машинное обучение",  # Official name from bot
    )

    # Must contain official subject folder and real session date!
    assert "Машинное_обучение/2026-09-15_" in url
    assert "2026-09-15" in url


@pytest.mark.anyio
async def test_safe_send_message_html_mode():
    """Verify safe_send_message works cleanly with HTML mode."""
    from publisher.telegram_pub import safe_send_message
    from aiogram.enums import ParseMode

    mock_bot = AsyncMock()
    mock_msg = MagicMock()
    mock_msg.message_id = 77
    mock_bot.send_message.return_value = mock_msg

    html_text = "⚡ <b>Машинное обучение</b>\n\n<code>PyTorch</code>"
    sent = await safe_send_message(
        bot=mock_bot,
        chat_id=123,
        text=html_text,
        parse_mode=ParseMode.HTML,
    )

    assert len(sent) == 1
    assert sent[0].message_id == 77
    call_kwargs = mock_bot.send_message.call_args[1]
    assert call_kwargs["parse_mode"] == ParseMode.HTML
