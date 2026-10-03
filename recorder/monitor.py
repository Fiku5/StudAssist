import asyncio
import io
import logging
import time
from pathlib import Path
from typing import Optional, List
from PIL import Image

from config import settings
from recorder.browser import MeetingRecorder
from recorder.audio import AudioRecorder
from recorder.visual import SlideDeduplicator
from recorder.schedule_manager import ScheduleManager
from recorder.attendance import ParticipantTracker

logger = logging.getLogger(__name__)


class MeetingMonitor:
    """
    Supervises an active lecture call:
    - Takes periodic screenshots and feeds them to SlideDeduplicator.
    - Synchronizes audio recording.
    - Tracks attendee presence intervals and debounced reconnects (ParticipantTracker).
    - Applies Smart Exit 2.0 (4 universal rules: 180m limit / schedule, platform closed, >95.3m + silence + drop, continue).
    """

    def __init__(
        self,
        recorder: MeetingRecorder,
        deduplicator: SlideDeduplicator,
        audio_recorder: Optional[AudioRecorder] = None,
        schedule_manager: Optional[ScheduleManager] = None,
        participant_tracker: Optional[ParticipantTracker] = None,
        check_interval_sec: Optional[int] = None,
        participant_check_interval_sec: float = 30.0,
    ):
        self.recorder = recorder
        self.deduplicator = deduplicator
        self.audio_recorder = audio_recorder or AudioRecorder()
        self.schedule_manager = schedule_manager
        self.tracker = participant_tracker or ParticipantTracker(self_name=settings.student_name)
        self.check_interval_sec = check_interval_sec or settings.slide_capture_interval_sec
        self.participant_check_interval_sec = participant_check_interval_sec
        self._stop_requested = False
        self.consecutive_silence_checks = 0
        self.consecutive_ended_checks = 0
        self.last_silence_check_time = 0.0
        self.last_participant_check_time = 0.0
        self.audio_check_interval_sec = 30.0
        self.last_audio_check_time = 0.0
        self.audio_crashed = False

    def request_stop(self):
        """Allows manual early termination."""
        self._stop_requested = True

    async def monitor_session(
        self,
        audio_dest: Optional[Path] = None,
        max_duration_min: Optional[int] = None,
        min_duration_min: Optional[float] = None,
        drop_threshold: Optional[int] = None,
        silence_checks_threshold: Optional[int] = None,
        next_pair_buffer_min: Optional[int] = None,
        exclude_schedule_entry_id: Optional[str] = None,
        min_elapsed_before_next_pair_check: float = 15.0,
        ended_checks_threshold: int = 2,
    ) -> dict:
        """
        Runs the monitoring loop until Smart Exit 2.0 condition is triggered.
        Returns paths to the recorded audio and unique slide images.
        """
        max_min = max_duration_min if max_duration_min is not None else settings.max_lecture_duration_min
        min_min = min_duration_min if min_duration_min is not None else settings.smart_exit_min_duration_min
        thresh = drop_threshold if drop_threshold is not None else settings.participant_drop_threshold
        silence_thresh = (
            silence_checks_threshold
            if silence_checks_threshold is not None
            else settings.smart_exit_consecutive_silence_threshold
        )
        next_pair_buf = (
            next_pair_buffer_min
            if next_pair_buffer_min is not None
            else settings.smart_exit_next_pair_buffer_min
        )

        logger.info(
            f"Starting Smart Exit 2.0 session (max: {max_min}m, min: {min_min}m, "
            f"drop_thresh: {thresh}, silence_thresh: {silence_thresh}, next_pair_buf: {next_pair_buf}m)"
        )

        # 1. Start audio recording
        audio_file = self.audio_recorder.start(dest_path=audio_dest)
        start_time = time.time()
        self.tracker.start(start_time)
        self.consecutive_silence_checks = 0
        self.consecutive_ended_checks = 0
        self.last_silence_check_time = 0.0
        self.last_participant_check_time = 0.0
        self.last_audio_check_time = 0.0
        self.audio_crashed = False

        exit_reason = "normal"

        try:
            while not self._stop_requested:
                elapsed_sec = time.time() - start_time
                elapsed_min = elapsed_sec / 60.0

                # Heartbeat logging every ~60 seconds
                if int(elapsed_sec) % 60 < self.check_interval_sec:
                    audio_tag = "⚠️ CRASHED" if self.audio_crashed else "active"
                    logger.info(
                        f"Monitor heartbeat: elapsed {int(elapsed_min)}m, "
                        f"unique slides captured: {len(self.deduplicator.saved_slides)}, "
                        f"audio: {audio_tag}"
                    )

                # Periodic audio recording health-check (~30 seconds)
                if (elapsed_sec - self.last_audio_check_time) >= self.audio_check_interval_sec or self.last_audio_check_time == 0.0:
                    self.last_audio_check_time = elapsed_sec
                    if self.audio_recorder:
                        alive_val = getattr(self.audio_recorder, "is_alive", None)
                        is_alive = alive_val() if callable(alive_val) else (alive_val if alive_val is not None else getattr(self.audio_recorder, "is_recording", True))
                        if not is_alive:
                            if not self.audio_crashed:
                                self.audio_crashed = True
                                proc = getattr(self.audio_recorder, "process", None)
                                retcode = proc.poll() if proc and hasattr(proc, "poll") else getattr(proc, "returncode", None)
                                logger.error(
                                    f"⚠️ [AUDIO HEALTH-CHECK] Audio recording process has died unexpectedly! "
                                    f"(exit code: {retcode}, elapsed: {int(elapsed_min)}m)"
                                )

                # Track attendee presence via DOM polling
                if (elapsed_sec - self.last_participant_check_time) >= self.participant_check_interval_sec or self.last_participant_check_time == 0.0:
                    self.last_participant_check_time = elapsed_sec
                    try:
                        active_names = await self.recorder.get_active_participants()
                        self.tracker.update_presence(active_names, timestamp=time.time())
                    except Exception as e:
                        logger.debug(f"Attendance tracking update error: {e}")

                # =====================================================================
                # ПРАВИЛО 1: Лимит длительности (180 мин) или следующая пара по расписанию
                # =====================================================================
                if elapsed_min >= max_min:
                    exit_reason = f"Лимит длительности пары достигнут ({max_min} мин)"
                    logger.info(exit_reason)
                    break

                # Next pair check: only trigger if the current lecture has been running for at least min_elapsed_before_next_pair_check minutes,
                # preventing an instant exit if the bot joined 1-4 minutes before the pair's official start time.
                if elapsed_min >= min_elapsed_before_next_pair_check and self.schedule_manager and self.schedule_manager.is_next_pair_imminent(
                    buffer_minutes=next_pair_buf,
                    exclude_entry_id=exclude_schedule_entry_id,
                ):
                    exit_reason = f"Скоро начинается следующая пара по расписанию (<={next_pair_buf} мин)"
                    logger.info(exit_reason)
                    break

                # =====================================================================
                # Захват скриншота и дедупликация
                # =====================================================================
                frame_bytes = await self.recorder.capture_screenshot()
                if frame_bytes:
                    try:
                        img = Image.open(io.BytesIO(frame_bytes))
                        self.deduplicator.process_frame(img, timestamp_sec=elapsed_sec)
                    except Exception as e:
                        logger.warning(f"Failed to process screenshot frame: {e}")

                # =====================================================================
                # ПРАВИЛО 2: Официальное закрытие встречи платформой или кик
                # =====================================================================
                if await self.recorder.is_meeting_ended():
                    self.consecutive_ended_checks += 1
                    logger.info(
                        f"Meeting ended marker detected ({self.consecutive_ended_checks}/{ended_checks_threshold})."
                    )
                    if self.consecutive_ended_checks >= ended_checks_threshold:
                        exit_reason = "Host closed meeting or bot was disconnected/removed"
                        logger.info(exit_reason)
                        break
                else:
                    if self.consecutive_ended_checks > 0:
                        logger.debug("Meeting ended check reset back to 0 (meeting still alive).")
                    self.consecutive_ended_checks = 0

                # =====================================================================
                # ПРАВИЛО 3: Тройное условие завершения (> 95.3 мин И <= 3 чел И тишина 3 мин)
                # =====================================================================
                if elapsed_min >= min_min:
                    # Пульс-проверка тишины раз в ~60 секунд (проверяем последние 10 сек звука)
                    if (elapsed_sec - self.last_silence_check_time) >= 60.0 or self.last_silence_check_time == 0.0:
                        self.last_silence_check_time = elapsed_sec
                        check_res = self.audio_recorder.check_recent_audio_level(
                            duration_sec=settings.smart_exit_silence_sample_sec
                        )
                        if check_res.get("is_silent", False):
                            self.consecutive_silence_checks += 1
                            logger.info(
                                f"Smart Exit 2.0: тихий замер звука "
                                f"({self.consecutive_silence_checks}/{silence_thresh}, max_vol: {check_res.get('max_volume')} dB)"
                            )
                        else:
                            if self.consecutive_silence_checks > 0:
                                logger.info(
                                    f"Smart Exit 2.0: обнаружен голос/звук "
                                    f"(сброс тишины с {self.consecutive_silence_checks} до 0, max_vol: {check_res.get('max_volume')} dB)"
                                )
                            self.consecutive_silence_checks = 0

                    count = await self.recorder.get_participant_count()
                    if count is not None and count <= thresh and self.consecutive_silence_checks >= silence_thresh:
                        exit_reason = (
                            f"Smart Exit 2.0: пара завершена (время {elapsed_min:.1f}м >= {min_min:.1f}м, "
                            f"участников {count} <= {thresh}, тишина {self.consecutive_silence_checks} мин)"
                        )
                        logger.info(exit_reason)
                        break

                # =====================================================================
                # ПРАВИЛО 4: Во всех остальных случаях — ПРОДОЛЖАТЬ ЗАПИСЬ
                # =====================================================================
                await asyncio.sleep(self.check_interval_sec)

            if self._stop_requested:
                exit_reason = "Запись остановлена пользователем через Telegram (/stop)"
                logger.info(exit_reason)

        finally:
            # Clean up audio and browser
            self.audio_recorder.stop()
            await self.recorder.close()

        total_sec = time.time() - start_time
        logger.info(f"Meeting session ended. Exit reason: {exit_reason}. Raw duration: {int(total_sec)}s")

        # 1 & 2. Run heavy post-processing (ffmpeg silencedetect and PIL dHash deduplication)
        # in a worker thread via asyncio.to_thread to keep the event loop completely responsive!
        def _execute_postprocess():
            s_cut, e_cut = None, None
            try:
                res = self.audio_recorder.trim_silence_boundaries(audio_file)
                if isinstance(res, tuple) and len(res) == 2:
                    s_cut, e_cut = res
                elif isinstance(res, (int, float)):
                    e_cut = float(res)
            except Exception:
                pass

            if s_cut is None and e_cut is None:
                try:
                    trail_res = self.audio_recorder.trim_trailing_silence(audio_file)
                    if isinstance(trail_res, (int, float)):
                        e_cut = float(trail_res)
                except Exception:
                    pass

            self.deduplicator.postprocess(
                start_cutoff_sec=s_cut,
                cutoff_sec=e_cut,
            )

            lvl = self.audio_recorder.check_audio_level(audio_file)
            return s_cut, e_cut, lvl

        start_cutoff, end_cutoff, audio_level = await asyncio.to_thread(_execute_postprocess)

        if start_cutoff is not None or end_cutoff is not None:
            eff_start = start_cutoff or 0.0
            eff_end = end_cutoff or total_sec
            new_duration = max(0.0, eff_end - eff_start)
            logger.info(
                f"Audio silence boundaries trimmed: duration updated from {total_sec:.1f}s to {new_duration:.1f}s "
                f"(leading cut: {eff_start:.1f}s, trailing cut: {eff_end:.1f}s)"
            )
            total_sec = new_duration

        attendance_data = self.tracker.finalize(end_time=time.time())

        return {
            "exit_reason": exit_reason,
            "duration_seconds": total_sec,
            "audio_file": audio_file,
            "is_audio_silent": audio_level.get("is_silent", False),
            "audio_level": audio_level,
            "audio_crashed": self.audio_crashed,
            "unique_slides": [s.file_path for s in self.deduplicator.saved_slides],
            "total_slides_count": len(self.deduplicator.saved_slides),
            "attendance": attendance_data,
        }
