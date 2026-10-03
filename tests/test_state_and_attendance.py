import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock
from aiogram.types import CallbackQuery, Message, User, Chat
from aiogram.enums import ParseMode

from bot.state_tracker import BotStateTracker, BotState, bot_state_tracker
from bot.handlers import handle_session_attendance
from recorder.session_manager import session_mgr
from recorder.schedule_manager import ScheduleEntry, Subject


def test_state_tracker_transitions():
    tracker = BotStateTracker()
    assert tracker.state == BotState.IDLE
    assert not tracker.is_recording()

    # Recording state
    mock_monitor = MagicMock()
    mock_monitor.deduplicator.saved_slides = ["s1.png", "s2.png", "s3.png"]
    tracker.set_recording(session_id="sess_001", subject_title="Компьютерные сети", monitor_ref=mock_monitor)
    assert tracker.state == BotState.RECORDING
    assert tracker.is_recording()
    assert tracker.get_slide_count() == 3

    card_rec = tracker.get_status_card_markdown()
    assert "ИДЁТ ЗАПИСЬ ПАРЫ" in card_rec
    assert "Компьютерные сети" in card_rec
    assert "sess_001" in card_rec
    assert "Слайдов" in card_rec

    # Postprocessing state
    tracker.set_postprocessing(session_id="sess_001", subject_title="Компьютерные сети", details="срез тишины")
    assert tracker.state == BotState.POSTPROCESSING
    assert not tracker.is_recording()
    card_post = tracker.get_status_card_markdown()
    assert "ПОСТОБРАБОТКА МАТЕРИАЛОВ" in card_post
    assert "sess_001" in card_post

    # Analyzing state
    tracker.set_analyzing(session_id="sess_001", subject_title="Компьютерные сети", model_name="gemini-2.5-flash")
    assert tracker.state == BotState.ANALYZING
    card_ai = tracker.get_status_card_markdown()
    assert "НЕЙРОАНАЛИЗ КОНСПЕКТА (GEMINI)" in card_ai
    assert "gemini-2.5-flash" in card_ai

    # Publishing state
    tracker.set_publishing(session_id="sess_001", subject_title="Компьютерные сети")
    assert tracker.state == BotState.PUBLISHING
    card_pub = tracker.get_status_card_markdown()
    assert "ПУБЛИКАЦИЯ МАТЕРИАЛОВ" in card_pub

    # Idle state
    tracker.set_idle()
    assert tracker.state == BotState.IDLE
    card_idle = tracker.get_status_card_markdown()
    assert "СВОБОДЕН (ОЖИДАНИЕ ПАРЫ)" in card_idle


def test_state_tracker_idle_with_schedule():
    tracker = BotStateTracker()
    mock_schedule = MagicMock()
    entry = ScheduleEntry(
        id="s1",
        subject_id="os",
        day_of_week=0,
        time_str="14:15",
        url="https://telemost.yandex.ru/j/123",
    )
    subj = Subject(
        id="os",
        title="Операционные системы",
        short_name="ОС",
    )
    mock_schedule.get_minutes_until_next_pair.return_value = (entry, subj, 25)

    card = tracker.get_status_card_markdown(mock_schedule)
    assert "СВОБОДЕН" in card
    assert "Следующая: **Операционные системы** в `14:15` (через 25 мин)" in card


def test_handle_session_attendance_empty_and_populated(tmp_path, monkeypatch):
    # Setup test session in temporary directory
    monkeypatch.setattr(session_mgr, "base_dir", tmp_path)
    session_dir = session_mgr.create_session(subject="Базы данных", subject_slug="bd")
    session_id = session_dir.name

    # 1. Test when attendance has no participants
    cb = MagicMock(spec=CallbackQuery)
    cb.data = f"sess_attendance:{session_id}"
    cb.answer = AsyncMock()
    cb.message = MagicMock(spec=Message)
    cb.message.chat = MagicMock(spec=Chat)
    cb.message.chat.id = 12345
    cb.message.edit_text = AsyncMock()
    cb.message.answer = AsyncMock()

    asyncio.run(handle_session_attendance(cb))
    cb.answer.assert_awaited_once()
    cb.message.edit_text.assert_awaited_once()
    empty_call_args = cb.message.edit_text.call_args[0][0]
    assert "Данные о присутствующих студентах отсутствуют" in empty_call_args

    # 2. Test when attendance is populated
    attendance_payload = {
        "meeting_duration_minutes": 90.0,
        "total_unique_participants": 2,
        "debounce_seconds": 90.0,
        "participants": [
            {
                "name": "Иванов Иван",
                "first_seen": "09:00:00",
                "last_seen": "10:30:00",
                "total_minutes": 90.0,
                "attendance_percentage": 100.0,
                "intervals_count": 1,
            },
            {
                "name": "Алексеев Алексей",
                "first_seen": "09:05:00",
                "last_seen": "10:25:00",
                "total_minutes": 80.0,
                "attendance_percentage": 88.9,
                "intervals_count": 1,
            },
        ],
    }
    session_mgr.update_session_metadata(session_id, {"attendance": attendance_payload})

    cb.message.edit_text.reset_mock()
    cb.answer.reset_mock()

    asyncio.run(handle_session_attendance(cb))
    cb.answer.assert_awaited_once()
    cb.message.edit_text.assert_awaited_once()
    call_args = cb.message.edit_text.call_args[0][0]

    # Verify roster header
    assert "Журнал посещаемости пары (для старосты)" in call_args
    assert "Базы данных" in call_args
    assert "90.0 мин" in call_args
    assert "2 чел" in call_args

    # Verify alphabetical sorting (Алексеев first, Иванов second)
    assert call_args.find("Алексеев Алексей") < call_args.find("Иванов Иван")

    # Verify copyable block
    assert "```text" in call_args
    assert "Посещаемость: Базы данных" in call_args
    assert "1. Алексеев Алексей — 80.0 мин (88.9%)" in call_args
    assert "2. Иванов Иван — 90.0 мин (100.0%)" in call_args

    # Verify back button keyboard
    reply_markup = cb.message.edit_text.call_args[1]["reply_markup"]
    assert reply_markup.inline_keyboard[0][0].callback_data == f"sess_view:{session_id}"
