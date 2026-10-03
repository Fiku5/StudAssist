import asyncio
import logging
import sys
from pathlib import Path

# Add project root directory to sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import signal
from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from config import settings
from bot.handlers import router, auto_join_service
from bot.middlewares import AuthMiddleware
from bot.shutdown_manager import shutdown_manager
from recorder.session_manager import session_mgr

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - [%(levelname)s] - %(name)s - %(message)s",
)
logger = logging.getLogger(__name__)


async def start_bot():
    """Initializes and starts the Telegram Bot via Long Polling."""
    if not settings.telegram_bot_token:
        logger.error(
            "TELEGRAM_BOT_TOKEN is not set in environment or .env file! "
            "Please add your token from @BotFather to run the Telegram bot."
        )
        sys.exit(1)

    from recorder.audio import AudioRecorder
    AudioRecorder.ensure_pulseaudio()

    if settings.telegram_api_server:
        import os
        from aiogram.client.telegram import TelegramAPIServer, SimpleFilesPathWrapper, BareFilesPathWrapper
        from aiogram.client.session.aiohttp import AiohttpSession
        logger.info(f"Using custom/local Telegram Bot API server: {settings.telegram_api_server}")

        # Check local paths for telegram-bot-api storage (e.g. host mapping from Docker)
        possible_local_roots = []
        if settings.telegram_api_local_root:
            possible_local_roots.append(Path(settings.telegram_api_local_root))
        possible_local_roots.extend([
            Path("/root/Byakugan/storage/bot-api-data"),
            Path("/var/lib/telegram-bot-api"),
        ])

        resolved_local_path = None
        for root_cand in possible_local_roots:
            if root_cand.exists() and root_cand.is_dir():
                resolved_local_path = root_cand
                break

        if resolved_local_path and (os.getenv("TELEGRAM_API_SERVER_IS_LOCAL", "true").lower() == "true"):
            logger.info(f"Detected local Telegram Bot API storage on host: {resolved_local_path}")
            container_srv_path = Path("/var/lib/telegram-bot-api")
            if resolved_local_path.resolve() != container_srv_path.resolve():
                wrapper = SimpleFilesPathWrapper(server_path=container_srv_path, local_path=resolved_local_path)
            else:
                wrapper = BareFilesPathWrapper()
            api_server = TelegramAPIServer.from_base(
                settings.telegram_api_server,
                is_local=True,
                wrap_local_file=wrapper,
            )
        else:
            api_server = TelegramAPIServer.from_base(settings.telegram_api_server, is_local=False)

        session = AiohttpSession(api=api_server)
        bot = Bot(token=settings.telegram_bot_token, session=session)
    else:
        bot = Bot(token=settings.telegram_bot_token)
    dp = Dispatcher()
    dp.message.outer_middleware(AuthMiddleware())
    dp.callback_query.outer_middleware(AuthMiddleware())
    dp.include_router(router)

    # 1. Recover stale sessions if previous shutdown was sudden (kill -9 or power loss)
    recovered = session_mgr.recover_stale_sessions()
    if recovered:
        logger.warning(
            "Recovered %d stale session(s) pending postprocessing: %s",
            len(recovered),
            [s.get("session_id") for s in recovered],
        )
        from config import get_allowed_user_ids
        admin_ids = get_allowed_user_ids()
        admin_id = admin_ids[0] if admin_ids else None
        if admin_id:
            try:
                rec_lines = "\n".join([f"• `{s.get('session_id')}` ({s.get('subject', 'Лекция')})" for s in recovered[:5]])
                await bot.send_message(
                    chat_id=admin_id,
                    text=(
                        f"🔄 **Сервер запущен.**\n\n"
                        f"Обнаружено незавершенных сессий: **{len(recovered)}**\n"
                        f"{rec_lines}\n\n"
                        f"💡 _Они заморожены и ожидают постобработки. Откройте `/sessions`, "
                        f"чтобы запустить срез тишины по кнопке._"
                    ),
                    parse_mode=ParseMode.MARKDOWN,
                )
            except Exception as e:
                logger.warning(f"Could not send recovery alert to admin: {e}")

    # 2. Cleanup expired slide backups (> 7 days old)
    try:
        purged = session_mgr.cleanup_expired_backups()
        if purged > 0:
            logger.info("Startup cleanup: purged %d expired slides backup folder(s).", purged)
    except Exception as e:
        logger.warning(f"Error during startup slides backup cleanup: {e}")

    # 3. Register signals for graceful fast-freeze (<1.5s shutdown)
    loop = asyncio.get_running_loop()

    def _trigger_shutdown(sig_name: str):
        logger.warning(f"Signal {sig_name} caught! Initiating graceful fast freeze...")
        asyncio.create_task(
            shutdown_manager.fast_freeze(
                signal_name=sig_name,
                bot=bot,
                dp=dp,
                auto_join_service=auto_join_service,
            )
        )

    for sig in (getattr(signal, "SIGTERM", None), getattr(signal, "SIGINT", None)):
        if sig is not None:
            try:
                loop.add_signal_handler(sig, lambda s=sig: _trigger_shutdown(s.name))
            except (NotImplementedError, RuntimeError):
                # Fallback for Windows event loops
                try:
                    signal.signal(
                        sig,
                        lambda signum, frame: asyncio.run_coroutine_threadsafe(
                            shutdown_manager.fast_freeze(
                                signal_name=signal.Signals(signum).name,
                                bot=bot,
                                dp=dp,
                                auto_join_service=auto_join_service,
                            ),
                            loop,
                        ),
                    )
                except Exception as e:
                    logger.debug(f"Could not register signal {sig}: {e}")

    # 3. Start autonomous schedule auto-join background worker
    auto_join_service.start(bot)

    logger.info("Starting Telegram Bot long polling...")
    try:
        # Delete webhook if previously set to ensure polling works
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
    finally:
        try:
            auto_join_service.stop()
        except Exception:
            pass
        try:
            if hasattr(bot, "session") and bot.session:
                await bot.session.close()
        except Exception:
            pass


if __name__ == "__main__":
    try:
        asyncio.run(start_bot())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Telegram Bot stopped.")
