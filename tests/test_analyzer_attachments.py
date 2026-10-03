import pytest
from pathlib import Path
from ai.analyzer import LectureAnalyzer
from ai.prompts import create_lecture_analysis_prompt


def test_create_lecture_analysis_prompt_with_attachments():
    attachments = [
        {"filename": "test_solution.py", "content": "def solve(): return 42"},
        {"filename": "guide.docx", "content": "Методические указания к лабораторной работе №1"},
    ]
    prompt = create_lecture_analysis_prompt(
        subject_hint="Программирование",
        slides_info=[{"filename": "slide_001.png", "timecode": "01m00s"}],
        is_audio_silent=False,
        attachment_texts=attachments,
    )

    assert "test_solution.py" in prompt
    assert "def solve(): return 42" in prompt
    assert "guide.docx" in prompt
    assert "Методические указания" in prompt
    assert "slide_001.png" in prompt


def test_analyzer_simulation_with_attachments(tmp_path):
    py_file = tmp_path / "solution.py"
    py_file.write_text("print('hello')", encoding="utf-8")

    analyzer = LectureAnalyzer()
    analyzer.simulation_mode = True

    result = analyzer.analyze(
        attachment_paths=[py_file],
        subject_hint="Тестовый предмет",
    )

    assert result is not None
    assert result.digest.subject == "Тестовый предмет"
    assert result.model_name is not None
