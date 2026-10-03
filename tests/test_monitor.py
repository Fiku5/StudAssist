import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock
from pathlib import Path

from recorder.monitor import MeetingMonitor
from recorder.visual import SlideDeduplicator
from recorder.audio import AudioRecorder


@pytest.mark.anyio
async def test_monitor_rule1_exits_on_max_duration(tmp_path):
    mock_recorder = AsyncMock()
    mock_recorder.capture_screenshot.return_value = None
    mock_recorder.is_meeting_ended.return_value = False
    mock_recorder.close = AsyncMock()

    mock_audio = MagicMock()
    mock_audio.start.return_value = tmp_path / "audio.mp3"
    mock_audio.stop.return_value = tmp_path / "audio.mp3"
    mock_audio.trim_silence_boundaries.return_value = None

    dedup = SlideDeduplicator(output_dir=tmp_path / "slides")

    monitor = MeetingMonitor(
        recorder=mock_recorder,
        deduplicator=dedup,
        audio_recorder=mock_audio,
        check_interval_sec=0.01,
    )

    result = await monitor.monitor_session(
        audio_dest=tmp_path / "audio.mp3",
        max_duration_min=0,  # Immediate hard limit trigger
    )

    assert "Лимит длительности пары достигнут" in result["exit_reason"]


@pytest.mark.anyio
async def test_monitor_rule1_exits_when_next_pair_imminent(tmp_path):
    mock_recorder = AsyncMock()
    mock_recorder.capture_screenshot.return_value = None
    mock_recorder.is_meeting_ended.return_value = False
    mock_recorder.close = AsyncMock()

    mock_audio = MagicMock()
    mock_audio.start.return_value = tmp_path / "audio.mp3"
    mock_audio.stop.return_value = tmp_path / "audio.mp3"
    mock_audio.trim_silence_boundaries.return_value = None

    mock_schedule = MagicMock()
    mock_schedule.is_next_pair_imminent.return_value = True

    dedup = SlideDeduplicator(output_dir=tmp_path / "slides")

    monitor = MeetingMonitor(
        recorder=mock_recorder,
        deduplicator=dedup,
        audio_recorder=mock_audio,
        schedule_manager=mock_schedule,
        check_interval_sec=0.01,
    )

    result = await monitor.monitor_session(
        audio_dest=tmp_path / "audio.mp3",
        max_duration_min=180,
        min_elapsed_before_next_pair_check=0,
    )

    assert "Скоро начинается следующая пара по расписанию" in result["exit_reason"]
    mock_schedule.is_next_pair_imminent.assert_called()


@pytest.mark.anyio
async def test_monitor_rule2_exits_when_meeting_ended(tmp_path):
    mock_recorder = AsyncMock()
    mock_recorder.capture_screenshot.return_value = None
    mock_recorder.is_meeting_ended.return_value = True  # Trigger immediate exit
    mock_recorder.get_participant_count.return_value = 25
    mock_recorder.close = AsyncMock()

    mock_audio = MagicMock()
    mock_audio.start.return_value = tmp_path / "audio.mp3"
    mock_audio.stop.return_value = tmp_path / "audio.mp3"
    mock_audio.trim_silence_boundaries.return_value = None

    dedup = SlideDeduplicator(output_dir=tmp_path / "slides")

    monitor = MeetingMonitor(
        recorder=mock_recorder,
        deduplicator=dedup,
        audio_recorder=mock_audio,
        check_interval_sec=0.01,
    )

    result = await monitor.monitor_session(
        audio_dest=tmp_path / "audio.mp3",
        max_duration_min=180,
        min_duration_min=95.3,
    )

    assert "Host closed meeting" in result["exit_reason"]
    mock_audio.start.assert_called_once()
    mock_audio.stop.assert_called_once()
    mock_recorder.close.assert_called_once()


@pytest.mark.anyio
async def test_monitor_rule2_requires_consecutive_confirmations(tmp_path):
    mock_recorder = AsyncMock()
    mock_recorder.capture_screenshot.return_value = None
    # Simulate a 1-tick transient glitch (True), then False, then 2 solid Trues
    mock_recorder.is_meeting_ended.side_effect = [True, False, True, True]
    mock_recorder.get_participant_count.return_value = 25
    mock_recorder.close = AsyncMock()

    mock_audio = MagicMock()
    mock_audio.start.return_value = tmp_path / "audio.mp3"
    mock_audio.stop.return_value = tmp_path / "audio.mp3"
    mock_audio.trim_silence_boundaries.return_value = None

    dedup = SlideDeduplicator(output_dir=tmp_path / "slides")

    monitor = MeetingMonitor(
        recorder=mock_recorder,
        deduplicator=dedup,
        audio_recorder=mock_audio,
        check_interval_sec=0.01,
    )

    result = await monitor.monitor_session(
        audio_dest=tmp_path / "audio.mp3",
        max_duration_min=180,
        min_duration_min=95.3,
        ended_checks_threshold=2,
    )

    # Must exit because of the 2 consecutive True values at the end, not on the first transient True!
    assert "Host closed meeting" in result["exit_reason"]
    assert mock_recorder.is_meeting_ended.call_count == 4


