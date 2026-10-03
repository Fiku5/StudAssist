#!/usr/bin/env python3
"""
CLI demonstration and testing script for StudentAssistant PoC.
Demonstrates:
1. Slide deduplication via pHash
2. AI-analysis with Gemini 3.8 Flash
3. Generation of Telegram Action Items (Tier 1) and Markdown Longread (Tier 2)
"""

import argparse
import sys
from pathlib import Path

# Ensure UTF-8 output on Windows consoles (cp1251)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from PIL import Image, ImageDraw, ImageFont

from config import settings, BASE_DIR
from recorder.visual import SlideDeduplicator
from ai.analyzer import LectureAnalyzer


def create_demo_assets(target_dir: Path):
    """Creates synthetic lecture frames (including duplicates) for demonstration."""
    target_dir.mkdir(parents=True, exist_ok=True)
    frames_info = [
        ("Слайд 1: Введение в Dependency Injection\nПаттерны проектирования", 0, "slide1_frame1.png"),
        ("Слайд 1: Введение в Dependency Injection\nПаттерны проектирования", 5, "slide1_frame2_dup.png"),  # Duplicate
        ("Слайд 1: Введение в Dependency Injection\nПаттерны проектирования", 10, "slide1_frame3_dup.png"), # Duplicate
        ("Слайд 2: Жизненные циклы (Lifetimes)\nTransient, Scoped, Singleton", 60, "slide2_frame1.png"),
        ("Слайд 2: Жизненные циклы (Lifetimes)\nTransient, Scoped, Singleton", 65, "slide2_frame2_dup.png"),# Duplicate
        ("Слайд 3: ДЗ и Лабораторная №3\nСрок сдачи: следующая пятница", 120, "slide3_frame1.png"),
    ]

    created_paths = []
    for text, sec, filename in frames_info:
        img_path = target_dir / filename
        img = Image.new("RGB", (800, 450), color=(30, 34, 42))
        draw = ImageDraw.Draw(img)
        draw.rectangle([(20, 20), (780, 430)], outline=(80, 160, 255), width=3)
        draw.text((50, 150), text, fill=(240, 240, 240))
        draw.text((50, 380), f"Таймкод: {sec}s", fill=(150, 150, 150))
        img.save(img_path, "PNG")
        created_paths.append((img_path, sec))

    return created_paths


def main():
    parser = argparse.ArgumentParser(description="StudentAssistant PoC Runner")
    parser.add_argument("--audio", type=str, help="Path to lecture audio file (MP3/WAV)")
    parser.add_argument("--slides-dir", type=str, help="Path to folder containing raw screenshot frames")
    parser.add_argument("--subject", type=str, default="Архитектура ПО", help="Subject name hint")
    parser.add_argument("--output-dir", type=str, default="output", help="Output directory")
    parser.add_argument("--dry-run", action="store_true", help="Force simulation mode (no API key needed)")
    parser.add_argument("--demo", action="store_true", help="Run full end-to-end demo with synthetic slides")

    args = parser.parse_args()

    out_dir = Path(args.output_dir)
    out_dir.mkdir(exist_ok=True, parents=True)
    assets_dir = out_dir / "assets"
    assets_dir.mkdir(exist_ok=True, parents=True)

    print("=" * 60)
    print("🚀 Студенческий Ассистент (StudentAssistant) — Запуск PoC")
    print("=" * 60)

    # 1. Prepare visual frames
    raw_frames = []
    if args.demo or (not args.slides_dir and not args.audio):
        print("\n📸 [1/3] Создание демонстрационных кадров лекции (включая дубликаты)...")
        demo_dir = BASE_DIR / "temp" / "demo_raw_frames"
        raw_frames = create_demo_assets(demo_dir)
        print(f"   Создано сырых кадров: {len(raw_frames)}")
    elif args.slides_dir:
        slides_path = Path(args.slides_dir)
        for idx, f in enumerate(sorted(slides_path.glob("*.png"))):
            raw_frames.append((f, idx * 5))

    from orchestrator import LectureOrchestrator
    from publisher.github_sync import GitHubPublisher
    from publisher.telegram_pub import TelegramPublisher

    repo_dir = out_dir / "repo"
    orchestrator = LectureOrchestrator(
        github_pub=GitHubPublisher(local_repo_dir=repo_dir),
        telegram_pub=TelegramPublisher(),
    )
    if args.dry_run:
        orchestrator.analyzer.simulation_mode = True
        orchestrator.github_pub.simulation_mode = True
        orchestrator.telegram_pub.simulation_mode = True

    slide_paths_only = [p for p, _ in raw_frames]
    summary = orchestrator.run_pipeline_sync(
        audio_path=args.audio,
        raw_slide_paths=slide_paths_only,
        subject_hint=args.subject,
        assets_output_dir=assets_dir,
    )

    print("\n" + "=" * 60)
    print("🎉 Результаты работы пайплайна:")
    print(f"   • Предмет: {summary['subject']}")
    print(f"   • Тема: {summary['topic']}")
    print(f"   • Отобрано уникальных слайдов: {summary['unique_slides_count']}")
    print(f"   • Извлечено Action Items (ДЗ/дедлайны): {summary['action_items_count']}")
    print(f"   • Ссылка на конспект (GitHub): {summary['github_url']}")
    if repo_dir.exists():
        print(f"   • Локальный репозиторий обновлен: {repo_dir.resolve()}")
        readme = repo_dir / "README.md"
        if readme.exists():
            print(f"   • Сводное оглавление README.md актуализировано!")
    print("=" * 60)


if __name__ == "__main__":
    main()
