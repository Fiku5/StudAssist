from __future__ import annotations

import asyncio
import io
import re
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, BufferedInputFile, FSInputFile, InputMediaPhoto
from aiogram.filters import CommandStart, Command
from aiogram.enums import ParseMode

from config import (
    settings,
    TEMP_DIR,
    set_student_name,
    save_user_setting,
    is_headman,
    is_owner,
    get_headman_telegram_id,
    set_headman_telegram_id,
)
from recorder.session_manager import SessionManager, session_mgr
from recorder.schedule_manager import ScheduleManager, DAY_SHORT, DAY_NAMES, parse_telegram_topic_url
from bot.keyboards import (
    make_back_button,
    get_back_keyboard,
    get_settings_keyboard,
    get_headman_settings_keyboard,
    get_main_menu_keyboard,
    get_active_meeting_keyboard,
    get_session_card_keyboard,
    get_sessions_subjects_keyboard,
    get_sessions_list_keyboard,
    get_session_assign_subject_keyboard,
    get_delete_confirm_keyboard,
    get_attachment_mode_keyboard,
    get_attachments_menu_keyboard,
    get_attachment_delete_picker_keyboard,
    get_attachment_clear_confirm_keyboard,
    get_schedule_menu_keyboard,
    get_schedule_day_picker_keyboard,
    get_schedule_time_picker_keyboard,
    get_schedule_subject_picker_keyboard,
    get_schedule_delete_keyboard,
    get_schedule_links_keyboard,
    get_schedule_slot_detail_keyboard,
    get_subjects_management_keyboard,
    get_subject_topic_edit_keyboard,
    get_quick_pair_confirm_keyboard,
    get_quick_pick_subject_keyboard,
    get_attendance_keyboard,
)

logger = logging.getLogger(__name__)
router = Router()


async def ensure_owner(event: Message | CallbackQuery) -> bool:
    """
    Checks if the user has owner privileges.
    If the action was attempted by a headman (староста / viewer),
    blocks the action and sends an alert.
    """
    from_user = getattr(event, "from_user", None)
    user_id = from_user.id if from_user else 0
    if is_headman(user_id):
        if isinstance(event, CallbackQuery):
            await event.answer("⚠️ Это действие доступно только владельцу бота.", show_alert=True)
        else:
            await event.answer("⚠️ Это действие доступно только владельцу бота.")
        return False
    return True

from publisher.telegram_pub import safe_send_message, split_text_into_chunks
from bot.scheduler import ScheduleAutoJoinService
from bot.shutdown_manager import shutdown_manager
from bot.state_tracker import bot_state_tracker

# Global singletons & state trackers
schedule_mgr = ScheduleManager()
auto_join_service = ScheduleAutoJoinService(schedule_manager=schedule_mgr)
CAPTURE_SEMAPHORE = asyncio.Semaphore(1)

ACTIVE_RECORDERS: dict[int, any] = {}
ACTIVE_MONITORS: dict[int, any] = {}
USER_SESSION_STATES: dict[int, dict] = {}


# =============================================================================
# Basic Bot Commands
# =============================================================================

@router.message(CommandStart())
@router.callback_query(F.data == "main_menu")
async def handle_start(event: Message | CallbackQuery):
    """Sends greeting and main menu with dynamic status card."""
    chat_id = event.message.chat.id if isinstance(event, CallbackQuery) else event.chat.id
    auto_join_service.record_active_chat(chat_id)
    status_card = bot_state_tracker.get_status_card_markdown(schedule_mgr)
    text = (
        f"{status_card}\n\n"
        "👋 **Привет! Я твой студенческий ассистент.**\n\n"
        "Я помогаю фиксировать всё важное на онлайн-парах и готовить "
        "структурированные конспекты для тебя и всей группы.\n\n"
        "**Что я умею:**\n"
        "1. 🔗 **Запись пары**: пришли ссылку на **МТС-Линк** или **Яндекс-Телемост**.\n"
        "2. 📅 **Расписание (`/schedule`)**: настрой расписание, чтобы бот знал названия предметов!\n"
        "3. 🗂 **Каталог лекций (`/sessions`)**: конспекты сгруппированы по предметам и датам.\n"
        "4. 📎 **Обогащение материалами**: прикрепляй код и методички преподавателя.\n"
        "5. 🚀 **Контролируемый анализ**: формируй конспект по кнопке в удобный момент!"
    )
    user_id = event.from_user.id if event.from_user else 0
    is_h = is_headman(user_id)
    kb = get_main_menu_keyboard(is_recording=bot_state_tracker.is_recording(), is_headman=is_h)
    if isinstance(event, CallbackQuery):
        await event.message.edit_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
        await event.answer()
    else:
        await event.answer(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)


