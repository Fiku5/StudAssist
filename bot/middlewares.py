import logging
from typing import Any, Awaitable, Callable, Dict
from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

from config import get_allowed_user_ids

logger = logging.getLogger(__name__)


class AuthMiddleware(BaseMiddleware):
    """
    Middleware that restricts access to the bot strictly to allowed Telegram user IDs.
    Silently ignores all updates (messages, callbacks) from unauthorized users.
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        from_user = getattr(event, "from_user", None)
        if not from_user:
            return None

        chat = getattr(event, "chat", None)
        if not chat and hasattr(event, "message"):
            chat = getattr(event.message, "chat", None)
        if chat and getattr(chat, "type", None) != "private":
            # The bot is strictly a private assistant and only outputs digests to topics.
            # Silently ignore ANY incoming message, command, or callback from groups, supergroups, channels, or topics.
            return None

        allowed_ids = get_allowed_user_ids()

        # Allow only if user is in whitelist
        if from_user.id in allowed_ids:
            return await handler(event, data)

        # User is unauthorized: do not react at all (silently ignore)
        logger.warning(
            f"Silently ignored unauthorized event from user {from_user.id} "
            f"(@{from_user.username or 'no_username'})"
        )
        return None
