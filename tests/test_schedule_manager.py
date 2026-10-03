import pytest
from datetime import datetime
from pathlib import Path
from recorder.schedule_manager import ScheduleManager, slugify_subject


@pytest.fixture
def temp_schedule_manager(tmp_path):
    mgr = ScheduleManager(data_file=tmp_path / "schedule_test.json")
    return mgr


def test_slugify_subject():
    assert slugify_subject("Архитектура ПО") == "arkhitektura_po"
    assert slugify_subject("Базы данных") == "bazy_dannykh"
    assert slugify_subject("Computer Networks 101") == "computer_networks_101"


def test_subjects_crud(temp_schedule_manager):
    mgr = temp_schedule_manager
    # Default seeded subjects should exist
    assert len(mgr.get_subjects()) >= 4

    # Add new subject
    subj = mgr.add_subject(title="Машинное обучение", short_name="МО", color_icon="🧠")
    assert subj.id == "mo"
    assert subj.short_name == "МО"
    assert subj.color_icon == "🧠"

    # Get subject by id
    fetched = mgr.get_subject("mo")
    assert fetched is not None
    assert fetched.title == "Машинное обучение"

    # Delete subject
    deleted = mgr.delete_subject("mo")
    assert deleted is True
    assert mgr.get_subject("mo") is None


def test_schedule_crud(temp_schedule_manager):
    mgr = temp_schedule_manager
    subj = mgr.add_subject(title="Теория алгоритмов", short_name="Алгоритмы")

    # Add slot: Monday (0) at 09:00
    entry = mgr.add_schedule_entry(
        subject_id=subj.id,
        day_of_week=0,
        time_str="09:00",
        url="https://telemost.yandex.ru/j/12345",
        duration_min=95,
    )
    assert entry.id.startswith("sched_")
    assert entry.time_str == "09:00"
    assert entry.day_of_week == 0

    # Retrieve schedule for Monday
    mon_entries = mgr.get_schedule(day_of_week=0)
    assert any(e.id == entry.id for e in mon_entries)

    # Add slot: Monday (0) at 09:10
    entry2 = mgr.add_schedule_entry(
        subject_id=subj.id,
        day_of_week=0,
        time_str="09:10",
        url="https://telemost.yandex.ru/j/99999",
    )
    assert entry2.time_str == "09:10"

    # Add slot: Monday (0) with dash format "09-10"
    entry3 = mgr.add_schedule_entry(
        subject_id=subj.id,
        day_of_week=0,
        time_str="09-10",
        url="https://telemost.yandex.ru/j/88888",
    )
    assert entry3.time_str == "09:10"

    # Delete slot
    del_res = mgr.delete_schedule_entry(entry.id)
    assert del_res is True
    assert mgr.get_schedule_entry(entry.id) is None


def test_current_or_upcoming_pair_matching(temp_schedule_manager):
    mgr = temp_schedule_manager
    subj = mgr.add_subject(title="Веб-разработка", short_name="Веб")

    # Schedule: Thursday (day 3) at 10:45 (duration 95 min, ends at 12:20)
    mgr.add_schedule_entry(
        subject_id=subj.id,
        day_of_week=3,
        time_str="10:45",
        url="https://link.mts.ru/stream/123",
        duration_min=95,
    )

    # Case 1: 15 minutes before start (Thursday 10:30) -> should match within tolerance (20m)
    dt_before = datetime(2026, 9, 10, 10, 30)  # 2026-09-10 is Thursday (weekday 3)
    assert dt_before.weekday() == 3
    match = mgr.get_current_or_upcoming_pair(now_dt=dt_before, tolerance_minutes=20)
    assert match is not None
    entry, matched_subj = match
    assert matched_subj.id == subj.id
    assert entry.time_str == "10:45"

    # Case 2: During lecture (Thursday 11:30) -> should match
    dt_during = datetime(2026, 9, 10, 11, 30)
    match = mgr.get_current_or_upcoming_pair(now_dt=dt_during)
    assert match is not None

    # Case 3: After lecture (Thursday 13:00) -> should not match
    dt_after = datetime(2026, 9, 10, 13, 00)
    match = mgr.get_current_or_upcoming_pair(now_dt=dt_after)
    assert match is None

    # Case 4: Different day (Wednesday 10:45) -> should not match
    dt_diff_day = datetime(2026, 9, 9, 10, 45)  # Wednesday (weekday 2)
    match = mgr.get_current_or_upcoming_pair(now_dt=dt_diff_day)
    assert match is None


def test_update_schedule_entry_url(temp_schedule_manager):
    mgr = temp_schedule_manager
    subj = mgr.add_subject(title="Безопасность жизнедеятельности", short_name="БЖД")

    entry = mgr.add_schedule_entry(
        subject_id=subj.id,
        day_of_week=4,
        time_str="09:10",
        url="",
    )
    assert entry.url == ""

    # Update with new URL
    updated = mgr.update_schedule_entry_url(entry.id, "https://telemost.yandex.ru/j/57547977131835")
    assert updated is not None
    assert updated.url == "https://telemost.yandex.ru/j/57547977131835"

    # Verify persistent state
    fetched = mgr.get_schedule_entry(entry.id)
    assert fetched.url == "https://telemost.yandex.ru/j/57547977131835"

    # Clear URL
    cleared = mgr.update_schedule_entry_url(entry.id, "")
    assert cleared is not None
    assert cleared.url == ""

    # Non-existent entry
    assert mgr.update_schedule_entry_url("invalid_id", "https://example.com") is None


def test_next_pair_imminent(temp_schedule_manager):
    mgr = temp_schedule_manager
    subj = mgr.add_subject(title="Web-технологии", short_name="Web")

    # Pair on Wednesday (weekday=2) at 10:45
    entry = mgr.add_schedule_entry(
        subject_id=subj.id,
        day_of_week=2,
        time_str="10:45",
        url="https://telemost.yandex.ru/j/test",
    )

    # 1. At 10:41 (4 minutes before start) -> imminent is True
    dt_imminent = datetime(2026, 9, 16, 10, 41)  # 2026-09-16 is Wednesday (weekday 2)
    assert mgr.is_next_pair_imminent(now_dt=dt_imminent, buffer_minutes=4) is True
    res = mgr.get_minutes_until_next_pair(now_dt=dt_imminent)
    assert res is not None
    assert res[0].id == entry.id
    assert res[2] == 4

    # 2. At 10:30 (15 minutes before start) -> imminent is False (buffer=4)
    dt_far = datetime(2026, 9, 16, 10, 30)
    assert mgr.is_next_pair_imminent(now_dt=dt_far, buffer_minutes=4) is False
    assert mgr.get_minutes_until_next_pair(now_dt=dt_far)[2] == 15

    # 3. At 10:50 (already started) -> not in the future
    dt_past = datetime(2026, 9, 16, 10, 50)
    assert mgr.is_next_pair_imminent(now_dt=dt_past, buffer_minutes=4) is False


