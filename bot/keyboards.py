import math
from pathlib import Path
from typing import Dict, List, Optional
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from config import settings
from recorder.schedule_manager import Subject, ScheduleEntry, DAY_SHORT, DAY_NAMES


# -----------------------------------------------------------------------------
# Main Navigation Keyboards & Helper
# -----------------------------------------------------------------------------

def make_back_button(text: str = "⬅️ Назад", callback_data: str = "main_menu") -> InlineKeyboardButton:
    """Creates a standard blue 'Back' button (style='primary') positioned at the bottom of keyboards."""
    return InlineKeyboardButton(text=text, callback_data=callback_data, style="primary")


def get_back_keyboard(callback_data: str = "main_menu", text: str = "⬅️ Назад") -> InlineKeyboardMarkup:
    """Returns a standalone single-button blue back keyboard."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [make_back_button(text=text, callback_data=callback_data)]
        ]
    )


def get_main_menu_keyboard(is_recording: bool = False, is_headman: bool = False) -> InlineKeyboardMarkup:
    """Returns main menu inline keyboard."""
    buttons = []
    if is_recording:
        if is_headman:
            # Headman only gets the screenshot button, no stop recording button!
            buttons.append([
                InlineKeyboardButton(text="📸 Снимок экрана", callback_data="btn_screenshot"),
            ])
        else:
            buttons.append([
                InlineKeyboardButton(text="📸 Снимок экрана", callback_data="btn_screenshot"),
                InlineKeyboardButton(text="⏹️ Завершить запись", callback_data="btn_stop"),
            ])
    buttons.extend([
        [
            InlineKeyboardButton(text="🗂 Предметы и сессии", callback_data="sess_cats"),
            InlineKeyboardButton(text="📅 Расписание пар", callback_data="sched_menu"),
        ],
        [
            InlineKeyboardButton(text="📊 Статус сервера", callback_data="check_status"),
            InlineKeyboardButton(text="⚙️ Настройки бота", callback_data="settings_menu"),
        ]
    ])

    if settings.github_repo_url:
        repo_web_url = settings.github_repo_url.replace(".git", "")
        buttons.append(
            [InlineKeyboardButton(text="📚 База конспектов (GitHub)", url=repo_web_url)]
        )

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_settings_keyboard(is_headman: bool = False) -> InlineKeyboardMarkup:
    """Settings menu keyboard. Headman only sees the back button."""
    if is_headman:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    make_back_button(text="⬅️ В главное меню", callback_data="main_menu")
                ],
            ]
        )

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="👤 Изменить имя входа", callback_data="settings_change_name")
            ],
            [
                InlineKeyboardButton(text="👥 Управление старостой", callback_data="settings_headman")
            ],
            [
                make_back_button(text="⬅️ В главное меню", callback_data="main_menu")
            ],
        ]
    )


def get_headman_settings_keyboard(headman_id: Optional[int] = None) -> InlineKeyboardMarkup:
    """Keyboard for owner to assign, change, or revoke headman access."""
    buttons = []
    if headman_id:
        buttons.append([
            InlineKeyboardButton(text="✏️ Изменить ID старосты", callback_data="settings_headman_set"),
            InlineKeyboardButton(text="❌ Отозвать доступ", callback_data="settings_headman_remove"),
        ])
    else:
        buttons.append([
            InlineKeyboardButton(text="➕ Назначить старосту", callback_data="settings_headman_set"),
        ])
    buttons.append([
        make_back_button(text="⬅️ Назад в настройки", callback_data="settings_menu")
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_cancel_keyboard(callback_data: str = "cancel_task") -> InlineKeyboardMarkup:
    """Returns a simple blue cancel/back button."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [make_back_button(text="⬅️ Отмена", callback_data=callback_data)]
        ]
    )


def get_active_meeting_keyboard(is_headman: bool = False) -> InlineKeyboardMarkup:
    """Returns controls for an active recording session."""
    if is_headman:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="📸 Снимок экрана", callback_data="btn_screenshot"),
                ]
            ]
        )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📸 Снимок экрана", callback_data="btn_screenshot"),
                InlineKeyboardButton(text="⏹️ Завершить запись", callback_data="btn_stop"),
            ]
        ]
    )