@pytest.mark.anyio
async def test_monitor_rule3_triggers_on_silence_and_participant_drop(tmp_path):
    mock_recorder = AsyncMock()
    mock_recorder.capture_screenshot.return_value = None
    mock_recorder.is_meeting_ended.return_value = False
    mock_recorder.get_participant_count.return_value = 2  # <= 3
    mock_recorder.close = AsyncMock()

    mock_audio = MagicMock()
    mock_audio.start.return_value = tmp_path / "audio.mp3"
    mock_audio.stop.return_value = tmp_path / "audio.mp3"
    mock_audio.trim_silence_boundaries.return_value = None
    mock_audio.check_recent_audio_level.return_value = {"is_silent": True, "max_volume": -42.0}

    dedup = SlideDeduplicator(output_dir=tmp_path / "slides")

    monitor = MeetingMonitor(
        recorder=mock_recorder,
        deduplicator=dedup,
        audio_recorder=mock_audio,
        check_interval_sec=0.01,
    )

    # Set min_duration_min=0 and silence_checks_threshold=1 for instant trigger
    result = await monitor.monitor_session(
        audio_dest=tmp_path / "audio.mp3",
        max_duration_min=180,
        min_duration_min=0,
        drop_threshold=3,
        silence_checks_threshold=1,
    )

    assert "Smart Exit 2.0: пара завершена" in result["exit_reason"]
    assert "участников 2 <= 3" in result["exit_reason"]


@pytest.mark.anyio
async def test_monitor_rule4_ignores_silence_and_drop_before_min_duration(tmp_path):
    """Protects laboratory sessions: even with 1 participant and silence, bot stays before 95.3 min."""
    mock_recorder = AsyncMock()
    mock_recorder.capture_screenshot.return_value = None
    mock_recorder.is_meeting_ended.return_value = False
    mock_recorder.get_participant_count.return_value = 1  # Alone on call
    mock_recorder.close = AsyncMock()

    mock_audio = MagicMock()
    mock_audio.start.return_value = tmp_path / "audio.mp3"
    mock_audio.stop.return_value = tmp_path / "audio.mp3"
    mock_audio.trim_silence_boundaries.return_value = None
    mock_audio.check_recent_audio_level.return_value = {"is_silent": True, "max_volume": -50.0}

    dedup = SlideDeduplicator(output_dir=tmp_path / "slides")

    monitor = MeetingMonitor(
        recorder=mock_recorder,
        deduplicator=dedup,
        audio_recorder=mock_audio,
        check_interval_sec=0.01,
    )

    async def delayed_stop():
        await asyncio.sleep(0.05)
        monitor.request_stop()

    asyncio.create_task(delayed_stop())

    result = await monitor.monitor_session(
        audio_dest=tmp_path / "audio.mp3",
        max_duration_min=180,
        min_duration_min=95.3,  # Minimum duration not reached yet!
        drop_threshold=3,
        silence_checks_threshold=1,
    )

    assert "Smart Exit" not in result["exit_reason"]
    assert "Запись остановлена пользователем" in result["exit_reason"]


@pytest.mark.anyio
async def test_monitor_rule4_ignores_silence_when_many_participants(tmp_path):
    mock_recorder = AsyncMock()
    mock_recorder.capture_screenshot.return_value = None
    mock_recorder.is_meeting_ended.return_value = False
    mock_recorder.get_participant_count.return_value = 18  # 18 participants
    mock_recorder.close = AsyncMock()

    mock_audio = MagicMock()
    mock_audio.start.return_value = tmp_path / "audio.mp3"
    mock_audio.stop.return_value = tmp_path / "audio.mp3"
    mock_audio.trim_silence_boundaries.return_value = None
    mock_audio.check_recent_audio_level.return_value = {"is_silent": True, "max_volume": -50.0}

    dedup = SlideDeduplicator(output_dir=tmp_path / "slides")

    monitor = MeetingMonitor(
        recorder=mock_recorder,
        deduplicator=dedup,
        audio_recorder=mock_audio,
        check_interval_sec=0.01,
    )

    async def delayed_stop():
        await asyncio.sleep(0.05)
        monitor.request_stop()

    asyncio.create_task(delayed_stop())

    result = await monitor.monitor_session(
        audio_dest=tmp_path / "audio.mp3",
        max_duration_min=180,
        min_duration_min=0,
        drop_threshold=3,
        silence_checks_threshold=1,
    )

    assert "Smart Exit" not in result["exit_reason"]


