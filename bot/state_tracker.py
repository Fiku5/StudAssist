"""
Global state tracker for the StudentAssistant bot.
Tracks the active operational state of the assistant:
- IDLE: Free / waiting for meetings
- RECORDING: Active meeting recording in progress
- POSTPROCESSING: Trimming audio silence, deduplicating slides
- ANALYZING: Generating AI summary via Gemini
- PUBLISHING: Publishing notes to GitHub and Telegram group topics
"""

from __future__ import annotations

import enum
import time
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from recorder.schedule_manager import ScheduleManager


class BotState(str, enum.Enum):
    IDLE = "idle"
    RECORDING = "recording"
    POSTPROCESSING = "postprocessing"
    ANALYZING = "analyzing"
    PUBLISHING = "publishing"


class BotStateTracker:
    def __init__(self):
        self.state: BotState = BotState.IDLE
        self.session_id: Optional[str] = None
        self.subject_title: Optional[str] = None
        self.start_time: Optional[float] = None
        self.details: Optional[str] = None
        self.monitor_ref = None

    def set_idle(self):
        self.state = BotState.IDLE
        self.session_id = None
        self.subject_title = None
        self.start_time = None
        self.details = None
        self.monitor_ref = None

    def set_recording(self, session_id: str, subject_title: str, monitor_ref=None):
        self.state = BotState.RECORDING
        self.session_id = session_id
        self.subject_title = subject_title
        self.start_time = time.time()
        self.details = None
        self.monitor_ref = monitor_ref

    def set_postprocessing(self, session_id: str, subject_title: str, details: Optional[str] = None):
        self.state = BotState.POSTPROCESSING
        self.session_id = session_id
        self.subject_title = subject_title
        self.details = details

    def set_analyzing(self, session_id: str, subject_title: str, model_name: Optional[str] = None):
        self.state = BotState.ANALYZING
        self.session_id = session_id
        self.subject_title = subject_title
        self.details = f"Модель: {model_name}" if model_name else None

    def set_publishing(self, session_id: str, subject_title: str):
        self.state = BotState.PUBLISHING
        self.session_id = session_id
        self.subject_title = subject_title
        self.details = None

    def is_recording(self) -> bool:
        return self.state == BotState.RECORDING

    def get_slide_count(self) -> int:
        if self.monitor_ref and hasattr(self.monitor_ref, "deduplicator"):
            return len(self.monitor_ref.deduplicator.saved_slides)
        return 0

    def get_status_card_markdown(self, schedule_mgr: Optional[ScheduleManager] = None) -> str:
        """Generates dynamic markdown status card for the bot's current activity."""
        if self.state == BotState.RECORDING:
            elapsed_min = int((time.time() - (self.start_time or time.time())) / 60)
            slides = self.get_slide_count()
            subj = self.subject_title or "Онлайн-пара"
            sess = self.session_id or ""
            return (
                "╭─ 🔴 **СТАТУС: ИДЁТ ЗАПИСЬ ПАРЫ**\n"
                f"│ 🏛 **Предмет**: {subj}\n"
                f"│ ⏱ **В эфире**: {elapsed_min} мин.\n"
                f"│ 🖼 **Слайдов**: {slides}\n"
                f"│ 📁 **Сессия**: `{sess}`\n"
                "╰──────────────────────────────────"
            )

        elif self.state == BotState.POSTPROCESSING:
            subj = self.subject_title or "Лекция"
            sess = self.session_id or ""
            detail = f" ({self.details})" if self.details else ""
            return (
                "╭─ ⚙️ **СТАТУС: ПОСТОБРАБОТКА МАТЕРИАЛОВ**\n"
                f"│ 🏛 **Предмет**: {subj}\n"
                f"│ 📁 **Сессия**: `{sess}`\n"
                f"│ 🔄 _Срез краевой тишины и дедупликация слайдов{detail}..._\n"
                "╰──────────────────────────────────"
            )

        elif self.state == BotState.ANALYZING:
            subj = self.subject_title or "Лекция"
            sess = self.session_id or ""
            detail = f"\n│ 🤖 {self.details}" if self.details else ""
            return (
                "╭─ 🧠 **СТАТУС: НЕЙРОАНАЛИЗ КОНСПЕКТА (GEMINI)**\n"
                f"│ 🏛 **Предмет**: {subj}\n"
                f"│ 📁 **Сессия**: `{sess}`"
                f"{detail}\n"
                "│ ⏳ _Сборка слайдов, распознавание и структурирование..._\n"
                "╰──────────────────────────────────"
            )

        elif self.state == BotState.PUBLISHING:
            subj = self.subject_title or "Лекция"
            sess = self.session_id or ""
            return (
                "╭─ 🚀 **СТАТУС: ПУБЛИКАЦИЯ МАТЕРИАЛОВ**\n"
                f"│ 🏛 **Предмет**: {subj}\n"
                f"│ 📁 **Сессия**: `{sess}`\n"
                "│ 📤 _Синхронизация с GitHub и отправка в чат группы..._\n"
                "╰──────────────────────────────────"
            )

        else:  # IDLE
            next_info = ""
            if schedule_mgr:
                try:
                    upcoming = schedule_mgr.get_minutes_until_next_pair()
                    if upcoming:
                        entry, subj_obj, diff = upcoming
                        next_info = f"\n│ 🔜 Следующая: **{subj_obj.title}** в `{entry.time_str}` (через {diff} мин)"
                except Exception:
                    pass
            return (
                "╭─ 🟢 **СТАТУС: СВОБОДЕН (ОЖИДАНИЕ ПАРЫ)**"
                f"{next_info}\n"
                "│ 💡 Готов к записи по ссылке или расписанию\n"
                "╰──────────────────────────────────"
            )


bot_state_tracker = BotStateTracker()
