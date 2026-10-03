import asyncio
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from PIL import Image

from bot.keyboards import get_session_card_keyboard, get_sessions_list_keyboard
from bot.shutdown_manager import GracefulShutdownManager
from recorder.session_manager import SessionManager


@pytest.fixture
def temp_session_mgr(tmp_path):
    mgr = SessionManager(base_dir=tmp_path / "sessions")
    return mgr


def test_shutdown_manager_registration(temp_session_mgr):
    mgr = GracefulShutdownManager(session_manager=temp_session_mgr)
    dummy_rec = MagicMock()
    dummy_audio = MagicMock()
    dummy_mon = MagicMock()

    mgr.register_session(
        chat_id=12345,
        session_id="2026-09-15_test",
        subject_title="Math",
        recorder=dummy_rec,
        audio_recorder=dummy_audio,
        monitor=dummy_mon,
        start_time=time.time(),
        audio_path=Path("/tmp/audio.mp3"),
        slides_dir=Path("/tmp/slides"),
    )

    assert 12345 in mgr.active_sessions
    assert mgr.active_sessions[12345].session_id == "2026-09-15_test"

    mgr.unregister_session(12345)
    assert 12345 not in mgr.active_sessions


def test_shutdown_manager_fast_freeze(temp_session_mgr):
    mgr = GracefulShutdownManager(session_manager=temp_session_mgr)

    # Prepare session dir and fake files
    session_dir = temp_session_mgr.create_session(subject="Физика", subject_slug="physics")
    session_id = session_dir.name
    slides_dir = session_dir / "slides"
    audio_path = session_dir / "audio.mp3"

    # Create dummy slide & audio
    img = Image.new("RGB", (100, 100), color=(100, 150, 200))
    img.save(slides_dir / "slide_001_10s.png")
    img.save(slides_dir / "slide_002_20s.png")
    audio_path.write_bytes(b"dummy mp3 data " * 100)

    mock_recorder = AsyncMock()
    mock_audio = MagicMock()
    mock_monitor = MagicMock()
    mock_bot = AsyncMock()

    mgr.register_session(
        chat_id=999,
        session_id=session_id,
        subject_title="Физика",
        recorder=mock_recorder,
        audio_recorder=mock_audio,
        monitor=mock_monitor,
        start_time=time.time() - 30.0,
        audio_path=audio_path,
        slides_dir=slides_dir,
    )

    # Perform fast freeze
    asyncio.run(mgr.fast_freeze(signal_name="SIGTERM", bot=mock_bot))

    # Verify calls
    assert mock_monitor.request_stop.called
    assert mock_audio.stop.called
    assert mock_recorder.close.called
    assert mock_bot.send_message.called

    # Verify metadata saved properly
    meta = temp_session_mgr.get_session_metadata(session_id)
    assert meta["status"] == "interrupted"
    assert meta["postprocess_pending"] is True
    assert meta["slides_count"] == 2
    assert meta["audio"]["exists"] is True
    assert "SIGTERM" in meta["exit_reason"]

    # Test idempotency (calling fast_freeze again does nothing)
    assert mgr.is_shutting_down is True
    mock_audio.reset_mock()
    asyncio.run(mgr.fast_freeze(signal_name="SIGTERM", bot=mock_bot))
    assert not mock_audio.stop.called


def test_recover_stale_sessions(temp_session_mgr):
    # 1. Create a session left in 'recording' status
    s1_dir = temp_session_mgr.create_session(subject="Алгебра")
    s1_id = s1_dir.name
    temp_session_mgr.update_session_metadata(s1_id, {"status": "recording"})

    # 2. Create a session already completed
    s2_dir = temp_session_mgr.create_session(subject="Химия")
    s2_id = s2_dir.name
    temp_session_mgr.update_session_metadata(s2_id, {"status": "completed"})

    # 3. Create a session interrupted with postprocess_pending=True
    s3_dir = temp_session_mgr.create_session(subject="История")
    s3_id = s3_dir.name
    temp_session_mgr.update_session_metadata(s3_id, {"status": "interrupted", "postprocess_pending": True})

    recovered = temp_session_mgr.recover_stale_sessions()
    recovered_ids = [s["session_id"] for s in recovered]

    assert s1_id in recovered_ids
    assert s3_id in recovered_ids
    assert s2_id not in recovered_ids

    # s1 should now be interrupted with postprocess_pending=True
    meta1 = temp_session_mgr.get_session_metadata(s1_id)
    assert meta1["status"] == "interrupted"
    assert meta1["postprocess_pending"] is True


