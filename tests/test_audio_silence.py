import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock
import pytest
from recorder.audio import AudioRecorder


def test_trim_silence_boundaries_no_file(tmp_path):
    p = tmp_path / "non_existent.mp3"
    start, end = AudioRecorder.trim_silence_boundaries(p)
    assert start is None
    assert end is None


def test_trim_silence_boundaries_leading_and_trailing(tmp_path):
    p = tmp_path / "lecture.mp3"
    p.write_bytes(b"\x00" * 2048)

    # Simulated ffmpeg silencedetect stderr output:
    # 0 to 180s: silence at beginning
    # 180s to 5000s: lecture
    # 5000s to 5400s: silence at end
    fake_ffmpeg_output = (
        "Duration: 01:30:00.00, start: 0.000000, bitrate: 64 kb/s\n"
        "[silencedetect @ 0x123] silence_start: 0.000\n"
        "[silencedetect @ 0x123] silence_end: 180.000 | silence_duration: 180.000\n"
        "[silencedetect @ 0x123] silence_start: 5000.000\n"
    )

    def mock_subprocess_run(cmd, *args, **kwargs):
        # First call is silencedetect analysis
        if "silencedetect" in " ".join(cmd):
            mock_res = MagicMock()
            mock_res.returncode = 0
            mock_res.stderr = fake_ffmpeg_output
            return mock_res
        # Second call is the trim cut
        mock_res = MagicMock()
        mock_res.returncode = 0
        # Create the temp target file to simulate ffmpeg cut output
        for arg in cmd:
            if "trimmed_" in str(arg):
                Path(arg).write_bytes(b"\x00" * 1500)
        return mock_res

    with patch("subprocess.run", side_effect=mock_subprocess_run):
        start_cut, end_cut = AudioRecorder.trim_silence_boundaries(
            p,
            min_leading_silence_sec=20.0,
            min_trailing_silence_sec=25.0,
        )

        # 180.0 - 1.5 = 178.5s
        assert start_cut is not None
        assert abs(start_cut - 178.5) < 0.01

        # 5000.0 + 1.5 = 5001.5s
        assert end_cut is not None
        assert abs(end_cut - 5001.5) < 0.01


def test_trim_leading_silence_wrapper(tmp_path):
    p = tmp_path / "lecture.mp3"
    p.write_bytes(b"\x00" * 2048)

    fake_ffmpeg_output = (
        "Duration: 01:00:00.00, start: 0.000000, bitrate: 64 kb/s\n"
        "[silencedetect @ 0x123] silence_end: 240.000 | silence_duration: 240.000\n"
    )

    def mock_subprocess_run(cmd, *args, **kwargs):
        if "silencedetect" in " ".join(cmd):
            mock_res = MagicMock()
            mock_res.returncode = 0
            mock_res.stderr = fake_ffmpeg_output
            return mock_res
        mock_res = MagicMock()
        mock_res.returncode = 0
        for arg in cmd:
            if "trimmed_" in str(arg):
                Path(arg).write_bytes(b"\x00" * 1500)
        return mock_res

    with patch("subprocess.run", side_effect=mock_subprocess_run):
        start_cut = AudioRecorder.trim_leading_silence(p, min_leading_silence_sec=20.0)
        # 240.0 - 1.5 = 238.5s
        assert start_cut is not None
        assert abs(start_cut - 238.5) < 0.01


def test_trim_silence_no_silence(tmp_path):
    p = tmp_path / "lecture.mp3"
    p.write_bytes(b"\x00" * 2048)

    fake_ffmpeg_output = (
        "Duration: 01:00:00.00, start: 0.000000, bitrate: 64 kb/s\n"
        "Stream #0:0: Audio: mp3, 16000 Hz, mono, fltp, 64 kb/s\n"
    )

    def mock_subprocess_run(cmd, *args, **kwargs):
        mock_res = MagicMock()
        mock_res.returncode = 0
        mock_res.stderr = fake_ffmpeg_output
        return mock_res

    with patch("subprocess.run", side_effect=mock_subprocess_run):
        start_cut, end_cut = AudioRecorder.trim_silence_boundaries(p)
        assert start_cut is None
        assert end_cut is None


def test_check_recent_audio_level_mocked(tmp_path):
    p = tmp_path / "recording.mp3"
    p.write_bytes(b"\x00" * 2048)

    rec = AudioRecorder(output_path=p)
    rec.simulation_mode = False

    fake_silent = (
        "[Parsed_volumedetect_0] mean_volume: -45.2 dB\n"
        "[Parsed_volumedetect_0] max_volume: -40.1 dB\n"
    )

    with patch("subprocess.run") as mock_run:
        mock_res = MagicMock()
        mock_res.returncode = 0
        mock_res.stderr = fake_silent
        mock_run.return_value = mock_res

        res = rec.check_recent_audio_level(duration_sec=10.0, silence_threshold_db=-38.0)
        assert res["is_silent"] is True
        assert res["max_volume"] == -40.1

        # Loud audio
        fake_loud = (
            "[Parsed_volumedetect_0] mean_volume: -20.2 dB\n"
            "[Parsed_volumedetect_0] max_volume: -15.1 dB\n"
        )
        mock_res.stderr = fake_loud
        res_loud = rec.check_recent_audio_level(duration_sec=10.0, silence_threshold_db=-38.0)
        assert res_loud["is_silent"] is False
        assert res_loud["max_volume"] == -15.1


def test_audio_recorder_is_alive_property(tmp_path):
    rec = AudioRecorder(output_path=tmp_path / "test.mp3")
    rec.simulation_mode = False
    rec._is_recording = False
    assert rec.is_alive is False

    rec._is_recording = True
    # No process
    rec.process = None
    assert rec.is_alive is False

    # Process running (poll() is None)
    mock_proc = MagicMock()
    mock_proc.poll.return_value = None
    rec.process = mock_proc
    assert rec.is_alive is True

    # Process exited (poll() is 1)
    mock_proc.poll.return_value = 1
    assert rec.is_alive is False

    # Simulation mode always returns True if recording
    rec.simulation_mode = True
    assert rec.is_alive is True

