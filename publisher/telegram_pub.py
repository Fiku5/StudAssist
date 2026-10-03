import logging
from typing import Optional, Union, Any, List
from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, Message, LinkPreviewOptions
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest

from config import settings
from ai.schemas import ActionItemsDigest

logger = logging.getLogger(__name__)


def split_text_into_chunks(text: str, max_chars: int = 4000) -> List[str]:
    """
    Splits text into chunks of at most max_chars characters.
    Preserves paragraphs where possible, falling back to line breaks, then slicing.
    """
    if not text:
        return [""]
    if len(text) <= max_chars:
        return [text]

    paragraphs = text.split("\n\n")
    chunks = []
    current_chunk = []
    current_len = 0

    for p in paragraphs:
        p_len = len(p)
        sep_len = 2 if current_chunk else 0

        if current_len + sep_len + p_len <= max_chars:
            current_chunk.append(p)
            current_len += sep_len + p_len
        else:
            if current_chunk:
                chunks.append("\n\n".join(current_chunk))
                current_chunk = []
                current_len = 0

            if len(p) > max_chars:
                lines = p.split("\n")
                line_chunk = []
                line_len = 0
                for line in lines:
                    l_sep = 1 if line_chunk else 0
                    if line_len + l_sep + len(line) <= max_chars:
                        line_chunk.append(line)
                        line_len += l_sep + len(line)
                    else:
                        if line_chunk:
                            chunks.append("\n".join(line_chunk))
                            line_chunk = []
                            line_len = 0
                        while len(line) > max_chars:
                            chunks.append(line[:max_chars])
                            line = line[max_chars:]
                        if line:
                            line_chunk.append(line)
                            line_len = len(line)
                if line_chunk:
                    chunks.append("\n".join(line_chunk))
            else:
                current_chunk.append(p)
                current_len = len(p)

    if current_chunk:
        chunks.append("\n\n".join(current_chunk))

    return chunks


async def safe_send_message(
    bot: Bot,
    chat_id: Union[int, str],
    text: str,
    parse_mode: Optional[str] = ParseMode.HTML,
    reply_markup: Optional[Any] = None,
    message_thread_id: Optional[int] = None,
    disable_web_page_preview: bool = True,
) -> List[Message]:
    """
    Safely sends a message to Telegram:
    - Splits text into chunks <= 4000 chars.
    - If Telegram throws an entity/html/markdown parse error, falls back to plain text (parse_mode=None).
    - Attaches reply_markup only to the last chunk.
    - Disables web page preview by default to prevent large OpenGraph cards taking screen space.
    """
    chunks = split_text_into_chunks(text, max_chars=4000)
    sent_messages = []

    for idx, chunk in enumerate(chunks):
        is_last = (idx == len(chunks) - 1)
        kb = reply_markup if is_last else None

        send_kwargs = {
            "chat_id": chat_id,
            "text": chunk,
            "reply_markup": kb,
        }
        if message_thread_id:
            send_kwargs["message_thread_id"] = message_thread_id
        if disable_web_page_preview:
            send_kwargs["link_preview_options"] = LinkPreviewOptions(is_disabled=True)

        try:
            msg = await bot.send_message(**send_kwargs, parse_mode=parse_mode)
            sent_messages.append(msg)
        except TelegramBadRequest as e:
            err_msg = str(e).lower()
            if any(k in err_msg for k in ["entity", "parse", "tag", "markdown", "entities", "html"]):
                logger.warning(
                    f"Telegram parse error with parse_mode={parse_mode} for chat {chat_id} ({e}). "
                    f"Retrying chunk {idx + 1}/{len(chunks)} in plain text (parse_mode=None)..."
                )
                msg = await bot.send_message(**send_kwargs, parse_mode=None)
                sent_messages.append(msg)
            else:
                raise

    return sent_messages


class TelegramPublisher:
    """
    Handles distribution of Action Items digests and status alerts to Telegram channels/chats.
    """

    def __init__(
        self,
        token: Optional[str] = None,
        channel_id: Optional[str] = None,
        bot: Optional[Bot] = None,
    ):
        self.token = token or settings.telegram_bot_token
        self.channel_id = channel_id or settings.telegram_channel_id
        self._external_bot = bot is not None

        if bot is not None:
            self.bot = bot
            self.simulation_mode = False
        else:
            self.simulation_mode = settings.simulation_mode or not bool(self.token)
            if not self.simulation_mode:
                self.bot = Bot(token=self.token)
            else:
                self.bot = None

    async def publish_to_channel(
        self,
        digest: ActionItemsDigest,
        full_article_url: str,
        chat_id: Optional[Union[str, int]] = None,
        message_thread_id: Optional[int] = None,
        with_keyboard: bool = True,
        custom_text: Optional[str] = None,
        parse_mode: Optional[str] = ParseMode.HTML,
    ) -> Optional[int]:
        """
        Publishes the 1-minute Action Items digest to the channel, group topic, or direct chat.
        If no channel_id is configured, logs locally without erroring.
        """
        if custom_text:
            message_text = custom_text
        elif parse_mode == ParseMode.HTML:
            message_text = digest.to_telegram_html(full_article_url=full_article_url)
        else:
            message_text = digest.to_telegram_markdown(full_article_url=full_article_url)

        keyboard = None
        if with_keyboard and full_article_url:
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="📖 Читать полный конспект (GitHub)",
                            url=full_article_url,
                        )
                    ]
                ]
            )

        target_chat = chat_id or self.channel_id

        if self.simulation_mode or not target_chat:
            logger.info("TelegramPublisher: No channel specified or running in simulation. Logging digest:")
            print("\n" + "=" * 50)
            topic_hint = f" (тема #{message_thread_id})" if message_thread_id else ""
            print(f"📢 Сводка лекции (целевой чат: {target_chat or 'Локальный вывод'}{topic_hint}):")
            print("=" * 50)
            print(message_text)
            if keyboard:
                print(f"🔘 Кнопка: [📖 Читать полный конспект (GitHub)] -> {full_article_url}")
            print("=" * 50 + "\n")
            return None

        try:
            msgs = await safe_send_message(
                bot=self.bot,
                chat_id=target_chat,
                text=message_text,
                parse_mode=parse_mode,
                reply_markup=keyboard,
                message_thread_id=message_thread_id,
            )
            first_msg = msgs[0] if msgs else None
            logger.info(
                f"Published digest to chat {target_chat}"
                f"{f' (topic #{message_thread_id})' if message_thread_id else ''}, "
                f"message_id: {first_msg.message_id if first_msg else None}"
            )
            return first_msg.message_id if first_msg else None
        except Exception as e:
            logger.error(f"Failed to publish digest to Telegram: {e}")
            return None

    async def send_admin_alert(self, text: str, chat_id: Optional[int] = None):
        """Sends an administrative status notification to the bot owner."""
        if self.simulation_mode:
            logger.info(f"[SIMULATION ADMIN ALERT]: {text}")
            return

        target_id = chat_id or self.channel_id
        if not target_id:
            logger.warning("No chat_id specified for admin alert.")
            return

        try:
            await safe_send_message(
                bot=self.bot,
                chat_id=target_id,
                text=f"🤖 **Студенческий Ассистент:**\n{text}",
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception as e:
            logger.warning(f"Could not send admin alert: {e}")

    async def close(self):
        """Closes the bot session cleanly if it was created internally."""
        if self.bot and not self._external_bot and self.bot.session:
            await self.bot.session.close()
