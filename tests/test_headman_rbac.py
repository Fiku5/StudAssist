import asyncio
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from aiogram.types import CallbackQuery, Message, User, Chat
from aiogram.enums import ParseMode

import config
from config import (
    get_owner_user_ids,
    get_headman_telegram_id,
    set_headman_telegram_id,
    is_headman,
    is_owner,
    get_allowed_user_ids,
)
from bot.keyboards import (
    get_main_menu_keyboard,
    get_active_meeting_keyboard,
    get_settings_keyboard,
    get_headman_settings_keyboard,
    get_session_card_keyboard,
    get_schedule_menu_keyboard,
)
from bot.middlewares import AuthMiddleware
from bot.handlers import (
    ensure_owner,
    handle_stop,
    handle_session_run_ai,
    handle_session_postprocess,
    handle_session_restore_slides,
    handle_session_del_confirm,
    handle_session_del_do,
    handle_sched_add_day,
    handle_session_add_files,
    handle_settings_headman,
    handle_settings_headman_set,
    handle_settings_headman_remove,
    handle_text_message,
    handle_document_or_audio,
    USER_SESSION_STATES,
)


@pytest.fixture(autouse=True)
def cleanup_headman(tmp_path, monkeypatch):
    """Ensure headman settings file is isolated and cleaned up for each test."""
    test_settings_file = tmp_path / "user_settings.json"
    monkeypatch.setattr(config, "USER_SETTINGS_FILE", test_settings_file)
    monkeypatch.setattr(config.settings, "allowed_telegram_users", "1017867274")
    USER_SESSION_STATES.clear()
    yield
    USER_SESSION_STATES.clear()


# =====================================================================
# 1. Config & RBAC Helper Tests
# =====================================================================

def test_config_rbac_helpers(monkeypatch):
    monkeypatch.setattr(config.settings, "allowed_telegram_users", "1017867274, 999999")
    owners = get_owner_user_ids()
    assert owners == [1017867274, 999999]

    assert is_owner(1017867274) is True
    assert is_owner(999999) is True
    assert is_owner(123456) is False

    # Headman initially None
    assert get_headman_telegram_id() is None
    assert is_headman(123456) is False
    assert get_allowed_user_ids() == [1017867274, 999999]

    # Assign headman
    set_headman_telegram_id(555555)
    assert get_headman_telegram_id() == 555555
    assert is_headman(555555) is True
    assert is_headman(1017867274) is False
    assert is_owner(555555) is False
    assert get_allowed_user_ids() == [1017867274, 999999, 555555]

    # Revoke headman
    set_headman_telegram_id(None)
    assert get_headman_telegram_id() is None
    assert is_headman(555555) is False
    assert get_allowed_user_ids() == [1017867274, 999999]


# =====================================================================
# 2. AuthMiddleware Tests for Headman
# =====================================================================

def test_auth_middleware_allows_headman_and_blocks_strangers():
    set_headman_telegram_id(777777)
    middleware = AuthMiddleware()
    handler = AsyncMock(return_value="success")

    # 1. Owner allowed
    msg_owner = MagicMock(spec=Message)
    msg_owner.from_user = User(id=1017867274, is_bot=False, first_name="Owner")
    res_owner = asyncio.run(middleware(handler, msg_owner, {}))
    assert res_owner == "success"

    # 2. Headman allowed
    handler.reset_mock()
    msg_headman = MagicMock(spec=Message)
    msg_headman.from_user = User(id=777777, is_bot=False, first_name="Headman")
    res_headman = asyncio.run(middleware(handler, msg_headman, {}))
    assert res_headman == "success"

    # 3. Stranger blocked silently
    handler.reset_mock()
    msg_stranger = MagicMock(spec=Message)
    msg_stranger.from_user = User(id=111222, is_bot=False, first_name="Stranger")
    msg_stranger.answer = AsyncMock()
    res_stranger = asyncio.run(middleware(handler, msg_stranger, {}))
    assert res_stranger is None
    handler.assert_not_awaited()
    msg_stranger.answer.assert_not_awaited()


# =====================================================================
# 3. Keyboards Tests for Headman Role
# =====================================================================

