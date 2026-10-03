import pytest
from pathlib import Path
from PIL import Image

from orchestrator import LectureOrchestrator
from ai.analyzer import LectureAnalyzer
from publisher.github_sync import GitHubPublisher
from publisher.telegram_pub import TelegramPublisher


@pytest.mark.anyio
async def test_orchestrator_pipeline_end_to_end(tmp_path):
    mock_repo = tmp_path / "test_repo"
    assets_dir = tmp_path / "test_assets"

    from PIL import ImageDraw

    # 1. Create frames (2 duplicates, 1 different)
    frame1 = tmp_path / "frame1.png"
    img1 = Image.new("RGB", (400, 300), color=(100, 150, 200))
    d1 = ImageDraw.Draw(img1)
    d1.text((30, 30), "Slide 1: Architecture", fill=(255, 255, 255))
    img1.save(frame1)

    frame2 = tmp_path / "frame2_dup.png"
    img1.save(frame2)  # Duplicate

    frame3 = tmp_path / "frame3_diff.png"
    img3 = Image.new("RGB", (400, 300), color=(30, 30, 30))
    d3 = ImageDraw.Draw(img3)
    d3.rectangle([(50, 50), (350, 250)], fill=(255, 200, 50))
    d3.text((60, 60), "Slide 2: Completely New Topic", fill=(0, 0, 0))
    img3.save(frame3)

    # 2. Setup orchestrator in simulation mode
    analyzer = LectureAnalyzer()
    analyzer.simulation_mode = True

    github_pub = GitHubPublisher(local_repo_dir=mock_repo)
    github_pub.simulation_mode = True

    telegram_pub = TelegramPublisher()
    telegram_pub.simulation_mode = True

    orchestrator = LectureOrchestrator(
        analyzer=analyzer,
        github_pub=github_pub,
        telegram_pub=telegram_pub,
    )

    # 3. Run pipeline
    summary = await orchestrator.run_pipeline(
        audio_path=None,
        raw_slide_paths=[frame1, frame2, frame3],
        subject_hint="Операционные Системы",
        assets_output_dir=assets_dir,
    )

    # 4. Assertions
    assert summary["subject"] == "Операционные Системы"
    assert summary["unique_slides_count"] == 2  # 3 frames -> 2 unique slides!
    assert "github_url" in summary
    assert summary["action_items_count"] > 0

    # Verify mock repo contains generated files
    assert mock_repo.exists()
    readme_file = mock_repo / "README.md"
    assert readme_file.exists()
    assert "Операционные Системы" in readme_file.read_text(encoding="utf-8")


@pytest.mark.anyio
async def test_orchestrator_caching(tmp_path):
    """Verifies that orchestrator reuses analysis_cache.json if present in session dir."""
    from unittest.mock import MagicMock
    from ai.schemas import ActionItemsDigest, IllustratedLongread, LectureAnalysisResult

    sess_dir = tmp_path / "session_test"
    sess_dir.mkdir()
    audio_path = sess_dir / "audio.mp3"
    audio_path.write_bytes(b"dummy_mp3")

    cached_digest = ActionItemsDigest(
        subject="CachedSubject",
        topic="CachedTopic",
        executive_summary="From Cache",
        action_items=[],
        key_technologies_and_terms=[],
    )
    cached_longread = IllustratedLongread(
        title="Cached",
        subject="CachedSubject",
        topic="CachedTopic",
        overview="From Cache",
        sections=[],
    )
    cached_res = LectureAnalysisResult(digest=cached_digest, longread=cached_longread)
    (sess_dir / "analysis_cache.json").write_text(cached_res.model_dump_json(), encoding="utf-8")

    mock_analyzer = MagicMock()
    mock_analyzer.model_name = "gemini-test"

    mock_github = MagicMock()
    mock_github.publish_lecture.return_value = "https://github.com/test/repo"

    mock_tg = MagicMock()

    async def mock_pub(*args, **kwargs):
        return 12345
    mock_tg.publish_to_channel = mock_pub

    orchestrator = LectureOrchestrator(
        analyzer=mock_analyzer,
        github_pub=mock_github,
        telegram_pub=mock_tg,
    )

    summary = await orchestrator.run_pipeline(
        audio_path=audio_path,
        slide_paths=[],
        subject_hint="NewHint",
    )

    # analyzer.analyze should NOT be called because it loaded from cache
    mock_analyzer.analyze.assert_not_called()
    assert summary["subject"] == "CachedSubject"
    assert summary["topic"] == "CachedTopic"


@pytest.mark.anyio
async def test_orchestrator_force_reanalyze(tmp_path):
    """Verifies that orchestrator bypasses analysis_cache.json if force_reanalyze=True."""
    from unittest.mock import MagicMock
    from ai.schemas import ActionItemsDigest, IllustratedLongread, LectureAnalysisResult

    sess_dir = tmp_path / "session_test_force"
    sess_dir.mkdir()
    audio_path = sess_dir / "audio.mp3"
    audio_path.write_bytes(b"dummy_mp3")

    cached_digest = ActionItemsDigest(
        subject="OldSubject",
        topic="OldTopic",
        executive_summary="Old summary",
        action_items=[],
        key_technologies_and_terms=[],
    )
    cached_longread = IllustratedLongread(
        title="Old",
        subject="OldSubject",
        topic="OldTopic",
        overview="Old overview",
        sections=[],
    )
    cached_res = LectureAnalysisResult(digest=cached_digest, longread=cached_longread)
    (sess_dir / "analysis_cache.json").write_text(cached_res.model_dump_json(), encoding="utf-8")

    fresh_digest = ActionItemsDigest(
        subject="FreshSubject",
        topic="FreshTopic",
        executive_summary="Fresh summary",
        action_items=[],
        key_technologies_and_terms=[],
    )
    fresh_longread = IllustratedLongread(
        title="Fresh",
        subject="FreshSubject",
        topic="FreshTopic",
        overview="Fresh overview",
        sections=[],
    )
    fresh_res = LectureAnalysisResult(digest=fresh_digest, longread=fresh_longread)

    mock_analyzer = MagicMock()
    mock_analyzer.model_name = "gemini-flash-latest"
    mock_analyzer.analyze.return_value = fresh_res

    mock_github = MagicMock()
    mock_github.publish_lecture.return_value = "https://github.com/test/repo"

    mock_tg = MagicMock()
    async def mock_pub(*args, **kwargs):
        return 12345
    mock_tg.publish_to_channel = mock_pub

    orchestrator = LectureOrchestrator(
        analyzer=mock_analyzer,
        github_pub=mock_github,
        telegram_pub=mock_tg,
    )

    summary = await orchestrator.run_pipeline(
        audio_path=audio_path,
        slide_paths=[],
        subject_hint="FreshSubject",
        force_reanalyze=True,
    )

    # analyzer.analyze SHOULD be called because force_reanalyze was True
    mock_analyzer.analyze.assert_called_once()
    assert summary["subject"] == "FreshSubject"
    assert summary["topic"] == "FreshTopic"