# -----------------------------------------------------------------------------
# Session Catalog & Card Keyboards (Two-level Navigation)
# -----------------------------------------------------------------------------

def get_sessions_subjects_keyboard(
    subjects_summary: List[dict],
    total_sessions_count: int,
) -> InlineKeyboardMarkup:
    """Screen 1: Catalog of academic subjects."""
    buttons = []

    for s in subjects_summary:
        slug = s.get("slug", "unsorted")
        title = s.get("title", "Предмет")
        count = s.get("count", 0)
        icon = "📁" if slug == "unsorted" else "🏛"

        btn_text = f"{icon} {title} ({count})"
        buttons.append([
            InlineKeyboardButton(text=btn_text, callback_data=f"sess_sub:{slug}:0")
        ])

    # Utility row
    buttons.append([
        InlineKeyboardButton(text=f"🕒 Все записи ({total_sessions_count})", callback_data="sess_sub:all:0"),
        InlineKeyboardButton(text="🔄 Обновить", callback_data="sess_cats"),
    ])

    # Blue Back button to main menu at the bottom
    buttons.append([
        make_back_button(text="⬅️ В главное меню", callback_data="main_menu")
    ])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_sessions_list_keyboard(
    sessions: List[dict],
    subject_slug: str = "all",
    page: int = 0,
    per_page: int = 5,
) -> InlineKeyboardMarkup:
    """Screen 2: Paginated list of sessions for a specific subject or all."""
    buttons = []
    total_sessions = len(sessions)
    total_pages = max(1, math.ceil(total_sessions / per_page))
    page = max(0, min(page, total_pages - 1))

    start_idx = page * per_page
    page_items = sessions[start_idx : start_idx + per_page]

    for s in page_items:
        sid = s.get("session_id", "")
        created_at = s.get("created_at", "")
        # Short format: '10.09 09:00'
        try:
            date_part = created_at.split()[0].split("-")
            time_part = ":".join(created_at.split()[1].split(":")[:2])
            short_date = f"{date_part[2]}.{date_part[1]} {time_part}"
        except Exception:
            short_date = created_at

        subj = s.get("subject", "Пара")[:13]
        slides = s.get("slides_count", 0)
        atts = s.get("attachments_count", len(s.get("attachments", [])))
        status_icon = "✅" if s.get("status") == "completed" else (
            "⏳" if s.get("status") == "analyzing" else (
                "⏸️" if s.get("postprocess_pending") or s.get("status") == "interrupted" else "📁"
            )
        )

        btn_text = f"{status_icon} {short_date} | {subj} | 🖼{slides}"
        if atts > 0:
            btn_text += f" 📎{atts}"

        buttons.append([
            InlineKeyboardButton(text=btn_text, callback_data=f"sess_view:{sid}")
        ])

    # Pagination controls
    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton(text="◀️ Пред", callback_data=f"sess_sub:{subject_slug}:{page-1}"))
    nav_row.append(InlineKeyboardButton(text=f"Стр {page + 1}/{total_pages}", callback_data="sess_noop"))
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton(text="След ▶️", callback_data=f"sess_sub:{subject_slug}:{page+1}"))
    if nav_row:
        buttons.append(nav_row)

    # Refresh row
    buttons.append([
        InlineKeyboardButton(text="🔄 Обновить список", callback_data=f"sess_sub:{subject_slug}:{page}"),
    ])

    # Blue Back button to subjects catalog at the very bottom
    buttons.append([
        make_back_button(text="⬅️ К списку предметов", callback_data="sess_cats")
    ])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_session_card_keyboard(
    session_id: str,
    has_audio: bool = True,
    slides_count: int = 0,
    attachments_count: int = 0,
    github_url: Optional[str] = None,
    subject_slug: str = "unsorted",
    postprocess_pending: bool = False,
    postprocessed: bool = False,
    has_slides_backup: bool = False,
    participants_count: int = 0,
    is_headman: bool = False,
) -> InlineKeyboardMarkup:
    """Screen 3: Interactive action keyboard for a recorded session card."""
    buttons = []

    # Headman (viewer) role: inspection & viewing only!
    if is_headman:
        row1 = []
        if slides_count > 0:
            row1.append(InlineKeyboardButton(text=f"🖼 Слайды ({slides_count})", callback_data=f"sess_slides:{session_id}"))
        if has_audio:
            row1.append(InlineKeyboardButton(text="🎵 Аудио", callback_data=f"sess_audio:{session_id}"))
        if participants_count > 0:
            row1.append(InlineKeyboardButton(text=f"👥 Участники ({participants_count})", callback_data=f"sess_attendance:{session_id}"))
        if row1:
            buttons.append(row1)

        if github_url:
            buttons.append([
                InlineKeyboardButton(text="📖 Открыть конспект на GitHub", url=github_url)
            ])

        buttons.append([
            make_back_button(text="⬅️ Назад к списку", callback_data=f"sess_sub:{subject_slug}:0"),
        ])
        return InlineKeyboardMarkup(inline_keyboard=buttons)

    # If session was frozen during shutdown, offer manual post-processing button prominently at top
    if postprocess_pending:
        buttons.append([
            InlineKeyboardButton(
                text="⚙️ Запустить постобработку (срез тишины)",
                callback_data=f"sess_postproc:{session_id}",
            )
        ])

    # Row 1: Content inspection
    row1 = []
    if slides_count > 0:
        row1.append(InlineKeyboardButton(text=f"🖼 Слайды ({slides_count})", callback_data=f"sess_slides:{session_id}"))
    if has_audio:
        row1.append(InlineKeyboardButton(text="🎵 Аудио", callback_data=f"sess_audio:{session_id}"))
    if participants_count > 0:
        row1.append(InlineKeyboardButton(text=f"👥 Участники ({participants_count})", callback_data=f"sess_attendance:{session_id}"))
    if row1:
        buttons.append(row1)

    # Row 2: Attachments and subject edit
    row2 = [
        InlineKeyboardButton(text=f"📎 Файлы ({attachments_count})", callback_data=f"sess_add_files:{session_id}"),
        InlineKeyboardButton(text="🏛 Предмет", callback_data=f"sess_pick_subj:{session_id}"),
        InlineKeyboardButton(text="✏️ Тема", callback_data=f"sess_set_topic:{session_id}"),
    ]
    buttons.append(row2)

    # Row 3: Action - Postprocess (available on every card if not already shown at top)
    if not postprocess_pending:
        btn_text = "⚙️ Перезапустить постобработку" if postprocessed else "⚙️ Запустить постобработку"
        buttons.append([
            InlineKeyboardButton(
                text=btn_text,
                callback_data=f"sess_postproc:{session_id}",
            )
        ])

    # Row 3.5: Restore slides from backup if available
    if has_slides_backup:
        buttons.append([
            InlineKeyboardButton(
                text="↩️ Откатить слайды из бэкапа",
                callback_data=f"sess_restore_slides:{session_id}",
            )
        ])

    # Row 4: Primary AI trigger
    ai_btn_text = "🔄 Перегенерировать конспект" if github_url else "🚀 Сформировать конспект"
    buttons.append([
        InlineKeyboardButton(text=ai_btn_text, callback_data=f"sess_run_ai:{session_id}")
    ])

    # Row 5: Direct link to GitHub
    if github_url:
        buttons.append([
            InlineKeyboardButton(text="📖 Открыть конспект на GitHub", url=github_url)
        ])

    # Row 6: Deletion
    buttons.append([
        InlineKeyboardButton(text="🗑 Удалить сессию", callback_data=f"sess_del_confirm:{session_id}"),
    ])

    # Row 7: Blue Back button to sessions list at the very bottom
    buttons.append([
        make_back_button(text="⬅️ Назад к списку", callback_data=f"sess_sub:{subject_slug}:0"),
    ])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_attendance_keyboard(session_id: str) -> InlineKeyboardMarkup:
    """Returns back button to the session card from the attendance roster view."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [make_back_button(text="⬅️ Назад к карточке", callback_data=f"sess_view:{session_id}")]
        ]
    )


def get_session_assign_subject_keyboard(
    session_id: str,
    subjects: List[Subject],
) -> InlineKeyboardMarkup:
    """Picker to assign or change subject of a session."""
    buttons = []

    # Group subjects in rows of 2
    row = []
    for s in subjects:
        row.append(InlineKeyboardButton(
            text=f"{s.color_icon} {s.short_name}",
            callback_data=f"sess_set_subj:{session_id}:{s.id}",
        ))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    buttons.append([
        InlineKeyboardButton(text="➕ Новый предмет", callback_data=f"sess_new_subj:{session_id}"),
    ])
    buttons.append([
        make_back_button(text="⬅️ Назад к карточке", callback_data=f"sess_view:{session_id}")
    ])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_delete_confirm_keyboard(session_id: str) -> InlineKeyboardMarkup:
    """Confirms session deletion."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="❌ Да, удалить сессию", callback_data=f"sess_del_do:{session_id}"),
            ],
            [
                make_back_button(text="⬅️ Назад (отмена)", callback_data=f"sess_view:{session_id}"),
            ]
        ]
    )