def test_main_menu_keyboard_headman():
    # Idle mode: same options for owner and headman
    kb_idle_headman = get_main_menu_keyboard(is_recording=False, is_headman=True)
    cbs_idle_h = [btn.callback_data for row in kb_idle_headman.inline_keyboard for btn in row if btn.callback_data]
    assert "btn_stop" not in cbs_idle_h
    assert "btn_screenshot" not in cbs_idle_h
    assert "sess_cats" in cbs_idle_h
    assert "sched_menu" in cbs_idle_h
    assert "check_status" in cbs_idle_h
    assert "settings_menu" in cbs_idle_h

    # Recording mode: Headman sees ONLY screenshot, NO stop button
    kb_rec_headman = get_main_menu_keyboard(is_recording=True, is_headman=True)
    cbs_rec_h = [btn.callback_data for row in kb_rec_headman.inline_keyboard for btn in row if btn.callback_data]
    assert "btn_screenshot" in cbs_rec_h
    assert "btn_stop" not in cbs_rec_h

    # Recording mode: Owner sees BOTH screenshot AND stop button
    kb_rec_owner = get_main_menu_keyboard(is_recording=True, is_headman=False)
    cbs_rec_o = [btn.callback_data for row in kb_rec_owner.inline_keyboard for btn in row if btn.callback_data]
    assert "btn_screenshot" in cbs_rec_o
    assert "btn_stop" in cbs_rec_o


def test_active_meeting_keyboard_headman():
    # Headman sees only screenshot button
    kb_h = get_active_meeting_keyboard(is_headman=True)
    cbs_h = [btn.callback_data for row in kb_h.inline_keyboard for btn in row if btn.callback_data]
    assert "btn_screenshot" in cbs_h
    assert "btn_stop" not in cbs_h

    # Owner sees both screenshot and stop button
    kb_o = get_active_meeting_keyboard(is_headman=False)
    cbs_o = [btn.callback_data for row in kb_o.inline_keyboard for btn in row if btn.callback_data]
    assert "btn_screenshot" in cbs_o
    assert "btn_stop" in cbs_o


def test_settings_keyboard_headman_and_owner():
    # Headman sees only back button
    kb_h = get_settings_keyboard(is_headman=True)
    cbs_h = [btn.callback_data for row in kb_h.inline_keyboard for btn in row if btn.callback_data]
    assert cbs_h == ["main_menu"]

    # Owner sees change name, headman management, and back button
    kb_o = get_settings_keyboard(is_headman=False)
    cbs_o = [btn.callback_data for row in kb_o.inline_keyboard for btn in row if btn.callback_data]
    assert "settings_change_name" in cbs_o
    assert "settings_headman" in cbs_o
    assert "main_menu" in cbs_o


def test_headman_settings_keyboard():
    # No headman set
    kb_none = get_headman_settings_keyboard(headman_id=None)
    cbs_none = [btn.callback_data for row in kb_none.inline_keyboard for btn in row if btn.callback_data]
    assert "settings_headman_set" in cbs_none
    assert "settings_headman_remove" not in cbs_none
    assert "settings_menu" in cbs_none

    # Headman set
    kb_set = get_headman_settings_keyboard(headman_id=888888)
    cbs_set = [btn.callback_data for row in kb_set.inline_keyboard for btn in row if btn.callback_data]
    assert "settings_headman_set" in cbs_set
    assert "settings_headman_remove" in cbs_set
    assert "settings_menu" in cbs_set


def test_session_card_keyboard_headman():
    # Headman card has only slides, audio, participants, github url, and back
    kb_h = get_session_card_keyboard(
        session_id="sess_rbac",
        has_audio=True,
        slides_count=10,
        participants_count=25,
        attachments_count=3,
        github_url="https://github.com/example/repo",
        has_slides_backup=True,
        subject_slug="all",
        is_headman=True,
    )
    cbs_h = [btn.callback_data for row in kb_h.inline_keyboard for btn in row if btn.callback_data]
    urls_h = [btn.url for row in kb_h.inline_keyboard for btn in row if btn.url]

    # Allowed for headman:
    assert "sess_slides:sess_rbac" in cbs_h
    assert "sess_audio:sess_rbac" in cbs_h
    assert "sess_attendance:sess_rbac" in cbs_h
    assert "sess_sub:all:0" in cbs_h
    assert "https://github.com/example/repo" in urls_h

    # Forbidden for headman (MUST NOT be in keyboard):
    assert "sess_add_files:sess_rbac" not in cbs_h
    assert "sess_pick_subj:sess_rbac" not in cbs_h
    assert "sess_set_topic:sess_rbac" not in cbs_h
    assert "sess_postproc:sess_rbac" not in cbs_h
    assert "sess_run_ai:sess_rbac" not in cbs_h
    assert "sess_del_confirm:sess_rbac" not in cbs_h
    assert "sess_restore_slides:sess_rbac" not in cbs_h


