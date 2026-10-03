import json
from ai.schemas import (
    ActionItem,
    ActionItemsDigest,
    LectureSection,
    IllustratedLongread,
    LectureAnalysisResult,
)


def test_action_items_digest_formatting():
    item1 = ActionItem(
        category="lab",
        title="Лаба №1",
        description="Реализовать HTTP-сервер",
        deadline="15 октября",
        importance="critical",
    )
    item2 = ActionItem(
        category="exam_hint",
        title="Теорема CAP",
        description="Препод спросит доказательство",
        importance="high",
    )

    digest = ActionItemsDigest(
        subject="Распределенные системы",
        topic="Консенсус Raft",
        executive_summary="Рассмотрели выборы лидера и репликацию логов.",
        action_items=[item1, item2],
        key_technologies_and_terms=["Raft", "CAP Theorem", "Paxos"],
    )

    msg = digest.to_telegram_markdown(full_article_url="https://github.com/repo/notes.md")

    assert "Распределенные системы" in msg
    assert "Консенсус Raft" in msg
    assert "[Лабораторная]" in msg
    assert "Лаба №1" in msg
    assert "15 октября" in msg
    assert "[Экзамен/Зачет]" in msg
    assert "`Raft`" in msg
    assert "https://github.com/repo/notes.md" in msg

    # Test HTML rendering
    html_msg = digest.to_telegram_html(full_article_url="https://github.com/repo/notes.md")
    assert "<b>Распределенные системы</b>" in html_msg
    assert "<i>Консенсус Raft</i>" in html_msg
    assert "<b>[Лабораторная]</b>" in html_msg
    assert "<code>Raft</code>" in html_msg
    assert '<a href="https://github.com/repo/notes.md">' in html_msg


def test_illustrated_longread_rendering():
    sec = LectureSection(
        heading="Выборы лидера в Raft",
        timecode="12m30s",
        content_markdown="Каждый узел может находиться в одном из трех состояний.",
        referenced_slide_filenames=["slide_001_12m30s.png"],
        exam_cues=["Препод требует знать таймауты сердечных сокращений (heartbeat)."],
    )

    longread = IllustratedLongread(
        title="Конспект по Raft",
        subject="Распределенные системы",
        topic="Алгоритм Raft",
        overview="Введение в алгоритмы консенсуса.",
        sections=[sec],
    )

    md = longread.to_markdown_document(assets_relative_path="assets")

    assert "# Распределенные системы: Алгоритм Raft" in md
    assert "## Выборы лидера в Raft `[12m30s]`" in md
    assert "![slide_001_12m30s.png](assets/slide_001_12m30s.png)" in md
    assert "> [!IMPORTANT]" in md
    assert "таймауты сердечных сокращений" in md


def test_lecture_analysis_result_json_roundtrip():
    item = ActionItem(
        category="homework",
        title="ДЗ 1",
        description="Прочитать статью",
    )
    digest = ActionItemsDigest(
        subject="Тестирование",
        topic="Unit тесты",
        executive_summary="Кратко о тестах",
        action_items=[item],
        key_technologies_and_terms=["pytest"],
    )
    longread = IllustratedLongread(
        title="Тестирование: Unit",
        subject="Тестирование",
        topic="Unit тесты",
        overview="Обзор",
        sections=[],
    )

    original = LectureAnalysisResult(digest=digest, longread=longread)
    json_str = original.model_dump_json()

    restored = LectureAnalysisResult.model_validate_json(json_str)
    assert restored.digest.subject == "Тестирование"
    assert len(restored.digest.action_items) == 1
    assert restored.digest.action_items[0].title == "ДЗ 1"


def test_empty_action_items_and_source_quote():
    # 1. Empty action items message
    empty_digest = ActionItemsDigest(
        subject="Базы данных",
        topic="Нормализация",
        executive_summary="Изучили 1NF, 2NF, 3NF.",
        action_items=[],
        key_technologies_and_terms=["1NF", "2NF", "3NF"],
    )
    empty_md = empty_digest.to_telegram_markdown()
    assert "не озвучил домашних заданий" in empty_md

    # 2. Source quote rendering
    item_with_quote = ActionItem(
        category="homework",
        title="Сдать схему БД",
        description="Прислать ER-диаграмму",
        source_quote="Схему сдаем к следующему понедельнику в личку",
    )
    digest_quoted = ActionItemsDigest(
        subject="Базы данных",
        topic="Нормализация",
        executive_summary="Изучили 1NF, 2NF, 3NF.",
        action_items=[item_with_quote],
        key_technologies_and_terms=["ERD"],
    )
    quoted_md = digest_quoted.to_telegram_markdown()
    assert "«Схему сдаем к следующему понедельнику в личку»" in quoted_md
