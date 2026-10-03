import pytest
import time
from recorder.attendance import ParticipantTracker, AttendanceInterval, StudentAttendance


def test_continuous_presence():
    tracker = ParticipantTracker(debounce_sec=90.0, self_name="Бот-Студент")
    tracker.start(start_time=0.0)

    # 4 snapshots over 120 seconds
    tracker.update_presence(["Алиса Иванова"], timestamp=0.0)
    tracker.update_presence(["Алиса Иванова"], timestamp=30.0)
    tracker.update_presence(["Алиса Иванова"], timestamp=60.0)
    tracker.update_presence(["Алиса Иванова"], timestamp=120.0)

    res = tracker.finalize(end_time=120.0)

    assert res["total_unique_participants"] == 1
    assert res["meeting_duration_minutes"] == 2.0
    participant = res["participants"][0]
    assert participant["name"] == "Алиса Иванова"
    assert participant["intervals_count"] == 1
    assert participant["total_minutes"] == 2.0
    assert participant["attendance_percentage"] == 100.0


def test_transient_disconnect_debounced():
    tracker = ParticipantTracker(debounce_sec=90.0)
    tracker.start(start_time=0.0)

    tracker.update_presence(["Борис Смирнов"], timestamp=0.0)
    tracker.update_presence(["Борис Смирнов"], timestamp=30.0)

    # Gap of 40 seconds (absent at t=70)
    tracker.update_presence([], timestamp=70.0)

    # Reconnects at t=90 (gap between 30 and 90 is 60s <= 90s debounce)
    tracker.update_presence(["Борис Смирнов"], timestamp=90.0)
    tracker.update_presence(["Борис Смирнов"], timestamp=120.0)

    res = tracker.finalize(end_time=120.0)

    assert res["total_unique_participants"] == 1
    participant = res["participants"][0]
    # Debounce bridged the reconnect into a single continuous interval
    assert participant["intervals_count"] == 1
    assert participant["total_minutes"] == 2.0
    assert participant["attendance_percentage"] == 100.0


def test_long_disconnect_creates_separate_intervals():
    tracker = ParticipantTracker(debounce_sec=90.0)
    tracker.start(start_time=0.0)

    # Joined for first 5 minutes (0 to 300s)
    tracker.update_presence(["Дмитрий Волков"], timestamp=0.0)
    tracker.update_presence(["Дмитрий Волков"], timestamp=300.0)

    # Student disconnects. Polling at t=400s (gap = 100s > 90s debounce -> closed at t=300s)
    tracker.update_presence([], timestamp=400.0)

    # Absent for 35 minutes! Re-joins at t=2400s (40 min) and stays till t=2700s (45 min)
    tracker.update_presence(["Дмитрий Волков"], timestamp=2400.0)
    tracker.update_presence(["Дмитрий Волков"], timestamp=2700.0)

    res = tracker.finalize(end_time=2700.0)

    assert res["meeting_duration_minutes"] == 45.0
    participant = res["participants"][0]
    # Two distinct intervals
    assert participant["intervals_count"] == 2
    # Total active minutes = 5.0m + 5.0m = 10.0 minutes
    assert participant["total_minutes"] == 10.0
    # 600s / 2700s = 22.2% (NOT 100%!)
    assert participant["attendance_percentage"] == pytest.approx(22.2, abs=0.1)


def test_late_joiner_and_early_leaver():
    tracker = ParticipantTracker(debounce_sec=90.0)
    tracker.start(start_time=0.0)

    # Late joiner joins at minute 30 (1800s)
    tracker.update_presence(["Опоздавший"], timestamp=1800.0)
    tracker.update_presence(["Опоздавший"], timestamp=3600.0)

    # Early leaver leaves at minute 15 (900s)
    tracker.update_presence(["УшедшийРано"], timestamp=0.0)
    tracker.update_presence(["УшедшийРано"], timestamp=900.0)
    tracker.update_presence([], timestamp=1100.0)  # gap > 90s

    res = tracker.finalize(end_time=3600.0)
    participants = {p["name"]: p for p in res["participants"]}

    assert participants["Опоздавший"]["total_minutes"] == 30.0
    assert participants["Опоздавший"]["attendance_percentage"] == 50.0

    assert participants["УшедшийРано"]["total_minutes"] == 15.0
    assert participants["УшедшийРано"]["attendance_percentage"] == 25.0


def test_self_bot_filtering():
    tracker = ParticipantTracker(debounce_sec=90.0, self_name="Бот-Студент")
    tracker.start(start_time=0.0)

    tracker.update_presence(
        [
            "Бот-Студент",
            "бот-студент",
            "Я",
            "я",
            "Вы",
            "(вы)",
            "  Павел Николаев  ",
            "",
            "   ",
        ],
        timestamp=60.0,
    )

    res = tracker.finalize(end_time=60.0)

    assert res["total_unique_participants"] == 1
    assert res["participants"][0]["name"] == "Павел Николаев"


def test_metadata_structure_and_formatting():
    # Test realistic epoch timestamps
    epoch_start = 1726580000.0  # e.g. 16:13:20
    tracker = ParticipantTracker(debounce_sec=60.0)
    tracker.start(start_time=epoch_start)

    tracker.update_presence(["Иван Петров"], timestamp=epoch_start)
    tracker.update_presence(["Иван Петров"], timestamp=epoch_start + 600.0)

    res = tracker.finalize(end_time=epoch_start + 600.0)

    assert "meeting_duration_minutes" in res
    assert "total_unique_participants" in res
    assert "debounce_seconds" in res
    assert "participants" in res

    p = res["participants"][0]
    assert ":" in p["first_seen"]
    assert ":" in p["last_seen"]
    assert len(p["intervals"]) == 1
    assert "duration_minutes" in p["intervals"][0]