def get_attachment_mode_keyboard(session_id: str) -> InlineKeyboardMarkup:
    """Shown while student is sending files for a session."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Готово (к списку файлов)", callback_data=f"sess_add_files:{session_id}"),
                make_back_button(text="⬅️ К карточке", callback_data=f"sess_view:{session_id}"),
            ]
        ]
    )


def get_attachments_menu_keyboard(session_id: str, count: int = 0) -> InlineKeyboardMarkup:
    """Menu for managing already attached files."""
    buttons = [
        [
            InlineKeyboardButton(text="➕ Добавить еще файлы", callback_data=f"sess_add_files_upload:{session_id}"),
        ]
    ]
    if count > 0:
        buttons.append([
            InlineKeyboardButton(text="🗑 Удалить файл...", callback_data=f"sess_att_del_pick:{session_id}"),
            InlineKeyboardButton(text="🧹 Очистить все", callback_data=f"sess_att_clear_confirm:{session_id}"),
        ])
    buttons.append([
        make_back_button(text="⬅️ Назад к карточке", callback_data=f"sess_view:{session_id}")
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_attachment_delete_picker_keyboard(session_id: str, attachments: List[Path]) -> InlineKeyboardMarkup:
    """List of files with delete (❌) buttons using index to respect 64-byte callback_data limit."""
    buttons = []
    for idx, att in enumerate(attachments):
        label = att.name
        if len(label) > 30:
            label = label[:14] + "..." + label[-13:]
        buttons.append([
            InlineKeyboardButton(
                text=f"❌ {label}",
                callback_data=f"sess_att_del_idx:{session_id}:{idx}",
            )
        ])
    buttons.append([
        make_back_button(text="⬅️ Назад к файлам", callback_data=f"sess_add_files:{session_id}")
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_attachment_clear_confirm_keyboard(session_id: str) -> InlineKeyboardMarkup:
    """Confirmation before deleting all attachments."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="⚠️ Да, удалить все файлы", callback_data=f"sess_att_clear_do:{session_id}"),
            ],
            [
                make_back_button(text="⬅️ Отмена (назад к файлам)", callback_data=f"sess_add_files:{session_id}"),
            ]
        ]
    )


