import pytest
from pathlib import Path
from unittest.mock import MagicMock

from ai.analyzer import LectureAnalyzer, extract_slide_timecodes, _upload_file_with_retry
from ai.schemas import LectureAnalysisResult, LectureSection, IllustratedLongread, ActionItemsDigest
from orchestrator import LectureOrchestrator


def test_extract_slide_timecodes_absolute_timestamps(tmp_path):
    """Verifies that absolute timestamps YYYY-MM-DD_HH-MM-SS are converted to relative MM:SS."""
    f1 = tmp_path / "slide_001_2026-09-15_10-00-00.png"
    f2 = tmp_path / "slide_002_2026-09-15_10-05-30.png"
    f3 = tmp_path / "slide_003_2026-09-15_10-18-45.png"
    for f in (f1, f2, f3):
        f.touch()

    parsed = extract_slide_timecodes([f1, f2, f3])
    assert len(parsed) == 3
    assert parsed[0]["timecode"] == "00:00"
    assert parsed[1]["timecode"] == "05:30"
    assert parsed[2]["timecode"] == "18:45"


def test_extract_slide_timecodes_relative_formats(tmp_path):
    """Verifies parsing of formats like 14m20s and 120s."""
    f1 = tmp_path / "slide_001_00m00s.png"
    f2 = tmp_path / "slide_002_14m20s.png"
    f3 = tmp_path / "slide_003_90s.png"
    f4 = tmp_path / "slide_004_plain.png"
    for f in (f1, f2, f3, f4):
        f.touch()

    parsed = extract_slide_timecodes([f1, f2, f3, f4])
    assert parsed[0]["timecode"] == "00:00"
    assert parsed[1]["timecode"] == "14:20"
    assert parsed[2]["timecode"] == "01:30"
    assert parsed[3]["timecode"] == "00:00"


def test_sanitize_slide_references():
    """Verifies that invalid/hallucinated slide filenames are stripped, but valid and prefix-matched ones are kept."""
    analyzer = LectureAnalyzer(api_key="fake_key")
    analyzer.simulation_mode = True

    sections = [
        LectureSection(
            heading="Section 1",
            timecode="00:00",
            content_markdown="Intro",
            referenced_slide_filenames=[
                "slide_001_2026-09-15_10-00-00.png",  # exact match
                "slide_999_hallucinated.png",         # non-existent
                "slide_002",                          # prefix match
            ],
        )
    ]
    result = LectureAnalysisResult(
        digest=ActionItemsDigest(
            subject="Тест",
            topic="Тест",
            date="Сегодня",
            executive_summary="Резюме",
            action_items=[],
            key_technologies_and_terms=[],
        ),
        longread=IllustratedLongread(
            title="Тест",
            subject="Тест",
            topic="Тест",
            overview="Обзор",
            sections=sections,
        ),
    )

    real_slides = [
        Path("slide_001_2026-09-15_10-00-00.png"),
        Path("slide_002_2026-09-15_10-05-30.png"),
    ]

    analyzer._sanitize_slide_references(result, real_slides)

    cleaned = result.longread.sections[0].referenced_slide_filenames
    assert "slide_001_2026-09-15_10-00-00.png" in cleaned
    assert "slide_002_2026-09-15_10-05-30.png" in cleaned
    assert "slide_999_hallucinated.png" not in cleaned


def test_upload_file_with_retry_on_429():
    """Verifies that transient rate-limit errors (429) trigger retries."""
    mock_client = MagicMock()
    mock_file = Path("dummy.png")

    calls = []

    def mock_upload(file):
        calls.append(len(calls) + 1)
        if len(calls) < 2:
            raise Exception("429 ResourceExhausted: rate limit exceeded")
        return MagicMock(name="uploaded_file")

    mock_client.files.upload.side_effect = mock_upload

    # Should succeed on 2nd attempt
    res = _upload_file_with_retry(mock_client, mock_file, max_retries=3)
    assert res is not None
    assert len(calls) == 2


@pytest.mark.anyio
async def test_orchestrator_bypasses_dedup_when_slide_paths_provided(tmp_path):
    """Verifies orchestrator uses slide_paths directly without re-deduplicating."""
    mock_repo = tmp_path / "repo"
    slide1 = tmp_path / "slide_001_2026-09-15_10-00-00.png"
    slide2 = tmp_path / "slide_002_2026-09-15_10-10-00.png"
    slide1.touch()
    slide2.touch()

    analyzer = LectureAnalyzer()
    analyzer.simulation_mode = True

    from publisher.github_sync import GitHubPublisher
    from publisher.telegram_pub import TelegramPublisher

    github_pub = GitHubPublisher(local_repo_dir=mock_repo)
    github_pub.simulation_mode = True
    telegram_pub = TelegramPublisher()
    telegram_pub.simulation_mode = True

    orch = LectureOrchestrator(
        analyzer=analyzer,
        github_pub=github_pub,
        telegram_pub=telegram_pub,
    )

    summary = await orch.run_pipeline(
        audio_path=None,
        slide_paths=[slide1, slide2],
        subject_hint="Тестовый Предмет",
    )

    assert summary["unique_slides_count"] == 2
