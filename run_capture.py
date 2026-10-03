#!/usr/bin/env python3
"""
CLI runner for autonomous meeting capture (MTS Link & Yandex Telemost).
Captures call, extracts unique slides via pHash, records audio,
and feeds everything into the AI & publishing pipeline.
"""

import argparse
import asyncio
import sys
from pathlib import Path

# Ensure UTF-8 output on Windows consoles (cp1251)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from config import settings, TEMP_DIR
from recorder.browser import MeetingRecorder
from recorder.audio import AudioRecorder
from recorder.visual import SlideDeduplicator
from recorder.monitor import MeetingMonitor
from orchestrator import LectureOrchestrator


async def run_capture(
    url: str,
    subject: str,
    student_name: str,
    max_duration_min: int,
    min_duration_min: int,
    mock_meeting: bool = False,
):
    print("=" * 60)
    print("🎥 Студенческий Ассистент — Автономный захват звонка")
    print("=" * 60)
    print(f"📍 Ссылка: {url}")
    print(f"📚 Предмет: {subject}")
    print(f"👤 Имя входа: {student_name}")

    session_dir = TEMP_DIR / "capture_session"
    session_dir.mkdir(parents=True, exist_ok=True)
    slides_dir = session_dir / "slides"
    audio_path = session_dir / "audio.mp3"

    deduplicator = SlideDeduplicator(output_dir=slides_dir)
    audio_recorder = AudioRecorder(output_path=audio_path)

    if mock_meeting:
        print("\n🧪 Режим симуляции звонка (Mock Meeting)...")
        from run_poc import create_demo_assets
        frames = create_demo_assets(session_dir / "raw_frames")
        for p, sec in frames:
            deduplicator.process_frame(p, timestamp_sec=sec)
        audio_recorder.start()
        await asyncio.sleep(1)
        audio_recorder.stop()
        exit_info = {
            "exit_reason": "Mock meeting completed",
            "audio_file": audio_path,
            "unique_slides": [s.file_path for s in deduplicator.saved_slides],
        }
    else:
        from recorder.schedule_manager import ScheduleManager
        recorder = MeetingRecorder(headless=True)
        monitor = MeetingMonitor(
            recorder=recorder,
            deduplicator=deduplicator,
            audio_recorder=audio_recorder,
            schedule_manager=ScheduleManager(),
        )

        print("\n🚀 Подключение браузера к конференции...")
        connected = await recorder.join_meeting(url=url, student_name=student_name)
        if not connected:
            print("❌ Не удалось подключиться к конференции!")
            return

        print("🟢 Подключено! Запущен мониторинг звонка и аудиозапись...")
        exit_info = await monitor.monitor_session(
            audio_dest=audio_path,
            max_duration_min=max_duration_min,
            min_duration_min=min_duration_min,
        )

    print(f"\n⏹️ Сессия завершена: {exit_info['exit_reason']}")
    print(f"📸 Собрано уникальных слайдов: {len(exit_info['unique_slides'])}")

    # Run orchestrator on collected assets
    print("\n⚡ Запуск анализа и публикации результатов...")
    orchestrator = LectureOrchestrator()
    summary = await orchestrator.run_pipeline(
        audio_path=exit_info["audio_file"],
        raw_slide_paths=exit_info["unique_slides"],
        subject_hint=subject,
    )

    print("\n" + "=" * 60)
    print("✅ Лекция успешно обработана и опубликована!")
    print(f"   • GitHub URL: {summary['github_url']}")
    print(f"   • Дедлайнов найдено: {summary['action_items_count']}")
    print("=" * 60)


def main():
    parser = argparse.ArgumentParser(description="Autonomous Lecture Capture Runner")
    parser.add_argument("--url", type=str, default="https://telemost.yandex.ru/j/mock-meeting", help="Meeting link")
    parser.add_argument("--subject", type=str, default="Компьютерные сети", help="Subject name")
    parser.add_argument("--name", type=str, default=settings.student_name, help="Student attendee name")
    parser.add_argument("--max-duration", type=int, default=settings.max_lecture_duration_min, help="Max minutes")
    parser.add_argument("--min-duration", type=int, default=settings.min_lecture_duration_min, help="Min minutes")
    parser.add_argument("--mock-meeting", action="store_true", help="Simulate meeting without opening browser")

    args = parser.parse_args()

    asyncio.run(
        run_capture(
            url=args.url,
            subject=args.subject,
            student_name=args.name,
            max_duration_min=args.max_duration,
            min_duration_min=args.min_duration,
            mock_meeting=args.mock_meeting,
        )
    )


if __name__ == "__main__":
    main()