# -----------------------------------------------------------------------------
# Schedule & Timetable Keyboards
# -----------------------------------------------------------------------------

def get_schedule_menu_keyboard(has_entries: bool = True, is_headman: bool = False) -> InlineKeyboardMarkup:
    """Main schedule controls."""
    if is_headman:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="🔄 Обновить расписание", callback_data="sched_menu"),
                ],
                [
                    make_back_button(text="⬅️ В главное меню", callback_data="main_menu"),
                ],
            ]
        )

    buttons = [
        [
            InlineKeyboardButton(text="➕ Добавить пару", callback_data="sched_add_day"),
            InlineKeyboardButton(text="📚 Предметы", callback_data="sched_subjs_view"),
        ]
    ]
    if has_entries:
        buttons.append([
            InlineKeyboardButton(text="🔗 Ссылки на пары", callback_data="sched_links_pick"),
            InlineKeyboardButton(text="🗑 Удалить пару", callback_data="sched_del_pick"),
        ])
    buttons.append([
        InlineKeyboardButton(text="🔄 Обновить расписание", callback_data="sched_menu"),
    ])
    buttons.append([
        make_back_button(text="⬅️ В главное меню", callback_data="main_menu"),
    ])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_schedule_day_picker_keyboard() -> InlineKeyboardMarkup:
    """Step 1: Choose day of the week."""
    buttons = [
        [
            InlineKeyboardButton(text="Пн", callback_data="sched_day:0"),
            InlineKeyboardButton(text="Вт", callback_data="sched_day:1"),
            InlineKeyboardButton(text="Ср", callback_data="sched_day:2"),
        ],
        [
            InlineKeyboardButton(text="Чт", callback_data="sched_day:3"),
            InlineKeyboardButton(text="Пт", callback_data="sched_day:4"),
            InlineKeyboardButton(text="Сб", callback_data="sched_day:5"),
        ],
        [
            make_back_button(text="⬅️ Назад к расписанию", callback_data="sched_menu")
        ]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_schedule_time_picker_keyboard(day_idx: int) -> InlineKeyboardMarkup:
    """Step 2: Choose lecture start time."""
    buttons = [
        [
            InlineKeyboardButton(text="09:00", callback_data=f"sched_time:{day_idx}:09-00"),
            InlineKeyboardButton(text="10:45", callback_data=f"sched_time:{day_idx}:10-45"),
            InlineKeyboardButton(text="13:00", callback_data=f"sched_time:{day_idx}:13-00"),
        ],
        [
            InlineKeyboardButton(text="14:45", callback_data=f"sched_time:{day_idx}:14-45"),
            InlineKeyboardButton(text="16:30", callback_data=f"sched_time:{day_idx}:16-30"),
            InlineKeyboardButton(text="18:15", callback_data=f"sched_time:{day_idx}:18-15"),
        ],
        [
            InlineKeyboardButton(text="✏️ Ввести другое время", callback_data=f"sched_time_custom:{day_idx}"),
        ],
        [
            make_back_button(text="⬅️ Назад к выбору дня", callback_data="sched_add_day"),
        ]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_schedule_subject_picker_keyboard(
    subjects: List[Subject],
    day_idx: int,
    time_str: str,
) -> InlineKeyboardMarkup:
    """Step 3: Choose subject for the scheduled slot."""
    buttons = []
    time_safe = time_str.replace(":", "-")

    row = []
    for s in subjects:
        row.append(InlineKeyboardButton(
            text=f"{s.color_icon} {s.short_name}",
            callback_data=f"sched_entry_subj:{day_idx}:{time_safe}:{s.id}",
        ))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    buttons.append([
        InlineKeyboardButton(text="➕ Создать новый предмет", callback_data=f"sched_new_subj:{day_idx}:{time_safe}"),
    ])
    buttons.append([
        make_back_button(text="⬅️ Назад к выбору времени", callback_data=f"sched_day:{day_idx}"),
    ])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_schedule_delete_keyboard(
    entries: List[ScheduleEntry],
    subjects_map: Dict[str, Subject],
) -> InlineKeyboardMarkup:
    """Picker to delete a scheduled lecture slot."""
    buttons = []

    for e in entries:
        subj = subjects_map.get(e.subject_id)
        subj_name = subj.short_name if subj else e.subject_id
        day_str = DAY_SHORT[e.day_of_week] if 0 <= e.day_of_week < len(DAY_SHORT) else str(e.day_of_week)
        btn_text = f"❌ {day_str} {e.time_str} | {subj_name}"
        buttons.append([
            InlineKeyboardButton(text=btn_text, callback_data=f"sched_del_do:{e.id}")
        ])

    buttons.append([
        make_back_button(text="⬅️ Назад к расписанию", callback_data="sched_menu")
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_schedule_links_keyboard(
    entries: List[ScheduleEntry],
    subjects_map: Dict[str, Subject],
) -> InlineKeyboardMarkup:
    """List of scheduled pairs with their link status for viewing/editing."""
    buttons = []
    for e in entries:
        subj = subjects_map.get(e.subject_id)
        subj_name = subj.short_name if subj else e.subject_id
        day_str = DAY_SHORT[e.day_of_week] if 0 <= e.day_of_week < len(DAY_SHORT) else str(e.day_of_week)
        link_icon = "🔗" if e.url else "⚠️ нет ссылки"
        btn_text = f"{day_str} {e.time_str} | {subj_name} ({link_icon})"
        buttons.append([
            InlineKeyboardButton(text=btn_text, callback_data=f"sched_link_detail:{e.id}")
        ])

    buttons.append([
        make_back_button(text="⬅️ Назад к расписанию", callback_data="sched_menu")
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_schedule_slot_detail_keyboard(
    entry_id: str,
    url: Optional[str] = None,
) -> InlineKeyboardMarkup:
    """Detail view for a single schedule slot to manage its link."""
    buttons = []

    # Action row 1: Open link directly in browser/app if URL is present
    if url:
        buttons.append([
            InlineKeyboardButton(text="🌐 Открыть ссылку на пару", url=url)
        ])

    # Action row 2: Edit or Clear link
    actions = [
        InlineKeyboardButton(text="✏️ Изменить ссылку", callback_data=f"sched_link_edit:{entry_id}")
    ]
    if url:
        actions.append(
            InlineKeyboardButton(text="🗑 Очистить ссылку", callback_data=f"sched_link_clear:{entry_id}")
        )
    buttons.append(actions)

    # Action row 3: Back navigation
    buttons.append([
        InlineKeyboardButton(text="📅 К расписанию", callback_data="sched_menu"),
    ])
    buttons.append([
        make_back_button(text="⬅️ Назад к списку ссылок", callback_data="sched_links_pick"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_subjects_management_keyboard(subjects: List[Subject]) -> InlineKeyboardMarkup:
    """Subject catalog management keyboard with topic configuration support."""
    buttons = []

    for s in subjects:
        btn_text = f"{s.color_icon} {s.short_name}"
        topic_status = f"💬 #{s.telegram_thread_id}" if s.telegram_thread_id else "🔗 Привязать тему"
        buttons.append([
            InlineKeyboardButton(text=btn_text, callback_data=f"sched_subj_topic:{s.id}"),
            InlineKeyboardButton(text=topic_status, callback_data=f"sched_subj_topic:{s.id}"),
            InlineKeyboardButton(text="🗑", callback_data=f"sched_subj_del:{s.id}"),
        ])

    buttons.append([
        InlineKeyboardButton(text="➕ Добавить предмет", callback_data="sched_subj_add_prompt"),
    ])
    buttons.append([
        make_back_button(text="⬅️ Назад к расписанию", callback_data="sched_menu")
    ])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_subject_topic_edit_keyboard(subject_id: str, has_topic: bool = False) -> InlineKeyboardMarkup:
    """Keyboard for editing or clearing a subject's Telegram forum topic."""
    buttons = []
    if has_topic:
        buttons.append([
            InlineKeyboardButton(text="🗑 Отвязать тему", callback_data=f"sched_subj_topic_clear:{subject_id}"),
        ])
    buttons.append([
        make_back_button(text="⬅️ Назад к списку предметов", callback_data="sched_subjs_view")
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# -----------------------------------------------------------------------------
# Quick Confirmation Keyboards (When sending meeting link manually)
# -----------------------------------------------------------------------------

def get_quick_pair_confirm_keyboard(
    subject_id: str,
    subject_short_title: str,
) -> InlineKeyboardMarkup:
    """Prompted when a link arrives during a scheduled lecture slot."""
    buttons = [
        [
            InlineKeyboardButton(
                text=f"✅ Да, {subject_short_title}",
                callback_data=f"pair_quick:{subject_id}",
            )
        ],
        [
            InlineKeyboardButton(text="📚 Выбрать другой предмет", callback_data="pair_quick_pick"),
            InlineKeyboardButton(text="📁 Без предмета", callback_data="pair_quick:unsorted"),
        ],
        [
            make_back_button(text="⬅️ Отмена", callback_data="cancel_task")
        ]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_quick_pick_subject_keyboard(subjects: List[Subject]) -> InlineKeyboardMarkup:
    """Picker when student wants to manually assign a subject to a fresh link."""
    buttons = []

    row = []
    for s in subjects:
        row.append(InlineKeyboardButton(
            text=f"{s.color_icon} {s.short_name}",
            callback_data=f"pair_quick:{s.id}",
        ))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    buttons.append([
        InlineKeyboardButton(text="📁 Без предмета (Неразобранное)", callback_data="pair_quick:unsorted")
    ])
    buttons.append([
        make_back_button(text="⬅️ Отмена", callback_data="cancel_task")
    ])

    return InlineKeyboardMarkup(inline_keyboard=buttons)

