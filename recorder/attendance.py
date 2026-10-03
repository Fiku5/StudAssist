"""
Module for tracking attendee presence during online meetings with interval debounce.
Handles brief network dropouts, late arrivals, early departures, and repeated reconnects.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Dict, Optional, Any
import logging
import time

logger = logging.getLogger(__name__)


@dataclass
class AttendanceInterval:
    """Represents a continuous period of active presence in the meeting."""
    start_time: float
    end_time: float
    duration_seconds: float = 0.0
    start_str: str = ""
    end_str: str = ""

    def to_dict(self) -> dict:
        return {
            "start": self.start_str,
            "end": self.end_str,
            "duration_minutes": round(self.duration_seconds / 60.0, 1),
        }


@dataclass
class StudentAttendance:
    """Tracks attendance statistics and intervals for a single student."""
    name: str
    intervals: List[AttendanceInterval] = field(default_factory=list)
    current_interval_start: Optional[float] = None
    last_seen_time: Optional[float] = None
    join_count: int = 0

    @property
    def total_active_seconds(self) -> float:
        return sum(iv.duration_seconds for iv in self.intervals)


class ParticipantTracker:
    """
    Interval-based participant attendance tracker.
    
    Uses a configurable debounce window (default: 90 seconds) to bridge transient
    disconnections (e.g. browser reloads, momentary Wi-Fi drops) without fragmenting intervals.
    If a participant disappears for longer than debounce_sec, the previous interval is closed
    at their last seen time, and a new interval starts when they reconnect.
    """

    def __init__(self, debounce_sec: float = 90.0, self_name: Optional[str] = None):
        self.debounce_sec = float(debounce_sec)
        self.self_name = self_name.strip().lower() if self_name else None
        self.start_time: Optional[float] = None
        self.end_time: Optional[float] = None
        self.students: Dict[str, StudentAttendance] = {}

    def start(self, start_time: Optional[float] = None):
        """Initializes the meeting start timestamp."""
        self.start_time = start_time if start_time is not None else time.time()
        logger.info(f"ParticipantTracker started at timestamp {self.start_time}")

    def update_presence(self, current_names: List[str], timestamp: Optional[float] = None):
        """
        Updates the presence records of students based on a current snapshot from the DOM.
        
        Args:
            current_names: List of names currently visible in the meeting room.
            timestamp: The timestamp of the snapshot (defaults to time.time()).
        """
        now = timestamp if timestamp is not None else time.time()
        if self.start_time is None:
            self.start_time = now

        # Clean, normalize and filter out self / empty names
        active_set = set()
        for raw_name in current_names:
            if not raw_name:
                continue
            cleaned = raw_name.strip()
            if not cleaned:
                continue
            if self.self_name and cleaned.lower() == self.self_name:
                continue
            if cleaned.lower() in ("я", "(вы)", "вы", "me"):
                continue
            active_set.add(cleaned)

        # 1. Update present students
        for name in active_set:
            if name not in self.students:
                student = StudentAttendance(
                    name=name,
                    current_interval_start=now,
                    last_seen_time=now,
                    join_count=1,
                )
                self.students[name] = student
            else:
                student = self.students[name]
                if student.current_interval_start is None:
                    # Student returned after being absent
                    gap = now - (student.last_seen_time if student.last_seen_time is not None else now)
                    if gap <= self.debounce_sec and student.intervals:
                        # Re-open / extend the last interval
                        last_iv = student.intervals.pop()
                        student.current_interval_start = last_iv.start_time
                    else:
                        student.current_interval_start = now
                        student.join_count += 1
                student.last_seen_time = now

        # 2. Check absent students for debounce expiry
        for name, student in self.students.items():
            if name not in active_set and student.current_interval_start is not None:
                gap = now - (student.last_seen_time if student.last_seen_time is not None else now)
                if gap > self.debounce_sec:
                    # Debounce window exceeded: close interval at last_seen_time
                    end_t = student.last_seen_time if student.last_seen_time is not None else now
                    dur = max(0.0, end_t - student.current_interval_start)
                    student.intervals.append(AttendanceInterval(
                        start_time=student.current_interval_start,
                        end_time=end_t,
                        duration_seconds=dur,
                        start_str=self._format_ts(student.current_interval_start),
                        end_str=self._format_ts(end_t),
                    ))
                    student.current_interval_start = None

    def finalize(self, end_time: Optional[float] = None) -> dict:
        """
        Finalizes the tracking session, closes any open intervals, and computes attendance stats.
        
        Returns:
            Dict containing detailed attendance metadata suitable for metadata.json.
        """
        now = end_time if end_time is not None else time.time()
        self.end_time = now
        meeting_start = self.start_time if self.start_time is not None else now
        total_meeting_sec = max(1.0, now - meeting_start)
        meeting_duration_min = round(total_meeting_sec / 60.0, 1)

        # Close any open intervals
        for student in self.students.values():
            if student.current_interval_start is not None:
                close_t = min(student.last_seen_time if student.last_seen_time is not None else now, now)
                dur = max(0.0, close_t - student.current_interval_start)
                student.intervals.append(AttendanceInterval(
                    start_time=student.current_interval_start,
                    end_time=close_t,
                    duration_seconds=dur,
                    start_str=self._format_ts(student.current_interval_start),
                    end_str=self._format_ts(close_t),
                ))
                student.current_interval_start = None

        # Build participant summaries
        records = []
        for name in sorted(self.students.keys()):
            student = self.students[name]
            total_active_sec = student.total_active_seconds
            total_active_min = round(total_active_sec / 60.0, 1)
            pct = round(min(100.0, (total_active_sec / total_meeting_sec) * 100.0), 1)

            first_seen = student.intervals[0].start_str if student.intervals else ""
            last_seen = student.intervals[-1].end_str if student.intervals else ""

            records.append({
                "name": name,
                "first_seen": first_seen,
                "last_seen": last_seen,
                "total_minutes": total_active_min,
                "attendance_percentage": pct,
                "intervals_count": len(student.intervals),
                "intervals": [iv.to_dict() for iv in student.intervals],
            })

        logger.info(
            f"Finalized attendance: {len(records)} participants tracked over {meeting_duration_min} minutes."
        )

        return {
            "meeting_duration_minutes": meeting_duration_min,
            "total_unique_participants": len(records),
            "debounce_seconds": self.debounce_sec,
            "participants": records,
        }

    def _format_ts(self, ts: float) -> str:
        """Formats a timestamp into human-readable local time or offset."""
        if ts > 1e8:
            return datetime.fromtimestamp(ts).strftime("%H:%M:%S")
        mins = int(ts // 60)
        secs = int(ts % 60)
        return f"{mins:02d}:{secs:02d}"