def test_run_session_postprocess(temp_session_mgr):
    sdir = temp_session_mgr.create_session(subject="Информатика")
    sid = sdir.name
    slides_dir = sdir / "slides"
    audio_path = sdir / "audio.mp3"

    # Create dummy slides
    img1 = Image.new("RGB", (100, 100), color=(10, 20, 30))
    img1.save(slides_dir / "slide_001_5s.png")
    img2 = Image.new("RGB", (100, 100), color=(150, 200, 250))
    img2.save(slides_dir / "slide_002_45s.png")
    audio_path.write_bytes(b"dummy mp3 data" * 100)

    temp_session_mgr.update_session_metadata(
        sid,
        {
            "status": "interrupted",
            "postprocess_pending": True,
            "duration_seconds": 60.0,
        },
    )

    with patch("recorder.audio.AudioRecorder.check_audio_level", return_value={"max_volume": -12.5, "is_silent": False}), \
         patch("recorder.audio.AudioRecorder.trim_silence_boundaries", return_value=(10.0, 50.0)):
        
        res = temp_session_mgr.run_session_postprocess(sid)

        assert res["success"] is True
        assert res["start_cutoff_sec"] == 10.0
        assert res["end_cutoff_sec"] == 50.0

        meta = temp_session_mgr.get_session_metadata(sid)
        assert meta["status"] == "ready_for_review"
        assert meta["postprocess_pending"] is False
        assert meta["duration_seconds"] == 40.0
        # slide at 5s was purged because it was before start_cutoff 10s
        assert meta["slides_count"] == 1


def test_keyboard_postprocess_button():
    # Card keyboard with postprocess_pending=True
    kb_pending = get_session_card_keyboard(
        session_id="sess_123",
        has_audio=True,
        slides_count=5,
        postprocess_pending=True,
    )
    callbacks = [btn.callback_data for row in kb_pending.inline_keyboard for btn in row if btn.callback_data]
    texts_pending = [btn.text for row in kb_pending.inline_keyboard for btn in row if btn.callback_data == "sess_postproc:sess_123"]
    assert "sess_postproc:sess_123" in callbacks
    assert any("срез тишины" in t for t in texts_pending)

    # Card keyboard with postprocess_pending=False and not postprocessed
    kb_normal = get_session_card_keyboard(
        session_id="sess_123",
        has_audio=True,
        slides_count=5,
        postprocess_pending=False,
        postprocessed=False,
    )
    callbacks_normal = [btn.callback_data for row in kb_normal.inline_keyboard for btn in row if btn.callback_data]
    texts_normal = [btn.text for row in kb_normal.inline_keyboard for btn in row if btn.callback_data == "sess_postproc:sess_123"]
    assert "sess_postproc:sess_123" in callbacks_normal
    assert "⚙️ Запустить постобработку" in texts_normal

    # Card keyboard with postprocessed=True
    kb_re = get_session_card_keyboard(
        session_id="sess_123",
        has_audio=True,
        slides_count=5,
        postprocess_pending=False,
        postprocessed=True,
    )
    texts_re = [btn.text for row in kb_re.inline_keyboard for btn in row if btn.callback_data == "sess_postproc:sess_123"]
    assert "⚙️ Перезапустить постобработку" in texts_re

    # List keyboard showing pause icon for interrupted/pending
    sessions = [
        {"session_id": "s1", "status": "interrupted", "postprocess_pending": True, "created_at": "2026-09-15 10:00:00"},
        {"session_id": "s2", "status": "completed", "postprocess_pending": False, "created_at": "2026-09-15 11:00:00"},
    ]
    list_kb = get_sessions_list_keyboard(sessions)
    texts = [btn.text for row in list_kb.inline_keyboard for btn in row]
    assert any("⏸️" in t for t in texts)
    assert any("✅" in t for t in texts)


def test_bot_main_import():
    import bot.main
    assert hasattr(bot.main, "start_bot")
    assert hasattr(bot.main, "shutdown_manager")
    assert hasattr(bot.main, "session_mgr")


@pytest.mark.anyio
async def test_handle_session_restore_slides(temp_session_mgr, monkeypatch):
    from bot.handlers import handle_session_restore_slides

    monkeypatch.setattr("bot.handlers.session_mgr", temp_session_mgr)
    session_id = "restore_handler_test"
    sdir = temp_session_mgr.create_session(session_id=session_id)
    slides_dir = sdir / "slides"

    # 1. No backup -> error response
    cb_fail = AsyncMock()
    cb_fail.data = f"sess_restore_slides:{session_id}"
    cb_fail.message = AsyncMock()

    await handle_session_restore_slides(cb_fail)
    assert cb_fail.answer.called
    assert cb_fail.message.answer.called
    assert "Не удалось восстановить" in cb_fail.message.answer.call_args[0][0]

    # 2. Add slides, create backup via postprocess
    img = Image.new("RGB", (200, 200), color=(100, 100, 100))
    img.save(slides_dir / "slide_001_2026-09-17_12-00-00.png")
    temp_session_mgr.run_session_postprocess(session_id)

    # Delete slides from slides_dir
    for f in slides_dir.glob("*.png"):
        f.unlink()
    assert len(temp_session_mgr.get_slides(session_id)) == 0

    # Restore via handler
    cb_success = AsyncMock()
    cb_success.data = f"sess_restore_slides:{session_id}"
    cb_success.message = AsyncMock()

    await handle_session_restore_slides(cb_success)
    assert cb_success.answer.called
    assert len(temp_session_mgr.get_slides(session_id)) == 1


