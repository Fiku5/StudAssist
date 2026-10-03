import json
import logging
import re
import uuid
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union
from pydantic import BaseModel, Field

from config import BASE_DIR, get_local_now, save_json_atomic

logger = logging.getLogger(__name__)

DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
DEFAULT_SCHEDULE_FILE = DATA_DIR / "schedule_data.json"

DAY_NAMES = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
DAY_SHORT = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
DEFAULT_ICONS = ["🏛", "🗄", "🌐", "⚡️", "💻", "🔬", "📐", "📘", "🧠", "🔧"]


def slugify_subject(text: str) -> str:
    """Creates a clean filesystem slug for subject identification."""
    translit_map = {
        "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "yo",
        "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
        "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
        "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch",
        "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
    }
    s = text.lower().strip()
    out = []
    for ch in s:
        if ch in translit_map:
            out.append(translit_map[ch])
        elif ch.isalnum():
            out.append(ch)
        elif ch in (" ", "_", "-"):
            out.append("_")
    slug = re.sub(r"_+", "_", "".join(out)).strip("_")
    return slug or "subject"


def parse_telegram_topic_url(text: str) -> tuple[Optional[int], Optional[int]]:
    """
    Parses Telegram topic URL or thread ID.
    Supports:
      - https://t.me/c/2443546271/3538 -> (chat_id: -1002443546271, thread_id: 3538)
      - https://t.me/c/123456789/100 -> (chat_id: -100123456789, thread_id: 100)
      - https://t.me/some_group/3538 -> (chat_id: None, thread_id: 3538)
      - Raw integer: "3538" / "#3538" -> (chat_id: None, thread_id: 3538)
    Returns:
      (chat_id, thread_id)
    """
    if not text:
        return None, None
    s = str(text).strip()

    # 1. Match https://t.me/c/<internal_id>/<thread_id>
    c_match = re.search(r"t\.me/c/(\d+)/(\d+)", s)
    if c_match:
        internal_id = c_match.group(1)
        thread_id = int(c_match.group(2))
        chat_id = -int(f"100{internal_id}")
        return chat_id, thread_id

    # 2. Match general t.me/<username>/<thread_id>
    u_match = re.search(r"t\.me/[^/]+/(\d+)", s)
    if u_match:
        return None, int(u_match.group(1))

    # 3. Match pure integer or #1234
    digits_only = re.sub(r"^[#№\s]+", "", s).strip()
    if digits_only.isdigit():
        return None, int(digits_only)

    return None, None


class Subject(BaseModel):
    """Academic discipline metadata."""
    id: str  # slug ID, e.g. "arhitektura_po"
    title: str  # e.g. "Архитектура программных систем"
    short_name: str  # e.g. "Архитектура ПО"
    color_icon: str = "📘"
    telegram_thread_id: Optional[int] = None  # Supergroup forum topic ID (message_thread_id)
    created_at: str = Field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S"))


class ScheduleEntry(BaseModel):
    """Scheduled lecture slot for autonomous/semi-autonomous capture."""
    id: str = Field(default_factory=lambda: f"sched_{uuid.uuid4().hex[:8]}")
    subject_id: str  # FK to Subject.id
    day_of_week: int  # 0=Mon, 1=Tue, ..., 6=Sun
    time_str: str  # "HH:MM", e.g. "09:00"
    duration_min: int = 95
    url: str = ""  # Default meeting link
    is_active: bool = True