def test_schedule_menu_keyboard_headman():
    # Headman schedule menu: only refresh and back to main menu
    kb_h = get_schedule_menu_keyboard(has_entries=True, is_headman=True)
    cbs_h = [btn.callback_data for row in kb_h.inline_keyboard for btn in row if btn.callback_data]
    assert cbs_h == ["sched_menu", "main_menu"]

    # Owner schedule menu has full management
    kb_o = get_schedule_menu_keyboard(has_entries=True, is_headman=False)
    cbs_o = [btn.callback_data for row in kb_o.inline_keyboard for btn in row if btn.callback_data]
    assert "sched_add_day" in cbs_o
    assert "sched_subjs_view" in cbs_o
    assert "sched_links_pick" in cbs_o
    assert "sched_del_pick" in cbs_o
    assert "main_menu" in cbs_o


# =====================================================================
# 4. Backend Protection Tests
# =====================================================================

def test_ensure_owner_guard():
    # Owner passes
    cb_owner = MagicMock(spec=CallbackQuery)
    cb_owner.from_user = User(id=1017867274, is_bot=False, first_name="Owner")
    cb_owner.answer = AsyncMock()
    assert asyncio.run(ensure_owner(cb_owner)) is True
    cb_owner.answer.assert_not_awaited()

    # Headman rejected with alert
    set_headman_telegram_id(333444)
    cb_headman = MagicMock(spec=CallbackQuery)
    cb_headman.from_user = User(id=333444, is_bot=False, first_name="Headman")
    cb_headman.answer = AsyncMock()
    assert asyncio.run(ensure_owner(cb_headman)) is False
    cb_headman.answer.assert_awaited_once_with("⚠️ Это действие доступно только владельцу бота.", show_alert=True)

    # Message from headman rejected with reply
    msg_headman = MagicMock(spec=Message)
    msg_headman.from_user = User(id=333444, is_bot=False, first_name="Headman")
    msg_headman.answer = AsyncMock()
    assert asyncio.run(ensure_owner(msg_headman)) is False
    msg_headman.answer.assert_awaited_once_with("⚠️ Это действие доступно только владельцу бота.")


def test_headman_cannot_stop_recording():
    set_headman_telegram_id(333444)
    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = User(id=333444, is_bot=False, first_name="Headman")
    cb.data = "btn_stop"
    cb.answer = AsyncMock()

    asyncio.run(handle_stop(cb))
    cb.answer.assert_awaited_once_with("⚠️ Это действие доступно только владельцу бота.", show_alert=True)


def test_headman_cannot_run_ai_or_postproc():
    set_headman_telegram_id(333444)
    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = User(id=333444, is_bot=False, first_name="Headman")
    cb.data = "sess_run_ai:sess_123"
    cb.answer = AsyncMock()

    asyncio.run(handle_session_run_ai(cb))
    cb.answer.assert_awaited_once_with("⚠️ Это действие доступно только владельцу бота.", show_alert=True)

    cb.reset_mock()
    cb.data = "sess_postproc:sess_123"
    asyncio.run(handle_session_postprocess(cb))
    cb.answer.assert_awaited_once_with("⚠️ Это действие доступно только владельцу бота.", show_alert=True)


def test_headman_cannot_restore_slides_or_delete_session():
    set_headman_telegram_id(333444)
    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = User(id=333444, is_bot=False, first_name="Headman")
    cb.answer = AsyncMock()

    cb.data = "sess_restore_slides:sess_123"
    asyncio.run(handle_session_restore_slides(cb))
    cb.answer.assert_awaited_once_with("⚠️ Это действие доступно только владельцу бота.", show_alert=True)

    cb.reset_mock()
    cb.data = "sess_del_confirm:sess_123"
    asyncio.run(handle_session_del_confirm(cb))
    cb.answer.assert_awaited_once_with("⚠️ Это действие доступно только владельцу бота.", show_alert=True)

    cb.reset_mock()
    cb.data = "sess_del_do:sess_123"
    asyncio.run(handle_session_del_do(cb))
    cb.answer.assert_awaited_once_with("⚠️ Это действие доступно только владельцу бота.", show_alert=True)


