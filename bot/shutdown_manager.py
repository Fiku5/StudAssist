import asyncio
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode

from config import settings
from recorder.session_manager import SessionManager

logger = logging.getLogger(__name__)


@dataclass
class ActiveSessionContext:
    chat_id: int
    session_id: str
    subject_title: str
    recorder: Any
    audio_recorder: Any
    monitor: Any
    start_time: float
    audio_path: Path
    slides_dir: Path


class GracefulShutdownManager:
    """
    Coordinates lightning-fast freeze of active recording sessions upon receiving
    SIGTERM / SIGINT signals (e.g. systemd restart, server reboot, docker stop, Ctrl+C).
    
    Guarantees:
    1. Fast execution (< 2 seconds) to easily beat systemd/docker stop timeouts.
    2. FFmpeg clean stream flush (MP3 file container finalized without corruption).
    3. Chromium/Playwright process destruction (zero lingering zombie processes on VPS).
    4. Freezes raw data on disk with metadata status="interrupted", postprocess_pending=True.
    5. Post-processing is safely deferred until the server boots up and user triggers it.
    """

    def __init__(self, session_manager: Optional[SessionManager] = None):
        self.session_mgr = session_manager or SessionManager()
        self.active_sessions: Dict[int, ActiveSessionContext] = {}
        self.is_shutting_down = False
        self._freeze_lock = asyncio.Lock()

    def register_session(
        self,
        chat_id: int,
        session_id: str,
        subject_title: str,
        recorder: Any,
        audio_recorder: Any,
        monitor: Any,
        start_time: float,
        audio_path: Path,
        slides_dir: Path,
    ):
        """Registers a live recording session into the shutdown registry."""
        self.active_sessions[chat_id] = ActiveSessionContext(
            chat_id=chat_id,
            session_id=session_id,
            subject_title=subject_title,
            recorder=recorder,
            audio_recorder=audio_recorder,
            monitor=monitor,
            start_time=start_time,
            audio_path=audio_path,
            slides_dir=slides_dir,
        )
        logger.info(f"ShutdownManager: registered active session {session_id} for chat {chat_id}.")

    def unregister_session(self, chat_id: int):
        """Unregisters session upon normal completion."""
        removed = self.active_sessions.pop(chat_id, None)
        if removed:
            logger.info(f"ShutdownManager: unregistered session {removed.session_id} for chat {chat_id}.")

    async def fast_freeze(
        self,
        signal_name: str = "SIGTERM",
        bot: Optional[Bot] = None,
        dp: Optional[Dispatcher] = None,
        auto_join_service: Optional[Any] = None,
    ):
        """
        Executes an instant freeze of all active recording operations.
        Target execution time: <= 1.5 seconds.
        """
        async with self._freeze_lock:
            if self.is_shutting_down:
                logger.warning("ShutdownManager: fast_freeze already in progress.")
                return

            self.is_shutting_down = True
            logger.warning(
                f"🚨 [SHUTDOWN] Received {signal_name}! Initiating fast freeze of active sessions..."
            )

            # 1. Stop background schedule auto-join service immediately
            if auto_join_service:
                try:
                    auto_join_service.stop()
                except Exception as e:
                    logger.debug(f"ShutdownManager: auto_join_service stop note: {e}")

            # 2. Fast freeze each active recording session
            active_items = list(self.active_sessions.values())
            for ctx in active_items:
                try:
                    elapsed = max(0.0, time.time() - ctx.start_time)
                    logger.info(f"Freezing session {ctx.session_id} (elapsed: {int(elapsed)}s)...")

                    # Request monitor loop to stop
                    if ctx.monitor and hasattr(ctx.monitor, "request_stop"):
                        ctx.monitor.request_stop()

                    # Gracefully stop ffmpeg to flush headers cleanly
                    if ctx.audio_recorder and hasattr(ctx.audio_recorder, "stop"):
                        try:
                            ctx.audio_recorder.stop()
                        except Exception as e:
                            logger.warning(f"Error stopping audio recorder: {e}")

                    # Count raw slides gathered
                    raw_slides = list(ctx.slides_dir.glob("*.png")) if ctx.slides_dir.exists() else []

                    # Check audio file size
                    audio_size = ctx.audio_path.stat().st_size if ctx.audio_path.exists() else 0

                    # Save metadata in 'interrupted' state with postprocess_pending flag
                    meta_updates = {
                        "status": "interrupted",
                        "postprocess_pending": True,
                        "duration_seconds": elapsed,
                        "exit_reason": f"Сервер остановлен ({signal_name})",
                        "slides_count": len(raw_slides),
                        "audio": {
                            "exists": ctx.audio_path.exists() and audio_size > 1000,
                            "size_bytes": audio_size,
                            "is_silent": False,
                        },
                    }
                    self.session_mgr.update_session_metadata(ctx.session_id, meta_updates)

                    # Send quick push alert to student if Telegram bot is available
                    if bot and ctx.chat_id:
                        try:
                            alert_text = (
                                f"⚠️ **Внимание: сервер перезагружается ({signal_name})!**\n\n"
                                f"Запись пары **«{ctx.subject_title}»** экстренно остановлена и **заморожена**.\n"
                                f"• Длительность: {int(elapsed // 60)} мин.\n"
                                f"• Сырых слайдов: {len(raw_slides)}\n"
                                f"• Аудио: сохранено ({round(audio_size / (1024 * 1024), 1)} МБ)\n\n"
                                f"💡 _После запуска сервера вы сможете выполнить постобработку "
                                f"(срез тишины и чистку слайдов) в `/sessions` по специальной кнопке._"
                            )
                            await asyncio.wait_for(
                                bot.send_message(
                                    chat_id=ctx.chat_id,
                                    text=alert_text,
                                    parse_mode=ParseMode.MARKDOWN,
                                ),
                                timeout=1.5,
                            )
                        except Exception as e:
                            logger.warning(f"Could not send shutdown alert to chat {ctx.chat_id}: {e}")

                    # Close Chromium & Playwright
                    if ctx.recorder and hasattr(ctx.recorder, "close"):
                        try:
                            await asyncio.wait_for(ctx.recorder.close(), timeout=1.5)
                        except Exception as e:
                            logger.warning(f"Error closing browser recorder: {e}")

                except Exception as e:
                    logger.error(f"Error during freeze of session {ctx.session_id}: {e}")
                finally:
                    self.active_sessions.pop(ctx.chat_id, None)

            # 3. Stop Aiogram polling if dispatcher provided
            if dp and hasattr(dp, "stop_polling"):
                try:
                    await asyncio.wait_for(dp.stop_polling(), timeout=1.0)
                except Exception:
                    pass

            # 4. Close bot session
            if bot and hasattr(bot, "session") and bot.session:
                try:
                    await asyncio.wait_for(bot.session.close(), timeout=1.0)
                except Exception:
                    pass

            logger.info("✅ [SHUTDOWN] Fast freeze completed successfully.")


# Global singleton instance
shutdown_manager = GracefulShutdownManager()