class ScheduleManager:
    """
    Manages persistent subject catalog and timetable slots.
    Stored in data/schedule_data.json.
    """

    def __init__(self, data_file: Optional[Path] = None):
        self.data_file = data_file or DEFAULT_SCHEDULE_FILE
        self.data_file.parent.mkdir(parents=True, exist_ok=True)
        self.subjects: Dict[str, Subject] = {}
        self.schedule: List[ScheduleEntry] = []
        self._load()

    def _load(self):
        if not self.data_file.exists():
            # Seed with default initial subjects
            self._seed_defaults()
            self._save()
            return

        try:
            with open(self.data_file, "r", encoding="utf-8") as f:
                data = json.load(f)

            self.subjects = {
                k: Subject(**v) for k, v in data.get("subjects", {}).items()
            }
            self.schedule = [
                ScheduleEntry(**item) for item in data.get("schedule", [])
            ]
        except Exception as e:
            logger.error(f"Failed to load schedule from {self.data_file}: {e}")
            self.subjects = {}
            self.schedule = []

    def _save(self):
        data = {
            "subjects": {k: v.model_dump() for k, v in self.subjects.items()},
            "schedule": [s.model_dump() for s in self.schedule],
        }
        save_json_atomic(self.data_file, data)

    def _seed_defaults(self):
        """Initial subjects seed if no data exists."""
        defaults = [
            ("Архитектура программных систем", "Архитектура ПО", "🏛"),
            ("Базы данных и СУБД", "Базы данных", "🗄"),
            ("Компьютерные сети", "Сети", "🌐"),
            ("Операционные системы", "ОС", "⚡️"),
        ]
        for title, short, icon in defaults:
            sid = slugify_subject(short)
            self.subjects[sid] = Subject(
                id=sid,
                title=title,
                short_name=short,
                color_icon=icon,
            )

    # -------------------------------------------------------------------------
    # Subjects CRUD
    # -------------------------------------------------------------------------

    def get_subjects(self) -> List[Subject]:
        return list(self.subjects.values())

    def get_subject(self, subject_id: str) -> Optional[Subject]:
        return self.subjects.get(subject_id)

    def add_subject(
        self,
        title: str,
        short_name: Optional[str] = None,
        color_icon: Optional[str] = None,
    ) -> Subject:
        short = short_name or title
        sid = slugify_subject(short)

        # Pick an unused icon if not specified
        if not color_icon:
            used_icons = {s.color_icon for s in self.subjects.values()}
            available = [ic for ic in DEFAULT_ICONS if ic not in used_icons]
            icon = available[0] if available else "📘"
        else:
            icon = color_icon

        subject = Subject(
            id=sid,
            title=title,
            short_name=short,
            color_icon=icon,
        )
        self.subjects[sid] = subject
        self._save()
        logger.info(f"Added subject: {title} ({sid})")
        return subject

    def delete_subject(self, subject_id: str) -> bool:
        if subject_id in self.subjects:
            del self.subjects[subject_id]
            # Cascade delete schedule entries for this subject
            self.schedule = [s for s in self.schedule if s.subject_id != subject_id]
            self._save()
            logger.info(f"Deleted subject {subject_id} and its schedule entries.")
            return True
        return False

    def update_subject_thread_id(self, subject_id: str, thread_id: Optional[int]) -> bool:
        """Updates the bound Telegram forum topic (thread_id) for a subject."""
        if subject_id in self.subjects:
            self.subjects[subject_id].telegram_thread_id = thread_id
            self._save()
            logger.info(f"Updated subject {subject_id} telegram_thread_id to {thread_id}")
            return True
        return False

    # -------------------------------------------------------------------------
    # Schedule CRUD
    # -------------------------------------------------------------------------

    def get_schedule(self, day_of_week: Optional[int] = None) -> List[ScheduleEntry]:
        if day_of_week is not None:
            entries = [s for s in self.schedule if s.day_of_week == day_of_week and s.is_active]
        else:
            entries = [s for s in self.schedule if s.is_active]
        # Sort by day, then by time
        return sorted(entries, key=lambda s: (s.day_of_week, s.time_str))

    def get_schedule_entry(self, entry_id: str) -> Optional[ScheduleEntry]:
        for s in self.schedule:
            if s.id == entry_id:
                return s
        return None

    def add_schedule_entry(
        self,
        subject_id: str,
        day_of_week: int,
        time_str: str,
        url: str,
        duration_min: int = 95,
    ) -> ScheduleEntry:
        # Standardize time to HH:MM (handles HH:MM, HH-MM, H:MM)
        clean = time_str.strip().replace("-", ":")
        parts = clean.split(":")
        hh = int(parts[0])
        mm = int(parts[1]) if len(parts) > 1 else 0
        formatted_time = f"{hh:02d}:{mm:02d}"

        entry = ScheduleEntry(
            subject_id=subject_id,
            day_of_week=day_of_week,
            time_str=formatted_time,
            duration_min=duration_min,
            url=url.strip(),
            is_active=True,
        )
        self.schedule.append(entry)
        self._save()
        logger.info(f"Added schedule entry for {subject_id} on day {day_of_week} at {formatted_time}")
        return entry

    def delete_schedule_entry(self, entry_id: str) -> bool:
        initial_len = len(self.schedule)
        self.schedule = [s for s in self.schedule if s.id != entry_id]
        if len(self.schedule) < initial_len:
            self._save()
            logger.info(f"Deleted schedule entry {entry_id}")
            return True
        return False

    def update_schedule_entry_url(self, entry_id: str, url: str) -> Optional[ScheduleEntry]:
        """Updates or clears the meeting URL for a scheduled lecture."""
        for s in self.schedule:
            if s.id == entry_id:
                s.url = url.strip()
                self._save()
                logger.info(f"Updated meeting URL for entry {entry_id} -> '{s.url}'")
                return s
        return None

    # -------------------------------------------------------------------------
    # Timetable Matching
    # -------------------------------------------------------------------------

    def get_current_or_upcoming_pair(
        self,
        now_dt: Optional[datetime] = None,
        tolerance_minutes: int = 20,
    ) -> Optional[Tuple[ScheduleEntry, Subject]]:
        """
        Detects if there is a lecture currently active or scheduled to start
        within `tolerance_minutes`.
        Returns (ScheduleEntry, Subject) or None.
        """
        dt = now_dt or get_local_now()
        current_day = dt.weekday()
        current_minutes = dt.hour * 60 + dt.minute

        for entry in self.get_schedule(day_of_week=current_day):
            try:
                hh, mm = map(int, entry.time_str.split(":"))
                start_minutes = hh * 60 + mm
                end_minutes = start_minutes + entry.duration_min

                # Match if we are within [start - tolerance, end]
                if (start_minutes - tolerance_minutes) <= current_minutes <= end_minutes:
                    subj = self.get_subject(entry.subject_id)
                    if subj:
                        return (entry, subj)
            except Exception as e:
                logger.warning(f"Failed to check schedule entry {entry.id}: {e}")

        return None

    def get_minutes_until_next_pair(
        self,
        now_dt: Optional[datetime] = None,
        exclude_entry_id: Optional[str] = None,
    ) -> Optional[Tuple[ScheduleEntry, Subject, int]]:
        """
        Finds the closest upcoming pair scheduled for today that has not started yet (start > current_time).
        Returns (ScheduleEntry, Subject, minutes_until_start) or None if no upcoming pair today.
        Optionally excludes a specific schedule entry (e.g. the currently active pair).
        """
        dt = now_dt or get_local_now()
        current_day = dt.weekday()
        current_minutes = dt.hour * 60 + dt.minute

        upcoming: List[Tuple[ScheduleEntry, Subject, int]] = []
        for entry in self.get_schedule(day_of_week=current_day):
            if exclude_entry_id and entry.id == exclude_entry_id:
                continue
            try:
                hh, mm = map(int, entry.time_str.split(":"))
                start_minutes = hh * 60 + mm
                if start_minutes > current_minutes:
                    diff = start_minutes - current_minutes
                    subj = self.get_subject(entry.subject_id)
                    if subj:
                        upcoming.append((entry, subj, diff))
            except Exception as e:
                logger.warning(f"Failed to calculate minutes until pair {entry.id}: {e}")

        if not upcoming:
            return None
        upcoming.sort(key=lambda x: x[2])
        return upcoming[0]

    def is_next_pair_imminent(
        self,
        now_dt: Optional[datetime] = None,
        buffer_minutes: Optional[int] = None,
        exclude_entry_id: Optional[str] = None,
    ) -> bool:
        """
        Returns True if the next scheduled pair starts within `buffer_minutes` (default 3-5 min).
        Useful for stopping current lecture capture before next one begins.
        """
        from config import settings
        buf = buffer_minutes if buffer_minutes is not None else settings.smart_exit_next_pair_buffer_min
        result = self.get_minutes_until_next_pair(now_dt=now_dt, exclude_entry_id=exclude_entry_id)
        if result is None:
            return False
        _, _, minutes_left = result
        return 0 < minutes_left <= buf