def test_headman_cannot_modify_schedule_or_add_files():
    set_headman_telegram_id(333444)
    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = User(id=333444, is_bot=False, first_name="Headman")
    cb.answer = AsyncMock()

    cb.data = "sched_add_day"
    asyncio.run(handle_sched_add_day(cb))
    cb.answer.assert_awaited_once_with("⚠️ Это действие доступно только владельцу бота.", show_alert=True)

    cb.reset_mock()
    cb.data = "sess_add_files:sess_123"
    asyncio.run(handle_session_add_files(cb))
    cb.answer.assert_awaited_once_with("⚠️ Это действие доступно только владельцу бота.", show_alert=True)


def test_headman_cannot_trigger_meeting_links():
    set_headman_telegram_id(333444)
    msg = MagicMock(spec=Message)
    msg.from_user = User(id=333444, is_bot=False, first_name="Headman")
    msg.chat = Chat(id=333444, type="private")
    msg.text = "https://telemost.yandex.ru/j/1234567890"
    msg.answer = AsyncMock()

    asyncio.run(handle_text_message(msg))
    msg.answer.assert_awaited_once()
    assert "только владельцу бота" in msg.answer.call_args[0][0]


def test_headman_cannot_upload_materials():
    set_headman_telegram_id(333444)
    msg = MagicMock(spec=Message)
    msg.from_user = User(id=333444, is_bot=False, first_name="Headman")
    msg.chat = Chat(id=333444, type="private")
    msg.document = MagicMock()
    msg.audio = None
    msg.answer = AsyncMock()

    asyncio.run(handle_document_or_audio(msg))
    msg.answer.assert_awaited_once()
    assert "доступна только владельцу бота" in msg.answer.call_args[0][0]


# =====================================================================
# 5. Owner Headman Management Flow Tests
# =====================================================================

def test_owner_headman_management_flow():
    # 1. Owner clicks "settings_headman" when no headman set
    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = User(id=1017867274, is_bot=False, first_name="Owner")
    cb.message = MagicMock()
    cb.message.chat = Chat(id=1017867274, type="private")
    cb.message.edit_text = AsyncMock()
    cb.answer = AsyncMock()

    asyncio.run(handle_settings_headman(cb))
    cb.answer.assert_awaited_once()
    cb.message.edit_text.assert_awaited_once()
    assert "Не назначен" in cb.message.edit_text.call_args[0][0]

    # 2. Owner clicks "settings_headman_set"
    cb.reset_mock()
    cb.message.edit_text.reset_mock()
    asyncio.run(handle_settings_headman_set(cb))
    assert USER_SESSION_STATES[1017867274]["action"] == "waiting_headman_id"
    assert "Telegram ID" in cb.message.edit_text.call_args[0][0]

    # 3. Owner enters non-numeric text
    msg = MagicMock(spec=Message)
    msg.from_user = User(id=1017867274, is_bot=False, first_name="Owner")
    msg.chat = Chat(id=1017867274, type="private")
    msg.text = "not_a_number"
    msg.answer = AsyncMock()

    asyncio.run(handle_text_message(msg))
    msg.answer.assert_awaited_once()
    assert "должен состоять только из цифр" in msg.answer.call_args[0][0]

    # 4. Owner enters owner's own ID
    msg.reset_mock()
    msg.text = "1017867274"
    asyncio.run(handle_text_message(msg))
    msg.answer.assert_awaited_once()
    assert "уже является владельцем бота" in msg.answer.call_args[0][0]

    # 5. Owner enters valid headman ID
    msg.reset_mock()
    msg.text = "123987456"
    asyncio.run(handle_text_message(msg))
    msg.answer.assert_awaited_once()
    assert "Староста успешно назначен!" in msg.answer.call_args[0][0]
    assert get_headman_telegram_id() == 123987456
    assert 1017867274 not in USER_SESSION_STATES

    # 6. Check headman settings view now shows active headman
    cb.reset_mock()
    cb.message.edit_text.reset_mock()
    asyncio.run(handle_settings_headman(cb))
    assert "123987456" in cb.message.edit_text.call_args[0][0]

    # 7. Owner removes headman
    cb.reset_mock()
    cb.message.edit_text.reset_mock()
    asyncio.run(handle_settings_headman_remove(cb))
    assert get_headman_telegram_id() is None
    assert cb.answer.call_args_list[0].args == ("✅ Доступ старосты отозван",)
    assert cb.answer.call_args_list[0].kwargs == {"show_alert": True}
    assert "Не назначен" in cb.message.edit_text.call_args[0][0]