@router.callback_query(F.data == "cancel_task")
async def handle_cancel_task(callback: CallbackQuery):
    """Universal cancellation handler for dialog flows and cancel buttons."""
    chat_id = callback.message.chat.id
    USER_SESSION_STATES.pop(chat_id, None)
    await callback.answer("Действие отменено.")
    try:
        await callback.message.delete()
    except Exception:
        try:
            user_id = callback.from_user.id if callback.from_user else 0
            is_h = is_headman(user_id)
            await callback.message.edit_text(
                "❌ **Действие отменено.**",
                reply_markup=get_main_menu_keyboard(is_headman=is_h),
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception:
            pass


@router.message(Command("help"))
async def handle_help(message: Message):
    """Detailed help command."""
    text = (
        "📖 **Справка по командам бота:**\n\n"
        "• `/start` — Главное меню\n"
        "• `/settings` — Настройки (имя при входе на пару, часовой пояс, модель)\n"
        "• `/name` — Быстро изменить имя студента для входа в звонки\n"
        "• `/sessions` — Каталог лекций по предметам и управление сессиями\n"
        "• `/schedule` — Интерактивное расписание занятий группы\n"
        "• `/status` — Проверить статус текущих записей и сервера\n"
        "• `/screenshot` — Запросить снимок экрана пары прямо сейчас\n"
        "• `/stop` — Завершить запись текущей пары\n"
        "• `/models` — Список всех доступных моделей Gemini\n\n"
        "**Как пользоваться:**\n"
        "1. Задай своё имя через `/settings` или `/name` (по умолчанию: `Студент`).\n"
        "2. Настрой предметы и расписание через `/schedule`.\n"
        "3. Отправь ссылку на пару или дождись звонка по расписанию.\n"
        "4. Бот зайдет под твоим именем, запишет звук и слайды, сохранив их в нужную папку.\n"
        "5. Открой сессию, прикрепи файлы лектора и нажми **«🚀 Сформировать конспект»**!"
    )
    kb = get_back_keyboard(callback_data="main_menu", text="⬅️ В главное меню")
    await message.answer(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)


@router.message(Command("status"))
@router.callback_query(F.data == "check_status")
async def handle_status(event: Message | CallbackQuery):
    """Returns current system status."""
    active_count = len(ACTIVE_MONITORS)
    sessions = session_mgr.list_sessions()
    subjects = schedule_mgr.get_subjects()
    schedule_slots = schedule_mgr.get_schedule()

    status_text = (
        "📊 **Текущий статус системы:**\n\n"
        f"• **Имя студента при входе**: `{settings.student_name}`\n"
        f"• **AI-Модель**: `{settings.gemini_model}`\n"
        f"• **Режим**: `{'Симуляция (Dry-run)' if settings.simulation_mode else 'Боевой (Production)'}`\n"
        f"• **Канал публикации**: `{settings.telegram_channel_id or 'Не настроен'}`\n"
        f"• **GitHub репозиторий**: `{settings.github_repo_url or 'Локальная папка (mock_repo)'}`\n"
        f"• **Предметов в базе**: {len(subjects)}\n"
        f"• **Пар в расписании**: {len(schedule_slots)}\n"
        f"• **Сохраненных сессий**: {len(sessions)}\n"
        f"• **Активных записей**: {active_count}\n"
        f"• **Сервер**: {'🟢 Записывает пару' if active_count > 0 else '🟢 Готов к приему задач'}"
    )
    kb = get_back_keyboard(callback_data="main_menu", text="⬅️ В главное меню")
    if isinstance(event, CallbackQuery):
        await event.answer()
        try:
            await event.message.edit_text(status_text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
        except Exception:
            await event.message.answer(status_text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
    else:
        await event.answer(status_text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)


@router.message(Command("settings"))
@router.callback_query(F.data == "settings_menu")
async def handle_settings_menu(event: Message | CallbackQuery):
    """Displays bot settings menu with current student name and headman status."""
    user_id = event.from_user.id if event.from_user else 0
    if is_headman(user_id):
        text = (
            "⚙️ **Настройки студенческого ассистента**\n\n"
            "🔒 _Раздел настроек и конфигурации бота доступен только владельцу._"
        )
        kb = get_settings_keyboard(is_headman=True)
        if isinstance(event, CallbackQuery):
            await event.answer()
            try:
                await event.message.edit_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
            except Exception:
                await event.message.answer(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
        else:
            await event.answer(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
        return

    headman_id = get_headman_telegram_id()
    headman_str = f"`{headman_id}`" if headman_id else "_Не назначен_"
    text = (
        "⚙️ **Настройки студенческого ассистента**\n\n"
        f"• 👤 **Имя при входе на пары**: `{settings.student_name}`\n"
        f"• 👥 **Староста (просмотр)**: {headman_str}\n"
        f"• 🕒 **Часовой пояс**: `{settings.timezone}`\n"
        f"• 🤖 **AI-модель**: `{settings.gemini_model}`\n"
        f"• 🌐 **Режим работы**: `{'Симуляция (Dry-run)' if settings.simulation_mode else 'Боевой (Production)'}`\n\n"
        "💡 _Под этим именем бот автоматически подключается к конференциям (Яндекс.Телемост, МТС Линк и др.) "
        "и отображается преподавателю и одногруппникам в списке участников._"
    )
    kb = get_settings_keyboard(is_headman=False)
    if isinstance(event, CallbackQuery):
        await event.answer()
        try:
            await event.message.edit_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
        except Exception:
            await event.message.answer(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
    else:
        await event.answer(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)


@router.message(Command("name"))
@router.callback_query(F.data == "settings_change_name")
async def handle_settings_change_name(event: Message | CallbackQuery):
    """Prompts the user to enter a new student attendee name or sets it immediately if given as command arg."""
    if not await ensure_owner(event):
        return

    chat_id = event.message.chat.id if isinstance(event, CallbackQuery) else event.chat.id
    if isinstance(event, Message) and event.text:
        parts = event.text.strip().split(maxsplit=1)
        if len(parts) > 1 and parts[1].strip():
            new_name = parts[1].strip()
            set_student_name(new_name)
            USER_SESSION_STATES.pop(chat_id, None)
            await event.answer(
                f"✅ **Имя студента успешно сохранено: `{new_name}`**\n\n"
                f"Теперь бот будет подключаться к конференциям под этим именем.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=get_settings_keyboard(is_headman=False),
            )
            return

    USER_SESSION_STATES[chat_id] = {"action": "waiting_student_name"}
    text = (
        f"👤 **Текущее имя входа на пары:** `{settings.student_name}`\n\n"
        "✏️ Отправьте в ответ новое имя, под которым бот будет подключаться к звонкам\n"
        "(например: `Иван Иванов` или `Павел`):"
    )
    kb = get_back_keyboard(callback_data="settings_menu", text="⬅️ Назад в настройки")
    if isinstance(event, CallbackQuery):
        await event.answer()
        try:
            await event.message.edit_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
        except Exception:
            await event.message.answer(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
    else:
        await event.answer(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)


@router.callback_query(F.data == "settings_headman")
async def handle_settings_headman(event: CallbackQuery):
    """Owner screen: manage headman role and assigned Telegram ID."""
    if not await ensure_owner(event):
        return
    await event.answer()
    headman_id = get_headman_telegram_id()
    if headman_id:
        status_text = (
            f"👥 **Управление доступом старосты**\n\n"
            f"• **Текущий Telegram ID старосты:** `{headman_id}`\n\n"
            "Староста имеет доступ к просмотру расписания, журналу посещаемости, "
            "слайдам и аудиозаписям пар, но не может останавливать запись, "
            "менять настройки или запускать генерации."
        )
    else:
        status_text = (
            "👥 **Управление доступом старосты**\n\n"
            "• **Староста:** _Не назначен_\n\n"
            "Вы можете выдать старосте группы доступ к просмотру журнала посещаемости, "
            "расписания и материалов пар."
        )
    kb = get_headman_settings_keyboard(headman_id)
    try:
        await event.message.edit_text(status_text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
    except Exception:
        await event.message.answer(status_text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)


@router.callback_query(F.data == "settings_headman_set")
async def handle_settings_headman_set(event: CallbackQuery):
    """Prompts owner to input Telegram user ID of headman."""
    if not await ensure_owner(event):
        return
    await event.answer()
    chat_id = event.message.chat.id
    USER_SESSION_STATES[chat_id] = {"action": "waiting_headman_id"}
    text = (
        "👥 **Назначение старосты**\n\n"
        "✏️ Отправьте в чат числовой **Telegram ID** старосты (например: `123456789`).\n\n"
        "💡 _Узнать свой ID староста может в любом боте типа @userinfobot._"
    )
    kb = get_back_keyboard(callback_data="settings_headman", text="⬅️ Назад")
    try:
        await event.message.edit_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
    except Exception:
        await event.message.answer(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)


@router.callback_query(F.data == "settings_headman_remove")
async def handle_settings_headman_remove(event: CallbackQuery):
    """Revokes headman access."""
    if not await ensure_owner(event):
        return
    set_headman_telegram_id(None)
    await event.answer("✅ Доступ старосты отозван", show_alert=True)
    await handle_settings_headman(event)


@router.message(Command("models"))
async def handle_models(message: Message):
    """Queries and lists all available Gemini models for the current API key."""
    kb = get_back_keyboard(callback_data="main_menu", text="⬅️ В главное меню")
    if not settings.gemini_api_key:
        await message.answer("⚠️ GEMINI_API_KEY не задан в конфигурации.", reply_markup=kb)
        return

    await message.answer("🔍 Запрашиваю список доступных моделей у Google API...")
    try:
        from google import genai
        client = genai.Client(api_key=settings.gemini_api_key)
        models_pager = client.models.list()

        lines = ["🤖 **Доступные модели Gemini по вашему API-ключу:**\n"]
        for m in models_pager:
            name = m.name.replace("models/", "")
            methods = getattr(m, "supported_generation_methods", []) or []
            if not methods or "generateContent" in methods:
                lines.append(f"• `{name}` — {m.display_name or ''}")

        text = "\n".join(lines[:35])
        text += f"\n\n⚙️ **Текущая модель в боте:** `{settings.gemini_model}`"
        await message.answer(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
    except Exception as e:
        logger.error(f"Error fetching models: {e}")
        await message.answer(f"⚠️ Ошибка при получении списка моделей: `{e}`", reply_markup=kb)


@router.message(Command("myid"))
async def handle_myid(message: Message):
    """Replies with user's Telegram ID for easy authorization setup."""
    uid = message.from_user.id if message.from_user else message.chat.id
    kb = get_back_keyboard(callback_data="main_menu", text="⬅️ В главное меню")
    await message.answer(
        f"🆔 **Ваш Telegram ID:** `{uid}`\n\n"
        f"Чтобы бот отвечал только вам, добавьте эту строчку в `.env` на сервере:\n"
        f"`ALLOWED_TELEGRAM_USERS={uid}`\n\n"
        f"После изменения `.env` перезапустите бота.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb,
    )


# =============================================================================
# Schedule & Timetable Management (/schedule)
# =============================================================================

@router.message(Command("schedule"))
@router.callback_query(F.data == "sched_menu")
async def handle_schedule(event: Message | CallbackQuery):
    """Displays weekly timetable and controls."""
    if isinstance(event, CallbackQuery):
        await event.answer()

    entries = schedule_mgr.get_schedule()
    subjects_map = {s.id: s for s in schedule_mgr.get_subjects()}
    user_id = event.from_user.id if event.from_user else 0
    is_h = is_headman(user_id)

    lines = ["📅 **Расписание занятий группы:**\n"]

    if not entries:
        lines.append("ℹ️ _Расписание пока не заполнено._")
        if not is_h:
            lines.append("Нажмите кнопку **«➕ Добавить пару»**, чтобы настроить автоматический вход и привязку предметов!\n")
    else:
        # Group by day
        for day_idx in range(6):  # Mon - Sat
            day_entries = [e for e in entries if e.day_of_week == day_idx]
            if day_entries:
                lines.append(f"📌 **{DAY_NAMES[day_idx]}:**")
                for e in day_entries:
                    subj = subjects_map.get(e.subject_id)
                    icon = subj.color_icon if subj else "📘"
                    name = subj.short_name if subj else e.subject_id
                    link_text = f"[ссылка]({e.url})" if e.url else "без ссылки"
                    lines.append(f"  • `{e.time_str}` — {icon} **{name}** ({link_text})")
                lines.append("")

    text = "\n".join(lines)
    kb = get_schedule_menu_keyboard(has_entries=bool(entries), is_headman=is_h)

    if isinstance(event, CallbackQuery):
        try:
            await event.message.edit_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
        except Exception:
            await event.message.answer(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
    else:
        await event.answer(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)


@router.callback_query(F.data == "sched_add_day")
async def handle_sched_add_day(callback: CallbackQuery):
    """Wizard Step 1: Select Day of Week."""
    if not await ensure_owner(callback):
        return
    await callback.answer()
    text = "📅 **Шаг 1 из 4:** Выберите день недели для пары:"
    await callback.message.edit_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=get_schedule_day_picker_keyboard())


@router.callback_query(F.data.startswith("sched_day:"))
async def handle_sched_day_chosen(callback: CallbackQuery):
    """Wizard Step 2: Select Start Time."""
    if not await ensure_owner(callback):
        return
    day_idx = int(callback.data.split(":")[1])
    await callback.answer()
    text = f"⏰ **Шаг 2 из 4:** Выберите время начала пары в **{DAY_NAMES[day_idx]}**:"
    await callback.message.edit_text(
        text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=get_schedule_time_picker_keyboard(day_idx),
    )


@router.callback_query(F.data.startswith("sched_time:"))
async def handle_sched_time_chosen(callback: CallbackQuery):
    """Wizard Step 3: Select Subject."""
    if not await ensure_owner(callback):
        return
    parts = callback.data.split(":")
    day_idx = int(parts[1])
    time_str = parts[2].replace("-", ":")
    await callback.answer()

    subjects = schedule_mgr.get_subjects()
    text = (
        f"📚 **Шаг 3 из 4:** Выберите предмет для пары в "
        f"**{DAY_SHORT[day_idx]} {time_str}**:"
    )
    await callback.message.edit_text(
        text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=get_schedule_subject_picker_keyboard(subjects, day_idx, time_str),
    )


@router.callback_query(F.data.startswith("sched_time_custom:"))
async def handle_sched_time_custom(callback: CallbackQuery):
    """Prompts student to type in custom time string."""
    if not await ensure_owner(callback):
        return
    day_idx = int(callback.data.split(":")[1])
    chat_id = callback.message.chat.id
    USER_SESSION_STATES[chat_id] = {"action": "waiting_schedule_time", "day_idx": day_idx}
    await callback.answer()
    kb = get_back_keyboard(callback_data=f"sched_day:{day_idx}", text="⬅️ Назад к выбору времени")
    await callback.message.answer(
        f"✏️ Введите время начала пары в {DAY_NAMES[day_idx]} в формате **ЧЧ:ММ** (например, `11:30`):",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb,
    )


@router.callback_query(F.data.startswith("sched_entry_subj:"))
async def handle_sched_entry_subj_chosen(callback: CallbackQuery):
    """Wizard Step 4: Prompt for meeting URL."""
    if not await ensure_owner(callback):
        return
    parts = callback.data.split(":")
    day_idx = int(parts[1])
    time_str = parts[2].replace("-", ":")
    time_safe = parts[2]
    subj_id = parts[3]

    chat_id = callback.message.chat.id
    USER_SESSION_STATES[chat_id] = {
        "action": "waiting_schedule_url",
        "day_idx": day_idx,
        "time_str": time_str,
        "subj_id": subj_id,
    }
    await callback.answer()

    subj = schedule_mgr.get_subject(subj_id)
    subj_name = subj.title if subj else subj_id

    text = (
        f"🔗 **Шаг 4 из 4: Ссылка на пару**\n\n"
        f"• День: **{DAY_NAMES[day_idx]}**\n"
        f"• Время: **{time_str}**\n"
        f"• Предмет: **{subj_name}**\n\n"
        f"Отправьте мне ссылку на пару (**МТС-Линк** или **Яндекс-Телемост**):\n"
        f"_(Или напишите `пропустить`, если ссылка появляется только перед парой)_"
    )
    kb = get_back_keyboard(callback_data=f"sched_time:{day_idx}:{time_safe}", text="⬅️ Назад к выбору предмета")
    await callback.message.answer(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)


@router.callback_query(F.data.startswith("sched_new_subj:"))
async def handle_sched_new_subj_prompt(callback: CallbackQuery):
    """Prompts for new subject name during schedule creation."""
    if not await ensure_owner(callback):
        return
    parts = callback.data.split(":")
    day_idx = int(parts[1])
    time_str = parts[2].replace("-", ":")
    time_safe = parts[2]
    chat_id = callback.message.chat.id
    USER_SESSION_STATES[chat_id] = {
        "action": "waiting_new_sched_subject",
        "day_idx": day_idx,
        "time_str": time_str,
    }
    await callback.answer()
    kb = get_back_keyboard(callback_data=f"sched_time:{day_idx}:{time_safe}", text="⬅️ Назад к выбору предмета")
    await callback.message.answer(
        "✏️ Введите название нового учебного предмета (например, `Параллельные вычисления`):",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb,
    )


@router.callback_query(F.data == "sched_del_pick")
async def handle_sched_del_pick(callback: CallbackQuery):
    """Shows list of entries to delete."""
    if not await ensure_owner(callback):
        return
    await callback.answer()
    entries = schedule_mgr.get_schedule()
    subjects_map = {s.id: s for s in schedule_mgr.get_subjects()}
    await callback.message.edit_text(
        "🗑 Выберите пару, которую хотите удалить из расписания:",
        reply_markup=get_schedule_delete_keyboard(entries, subjects_map),
    )


@router.callback_query(F.data.startswith("sched_del_do:"))
async def handle_sched_del_do(callback: CallbackQuery):
    """Deletes the chosen schedule entry."""
    if not await ensure_owner(callback):
        return
    entry_id = callback.data.split(":", 1)[1]
    await callback.answer("Удаляю...")
    schedule_mgr.delete_schedule_entry(entry_id)
    await callback.message.answer("✅ Пара успешно удалена из расписания.")
    await handle_schedule(callback)


@router.callback_query(F.data == "sched_links_pick")
async def handle_sched_links_pick(callback: CallbackQuery):
    """Shows picker to view and edit links for scheduled slots."""
    if not await ensure_owner(callback):
        return
    await callback.answer()
    entries = schedule_mgr.get_schedule()
    subjects_map = {s.id: s for s in schedule_mgr.get_subjects()}

    if not entries:
        await callback.message.edit_text(
            "ℹ️ В расписании пока нет занятий. Добавьте пару через «➕ Добавить пару».",
            reply_markup=get_schedule_menu_keyboard(has_entries=False),
        )
        return

    text = (
        "🔗 **Управление ссылками на пары:**\n\n"
        "Выберите пару из списка ниже, чтобы посмотреть текущую ссылку, изменить её или удалить:"
    )
    await callback.message.edit_text(
        text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=get_schedule_links_keyboard(entries, subjects_map),
    )


@router.callback_query(F.data.startswith("sched_link_detail:"))
async def handle_sched_link_detail(callback: CallbackQuery):
    """Displays detail view for a scheduled slot and its link."""
    if not await ensure_owner(callback):
        return
    entry_id = callback.data.split(":", 1)[1]
    await callback.answer()
    entry = schedule_mgr.get_schedule_entry(entry_id)
    if not entry:
        await callback.message.edit_text("⚠️ Пара не найдена в расписании.", reply_markup=get_schedule_menu_keyboard())
        return

    subj = schedule_mgr.get_subject(entry.subject_id)
    subj_name = subj.title if subj else entry.subject_id
    icon = subj.color_icon if subj else "🏛"
    day_name = DAY_NAMES[entry.day_of_week]

    link_display = f"🌐 `{entry.url}`" if entry.url else "⚠️ _Ссылка не задана (пара без постоянной ссылки)_"

    text = (
        f"🔗 **Детали пары:**\n\n"
        f"• **Предмет:** {icon} **{subj_name}**\n"
        f"• **День:** {day_name}\n"
        f"• **Время начала:** `{entry.time_str}` (длительность {entry.duration_min} мин.)\n"
        f"• **Ссылка на занятие:**\n{link_display}\n\n"
        f"💡 _Вы можете нажать «✏️ Изменить ссылку», чтобы вставить новую ссылку на Телемост, МТС Линк или Zoom._"
    )
    await callback.message.edit_text(
        text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=get_schedule_slot_detail_keyboard(entry_id, entry.url),
    )


@router.callback_query(F.data.startswith("sched_link_edit:"))
async def handle_sched_link_edit_prompt(callback: CallbackQuery):
    """Prompts user to send a new URL for the scheduled slot."""
    if not await ensure_owner(callback):
        return
    entry_id = callback.data.split(":", 1)[1]
    entry = schedule_mgr.get_schedule_entry(entry_id)
    if not entry:
        await callback.answer("⚠️ Пара не найдена", show_alert=True)
        return

    subj = schedule_mgr.get_subject(entry.subject_id)
    subj_name = subj.title if subj else entry.subject_id
    day_name = DAY_NAMES[entry.day_of_week]

    chat_id = callback.message.chat.id
    USER_SESSION_STATES[chat_id] = {
        "action": "waiting_edit_schedule_url",
        "entry_id": entry_id,
    }
    await callback.answer()
    kb = get_back_keyboard(callback_data=f"sched_link_detail:{entry_id}", text="⬅️ Назад к деталям пары")
    await callback.message.answer(
        f"✏️ Отправьте новую ссылку на подключение для пары **{day_name} {entry.time_str} ({subj_name})**:\n\n"
        f"_(Или напишите `очистить`, чтобы удалить ссылку)_",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb,
    )


@router.callback_query(F.data.startswith("sched_link_clear:"))
async def handle_sched_link_clear(callback: CallbackQuery):
    """Clears the URL for a scheduled slot."""
    if not await ensure_owner(callback):
        return
    entry_id = callback.data.split(":", 1)[1]
    schedule_mgr.update_schedule_entry_url(entry_id, "")
    await callback.answer("🗑 Ссылка очищена", show_alert=True)

    entry = schedule_mgr.get_schedule_entry(entry_id)
    if not entry:
        await handle_schedule(callback)
        return

    subj = schedule_mgr.get_subject(entry.subject_id)
    subj_name = subj.title if subj else entry.subject_id
    icon = subj.color_icon if subj else "🏛"
    day_name = DAY_NAMES[entry.day_of_week]

    text = (
        f"🔗 **Детали пары:**\n\n"
        f"• **Предмет:** {icon} **{subj_name}**\n"
        f"• **День:** {day_name}\n"
        f"• **Время начала:** `{entry.time_str}`\n"
        f"• **Ссылка на занятие:**\n⚠️ _Ссылка не задана_\n\n"
        f"✅ Ссылка успешно удалена."
    )
    await callback.message.edit_text(
        text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=get_schedule_slot_detail_keyboard(entry_id, ""),
    )


@router.callback_query(F.data == "sched_subjs_view")
async def handle_sched_subjs_view(callback: CallbackQuery):
    """Displays subjects management screen with bound Telegram forum topics."""
    if not await ensure_owner(callback):
        return
    await callback.answer()
    subjects = schedule_mgr.get_subjects()

    subj_lines = []
    for s in subjects:
        if s.telegram_thread_id:
            topic_desc = f"тема `#{s.telegram_thread_id}`"
        else:
            topic_desc = "_тема не привязана_"
        subj_lines.append(f"• {s.color_icon} **{s.title}**: {topic_desc}")

    subjs_text = "\n".join(subj_lines) if subj_lines else "• _Список предметов пуст_"

    text = (
        "📚 **Управление учебными предметами:**\n\n"
        "Здесь вы можете привязать темы Telegram-группы к предметам (для автоматической отправки дайджестов), "
        "добавить новые дисциплины или удалить неактуальные:\n\n"
        f"{subjs_text}\n\n"
        "💡 _Нажмите на кнопку темы, чтобы привязать ссылку или изменить топик._"
    )
    try:
        await callback.message.edit_text(
            text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=get_subjects_management_keyboard(subjects),
        )
    except Exception:
        await callback.message.answer(
            text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=get_subjects_management_keyboard(subjects),
        )


@router.callback_query(F.data.startswith("sched_subj_topic:"))
async def handle_sched_subj_topic(callback: CallbackQuery):
    """Prompts user to link a Telegram forum topic (URL or thread_id) to a subject."""
    if not await ensure_owner(callback):
        return
    subj_id = callback.data.split(":", 1)[1]
    subj = schedule_mgr.get_subject(subj_id)
    if not subj:
        await callback.answer("⚠️ Предмет не найден", show_alert=True)
        return

    await callback.answer()
    chat_id = callback.message.chat.id
    USER_SESSION_STATES[chat_id] = {
        "action": "waiting_subj_topic_url",
        "subj_id": subj_id,
    }

    current_topic_str = f"#{subj.telegram_thread_id}" if subj.telegram_thread_id else "не привязана"
    text = (
        f"🏛 **Настройка темы Telegram для предмета:**\n"
        f"{subj.color_icon} **{subj.title}**\n\n"
        f"• **Текущая тема**: `{current_topic_str}`\n\n"
        f"✏️ **Отправьте ссылку на тему в группе**\n"
        f"(например, `https://t.me/c/2443546271/3538` или просто ID темы `3538`):\n\n"
        f"_(Или напишите `очистить` / `удалить`, чтобы отвязать тему)_"
    )
    await callback.message.answer(
        text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=get_subject_topic_edit_keyboard(subj_id, has_topic=bool(subj.telegram_thread_id)),
    )


@router.callback_query(F.data.startswith("sched_subj_topic_clear:"))
async def handle_sched_subj_topic_clear(callback: CallbackQuery):
    """Clears the bound Telegram forum topic for a subject."""
    if not await ensure_owner(callback):
        return
    subj_id = callback.data.split(":", 1)[1]
    schedule_mgr.update_subject_thread_id(subj_id, None)
    await callback.answer("🗑 Привязка темы удалена", show_alert=True)
    await handle_sched_subjs_view(callback)


@router.callback_query(F.data.startswith("sched_subj_del:"))
async def handle_sched_subj_del(callback: CallbackQuery):
    """Deletes subject and related schedule slots."""
    if not await ensure_owner(callback):
        return
    subj_id = callback.data.split(":", 1)[1]
    await callback.answer("Удаляю предмет...")
    schedule_mgr.delete_subject(subj_id)
    await callback.message.answer("✅ Предмет и связанные слоты расписания удалены.")
    await handle_sched_subjs_view(callback)


@router.callback_query(F.data == "sched_subj_add_prompt")
async def handle_sched_subj_add_prompt(callback: CallbackQuery):
    """Prompts for new subject title from catalog screen."""
    if not await ensure_owner(callback):
        return
    chat_id = callback.message.chat.id
    USER_SESSION_STATES[chat_id] = {"action": "waiting_subj_catalog_add"}
    await callback.answer()
    kb = get_back_keyboard(callback_data="sched_subjs_view", text="⬅️ Назад к списку предметов")
    await callback.message.answer(
        "✏️ Введите название нового предмета (например, `Машинное обучение`):",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb,
    )


# =============================================================================
# Two-Level Session Catalog (/sessions)
# =============================================================================

@router.message(Command("sessions"))
@router.callback_query(F.data == "sess_cats")
async def handle_sessions_catalog(event: Message | CallbackQuery):
    """Screen 1: Two-level catalog — Subject list with lecture counters."""
    if isinstance(event, CallbackQuery):
        await event.answer()

    summary = session_mgr.get_subjects_summary()
    total_sessions = sum(s["count"] for s in summary)

    if total_sessions == 0:
        msg = (
            "ℹ️ **Сохраненных сессий пока нет.**\n\n"
            "Пришли ссылку на пару в МТС-Линк или Телемост, либо настрой расписание через `/schedule`!"
        )
        kb = get_back_keyboard(callback_data="main_menu", text="⬅️ В главное меню")
        if isinstance(event, CallbackQuery):
            await event.message.answer(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
        else:
            await event.answer(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
        return

    text = (
        f"🗂 **Каталог лекций по предметам** (Всего записей: {total_sessions})\n\n"
        f"Выберите учебную дисциплину для просмотра лекций и конспектов:"
    )
    kb = get_sessions_subjects_keyboard(summary, total_sessions)

    if isinstance(event, CallbackQuery):
        try:
            await event.message.edit_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
        except Exception:
            await event.message.answer(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
    else:
        await event.answer(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)


@router.callback_query(F.data.startswith("sess_sub:"))
async def handle_sessions_by_subject(callback: CallbackQuery):
    """Screen 2: Paginated list of lectures for a selected subject (or all)."""
    parts = callback.data.split(":")
    subject_slug = parts[1]
    page = int(parts[2]) if len(parts) > 2 else 0
    await callback.answer()

    if subject_slug == "all":
        sessions = session_mgr.list_sessions()
        subj_title = "Все записи семестра"
    else:
        sessions = session_mgr.list_sessions(subject_slug=subject_slug)
        subj = schedule_mgr.get_subject(subject_slug)
        subj_title = subj.title if subj else (sessions[0].get("subject") if sessions else subject_slug)

    if not sessions:
        kb = get_back_keyboard(callback_data="sess_cats", text="⬅️ К списку предметов")
        await callback.message.answer(f"ℹ️ В разделе **{subj_title}** пока нет записей.", parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
        return

    kb = get_sessions_list_keyboard(sessions, subject_slug=subject_slug, page=page)
    text = (
        f"📚 **{subj_title}**\n"
        f"Сохраненных лекций: **{len(sessions)}**\n\n"
        f"Выберите лекцию для открытия карточки:"
    )
    try:
        await callback.message.edit_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
    except Exception:
        await callback.message.answer(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)


@router.callback_query(F.data == "sess_noop")
async def handle_sess_noop(callback: CallbackQuery):
    await callback.answer()


@router.callback_query(F.data.startswith("sess_view:"))
async def handle_session_view(callback: CallbackQuery):
    """Screen 3: Detailed session card with interactive buttons."""
    session_id = callback.data.split(":", 1)[1]
    await callback.answer()

    # Clear active user input states
    USER_SESSION_STATES.pop(callback.message.chat.id, None)

    meta = session_mgr.get_session_metadata(session_id)
    if not meta:
        kb = get_back_keyboard(callback_data="sess_cats", text="⬅️ К списку предметов")
        await callback.message.answer(f"⚠️ Сессия `{session_id}` не найдена.", parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
        return

    dur_min = round(meta.get("duration_seconds", 0) / 60, 1)
    audio_info = meta.get("audio", {})
    audio_status = "✅ есть звук" if not audio_info.get("is_silent") and audio_info.get("exists") else "⚠️ тишина"
    vol = audio_info.get("max_volume", -91.0)
    attachments = session_mgr.get_attachments(session_id)
    slides = session_mgr.get_slides(session_id)
    subject_name = meta.get("subject", "Онлайн-пара")
    subject_slug = meta.get("subject_slug", "unsorted")
    postprocess_pending = meta.get("postprocess_pending", False)
    postprocessed_at = meta.get("postprocessed_at")

    pending_lines = ""
    if postprocess_pending:
        pending_lines = (
            "\n\n⏸️ **Внимание: Сессия была прервана перезагрузкой сервера.**\n"
            "Файлы заморожены в исходном виде. Постобработка (срез тишины в начале/конце и чистка слайдов) "
            "еще не выполнялась.\n"
            "💡 Нажмите кнопку **«⚙️ Запустить постобработку»** ниже для запуска."
        )

    postproc_status = (
        "⏸️ ожидает запуска" if postprocess_pending else (
            f"✅ выполнена ({postprocessed_at})" if postprocessed_at else "не выполнялась"
        )
    )

    att_lines = ""
    if attachments:
        att_lines = "\n\n📎 **Прикрепленные файлы:**\n" + "\n".join(
            [f"• `{a.name}` ({round(a.stat().st_size / 1024, 1)} КБ)" for a in attachments]
        )

    pub_info = meta.get("published")
    pub_lines = ""
    if pub_info and pub_info.get("github_url"):
        pub_lines = (
            f"\n\n✅ **Конспект сформирован!**\n"
            f"• [📖 Открыть конспект на GitHub]({pub_info['github_url']})\n"
            f"• Модель: `{pub_info.get('model_name', 'gemini')}`"
        )

    has_backup = session_mgr.has_slides_backup(session_id)
    backup_line = "\n• **Бэкап слайдов**: 💾 сохранен (доступен откат)" if has_backup else ""

    attendance_data = meta.get("attendance", {})
    participants = attendance_data.get("participants", []) if isinstance(attendance_data, dict) else []
    participants_count = len(participants) if participants else (attendance_data.get("total_unique_participants", 0) if isinstance(attendance_data, dict) else 0)
    attendance_line = f"\n• **Участников пары**: {participants_count} чел." if participants_count > 0 else ""

    text = (
        f"📁 **Карточка лекции:** `{session_id}`\n\n"
        f"• **Дисциплина**: **{subject_name}**\n"
        f"• **Дата и время**: {meta.get('created_at', '')}\n"
        f"• **Длительность**: {dur_min} мин.\n"
        f"• **Аудиопоток**: {audio_status} ({vol} dB)\n"
        f"• **Слайдов собрано**: {len(slides)}\n"
        f"• **Файлов преподавателя**: {len(attachments)}\n"
        f"• **Постобработка**: {postproc_status}"
        f"{backup_line}"
        f"{attendance_line}"
        f"{pending_lines}"
        f"{att_lines}"
        f"{pub_lines}\n\n"
        f"💡 _Используйте кнопки для просмотра материалов или формирования конспекта:_"
    )

    user_id = callback.from_user.id if callback.from_user else 0
    is_h = is_headman(user_id)
    kb = get_session_card_keyboard(
        session_id=session_id,
        has_audio=bool(session_mgr.get_audio_path(session_id)),
        slides_count=len(slides),
        attachments_count=len(attachments),
        github_url=pub_info.get("github_url") if pub_info else None,
        subject_slug=subject_slug,
        postprocess_pending=postprocess_pending,
        postprocessed=bool(postprocessed_at),
        has_slides_backup=has_backup,
        participants_count=participants_count,
        is_headman=is_h,
    )

    try:
        await callback.message.edit_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
    except Exception:
        await callback.message.answer(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)


@router.callback_query(F.data.startswith("sess_attendance:"))
async def handle_session_attendance(callback: CallbackQuery):
    """Screen for Starosta: detailed attendance roster and copyable block."""
    session_id = callback.data.split(":", 1)[1]
    await callback.answer()

    meta = session_mgr.get_session_metadata(session_id)
    if not meta:
        kb = get_back_keyboard(callback_data="sess_cats", text="⬅️ К списку предметов")
        await callback.message.answer(f"⚠️ Сессия `{session_id}` не найдена.", parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
        return

    attendance_data = meta.get("attendance") or {}
    participants = attendance_data.get("participants", []) if isinstance(attendance_data, dict) else []
    if not participants:
        kb = get_attendance_keyboard(session_id)
        text = (
            f"ℹ️ **Сессия**: `{session_id}`\n\n"
            "⚠️ Данные о присутствующих студентах отсутствуют или не были зафиксированы."
        )
        try:
            await callback.message.edit_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
        except Exception:
            await callback.message.answer(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
        return

    subject_name = meta.get("subject", "Онлайн-пара")
    created_at = meta.get("created_at", "")
    date_str = meta.get("date") or (created_at[:10] if created_at else datetime.now().strftime("%Y-%m-%d"))
    total_min = attendance_data.get("meeting_duration_minutes", round(meta.get("duration_seconds", 0) / 60, 1))
    total_count = len(participants)

    # Sort participants alphabetically by student name
    sorted_participants = sorted(participants, key=lambda p: str(p.get("name", "")).lower())

    roster_lines = []
    copy_lines = []
    for idx, p in enumerate(sorted_participants, 1):
        p_name = p.get("name", "Студент")
        p_min = p.get("total_minutes", 0.0)
        p_pct = p.get("attendance_percentage", 0.0)
        first_s = p.get("first_seen", "")
        last_s = p.get("last_seen", "")
        time_range = f" [{first_s} — {last_s}]" if (first_s and last_s) else ""
        roster_lines.append(f"{idx}. **{p_name}** — {p_min} мин ({p_pct}%){time_range}")
        copy_lines.append(f"{idx}. {p_name} — {p_min} мин ({p_pct}%)")

    roster_text = "\n".join(roster_lines)
    copy_block = "\n".join(copy_lines)

    text = (
        f"👥 **Журнал посещаемости пары (для старосты)**\n\n"
        f"• **Дисциплина**: **{subject_name}**\n"
        f"• **Дата**: {date_str}\n"
        f"• **Длительность пары**: {total_min} мин.\n"
        f"• **Всего студентов**: {total_count} чел.\n\n"
        f"📋 **Подробный список:**\n"
        f"{roster_text}\n\n"
        f"📋 **Готовый блок для копирования:**\n"
        f"```text\n"
        f"Посещаемость: {subject_name} ({date_str})\n"
        f"Всего: {total_count} чел. (длительность: {total_min} мин)\n"
        f"{copy_block}\n"
        f"```\n\n"
        f"💡 _Нажмите на блок с кодом выше, чтобы скопировать список в буфер обмена._"
    )

    kb = get_attendance_keyboard(session_id)

    if len(text) <= 4000:
        try:
            await callback.message.edit_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
        except Exception:
            await callback.message.answer(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
    else:
        parts = split_text_into_chunks(text, max_chars=3800)
        for i, part in enumerate(parts):
            reply_markup = kb if i == len(parts) - 1 else None
            await callback.message.answer(part, parse_mode=ParseMode.MARKDOWN, reply_markup=reply_markup)


@router.callback_query(F.data.startswith("sess_postproc:"))
async def handle_session_postprocess(callback: CallbackQuery):
    """Executes post-processing (silence trimming and slide deduplication) for any session."""
    if not await ensure_owner(callback):
        return
    session_id = callback.data.split(":", 1)[1]
    await callback.answer("Запускаю постобработку...")

    meta = session_mgr.get_session_metadata(session_id)
    if not meta:
        await callback.message.answer(f"⚠️ Сессия `{session_id}` не найдена.")
        return

    bot_state_tracker.set_postprocessing(
        session_id=session_id,
        subject_title=meta.get("subject", "Онлайн-пара"),
    )

    is_interrupted = meta.get("postprocess_pending") or meta.get("status") == "interrupted"
    title_prefix = "отложенной постобработки" if is_interrupted else "постобработки"
    prog_msg = await callback.message.answer(
        f"⚙️ **Запуск {title_prefix} для `{session_id}`**\n\n"
        f"• Анализирую аудио (срез тишины в начале и конце)...\n"
        f"• Синхронизирую слайды презентации...\n"
        f"⏳ Пожалуйста, подождите...",
        parse_mode=ParseMode.MARKDOWN,
    )

    try:
        loop = asyncio.get_running_loop()
        res = await loop.run_in_executor(None, session_mgr.run_session_postprocess, session_id)

        start_cut = res.get("start_cutoff_sec")
        end_cut = res.get("end_cutoff_sec")
        cut_parts = []
        if start_cut and start_cut > 0:
            cut_parts.append(f"в начале: срез {start_cut:.1f}с")
        if end_cut is not None:
            cut_parts.append(f"в конце: срез с {end_cut:.1f}с")
        cut_info = ", ".join(cut_parts) if cut_parts else "тишина не обнаружена (без изменений)"

        initial_slides = res.get("initial_slides_count", 0)
        clean_slides = res.get("clean_slides_count", 0)
        removed_slides = max(0, initial_slides - clean_slides)

        await prog_msg.edit_text(
            f"✅ **Постобработка успешно завершена!**\n\n"
            f"• Обрезка аудио: {cut_info}\n"
            f"• Слайдов сохранено: `{clean_slides}` (удалено дубликатов/пре-слайдов: `{removed_slides}`)\n"
            f"• Новая длительность: `{res.get('duration_seconds', 0) / 60:.1f} мин`\n"
            f"• Статус обновлен: готов к формированию конспекта.",
            parse_mode=ParseMode.MARKDOWN,
        )

        # Refresh the session card
        updated_meta = session_mgr.get_session_metadata(session_id)
        if updated_meta:
            dur_min = round(updated_meta.get("duration_seconds", 0) / 60, 1)
            audio_info = updated_meta.get("audio", {})
            audio_status = "✅ есть звук" if not audio_info.get("is_silent") and audio_info.get("exists") else "⚠️ тишина"
            vol = audio_info.get("max_volume", -91.0)
            attachments = session_mgr.get_attachments(session_id)
            slides = session_mgr.get_slides(session_id)
            subject_name = updated_meta.get("subject", "Онлайн-пара")
            subject_slug = updated_meta.get("subject_slug", "unsorted")
            pub_info = updated_meta.get("published")
            postproc_at = updated_meta.get("postprocessed_at")
            postproc_line = f"\n• **Постобработка**: ✅ выполнена ({postproc_at})" if postproc_at else "\n• **Постобработка**: ✅ выполнена"

            att_lines = ""
            if attachments:
                att_lines = "\n\n📎 **Прикрепленные файлы:**\n" + "\n".join(
                    [f"• `{a.name}` ({round(a.stat().st_size / 1024, 1)} КБ)" for a in attachments]
                )

            pub_lines = ""
            if pub_info and pub_info.get("github_url"):
                pub_lines = (
                    f"\n\n✅ **Конспект сформирован!**\n"
                    f"• [📖 Открыть конспект на GitHub]({pub_info['github_url']})\n"
                    f"• Модель: `{pub_info.get('model_name', 'gemini')}`"
                )

            has_backup = session_mgr.has_slides_backup(session_id)
            backup_line = "\n• **Бэкап слайдов**: 💾 сохранен (доступен откат)" if has_backup else ""
            topic_val = updated_meta.get("topic")
            topic_line = f"\n• **Тема лекции**: {topic_val}" if topic_val else ""

            card_text = (
                f"📁 **Карточка лекции:** `{session_id}`\n\n"
                f"• **Дисциплина**: **{subject_name}**"
                f"{topic_line}\n"
                f"• **Дата и время**: {updated_meta.get('created_at', '')}\n"
                f"• **Длительность**: {dur_min} мин.\n"
                f"• **Аудиопоток**: {audio_status} ({vol} dB)\n"
                f"• **Слайдов собрано**: {len(slides)}\n"
                f"• **Файлов преподавателя**: {len(attachments)}"
                f"{postproc_line}"
                f"{backup_line}"
                f"{att_lines}"
                f"{pub_lines}\n\n"
                f"💡 _Используйте кнопки для просмотра материалов или формирования конспекта:_"
            )
            part_count = len(updated_meta.get("attendance", {}).get("participants", []))
            kb = get_session_card_keyboard(
                session_id=session_id,
                has_audio=bool(session_mgr.get_audio_path(session_id)),
                slides_count=len(slides),
                attachments_count=len(attachments),
                github_url=pub_info.get("github_url") if pub_info else None,
                subject_slug=subject_slug,
                postprocess_pending=False,
                postprocessed=True,
                has_slides_backup=has_backup,
                participants_count=part_count,
            )
            try:
                await callback.message.edit_text(card_text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
            except Exception:
                await callback.message.answer(card_text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)

    except Exception as e:
        logger.error(f"Error during postprocess for session {session_id}: {e}", exc_info=True)
        await prog_msg.edit_text(
            f"❌ **Ошибка при постобработке:**\n`{e}`\n\nФайлы сохранены без изменений.",
            parse_mode=ParseMode.MARKDOWN,
        )
    finally:
        bot_state_tracker.set_idle()


@router.callback_query(F.data.startswith("sess_restore_slides:"))
async def handle_session_restore_slides(callback: CallbackQuery):
    """Restores pre-postprocessing raw slides from slides_raw/ backup."""
    if not await ensure_owner(callback):
        return
    session_id = callback.data.split(":", 1)[1]
    await callback.answer("Восстанавливаю слайды из бэкапа...")

    success = session_mgr.restore_slides_from_backup(session_id)
    if not success:
        await callback.message.answer(
            f"⚠️ **Не удалось восстановить слайды для `{session_id}`**.\n"
            "Бэкап отсутствует либо истек срок его хранения (7 дней).",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    meta = session_mgr.get_session_metadata(session_id) or {}
    slides = session_mgr.get_slides(session_id)
    dur_min = round(meta.get("duration_seconds", 0) / 60, 1)
    audio_info = meta.get("audio", {})
    audio_status = "✅ есть звук" if not audio_info.get("is_silent") and audio_info.get("exists") else "⚠️ тишина"
    vol = audio_info.get("max_volume", -91.0)
    attachments = session_mgr.get_attachments(session_id)
    subject_name = meta.get("subject", "Онлайн-пара")
    subject_slug = meta.get("subject_slug", "unsorted")
    pub_info = meta.get("published")
    has_backup = session_mgr.has_slides_backup(session_id)

    att_lines = ""
    if attachments:
        att_lines = "\n\n📎 **Прикрепленные файлы:**\n" + "\n".join(
            [f"• `{a.name}` ({round(a.stat().st_size / 1024, 1)} КБ)" for a in attachments]
        )

    pub_lines = ""
    if pub_info and pub_info.get("github_url"):
        pub_lines = (
            f"\n\n✅ **Конспект сформирован!**\n"
            f"• [📖 Открыть конспект на GitHub]({pub_info['github_url']})\n"
            f"• Модель: `{pub_info.get('model_name', 'gemini')}`"
        )

    backup_line = "\n• **Бэкап слайдов**: 💾 сохранен (доступен откат)" if has_backup else ""
    topic_val = meta.get("topic")
    topic_line = f"\n• **Тема лекции**: {topic_val}" if topic_val else ""

    card_text = (
        f"↩️ **Слайды восстановлены из бэкапа!**\n\n"
        f"📁 **Карточка лекции:** `{session_id}`\n\n"
        f"• **Дисциплина**: **{subject_name}**"
        f"{topic_line}\n"
        f"• **Дата и время**: {meta.get('created_at', '')}\n"
        f"• **Длительность**: {dur_min} мин.\n"
        f"• **Аудиопоток**: {audio_status} ({vol} dB)\n"
        f"• **Слайдов собрано**: {len(slides)} (восстановлено)\n"
        f"• **Файлов преподавателя**: {len(attachments)}\n"
        f"• **Постобработка**: ↩️ откат к исходным слайдам"
        f"{backup_line}"
        f"{att_lines}"
        f"{pub_lines}\n\n"
        f"💡 _Вы можете заново запустить постобработку или сформировать конспект:_"
    )

    kb = get_session_card_keyboard(
        session_id=session_id,
        has_audio=bool(session_mgr.get_audio_path(session_id)),
        slides_count=len(slides),
        attachments_count=len(attachments),
        github_url=pub_info.get("github_url") if pub_info else None,
        subject_slug=subject_slug,
        postprocess_pending=False,
        postprocessed=False,
        has_slides_backup=has_backup,
        participants_count=len(meta.get("attendance", {}).get("participants", [])),
    )

    try:
        await callback.message.edit_text(card_text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
    except Exception:
        await callback.message.answer(card_text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)



@router.callback_query(F.data.startswith("sess_pick_subj:"))
async def handle_session_pick_subject(callback: CallbackQuery):
    """Displays subject picker for a specific session."""
    if not await ensure_owner(callback):
        return
    session_id = callback.data.split(":", 1)[1]
    await callback.answer()
    subjects = schedule_mgr.get_subjects()
    await callback.message.edit_text(
        f"🏛 **Выберите предмет для лекции** `{session_id}`:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=get_session_assign_subject_keyboard(session_id, subjects),
    )


@router.callback_query(F.data.startswith("sess_set_subj:"))
async def handle_session_set_subject(callback: CallbackQuery):
    """Assigns chosen subject to session and refreshes card."""
    if not await ensure_owner(callback):
        return
    parts = callback.data.split(":")
    session_id = parts[1]
    subj_id = parts[2]
    await callback.answer("Привязываю предмет...")

    subj = schedule_mgr.get_subject(subj_id)
    title = subj.title if subj else subj_id
    session_mgr.assign_session_subject(session_id, subject_title=title, subject_slug=subj_id)

    # Re-render card
    callback.data = f"sess_view:{session_id}"
    await handle_session_view(callback)


@router.callback_query(F.data.startswith("sess_new_subj:"))
async def handle_session_new_subj_prompt(callback: CallbackQuery):
    """Prompts for new subject title and binds to session."""
    if not await ensure_owner(callback):
        return
    session_id = callback.data.split(":", 1)[1]
    chat_id = callback.message.chat.id
    USER_SESSION_STATES[chat_id] = {"action": "waiting_new_subject", "session_id": session_id}
    await callback.answer()
    kb = get_back_keyboard(callback_data=f"sess_pick_subj:{session_id}", text="⬅️ Назад к выбору предмета")
    await callback.message.answer(
        f"✏️ Введите название нового предмета для сессии `{session_id}`:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb,
    )


@router.callback_query(F.data.startswith("sess_slides:"))
async def handle_session_slides(callback: CallbackQuery):
    """Sends preview of recorded slides."""
    session_id = callback.data.split(":", 1)[1]
    await callback.answer("Загружаю слайды...")
    slides = session_mgr.get_slides(session_id)
    kb = get_back_keyboard(callback_data=f"sess_view:{session_id}", text="⬅️ Назад к карточке")
    if not slides:
        await callback.message.answer("🖼 В этой сессии нет слайдов.", reply_markup=kb)
        return

    preview_slides = slides[:10]
    media = []
    for i, s in enumerate(preview_slides):
        caption = f"Слайд {i+1}/{len(slides)}" if i == 0 else ""
        media.append(InputMediaPhoto(media=FSInputFile(s), caption=caption))

    try:
        await callback.message.answer_media_group(media=media)
        if len(slides) > 10:
            await callback.message.answer(f"ℹ️ Показаны первые 10 слайдов из {len(slides)}.", reply_markup=kb)
        else:
            await callback.message.answer("🖼 Все слайды отображены выше.", reply_markup=kb)
    except Exception as e:
        logger.error(f"Failed to send slides: {e}")
        await callback.message.answer(f"⚠️ Ошибка при отправке слайдов: `{e}`", reply_markup=kb)


@router.callback_query(F.data.startswith("sess_audio:"))
async def handle_session_audio(callback: CallbackQuery):
    """Sends recorded audio.mp3 to Telegram for user verification."""
    session_id = callback.data.split(":", 1)[1]
    await callback.answer("Отправляю аудиозапись...")
    kb = get_back_keyboard(callback_data=f"sess_view:{session_id}", text="⬅️ Назад к карточке")
    audio_path = session_mgr.get_audio_path(session_id)
    if not audio_path or not audio_path.exists():
        await callback.message.answer("⚠️ Аудиофайл отсутствует.", reply_markup=kb)
        return

    meta = session_mgr.get_session_metadata(session_id) or {}
    audio_info = meta.get("audio", {})
    vol = audio_info.get("max_volume", -91.0)
    is_silent = audio_info.get("is_silent", False)

    caption = (
        f"🎵 **Аудиозапись лекции:** `{session_id}`\n"
        f"Уровень: `{vol} dB` ({'⚠️ тишина' if is_silent else '✅ есть звук'})"
    )
    try:
        await callback.message.answer_audio(
            audio=FSInputFile(audio_path, filename=f"{session_id}.mp3"),
            caption=caption,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb,
        )
    except Exception as e:
        logger.error(f"Failed to send audio: {e}")
        await callback.message.answer(f"⚠️ Ошибка отправки аудио: `{e}`", reply_markup=kb)


@router.callback_query(F.data.startswith("sess_add_files:"))
async def handle_session_add_files(callback: CallbackQuery):
    """Shows attachments management menu if files exist, or prompts directly for upload if 0."""
    if not await ensure_owner(callback):
        return
    session_id = callback.data.split(":", 1)[1]
    chat_id = callback.message.chat.id
    attachments = session_mgr.get_attachments(session_id)
    await callback.answer()

    if not attachments:
        USER_SESSION_STATES[chat_id] = {"action": "waiting_for_files", "session_id": session_id}
        text = (
            f"📎 **Режим добавления файлов к лекции** `{session_id}`\n\n"
            f"Отправьте мне любые файлы от преподавателя прямо в этот диалог:\n"
            f"• Методички и конспекты (`.docx`, `.pdf`, `.txt`)\n"
            f"• Презентации (`.pptx`, `.pdf`)\n"
            f"• Изображения и схемы (`.png`, `.jpg`)\n"
            f"• Исходный код (`.py`, `.sh`, `.cpp`, `.sql`, `.java` и др.)\n\n"
            f"Все они сохранятся в папку сессии и войдут в контекст Gemini!\n\n"
            f"Когда закончите отправку, нажмите кнопку ниже:"
        )
        try:
            await callback.message.edit_text(
                text,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=get_attachment_mode_keyboard(session_id),
            )
        except Exception:
            await callback.message.answer(
                text,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=get_attachment_mode_keyboard(session_id),
            )
    else:
        # Show management menu with numbered list and sizes
        total_kb = sum(a.stat().st_size for a in attachments) / 1024
        size_str = f"{round(total_kb / 1024, 1)} МБ" if total_kb >= 1024 else f"{round(total_kb, 1)} КБ"
        file_list = []
        for idx, a in enumerate(attachments, 1):
            f_kb = round(a.stat().st_size / 1024, 1)
            file_list.append(f"{idx}. `{a.name}` ({f_kb} КБ)")

        text = (
            f"📎 **Прикрепленные файлы к лекции** `{session_id}`\n\n"
            + "\n".join(file_list) + f"\n\n"
            f"• Всего файлов: **{len(attachments)}** ({size_str})\n\n"
            f"Выберите действие ниже:"
        )
        try:
            await callback.message.edit_text(
                text,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=get_attachments_menu_keyboard(session_id, count=len(attachments)),
            )
        except Exception:
            await callback.message.answer(
                text,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=get_attachments_menu_keyboard(session_id, count=len(attachments)),
            )


@router.callback_query(F.data.startswith("sess_add_files_upload:"))
async def handle_session_add_files_upload(callback: CallbackQuery):
    """Enters file upload mode for the session."""
    if not await ensure_owner(callback):
        return
    session_id = callback.data.split(":", 1)[1]
    chat_id = callback.message.chat.id
    USER_SESSION_STATES[chat_id] = {"action": "waiting_for_files", "session_id": session_id}
    await callback.answer()
    text = (
        f"📎 **Режим добавления файлов к лекции** `{session_id}`\n\n"
        f"Отправьте мне любые файлы от преподавателя (презентации, код, конспекты, фото).\n"
        f"Когда закончите отправку, нажмите кнопку ниже:"
    )
    try:
        await callback.message.edit_text(
            text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=get_attachment_mode_keyboard(session_id),
        )
    except Exception:
        await callback.message.answer(
            text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=get_attachment_mode_keyboard(session_id),
        )


@router.callback_query(F.data.startswith("sess_att_del_pick:"))
async def handle_session_att_del_pick(callback: CallbackQuery):
    """Shows picker to delete attachments one by one."""
    if not await ensure_owner(callback):
        return
    session_id = callback.data.split(":", 1)[1]
    attachments = session_mgr.get_attachments(session_id)
    await callback.answer()
    if not attachments:
        await callback.message.edit_text(
            f"ℹ️ В сессии `{session_id}` нет прикрепленных файлов.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=get_attachments_menu_keyboard(session_id, count=0),
        )
        return
    text = (
        f"🗑 **Выберите файл для удаления** (сессия `{session_id}`):\n\n"
        f"Нажмите на кнопку с файлом, чтобы удалить его:"
    )
    await callback.message.edit_text(
        text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=get_attachment_delete_picker_keyboard(session_id, attachments),
    )


@router.callback_query(F.data.startswith("sess_att_del_idx:"))
async def handle_session_att_del_idx(callback: CallbackQuery):
    """Deletes selected attachment by index and refreshes picker."""
    if not await ensure_owner(callback):
        return
    parts = callback.data.split(":", 2)
    session_id = parts[1]
    idx = int(parts[2])
    attachments = session_mgr.get_attachments(session_id)
    if 0 <= idx < len(attachments):
        target_name = attachments[idx].name
        session_mgr.delete_attachment(session_id, target_name)
        await callback.answer(f"Файл {target_name} удален!", show_alert=False)
    else:
        await callback.answer("Файл уже удален.", show_alert=False)

    rem_attachments = session_mgr.get_attachments(session_id)
    if rem_attachments:
        text = (
            f"🗑 **Выберите файл для удаления** (сессия `{session_id}`):\n\n"
            f"Нажмите на кнопку с файлом, чтобы удалить его:"
        )
        await callback.message.edit_text(
            text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=get_attachment_delete_picker_keyboard(session_id, rem_attachments),
        )
    else:
        text = f"ℹ️ Все файлы сессии `{session_id}` удалены."
        await callback.message.edit_text(
            text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=get_attachments_menu_keyboard(session_id, count=0),
        )


@router.callback_query(F.data.startswith("sess_att_clear_confirm:"))
async def handle_session_att_clear_confirm(callback: CallbackQuery):
    """Prompts for confirmation to clear all attachments."""
    if not await ensure_owner(callback):
        return
    session_id = callback.data.split(":", 1)[1]
    attachments = session_mgr.get_attachments(session_id)
    await callback.answer()
    text = (
        f"⚠️ **Вы уверены, что хотите удалить ВСЕ ({len(attachments)}) прикрепленные файлы лекции `{session_id}`?**\n\n"
        f"Это действие необратимо."
    )
    await callback.message.edit_text(
        text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=get_attachment_clear_confirm_keyboard(session_id),
    )


@router.callback_query(F.data.startswith("sess_att_clear_do:"))
async def handle_session_att_clear_do(callback: CallbackQuery):
    """Deletes all attachments for the session."""
    if not await ensure_owner(callback):
        return
    session_id = callback.data.split(":", 1)[1]
    count = session_mgr.clear_attachments(session_id)
    await callback.answer("Все прикрепленные файлы удалены!", show_alert=True)
    text = f"🧹 Удалено файлов: **{count}**.\n\nВ сессии `{session_id}` больше нет вложений."
    await callback.message.edit_text(
        text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=get_attachments_menu_keyboard(session_id, count=0),
    )


@router.callback_query(F.data.startswith("sess_set_topic:"))
async def handle_session_set_topic(callback: CallbackQuery):
    """Prompts the user to update session subject/topic."""
    if not await ensure_owner(callback):
        return
    session_id = callback.data.split(":", 1)[1]
    chat_id = callback.message.chat.id
    USER_SESSION_STATES[chat_id] = {"action": "waiting_for_topic", "session_id": session_id}
    await callback.answer()
    kb = get_back_keyboard(callback_data=f"sess_view:{session_id}", text="⬅️ Назад к карточке")
    await callback.message.answer(
        f"✏️ Введите уточненную тему для лекции `{session_id}`:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb,
    )


@router.callback_query(F.data.startswith("sess_run_ai:"))
async def handle_session_run_ai(callback: CallbackQuery):
    """Executes multimodal Gemini lecture analysis and publication."""
    if not await ensure_owner(callback):
        return
    session_id = callback.data.split(":", 1)[1]
    await callback.answer("Запускаю анализ лекции...")

    meta = session_mgr.get_session_metadata(session_id) or {}
    if meta.get("postprocess_pending"):
        await callback.message.answer(
            f"⚙️ Выполняю отложенную постобработку (срез тишины и дедупликацию) для `{session_id}` перед анализом...",
            parse_mode=ParseMode.MARKDOWN,
        )
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, session_mgr.run_session_postprocess, session_id)
            meta = session_mgr.get_session_metadata(session_id) or {}
        except Exception as e:
            logger.error(f"Failed to postprocess session before AI run: {e}", exc_info=True)
            await callback.message.answer(f"⚠️ Ошибка постобработки: `{e}`. Продолжаю анализ с исходными файлами...")

    audio_path = session_mgr.get_audio_path(session_id)
    slides = session_mgr.get_slides(session_id)
    attachments = session_mgr.get_attachments(session_id)

    if not audio_path and not slides and not attachments:
        await callback.message.answer(f"⚠️ В сессии `{session_id}` нет данных для анализа.")
        return

    is_silent = meta.get("audio", {}).get("is_silent", False)

    init_msg = (
        f"🚀 **Запускаю мультимодальный анализ лекции!**\n\n"
        f"• **Дисциплина**: **{meta.get('subject', 'Онлайн-пара')}**\n"
        f"• **Сессия**: `{session_id}`\n"
        f"• **Аудио**: `{'есть' if audio_path else 'нет'}` ({'⚠️ тишина' if is_silent else '✅ есть звук'})\n"
        f"• **Слайдов**: `{len(slides)}`\n"
        f"• **Файлов преподавателя**: `{len(attachments)}`\n"
        f"⏳ Отправляю материалы в Google Gemini ({settings.gemini_model})..."
    )
    await callback.message.answer(init_msg, parse_mode=ParseMode.MARKDOWN)

    already_completed = meta.get("status") == "completed" or bool(meta.get("published"))
    session_mgr.update_session_metadata(session_id, {"status": "analyzing"})
    bot_state_tracker.set_analyzing(
        session_id=session_id,
        subject_title=meta.get("subject", "Онлайн-пара"),
        model_name=settings.gemini_model,
    )

    try:
        from orchestrator import LectureOrchestrator
        orchestrator = LectureOrchestrator(bot=callback.bot)
        subj_name = meta.get("subject", "Онлайн-пара")
        topic_name = meta.get("topic")
        subj_hint = f"{subj_name} ({topic_name})" if topic_name else subj_name

        # Extract real lecture date from session (YYYY-MM-DD)
        session_date = meta.get("date") or session_id[:10]

        # Determine official subject title from schedule_mgr if available
        subj_slug = meta.get("subject_slug")
        subj = schedule_mgr.get_subject(subj_slug) if subj_slug else None
        if not subj and subj_name != "Онлайн-пара":
            for s in schedule_mgr.get_subjects():
                if s.title.lower() == subj_name.lower() or s.short_name.lower() == subj_name.lower():
                    subj = s
                    break

        official_subject = subj.title if subj else (subj_name if subj_name != "Онлайн-пара" else None)

        summary = await orchestrator.run_pipeline(
            audio_path=audio_path,
            slide_paths=slides,
            attachment_paths=attachments,
            subject_hint=subj_hint,
            is_audio_silent=is_silent,
            force_reanalyze=already_completed,
            publish_to_telegram=False,  # Delivery to PM and bound group topic is handled cleanly below
            lecture_date=session_date,
            subject_title=official_subject,
        )

        actual_model = summary["result"].model_name or settings.gemini_model
        session_mgr.update_session_metadata(session_id, {
            "status": "completed",
            "published": {
                "github_url": summary["github_url"],
                "model_name": actual_model,
                "published_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
        })

        digest_html = summary["result"].digest.to_telegram_html(full_article_url=summary["github_url"])
        if is_silent:
            digest_html = "⚠️ <i>На аудиозаписи была тишина. Конспект сформирован строго по слайдам и материалам.</i>\n\n" + digest_html

        kb = get_session_card_keyboard(
            session_id=session_id,
            has_audio=bool(audio_path),
            slides_count=len(slides),
            attachments_count=len(attachments),
            github_url=summary["github_url"],
            subject_slug=meta.get("subject_slug", "unsorted"),
            postprocess_pending=False,
            postprocessed=True,
            has_slides_backup=session_mgr.has_slides_backup(session_id),
            participants_count=len(meta.get("attendance", {}).get("participants", [])),
        )

        # 1. Deliver full interactive session card to user in PM safely via HTML
        try:
            await safe_send_message(
                bot=callback.bot,
                chat_id=callback.message.chat.id,
                text=digest_html,
                parse_mode=ParseMode.HTML,
                reply_markup=kb,
            )
        except Exception as pe:
            logger.error(f"Failed to deliver digest card to PM for {session_id}: {pe}", exc_info=True)
            # Guaranteed fallback: at least deliver the interactive card with GitHub link
            await callback.message.answer(
                f"✅ Конспект готов и опубликован на GitHub!\n\n"
                f"🔗 Конспект: {summary['github_url']}\n\n"
                f"⚠️ Не удалось отобразить полный текст дайджеста в Telegram: {pe}",
                parse_mode=None,
                reply_markup=kb,
            )

        # 2. Deliver identical digest to Telegram supergroup topic without buttons (if topic is bound)
        thread_id = subj.telegram_thread_id if subj else meta.get("telegram_thread_id")
        target_group_id = settings.telegram_group_id or settings.telegram_channel_id

        if thread_id and target_group_id:
            bot_state_tracker.set_publishing(
                session_id=session_id,
                subject_title=subj.title if subj else subj_name,
            )
            try:
                # Deliver identical digest HTML text directly by thread ID, strictly WITHOUT buttons (reply_markup=None)
                await safe_send_message(
                    bot=callback.bot,
                    chat_id=target_group_id,
                    text=digest_html,
                    parse_mode=ParseMode.HTML,
                    reply_markup=None,
                    message_thread_id=thread_id,
                )
                logger.info(
                    f"Successfully published digest to group {target_group_id} "
                    f"topic #{thread_id} for subject '{subj.title if subj else subj_name}'"
                )
            except Exception as ge:
                logger.warning(
                    f"Could not send digest to group {target_group_id} topic #{thread_id}: {ge}"
                )
                await callback.message.answer(
                    f"⚠️ Не удалось отправить дайджест в ветку #{thread_id} группы {target_group_id}: {ge}\n"
                    f"Проверьте, добавлен ли бот в группу и есть ли у него права на отправку в топики.",
                    parse_mode=None,
                )

    except Exception as e:
        logger.error(f"Error during AI analysis for {session_id}: {e}", exc_info=True)
        session_mgr.update_session_metadata(session_id, {"status": "failed"})
        await callback.message.answer(
            f"⚠️ Ошибка при анализе сессии {session_id}: {e}\n"
            f"Вы можете повторить попытку кнопкой «🚀 Сформировать конспект».",
            parse_mode=None,
        )
    finally:
        bot_state_tracker.set_idle()


@router.callback_query(F.data.startswith("sess_del_confirm:"))
async def handle_session_del_confirm(callback: CallbackQuery):
    """Prompts for deletion confirmation."""
    if not await ensure_owner(callback):
        return
    session_id = callback.data.split(":", 1)[1]
    await callback.answer()
    await callback.message.edit_text(
        f"❓ **Вы уверены, что хотите удалить сессию** `{session_id}`?\n\n"
        f"Все записанные слайды, аудио и прикрепленные файлы будут удалены.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=get_delete_confirm_keyboard(session_id),
    )


@router.callback_query(F.data.startswith("sess_del_do:"))
async def handle_session_del_do(callback: CallbackQuery):
    """Deletes the session permanently."""
    if not await ensure_owner(callback):
        return
    session_id = callback.data.split(":", 1)[1]
    await callback.answer("Удаляю...")
    session_mgr.delete_session(session_id)
    await callback.message.answer(
        f"🗑 **Сессия** `{session_id}` **успешно удалена.**",
        parse_mode=ParseMode.MARKDOWN,
    )
    # Return to catalog
    await handle_sessions_catalog(callback)


# =============================================================================
# Live Controls (/stop, /screenshot, /reprocess)
# =============================================================================

@router.message(Command("stop"))
@router.callback_query(F.data == "btn_stop")
async def handle_stop(event: Message | CallbackQuery):
    """Manually stops an active lecture recording."""
    if not await ensure_owner(event):
        return
    chat_id = event.message.chat.id if isinstance(event, CallbackQuery) else event.chat.id
    if isinstance(event, CallbackQuery):
        await event.answer("Останавливаю запись...")

    monitor = ACTIVE_MONITORS.get(chat_id) or (next(iter(ACTIVE_MONITORS.values())) if ACTIVE_MONITORS else None)
    if monitor:
        monitor.request_stop()
        msg = (
            "🛑 **Останавливаю запись пары...**\n"
            "Финализирую аудио и слайды. Сессия будет сохранена в менеджере сессий!"
        )
    else:
        msg = "ℹ️ Сейчас нет активной записи."

    if isinstance(event, CallbackQuery):
        await event.message.answer(msg, parse_mode=ParseMode.MARKDOWN)
    else:
        await event.answer(msg, parse_mode=ParseMode.MARKDOWN)


@router.message(Command("screenshot"))
@router.callback_query(F.data == "btn_screenshot")
async def handle_screenshot(event: Message | CallbackQuery):
    """Takes a live screenshot of the meeting page and sends it to the user."""
    chat_id = event.message.chat.id if isinstance(event, CallbackQuery) else event.chat.id
    if isinstance(event, CallbackQuery):
        await event.answer("Делаю снимок...")

    recorder = ACTIVE_RECORDERS.get(chat_id) or (next(iter(ACTIVE_RECORDERS.values())) if ACTIVE_RECORDERS else None)
    if recorder:
        frame_bytes = await recorder.capture_screenshot()
        if frame_bytes:
            photo = BufferedInputFile(frame_bytes, filename="live_screen.png")
            caption = "📸 **Текущий экран встречи**"
            if isinstance(event, CallbackQuery):
                await event.message.answer_photo(photo, caption=caption, parse_mode=ParseMode.MARKDOWN)
            else:
                await event.answer_photo(photo, caption=caption, parse_mode=ParseMode.MARKDOWN)
            return
        else:
            err = "⚠️ Не удалось получить снимок экрана. Возможно, страница закрыта."
    else:
        err = "ℹ️ Бот сейчас не подключен ни к какой встрече."

    if isinstance(event, CallbackQuery):
        await event.message.answer(err, parse_mode=ParseMode.MARKDOWN)
    else:
        await event.answer(err, parse_mode=ParseMode.MARKDOWN)


# =============================================================================
# Quick Subject Confirmations for fresh meeting links
# =============================================================================

@router.callback_query(F.data.startswith("pair_quick:"))
async def handle_pair_quick_confirm(callback: CallbackQuery):
    """Triggered when student confirms or picks a subject for a fresh link."""
    if not await ensure_owner(callback):
        return
    subj_id = callback.data.split(":", 1)[1]
    chat_id = callback.message.chat.id
    state = USER_SESSION_STATES.pop(chat_id, {})
    url = state.get("url")

    await callback.answer()

    if not url:
        await callback.message.answer("⚠️ Ссылка устарела. Отправьте ссылку на пару заново.")
        return

    subj = schedule_mgr.get_subject(subj_id)
    subj_title = subj.title if subj else ("Неразобранное" if subj_id == "unsorted" else "Онлайн-пара")

    await callback.message.answer(
        f"🟢 **Запускаю запись:** {subj_title}!\n"
        f"📍 Ссылка: `{url}`",
        parse_mode=ParseMode.MARKDOWN,
    )

    asyncio.create_task(
        _execute_meeting_task(
            url=url,
            chat_id=chat_id,
            bot=callback.bot,
            subject_title=subj_title,
            subject_slug=subj_id,
        )
    )


@router.callback_query(F.data == "pair_quick_pick")
async def handle_pair_quick_pick_other(callback: CallbackQuery):
    """Shows full list of subjects to pick from for a fresh link."""
    if not await ensure_owner(callback):
        return
    await callback.answer()
    subjects = schedule_mgr.get_subjects()
    await callback.message.edit_text(
        "📚 Выберите предмет для этой пары:",
        reply_markup=get_quick_pick_subject_keyboard(subjects),
    )


# =============================================================================
# Background Meeting Capture Runner
# =============================================================================

async def _execute_meeting_task(
    url: str,
    chat_id: int,
    bot,
    subject_title: str = "Онлайн-пара",
    subject_slug: str = "unsorted",
    schedule_entry_id: Optional[str] = None,
):
    """
    Background worker that connects to call, records audio and slides,
    and structures everything cleanly by subject. Protected by CAPTURE_SEMAPHORE.
    """
    if shutdown_manager.is_shutting_down:
        await bot.send_message(
            chat_id=chat_id,
            text="⚠️ Сервер завершает работу или перезагружается. Запуск новых записей приостановлен.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return False

    if CAPTURE_SEMAPHORE.locked():
        await bot.send_message(
            chat_id=chat_id,
            text=(
                "⚠️ **Сервер сейчас уже записывает другую пару.**\n\n"
                "Для стабильной работы VPS 2 vCPU одновременный запуск ограничен. "
                "Пожалуйста, дождитесь окончания текущей записи."
            ),
            parse_mode=ParseMode.MARKDOWN,
        )
        return False

    async with CAPTURE_SEMAPHORE:
        # Check free disk space before session creation and alert user if low (< 1.5 GB), without stopping the recording
        free_gb = session_mgr.get_free_disk_space_gb()
        if free_gb < 1.5:
            logger.warning(f"Low disk space detected: {free_gb:.2f} GB free.")
            try:
                await bot.send_message(
                    chat_id=chat_id,
                    text=(
                        f"⚠️ **Внимание: мало места на сервере!**\n\n"
                        f"Свободно на диске: **{free_gb:.2f} ГБ** (< 1.5 ГБ).\n"
                        f"Запись пары продолжается, но рекомендуется освободить дисковое пространство после занятия."
                    ),
                    parse_mode=ParseMode.MARKDOWN,
                )
            except Exception as e:
                logger.warning(f"Could not send low disk space alert: {e}")

        session_dir = session_mgr.create_session(
            subject=subject_title,
            subject_slug=subject_slug,
        )
        session_id = session_dir.name
        slides_dir = session_dir / "slides"
        audio_path = session_dir / "audio.mp3"

        from recorder.browser import MeetingRecorder
        from recorder.audio import AudioRecorder
        from recorder.visual import SlideDeduplicator
        from recorder.monitor import MeetingMonitor
        import time

        AudioRecorder.ensure_pulseaudio()
        recorder = MeetingRecorder(headless=True)
        deduplicator = SlideDeduplicator(output_dir=slides_dir)
        audio_recorder = AudioRecorder(output_path=audio_path)
        from recorder.schedule_manager import ScheduleManager
        schedule_manager = ScheduleManager()
        monitor = MeetingMonitor(
            recorder=recorder,
            deduplicator=deduplicator,
            audio_recorder=audio_recorder,
            schedule_manager=schedule_manager,
        )

        ACTIVE_RECORDERS[chat_id] = recorder
        ACTIVE_MONITORS[chat_id] = monitor
        bot_state_tracker.set_recording(
            session_id=session_id,
            subject_title=subject_title,
            monitor_ref=monitor,
        )

        shutdown_manager.register_session(
            chat_id=chat_id,
            session_id=session_id,
            subject_title=subject_title,
            recorder=recorder,
            audio_recorder=audio_recorder,
            monitor=monitor,
            start_time=time.time(),
            audio_path=audio_path,
            slides_dir=slides_dir,
        )

        try:
            # 1. Connect
            connected = await recorder.join_meeting(url=url, student_name=settings.student_name)
            if not connected:
                bot_state_tracker.set_idle()
                await bot.send_message(
                    chat_id=chat_id,
                    text="❌ **Не удалось войти в конференцию.** Проверьте доступность ссылки.",
                    parse_mode=ParseMode.MARKDOWN,
                )
                session_mgr.delete_session(session_id)
                return

            await bot.send_message(
                chat_id=chat_id,
                text=(
                    f"🟢 **Успешно зашел на пару под именем «{settings.student_name}»:** **{subject_title}**!\n"
                    f"Микрофон и камера выключены. Идет запись звука и мониторинг слайдов...\n\n"
                    f"Сессия: `{session_id}`"
                ),
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=get_active_meeting_keyboard(),
            )

            # 2. Monitor session
            exit_info = await monitor.monitor_session(
                audio_dest=audio_path,
                exclude_schedule_entry_id=schedule_entry_id,
            )
            bot_state_tracker.set_postprocessing(
                session_id=session_id,
                subject_title=subject_title,
            )

            if shutdown_manager.is_shutting_down:
                logger.info(f"Meeting session {session_id} ended during shutdown.")
                return

            # 3. Finalize session metadata
            meta = session_mgr.finalize_recording(
                session_id=session_id,
                duration_seconds=exit_info.get("duration_seconds", 0),
                exit_reason=exit_info.get("exit_reason", "Завершено"),
                audio_path=audio_path,
                unique_slides=exit_info.get("unique_slides", []),
                is_silent=exit_info.get("is_audio_silent", False),
                audio_level=exit_info.get("audio_level", {}),
                attendance=exit_info.get("attendance"),
            )

            # 4. Deliver interactive Session Card
            dur_min = round(meta["duration_seconds"] / 60, 1)
            audio_info = meta.get("audio", {})
            audio_status = "✅ есть звук" if not audio_info.get("is_silent") and audio_info.get("exists") else "⚠️ тишина"
            vol = audio_info.get("max_volume", -91.0)
            participants_count = len(meta.get("attendance", {}).get("participants", []))
            part_line = f"• **Участников пары**: {participants_count}\n" if participants_count > 0 else ""

            card_text = (
                f"⏹️ **Запись пары завершена!**\n\n"
                f"• **Дисциплина**: **{subject_title}**\n"
                f"• **Сессия**: `{session_id}`\n"
                f"• **Причина**: {meta['exit_reason']}\n"
                f"• **Длительность**: {dur_min} мин.\n"
                f"• **Аудио**: {audio_status} ({vol} dB)\n"
                f"• **Слайдов**: {meta['slides_count']}\n"
                f"{part_line}"
                f"• **Файлов препода**: {len(meta.get('attachments', []))}\n\n"
                f"💡 _Вы можете просмотреть слайды и звук, прикрепить файлы от преподавателя (код, методички, презентации) "
                f"и нажать **«🚀 Сформировать конспект»**, когда будете готовы._"
            )

            await bot.send_message(
                chat_id=chat_id,
                text=card_text,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=get_session_card_keyboard(
                    session_id=session_id,
                    has_audio=audio_info.get("exists", False),
                    slides_count=meta["slides_count"],
                    attachments_count=len(meta.get("attachments", [])),
                    subject_slug=subject_slug,
                    postprocess_pending=False,
                    postprocessed=True,
                    has_slides_backup=session_mgr.has_slides_backup(session_id),
                    participants_count=participants_count,
                ),
            )

        except Exception as e:
            if shutdown_manager.is_shutting_down:
                logger.info(f"Meeting session {session_id} interrupted by shutdown: {e}")
                return
            logger.error(f"Error during meeting session: {e}", exc_info=True)
            session_mgr.update_session_metadata(session_id, {"status": "error", "error": str(e)})
            await bot.send_message(
                chat_id=chat_id,
                text=f"⚠️ Произошла ошибка во время записи: `{e}`\n\n"
                     f"Сессия сохранена: `{session_id}`. Проверьте её через `/sessions`.",
                parse_mode=ParseMode.MARKDOWN,
            )
        finally:
            bot_state_tracker.set_idle()
            shutdown_manager.unregister_session(chat_id)
            ACTIVE_RECORDERS.pop(chat_id, None)
            ACTIVE_MONITORS.pop(chat_id, None)
            await recorder.close()


# Wire auto-join service executor and busy status check
auto_join_service.execute_meeting_fn = _execute_meeting_task
auto_join_service.is_busy_fn = lambda: CAPTURE_SEMAPHORE.locked() or shutdown_manager.is_shutting_down


# =============================================================================
# Inbound Text Message Processing
# =============================================================================

@router.message(F.text)
async def handle_text_message(message: Message):
    """Handles text input: schedule setup wizards, topic edits, meeting links."""
    chat_id = message.chat.id
    auto_join_service.record_active_chat(chat_id)
    text = message.text.strip()
    state = USER_SESSION_STATES.get(chat_id, {})
    action = state.get("action")

    # 0. User is setting student attendee name
    if action == "waiting_student_name":
        USER_SESSION_STATES.pop(chat_id, None)
        new_name = text.strip()
        if new_name:
            set_student_name(new_name)
            await message.answer(
                f"✅ **Имя студента успешно сохранено: `{new_name}`**\n\n"
                f"Теперь бот будет подключаться к конференциям под этим именем.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=get_settings_keyboard(is_headman=False),
            )
        else:
            await message.answer(
                "⚠️ Имя не может быть пустым. Попробуйте снова через `/settings`.",
                reply_markup=get_settings_keyboard(is_headman=False),
            )
        return

    # 0.1. Owner is assigning headman Telegram ID
    if action == "waiting_headman_id":
        raw_val = text.strip()
        if not raw_val.isdigit():
            await message.answer(
                "❌ **Некорректный ID.** Telegram ID должен состоять только из цифр.\n"
                "Отправьте числовой ID старосты или нажмите кнопку отмены:",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=get_back_keyboard(callback_data="settings_headman", text="⬅️ Отмена"),
            )
            return
        new_hid = int(raw_val)
        if is_owner(new_hid):
            await message.answer(
                "⚠️ Этот Telegram ID уже является владельцем бота. Введите ID другого пользователя:",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=get_back_keyboard(callback_data="settings_headman", text="⬅️ Отмена"),
            )
            return
        USER_SESSION_STATES.pop(chat_id, None)
        set_headman_telegram_id(new_hid)
        await message.answer(
            f"✅ **Староста успешно назначен!**\n\n"
            f"• Telegram ID: `{new_hid}`\n\n"
            f"Теперь староста может зайти в бота, нажать `/start` и просматривать расписание, "
            f"материалы пар и журнал посещаемости.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=get_settings_keyboard(is_headman=False),
        )
        return

    # 1. User is entering custom time for schedule
    if action == "waiting_schedule_time":
        day_idx = state["day_idx"]
        USER_SESSION_STATES.pop(chat_id, None)
        # Parse time HH:MM
        time_match = re.search(r"(\d{1,2})[:.-](\d{2})", text)
        if time_match:
            hh, mm = int(time_match.group(1)), int(time_match.group(2))
            clean_time = f"{hh:02d}:{mm:02d}"
            subjects = schedule_mgr.get_subjects()
            await message.answer(
                f"📚 Выберите предмет для пары в **{DAY_SHORT[day_idx]} {clean_time}**:",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=get_schedule_subject_picker_keyboard(subjects, day_idx, clean_time),
            )
        else:
            await message.answer("⚠️ Неверный формат времени. Попробуйте снова через `/schedule`.")
        return

    # 2. User is entering meeting URL for schedule
    if action == "waiting_schedule_url":
        day_idx = state["day_idx"]
        time_str = state["time_str"]
        subj_id = state["subj_id"]
        USER_SESSION_STATES.pop(chat_id, None)

        url = text if re.search(r"https?://\S+", text) else ""
        schedule_mgr.add_schedule_entry(
            subject_id=subj_id,
            day_of_week=day_idx,
            time_str=time_str,
            url=url,
        )
        subj = schedule_mgr.get_subject(subj_id)
        subj_name = subj.title if subj else subj_id
        await message.answer(
            f"🎉 **Пара успешно добавлена в расписание!**\n\n"
            f"• День: **{DAY_NAMES[day_idx]}**\n"
            f"• Время: **{time_str}**\n"
            f"• Предмет: **{subj_name}**\n"
            f"• Ссылка: `{url or 'без постоянной ссылки'}`\n\n"
            f"Теперь бот знает расписание и будет автоматически связывать сессии с этим курсом!",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=get_schedule_menu_keyboard(),
        )
        return

    # 2.5 User is editing a meeting URL for an existing schedule entry
    if action == "waiting_edit_schedule_url":
        entry_id = state["entry_id"]
        USER_SESSION_STATES.pop(chat_id, None)

        if text.lower() in ("очистить", "удалить", "стереть", "clear", "-", "пропустить"):
            schedule_mgr.update_schedule_entry_url(entry_id, "")
            await message.answer(
                "🗑 Ссылка успешно удалена из расписания этой пары.",
                reply_markup=get_schedule_menu_keyboard(),
            )
            return

        match = re.search(r"https?://\S+", text)
        if match:
            new_url = match.group(0).rstrip(".,;!?)")
            schedule_mgr.update_schedule_entry_url(entry_id, new_url)
            entry = schedule_mgr.get_schedule_entry(entry_id)
            subj = schedule_mgr.get_subject(entry.subject_id) if entry else None
            subj_name = subj.title if subj else ""
            day_name = DAY_NAMES[entry.day_of_week] if entry else ""
            await message.answer(
                f"✅ **Ссылка на пару успешно обновлена!**\n\n"
                f"• Пара: **{day_name} {entry.time_str} ({subj_name})**\n"
                f"• Новая ссылка: `{new_url}`",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=get_schedule_slot_detail_keyboard(entry_id, new_url),
            )
        else:
            await message.answer(
                "⚠️ Ссылка должна начинаться с `http://` или `https://`. Попробуйте снова через `/schedule`.",
                reply_markup=get_schedule_menu_keyboard(),
            )
        return

    # 3. User is adding a new subject during schedule creation
    if action == "waiting_new_sched_subject":
        day_idx = state["day_idx"]
        time_str = state["time_str"]
        USER_SESSION_STATES.pop(chat_id, None)

        new_subj = schedule_mgr.add_subject(title=text)
        USER_SESSION_STATES[chat_id] = {
            "action": "waiting_schedule_url",
            "day_idx": day_idx,
            "time_str": time_str,
            "subj_id": new_subj.id,
        }
        await message.answer(
            f"✅ Создан новый предмет: **{new_subj.title}**!\n\n"
            f"Теперь отправьте ссылку на пару (или напишите `пропустить`):",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    # 4. User is creating a subject from catalog screen
    if action == "waiting_subj_catalog_add":
        USER_SESSION_STATES.pop(chat_id, None)
        new_subj = schedule_mgr.add_subject(title=text)
        await message.answer(
            f"✅ Предмет **{new_subj.title}** успешно добавлен в базу!",
            parse_mode=ParseMode.MARKDOWN,
        )
        subjects = schedule_mgr.get_subjects()
        await message.answer(
            "📚 Управление учебными предметами:",
            reply_markup=get_subjects_management_keyboard(subjects),
        )
        return

    # 4.5 User is configuring Telegram topic URL for a subject
    if action == "waiting_subj_topic_url":
        subj_id = state["subj_id"]
        USER_SESSION_STATES.pop(chat_id, None)

        if text.lower() in ("очистить", "удалить", "стереть", "clear", "-", "пропустить", "отвязать"):
            schedule_mgr.update_subject_thread_id(subj_id, None)
            subj = schedule_mgr.get_subject(subj_id)
            subj_title = subj.title if subj else subj_id
            await message.answer(
                f"🗑 Привязка темы Telegram для предмета **{subj_title}** успешно удалена.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=get_back_keyboard(callback_data="sched_subjs_view", text="⬅️ К списку предметов"),
            )
            return

        chat_id_parsed, thread_id = parse_telegram_topic_url(text)

        if thread_id is not None:
            schedule_mgr.update_subject_thread_id(subj_id, thread_id)
            if chat_id_parsed:
                settings.telegram_group_id = str(chat_id_parsed)
                save_user_setting("telegram_group_id", str(chat_id_parsed))

            subj = schedule_mgr.get_subject(subj_id)
            subj_title = subj.title if subj else subj_id
            group_info = f"\n• ID группы: `{settings.telegram_group_id}`" if settings.telegram_group_id else ""
            await message.answer(
                f"🎉 **Тема успешно привязана к предмету!**\n\n"
                f"• Дисциплина: **{subj_title}**\n"
                f"• ID темы (топика): `#{thread_id}`{group_info}\n\n"
                f"Теперь после формирования конспекта дайджест будет автоматически отправляться в эту тему группы (без служебных кнопок)!",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=get_back_keyboard(callback_data="sched_subjs_view", text="⬅️ К списку предметов"),
            )
        else:
            await message.answer(
                "⚠️ Не удалось определить ID темы из сообщения.\n\n"
                "Отправьте ссылку вида `https://t.me/c/2443546271/3538` или просто номер темы `3538`.\n"
                "Попробуйте снова через «📚 Управление предметами».",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=get_back_keyboard(callback_data="sched_subjs_view", text="⬅️ К списку предметов"),
            )
        return

    # 5. User is creating a new subject to bind to an existing session
    if action == "waiting_new_subject":
        session_id = state["session_id"]
        USER_SESSION_STATES.pop(chat_id, None)

        new_subj = schedule_mgr.add_subject(title=text)
        session_mgr.assign_session_subject(session_id, subject_title=new_subj.title, subject_slug=new_subj.id)

        meta = session_mgr.get_session_metadata(session_id) or {}
        slides = session_mgr.get_slides(session_id)
        attachments = session_mgr.get_attachments(session_id)
        pub_info = meta.get("published")

        await message.answer(
            f"✅ Сессия привязана к новому предмету: **{new_subj.title}**!",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=get_session_card_keyboard(
                session_id=session_id,
                has_audio=bool(session_mgr.get_audio_path(session_id)),
                slides_count=len(slides),
                attachments_count=len(attachments),
                github_url=pub_info.get("github_url") if pub_info else None,
                subject_slug=new_subj.id,
                has_slides_backup=session_mgr.has_slides_backup(session_id),
                participants_count=len(meta.get("attendance", {}).get("participants", [])),
            ),
        )
        return

    # 6. User is updating session topic
    if action == "waiting_for_topic":
        session_id = state["session_id"]
        USER_SESSION_STATES.pop(chat_id, None)
        session_mgr.update_session_metadata(session_id, {"topic": text})

        meta = session_mgr.get_session_metadata(session_id) or {}
        slides = session_mgr.get_slides(session_id)
        attachments = session_mgr.get_attachments(session_id)
        pub_info = meta.get("published")

        await message.answer(
            f"✅ **Тема лекции успешно обновлена:**\n`{text}`",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=get_session_card_keyboard(
                session_id=session_id,
                has_audio=bool(session_mgr.get_audio_path(session_id)),
                slides_count=len(slides),
                attachments_count=len(attachments),
                github_url=pub_info.get("github_url") if pub_info else None,
                subject_slug=meta.get("subject_slug", "unsorted"),
                has_slides_backup=session_mgr.has_slides_backup(session_id),
                participants_count=len(meta.get("attendance", {}).get("participants", [])),
            ),
        )
        return

    # 7. Check for meeting link
    match = re.search(r"https?://\S+", text)
    if match:
        if is_headman(message.from_user.id if message.from_user else 0):
            await message.answer("⚠️ Запуск записи пар доступен только владельцу бота.")
            return

        url = match.group(0).rstrip(".,;!?)")

        # Check if there is an active/upcoming pair in schedule right now!
        pair_match = schedule_mgr.get_current_or_upcoming_pair()
        if pair_match:
            entry, subj = pair_match
            USER_SESSION_STATES[chat_id] = {
                "action": "waiting_quick_pair_confirm",
                "url": url,
                "subj_id": subj.id,
            }
            prompt_text = (
                f"🎯 **Принята ссылка на пару!**\n\n"
                f"Сейчас по расписанию идет: {subj.color_icon} **{subj.title}** ({entry.time_str})?\n\n"
                f"Подтвердите привязку предмета для красивого сохранения в GitHub:"
            )
            await message.answer(
                prompt_text,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=get_quick_pair_confirm_keyboard(subj.id, subj.short_name),
            )
            return

        # No scheduled pair right now -> Quick pick subject or start unsorted
        subjects = schedule_mgr.get_subjects()
        if subjects:
            USER_SESSION_STATES[chat_id] = {
                "action": "waiting_quick_pair_confirm",
                "url": url,
            }
            await message.answer(
                f"🎯 **Принята ссылка на пару!**\n\n"
                f"📍 Ссылка: `{url}`\n\n"
                f"Выберите предмет для этой лекции:",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=get_quick_pick_subject_keyboard(subjects),
            )
            return

        # No subjects configured yet -> start immediately
        await message.answer(
            f"🎯 **Принята ссылка на конференцию!**\n\n"
            f"📍 Ссылка: `{url}`\n"
            f"🚀 Подключаюсь к звонку...",
            parse_mode=ParseMode.MARKDOWN,
        )
        asyncio.create_task(_execute_meeting_task(url=url, chat_id=chat_id, bot=message.bot))
        return

    # Default guidance
    user_id = message.from_user.id if message.from_user else 0
    is_h = is_headman(user_id)
    await message.answer(
        "👋 **Как со мной работать:**\n\n"
        "• Отправь мне ссылку на пару в **Яндекс-Телемост** или **МТС-Линк**.\n"
        "• Настрой расписание и предметы через `/schedule`.\n"
        "• Или открой каталог лекций через `/sessions`.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=get_main_menu_keyboard(is_headman=is_h),
    )


# =============================================================================
# Inbound Document & Media Handler
# =============================================================================

@router.message(F.document | F.audio | F.voice | F.photo)
async def handle_document_or_audio(message: Message):
    """Handles documents, images, code files, or audio uploads."""
    if is_headman(message.from_user.id if message.from_user else 0):
        await message.answer("⚠️ Загрузка материалов доступна только владельцу бота.")
        return
    chat_id = message.chat.id
    if isinstance(message.photo, (list, tuple)) and message.photo:
        doc = message.photo[-1]
        filename = f"photo_{getattr(doc, 'file_unique_id', 'img')}.jpg"
    else:
        doc = message.document or message.audio or message.voice
        if not doc:
            return
        filename = getattr(doc, "file_name", None) or f"file_{getattr(doc, 'file_unique_id', 'upload')}"

    # Check file size limit (20 MB for cloud Telegram Bot API, up to 2 GB with local Bot API server)
    max_mb = 2000 if settings.telegram_api_server else 20
    max_bytes = max_mb * 1024 * 1024
    file_size = getattr(doc, "file_size", None)
    if isinstance(file_size, (int, float)) and file_size > max_bytes:
        file_mb = round(doc.file_size / (1024 * 1024), 1)
        hint = ""
        if not settings.telegram_api_server:
            hint = (
                "\n\n💡 _Если на вашем VPS запущен локальный Bot API сервер, "
                "укажите `TELEGRAM_API_SERVER=http://127.0.0.1:8081` в `.env`, "
                "чтобы расширить лимит скачивания до 2 ГБ!_"
            )
        await message.answer(
            f"⚠️ **Файл слишком большой ({file_mb} МБ)!**\n\n"
            f"Telegram Bot API ограничивает скачивание файлов до **{max_mb} МБ**.\n"
            f"Пожалуйста, сожмите файл или загрузите его через облачный диск.{hint}",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    # Check if user is attaching files to a specific session
    state = USER_SESSION_STATES.get(chat_id, {})
    if state.get("action") == "waiting_for_files":
        session_id = state["session_id"]
        target_path = session_mgr.get_attachment_target_path(session_id, filename)
        try:
            bot = message.bot
            file_info = await bot.get_file(doc.file_id)

            download_success = False
            raw_file_path = getattr(file_info, "file_path", "") or ""

            # Helper to extract clean relative path (strip any /var/lib/... or <token>/ prefix)
            def _extract_relative_path(p: str, tok: str) -> str:
                clean = str(p).strip().replace("\\", "/")
                if tok and tok in clean:
                    parts = clean.split(tok, 1)
                    if len(parts) > 1 and parts[1].strip("/"):
                        return parts[1].lstrip("/")
                categories = [
                    "documents/", "photos/", "voice/", "video/",
                    "video_notes/", "audio/", "thumbnails/", "stickers/",
                    "animations/", "temp/"
                ]
                for cat in categories:
                    idx = clean.find(cat)
                    if idx != -1:
                        return clean[idx:]
                return clean.lstrip("/")

            clean_rel = _extract_relative_path(raw_file_path, bot.token)

            # 1. Direct copy: check raw_file_path and mapped host storage roots
            candidate_files = []
            if raw_file_path:
                candidate_files.append(Path(raw_file_path))
                raw_str = str(raw_file_path).replace("\\", "/")
                srv_prefix = "/var/lib/telegram-bot-api"
                if raw_str.startswith(srv_prefix):
                    rel_part = raw_str[len(srv_prefix):].lstrip("/")
                    possible_roots = []
                    if settings.telegram_api_local_root:
                        possible_roots.append(Path(settings.telegram_api_local_root))
                    possible_roots.extend([
                        Path("/root/Byakugan/storage/bot-api-data"),
                        Path("/var/lib/telegram-bot-api"),
                    ])
                    for r in possible_roots:
                        candidate_files.append(r / rel_part)

            for cand in candidate_files:
                if cand.is_file():
                    try:
                        import shutil
                        shutil.copy2(cand, target_path)
                        if target_path.exists() and target_path.stat().st_size > 0:
                            download_success = True
                            logger.info(f"Direct zero-copy file attached from: {cand}")
                            break
                    except Exception as e_copy:
                        logger.warning(f"Direct file copy from {cand} failed: {e_copy}")

            # 2. HTTP download with cleaned relative path (never duplicates /var/lib/... or token in URL)
            if not download_success:
                api_base = settings.telegram_api_server or "https://api.telegram.org"
                clean_base = api_base.rstrip("/")
                file_url = f"{clean_base}/file/bot{bot.token}/{clean_rel}"
                try:
                    import httpx
                    async with httpx.AsyncClient(timeout=60.0) as http_client:
                        resp = await http_client.get(file_url)
                        resp.raise_for_status()
                        target_path.write_bytes(resp.content)
                        if target_path.exists() and target_path.stat().st_size > 0:
                            download_success = True
                except Exception as e_http:
                    logger.warning(f"HTTP download from {file_url} failed: {e_http}")

            # 3. Standard aiogram download_file with clean relative path
            if not download_success:
                try:
                    await bot.download_file(clean_rel, destination=target_path)
                    if target_path.exists() and target_path.stat().st_size > 0:
                        download_success = True
                except Exception as e_dl:
                    logger.warning(f"aiogram download_file with clean_rel failed: {e_dl}")

            if not target_path.exists() or target_path.stat().st_size == 0:
                target_path.unlink(missing_ok=True)
                raise RuntimeError("Скачанный файл пуст (0 байт)")

            file_size = target_path.stat().st_size

            # Register attachment in session
            session_mgr.add_attachment(session_id, filename, target_path)

            atts = session_mgr.get_attachments(session_id)
            await message.answer(
                f"✅ **Файл успешно прикреплен!**\n\n"
                f"• Имя: `{filename}`\n"
                f"• Размер: `{round(file_size / 1024, 1)} КБ`\n"
                f"• Всего файлов в сессии: `{len(atts)}`\n\n"
                f"Отправьте еще файлы или нажмите «✅ Готово»:",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=get_attachment_mode_keyboard(session_id),
            )
        except Exception as e:
            target_path.unlink(missing_ok=True)
            logger.error(f"Failed to download/attach file: {e}", exc_info=True)
            await message.answer(f"⚠️ Ошибка при сохранении файла: `{e}`")
        return

    # User uploaded file outside of session context: ignore silently.
    return
