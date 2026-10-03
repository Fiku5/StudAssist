import pytest
from unittest.mock import MagicMock
from ai.analyzer import LectureAnalyzer


def test_analyzer_model_priority():
    """Verifies that configured model or gemini-flash-latest is prioritized."""
    analyzer = LectureAnalyzer(api_key="test_key", model="gemini-flash-latest")
    assert analyzer.model_name == "gemini-flash-latest"


def test_analyzer_simulation_mode():
    """Verifies analyzer runs cleanly in simulation mode and populates model_name."""
    analyzer = LectureAnalyzer(api_key=None)
    assert analyzer.simulation_mode is True

    result = analyzer.analyze(
        audio_path=None,
        slide_paths=None,
        subject_hint="Операционные системы",
        is_audio_silent=True,
    )
    assert result.digest.subject == "Операционные системы"
    assert result.longread is not None
    assert result.model_name is not None
    assert "AI-модель" in result.digest.to_telegram_markdown()
    assert "AI-модель" in result.longread.to_markdown_document()


def test_analyzer_flash_chain_execution():
    """Verifies that gemini-flash-latest is invoked and model_name is preserved."""
    analyzer = LectureAnalyzer(api_key="fake_key", model="gemini-flash-latest")
    analyzer.simulation_mode = False

    mock_client = MagicMock()
    analyzer.client = mock_client

    call_history = []

    def mock_generate(model, contents, config):
        call_history.append(model)
        mock_resp = MagicMock()
        mock_resp.text = analyzer._generate_simulated_result("Тест").model_dump_json()
        return mock_resp

    mock_client.models.generate_content.side_effect = mock_generate
    mock_client.files.upload.return_value = MagicMock(name="uploaded_file")
    mock_client.files.delete.return_value = None

    result = analyzer._run_gemini_analysis(
        audio_path=None,
        slide_paths=None,
        subject_hint="Тест",
        is_audio_silent=True,
    )

    assert result.digest.subject == "Тест"
    assert result.model_name == "gemini-flash-latest"
    assert call_history[0] == "gemini-flash-latest"
    assert "gemini-flash-latest" in result.digest.to_telegram_markdown()
    assert "gemini-flash-latest" in result.longread.to_markdown_document()


def test_analyzer_prioritizes_flash_latest_even_with_legacy_38_config():
    """Verifies that gemini-flash-latest is tried before gemini-3.8-flash even if 3.8 is configured."""
    analyzer = LectureAnalyzer(api_key="fake_key", model="gemini-3.8-flash")
    analyzer.simulation_mode = False

    mock_client = MagicMock()
    analyzer.client = mock_client
    call_history = []

    def mock_generate(model, contents, config):
        call_history.append(model)
        mock_resp = MagicMock()
        mock_resp.text = analyzer._generate_simulated_result("Тест").model_dump_json()
        return mock_resp

    mock_client.models.generate_content.side_effect = mock_generate
    mock_client.files.upload.return_value = MagicMock(name="uploaded_file")
    mock_client.files.delete.return_value = None

    result = analyzer._run_gemini_analysis(
        audio_path=None,
        slide_paths=None,
        subject_hint="Тест",
        is_audio_silent=True,
    )
    assert call_history[0] == "gemini-flash-latest"
    assert result.model_name == "gemini-flash-latest"


def test_analyzer_cascade_backoff_and_failover(monkeypatch):
    """Verifies that model retries 4 times with [20, 40, 60] delays before failing over to next model."""
    analyzer = LectureAnalyzer(api_key="fake_key", model="gemini-flash-latest")
    analyzer.simulation_mode = False

    mock_client = MagicMock()
    analyzer.client = mock_client
    call_history = []
    sleep_calls = []

    monkeypatch.setattr("time.sleep", lambda s: sleep_calls.append(s))

    class Mock503(Exception):
        pass

    def mock_generate(model, contents, config):
        call_history.append(model)
        if model == "gemini-flash-latest":
            # Always 503 for flash-latest
            raise Mock503("503 UNAVAILABLE. {'error': {'code': 503, 'message': 'high demand'}}")
        elif model == "gemini-3.8-flash":
            # 503 on first 2 attempts, then succeeds on attempt 3
            if call_history.count("gemini-3.8-flash") < 3:
                raise Mock503("503 UNAVAILABLE. {'error': {'code': 503, 'message': 'spikes in demand'}}")
            mock_resp = MagicMock()
            mock_resp.text = analyzer._generate_simulated_result("Тест").model_dump_json()
            return mock_resp
        raise Mock503("503 unexpected model")

    mock_client.models.generate_content.side_effect = mock_generate
    mock_client.files.upload.return_value = MagicMock(name="uploaded_file")
    mock_client.files.delete.return_value = None

    result = analyzer._run_gemini_analysis(
        audio_path=None,
        slide_paths=None,
        subject_hint="Тест",
        is_audio_silent=True,
    )

    # 1. gemini-flash-latest must have been tried exactly 4 times
    assert call_history.count("gemini-flash-latest") == 4
    # 2. Sleeps for flash-latest must be [20, 40, 60]
    assert sleep_calls[:3] == [20, 40, 60]

    # 3. gemini-3.8-flash was tried 3 times and succeeded on attempt 3
    assert call_history.count("gemini-3.8-flash") == 3
    # Sleeps for 3.8-flash must be [20, 40]
    assert sleep_calls[3:5] == [20, 40]

    # 4. Result model attribution
    assert result.model_name == "gemini-3.8-flash"


def test_analyzer_all_16_attempts_exhausted(monkeypatch):
    """Verifies that when all 4 candidate models fail 4 times, exactly 16 attempts are made."""
    analyzer = LectureAnalyzer(api_key="fake_key", model="gemini-flash-latest")
    analyzer.simulation_mode = False

    mock_client = MagicMock()
    analyzer.client = mock_client
    call_history = []
    sleep_calls = []

    monkeypatch.setattr("time.sleep", lambda s: sleep_calls.append(s))

    class Mock503(Exception):
        pass

    def mock_generate(model, contents, config):
        call_history.append(model)
        raise Mock503("503 UNAVAILABLE. {'error': {'code': 503, 'message': 'high demand'}}")

    mock_client.models.generate_content.side_effect = mock_generate
    mock_client.files.upload.return_value = MagicMock(name="uploaded_file")
    mock_client.files.delete.return_value = None

    with pytest.raises(Mock503):
        analyzer._run_gemini_analysis(
            audio_path=None,
            slide_paths=None,
            subject_hint="Тест",
            is_audio_silent=True,
        )

    # Exactly 4 models x 4 attempts = 16 attempts total
    assert len(call_history) == 16
    assert call_history.count("gemini-flash-latest") == 4
    assert call_history.count("gemini-3.8-flash") == 4
    assert call_history.count("gemini-3.7-flash") == 4
    assert call_history.count("gemini-3.6-flash") == 4

    # Each model has 3 sleeps (20, 40, 60), total 12 sleeps
    assert len(sleep_calls) == 12
    assert sleep_calls == [20, 40, 60] * 4

