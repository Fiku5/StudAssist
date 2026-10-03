import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock
from datetime import datetime

from bot.scheduler import ScheduleAutoJoinService
from recorder.schedule_manager import ScheduleManager, ScheduleEntry, Subject
import config


def test_check_entry_should_join():
    service = ScheduleAutoJoinService()

    # Entry at 20:45 with URL
    entry = ScheduleEntry(
        id="test_1",
        subject_id="bzhd",
        day_of_week=3,
        time_str="20:45",
        url="https://telemost.yandex.ru/j/123",
        is_active=True,
    )

    slot_minutes = 20 * 60 + 45  # 1245

    # 1 minute before start (20:44 = 1244) -> True
    assert service.check_entry_should_join(entry, 1244) is True

    # Exact start time (20:45 = 1245) -> True
    assert service.check_entry_should_join(entry, 1245) is True

    # 10 minutes after start (20:55 = 1255) -> True
    assert service.check_entry_should_join(entry, 1255) is True

    # Too early (20:40 = 1240) -> False
    assert service.check_entry_should_join(entry, 1240) is False

    # Too late (20:56 = 1256) -> False
    assert service.check_entry_should_join(entry, 1256) is False

    # Inactive or empty URL
    entry_no_url = ScheduleEntry(
        id="test_2", subject_id="bzhd", day_of_week=3, time_str="20:45", url=""
    )
    assert service.check_entry_should_join(entry_no_url, 1245) is False

    entry_inactive = ScheduleEntry(
        id="test_3", subject_id="bzhd", day_of_week=3, time_str="20:45", url="https://link", is_active=False
    )
    assert service.check_entry_should_join(entry_inactive, 1245) is False


def test_auto_join_triggers_execution_and_deduplicates(tmp_path, monkeypatch):
    data_file = tmp_path / "sched.json"
    mgr = ScheduleManager(data_file=data_file)
    subj = mgr.add_subject(title="Безопасность жизнедеятельности", short_name="БЖД")

    # Set mock current time: Thursday 20:45:00
    mock_now = datetime(2026, 9, 10, 20, 45, 0)  # Weekday 3 (Thursday)
    monkeypatch.setattr("bot.scheduler.get_local_now", lambda: mock_now)
    monkeypatch.setattr(config.settings, "allowed_telegram_users", "123456789")

    # Add schedule entry for Thursday 20:45
    entry = mgr.add_schedule_entry(
        subject_id=subj.id,
        day_of_week=3,
        time_str="20:45",
        url="https://telemost.yandex.ru/j/57547977131835",
    )

    mock_bot = MagicMock()
    mock_bot.send_message = AsyncMock()
    mock_execute = AsyncMock()

    service = ScheduleAutoJoinService(
        bot=mock_bot,
        schedule_manager=mgr,
        execute_meeting_fn=mock_execute,
    )

    # 1. Run check cycle
    asyncio.run(service.check_and_join_scheduled_lectures())

    # Wait brief moment for created task
    asyncio.run(asyncio.sleep(0.05))

    # Verification: Notification sent to user
    mock_bot.send_message.assert_awaited_once()
    sent_text = mock_bot.send_message.call_args[1]["text"]
    assert "Автоматический вход по расписанию" in sent_text
    assert "Безопасность жизнедеятельности" in sent_text
    assert "20:45" in sent_text

    # Verification: execute_meeting_fn called with correct parameters
    mock_execute.assert_awaited_once()
    assert mock_execute.call_args[1]["url"] == "https://telemost.yandex.ru/j/57547977131835"
    assert mock_execute.call_args[1]["subject_title"] == "Безопасность жизнедеятельности"

    # 2. Run second time immediately: Should NOT trigger again (deduplicated)
    mock_bot.send_message.reset_mock()
    mock_execute.reset_mock()

    asyncio.run(service.check_and_join_scheduled_lectures())
    asyncio.run(asyncio.sleep(0.05))

    mock_bot.send_message.assert_not_awaited()
    mock_execute.assert_not_awaited()


def test_record_active_chat():
    service = ScheduleAutoJoinService()
    assert service.last_active_chat_id is None
    service.record_active_chat(987654)
    assert service.last_active_chat_id == 987654
