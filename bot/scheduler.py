import asyncio
import logging
from typing import Callable, Optional, Set
from aiogram import Bot
from aiogram.enums import ParseMode

from config import settings, get_local_now, get_allowed_user_ids
from recorder.schedule_manager import ScheduleManager, ScheduleEntry

logger = logging.getLogger(__name__)


class ScheduleAutoJoinService:
    """
    Autonomous background worker that polls group timetable and automatically
    enters scheduled online lectures at their designated start time.
    """

    def __init__(
        self,
        bot: Optional[Bot] = None,
        schedule_manager: Optional[ScheduleManager] = None,
        execute_meeting_fn: Optional[Callable] = None,
        is_busy_fn: Optional[Callable[[], bool]] = None,
        check_interval_sec: int = 25,
    ):
        self.bot = bot
        self.schedule_mgr = schedule_manager or ScheduleManager()
        self.execute_meeting_fn = execute_meeting_fn
        self.is_busy_fn = is_busy_fn
        self.check_interval_sec = check_interval_sec
        self.already_joined_keys: Set[str] = set()
        self.last_active_chat_id: Optional[int] = None
        self.last_cleanup_date: Optional[str] = None
        self._is_running = False
        self._task: Optional[asyncio.Task] = None

    def record_active_chat(self, chat_id: int):
        """Remembers last active student chat for notifications."""
        if chat_id:
            self.last_active_chat_id = chat_id

    def start(self, bot: Optional[Bot] = None) -> asyncio.Task:
        """Starts background monitoring loop."""
        if bot:
            self.bot = bot
        if self._is_running and self._task and not self._task.done():
            return self._task
        self._is_running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info("Schedule Auto-Join Service successfully started.")
        return self._task

    def stop(self):
        """Stops background loop."""
        self._is_running = False
        if self._task and not self._task.done():
            self._task.cancel()
        logger.info("Schedule Auto-Join Service stopped.")

    def check_entry_should_join(
        self,
        entry: ScheduleEntry,
        current_minutes: int,
        tolerance_before: int = 1,
        tolerance_after: int = 10,
    ) -> bool:
        """
        Determines if a schedule entry is within the auto-join time window:
        [start_time - tolerance_before, start_time + tolerance_after].
        """
        if not entry.url or not entry.is_active:
            return False

        try:
            clean = entry.time_str.replace("-", ":")
            hh, mm = map(int, clean.split(":"))
            entry_minutes = hh * 60 + mm
            return (entry_minutes - tolerance_before) <= current_minutes <= (entry_minutes + tolerance_after)
        except Exception as e:
            logger.warning(f"Error checking time for entry {entry.id}: {e}")
            return False

    async def _run_loop(self):
        """Main periodic polling loop."""
        while self._is_running:
            try:
                today_str = get_local_now().strftime("%Y-%m-%d")
                if self.last_cleanup_date != today_str:
                    self.last_cleanup_date = today_str
                    try:
                        from recorder.session_manager import session_mgr
                        purged = session_mgr.cleanup_expired_backups()
                        if purged > 0:
                            logger.info(f"Daily scheduler maintenance: purged {purged} expired slides backup folder(s).")
                    except Exception as e:
                        logger.warning(f"Error during daily scheduler backup cleanup: {e}")

                await self.check_and_join_scheduled_lectures()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in Schedule Auto-Join cycle: {e}", exc_info=True)

            try:
                await asyncio.sleep(self.check_interval_sec)
            except asyncio.CancelledError:
                break

    async def check_and_join_scheduled_lectures(self):
        """Checks today's timetable and triggers connection if time matches."""
        now = get_local_now()
        today_date = now.strftime("%Y-%m-%d")
        current_day = now.weekday()
        current_minutes = now.hour * 60 + now.minute

        # Prune old keys from previous days
        self.already_joined_keys = {
            k for k in self.already_joined_keys if k.startswith(today_date)
        }

        # Check today's active schedule entries
        entries = self.schedule_mgr.get_schedule(day_of_week=current_day)
        for entry in entries:
            join_key = f"{today_date}_{entry.id}"
            if join_key in self.already_joined_keys:
                continue

            if not self.check_entry_should_join(entry, current_minutes):
                continue

            subj = self.schedule_mgr.get_subject(entry.subject_id)
            subj_title = subj.title if subj else entry.subject_id
            icon = subj.color_icon if subj else "🏛"

            # Check if recorder is currently busy (e.g. CAPTURE_SEMAPHORE is locked or server is shutting down)
            if self.is_busy_fn and self.is_busy_fn():
                logger.info(
                    f"⏳ Recorder is currently busy with another session or shutting down. "
                    f"Postponing auto-join for '{subj_title}' ({entry.time_str}) until next tick."
                )
                continue

            # Mark as joined for today to prevent duplicate launches
            self.already_joined_keys.add(join_key)

            # Determine recipient chat(s)
            # 1. Primary chat MUST be an authorized admin/user, never the public broadcast channel
            allowed_admins = get_allowed_user_ids()
            primary_chat = allowed_admins[0] if allowed_admins else (self.last_active_chat_id or 0)

            # 2. Target chats for the announcement: strictly private chat with authorized admins.
            # Never send auto-join status spam to public channels or forum groups!
            target_chat_ids = set(allowed_admins)
            if not target_chat_ids and self.last_active_chat_id:
                target_chat_ids.add(self.last_active_chat_id)

            logger.info(
                f"🔔 Auto-joining scheduled lecture: '{subj_title}' ({entry.time_str}) URL={entry.url}"
            )

            announcement = (
                f"🔔 **Автоматический вход по расписанию!**\n\n"
                f"• **Предмет:** {icon} **{subj_title}**\n"
                f"• **Время:** `{entry.time_str}`\n"
                f"• **Ссылка:** {entry.url}\n\n"
                f"🚀 _Бот подключается к встрече и начинает запись слайдов и аудио..._"
            )

            if self.bot:
                for cid in target_chat_ids:
                    try:
                        await self.bot.send_message(
                            chat_id=cid,
                            text=announcement,
                            parse_mode=ParseMode.MARKDOWN,
                        )
                    except Exception as e:
                        logger.warning(f"Failed to send auto-join alert to {cid}: {e}")

            # Trigger the meeting capture task safely
            if self.execute_meeting_fn:
                async def _run_scheduled_capture(j_key=join_key, s_title=subj_title, e_url=entry.url, e_slug=entry.subject_id, e_id=entry.id):
                    try:
                        res = await self.execute_meeting_fn(
                            url=e_url,
                            chat_id=primary_chat,
                            bot=self.bot,
                            subject_title=s_title,
                            subject_slug=e_slug,
                            schedule_entry_id=e_id,
                        )
                        if res is False:
                            logger.warning(
                                f"Meeting capture for '{s_title}' was rejected (server busy or unavailable). "
                                f"Releasing join key '{j_key}' to allow retry on next scheduler tick."
                            )
                            self.already_joined_keys.discard(j_key)
                    except Exception as exc:
                        logger.error(f"Error during scheduled capture of '{s_title}': {exc}")
                        self.already_joined_keys.discard(j_key)

                asyncio.create_task(_run_scheduled_capture())