@pytest.mark.anyio
async def test_monitor_rule4_resets_silence_counter_on_voice(tmp_path):
    mock_recorder = AsyncMock()
    mock_recorder.capture_screenshot.return_value = None
    mock_recorder.is_meeting_ended.return_value = False
    mock_recorder.get_participant_count.return_value = 2  # <= 3
    mock_recorder.close = AsyncMock()

    mock_audio = MagicMock()
    mock_audio.start.return_value = tmp_path / "audio.mp3"
    mock_audio.stop.return_value = tmp_path / "audio.mp3"
    mock_audio.trim_silence_boundaries.return_value = None

    # Alternates: silent, sound (resets counter!), silent -> never reaches 2 consecutive
    mock_audio.check_recent_audio_level.side_effect = [
        {"is_silent": True, "max_volume": -45.0},
        {"is_silent": False, "max_volume": -15.0},  # Teacher speaking
        {"is_silent": True, "max_volume": -45.0},
    ]

    dedup = SlideDeduplicator(output_dir=tmp_path / "slides")

    monitor = MeetingMonitor(
        recorder=mock_recorder,
        deduplicator=dedup,
        audio_recorder=mock_audio,
        check_interval_sec=0.01,
    )

    async def delayed_stop():
        await asyncio.sleep(0.06)
        monitor.request_stop()

    asyncio.create_task(delayed_stop())

    result = await monitor.monitor_session(
        audio_dest=tmp_path / "audio.mp3",
        max_duration_min=180,
        min_duration_min=0,
        drop_threshold=3,
        silence_checks_threshold=2,
    )

    assert "Smart Exit" not in result["exit_reason"]


@pytest.mark.anyio
async def test_monitor_applies_trailing_silence_cutoff(tmp_path):
    mock_recorder = AsyncMock()
    mock_recorder.capture_screenshot.return_value = None
    mock_recorder.is_meeting_ended.return_value = True
    mock_recorder.close = AsyncMock()

    mock_audio = MagicMock()
    mock_audio.start.return_value = tmp_path / "audio.mp3"
    mock_audio.stop.return_value = tmp_path / "audio.mp3"
    mock_audio.trim_trailing_silence.return_value = 4272.5
    mock_audio.trim_silence_boundaries.return_value = (None, 4272.5)

    dedup = SlideDeduplicator(output_dir=tmp_path / "slides")

    monitor = MeetingMonitor(
        recorder=mock_recorder,
        deduplicator=dedup,
        audio_recorder=mock_audio,
        check_interval_sec=0.01,
    )

    result = await monitor.monitor_session(
        audio_dest=tmp_path / "audio.mp3",
        max_duration_min=180,
    )

    assert result["duration_seconds"] == 4272.5


def test_audio_recorder_check_recent_audio_level(tmp_path):
    audio_rec = AudioRecorder(output_path=tmp_path / "missing.mp3")
    res = audio_rec.check_recent_audio_level(duration_sec=10.0)
    assert res["is_silent"] is True
    assert res["max_volume"] == -99.0


@pytest.mark.anyio
async def test_monitor_tracks_attendance_in_result(tmp_path):
    mock_recorder = AsyncMock()
    mock_recorder.capture_screenshot.return_value = None
    mock_recorder.is_meeting_ended.return_value = True  # Exit after 1 loop
    mock_recorder.get_active_participants.return_value = ["Павел Николаев", "Иван Иванов"]
    mock_recorder.close = AsyncMock()

    mock_audio = MagicMock()
    mock_audio.start.return_value = tmp_path / "audio.mp3"
    mock_audio.stop.return_value = tmp_path / "audio.mp3"
    mock_audio.trim_silence_boundaries.return_value = None

    dedup = SlideDeduplicator(output_dir=tmp_path / "slides")

    monitor = MeetingMonitor(
        recorder=mock_recorder,
        deduplicator=dedup,
        audio_recorder=mock_audio,
        check_interval_sec=0.01,
        participant_check_interval_sec=0.0,
    )

    result = await monitor.monitor_session(
        audio_dest=tmp_path / "audio.mp3",
        max_duration_min=180,
    )

    assert "attendance" in result
    att = result["attendance"]
    assert att["total_unique_participants"] == 2
    names = [p["name"] for p in att["participants"]]
    assert "Павел Николаев" in names
    assert "Иван Иванов" in names


@pytest.mark.anyio
async def test_monitor_audio_health_check_detects_crash(tmp_path):
    mock_recorder = AsyncMock()
    mock_recorder.capture_screenshot.return_value = None
    mock_recorder.is_meeting_ended.return_value = True
    mock_recorder.close = AsyncMock()

    mock_audio = MagicMock()
    mock_audio.start.return_value = tmp_path / "audio.mp3"
    mock_audio.stop.return_value = tmp_path / "audio.mp3"
    mock_audio.trim_silence_boundaries.return_value = None
    # Simulate crashed audio process
    mock_audio.is_alive = False
    mock_proc = MagicMock()
    mock_proc.poll.return_value = 137
    mock_proc.returncode = 137
    mock_audio.process = mock_proc

    dedup = SlideDeduplicator(output_dir=tmp_path / "slides")

    monitor = MeetingMonitor(
        recorder=mock_recorder,
        deduplicator=dedup,
        audio_recorder=mock_audio,
        check_interval_sec=0.01,
    )

    result = await monitor.monitor_session(
        audio_dest=tmp_path / "audio.mp3",
        max_duration_min=180,
    )

    assert result["audio_crashed"] is True
    assert monitor.audio_crashed is True

