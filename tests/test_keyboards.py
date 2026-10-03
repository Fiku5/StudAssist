import pytest
from bot.keyboards import (
    make_back_button,
    get_back_keyboard,
    get_settings_keyboard,
    get_session_card_keyboard,
    get_sessions_list_keyboard,
    get_sessions_subjects_keyboard,
    get_delete_confirm_keyboard,
    get_attachment_mode_keyboard,
    get_schedule_menu_keyboard,
    get_schedule_day_picker_keyboard,
    get_schedule_time_picker_keyboard,
    get_schedule_subject_picker_keyboard,
    get_schedule_links_keyboard,
    get_schedule_slot_detail_keyboard,
    get_quick_pair_confirm_keyboard,
    get_main_menu_keyboard,
    get_attendance_keyboard,
)
from recorder.schedule_manager import Subject, ScheduleEntry


def test_session_card_keyboard():
    kb = get_session_card_keyboard(
        session_id="session_123",
        has_audio=True,
        slides_count=5,
        attachments_count=2,
        github_url="https://github.com/example/repo",
        subject_slug="arhitektura_po",
    )
    callbacks = [btn.callback_data for row in kb.inline_keyboard for btn in row if btn.callback_data]
    urls = [btn.url for row in kb.inline_keyboard for btn in row if btn.url]

    assert "sess_slides:session_123" in callbacks
    assert "sess_audio:session_123" in callbacks
    assert "sess_add_files:session_123" in callbacks
    assert "sess_pick_subj:session_123" in callbacks
    assert "sess_set_topic:session_123" in callbacks
    assert "sess_postproc:session_123" in callbacks
    assert "sess_run_ai:session_123" in callbacks
    assert "https://github.com/example/repo" in urls
    assert "sess_del_confirm:session_123" in callbacks
    assert "sess_sub:arhitektura_po:0" in callbacks

    # Verify blue back button at the very bottom
    back_btn = kb.inline_keyboard[-1][0]
    assert back_btn.callback_data == "sess_sub:arhitektura_po:0"
    assert back_btn.style == "primary"

    # Verify backup rollback button appears when has_slides_backup=True
    kb_with_backup = get_session_card_keyboard(
        session_id="session_123",
        has_audio=True,
        slides_count=5,
        has_slides_backup=True,
    )
    callbacks_backup = [btn.callback_data for row in kb_with_backup.inline_keyboard for btn in row if btn.callback_data]
    assert "sess_restore_slides:session_123" in callbacks_backup

    kb_no_backup = get_session_card_keyboard(
        session_id="session_123",
        has_audio=True,
        slides_count=5,
        has_slides_backup=False,
    )
    callbacks_no_backup = [btn.callback_data for row in kb_no_backup.inline_keyboard for btn in row if btn.callback_data]
    assert "sess_restore_slides:session_123" not in callbacks_no_backup


def test_sessions_list_keyboard_pagination():
    sessions = [
        {"session_id": f"sess_{i}", "created_at": "2026-09-10 12:00:00", "subject": f"Subj {i}", "slides_count": i}
        for i in range(12)
    ]

    kb_p0 = get_sessions_list_keyboard(sessions, subject_slug="all", page=0, per_page=5)
    # 5 items + 1 nav row + 1 refresh row + 1 back row = 8 rows
    assert len(kb_p0.inline_keyboard) == 8

    nav_row = kb_p0.inline_keyboard[5]
    nav_callbacks = [btn.callback_data for btn in nav_row]
    assert "sess_sub:all:1" in nav_callbacks  # next page button
    assert "sess_noop" in nav_callbacks       # page indicator

    back_row = kb_p0.inline_keyboard[-1]
    back_callbacks = [btn.callback_data for btn in back_row]
    assert "sess_cats" in back_callbacks      # back to subject catalog
    assert back_row[0].style == "primary"


def test_sessions_subjects_keyboard():
    summary = [
        {"slug": "arhitektura_po", "title": "Архитектура ПО", "count": 3},
        {"slug": "bazy_dannykh", "title": "Базы данных", "count": 2},
    ]
    kb = get_sessions_subjects_keyboard(summary, total_sessions_count=5)
    callbacks = [btn.callback_data for row in kb.inline_keyboard for btn in row if btn.callback_data]

    assert "sess_sub:arhitektura_po:0" in callbacks
    assert "sess_sub:bazy_dannykh:0" in callbacks
    assert "sess_sub:all:0" in callbacks
    assert "sess_cats" in callbacks


def test_schedule_keyboards():
    # 1. Main schedule menu
    kb_menu = get_schedule_menu_keyboard(has_entries=True)
    menu_callbacks = [btn.callback_data for row in kb_menu.inline_keyboard for btn in row if btn.callback_data]
    assert "sched_add_day" in menu_callbacks
    assert "sched_subjs_view" in menu_callbacks
    assert "sched_links_pick" in menu_callbacks
    assert "sched_del_pick" in menu_callbacks

    # 2. Day picker
    kb_days = get_schedule_day_picker_keyboard()
    day_callbacks = [btn.callback_data for row in kb_days.inline_keyboard for btn in row if btn.callback_data]
    assert "sched_day:0" in day_callbacks  # Monday
    assert "sched_day:4" in day_callbacks  # Friday

    # 3. Time picker
    kb_times = get_schedule_time_picker_keyboard(day_idx=0)
    time_callbacks = [btn.callback_data for row in kb_times.inline_keyboard for btn in row if btn.callback_data]
    assert "sched_time:0:09-00" in time_callbacks
    assert "sched_time:0:10-45" in time_callbacks

    # 4. Subject picker
    test_subjs = [
        Subject(id="os", title="Операционные системы", short_name="ОС", color_icon="⚡️")
    ]
    kb_subj = get_schedule_subject_picker_keyboard(test_subjs, day_idx=0, time_str="09:10")
    subj_callbacks = [btn.callback_data for row in kb_subj.inline_keyboard for btn in row if btn.callback_data]
    assert "sched_entry_subj:0:09-10:os" in subj_callbacks

    # 5. Schedule links picker keyboard
    test_entries = [
        ScheduleEntry(id="sched_1", subject_id="os", day_of_week=0, time_str="09:10", url="https://link.com"),
        ScheduleEntry(id="sched_2", subject_id="os", day_of_week=1, time_str="10:45", url=""),
    ]
    subjs_map = {"os": test_subjs[0]}
    kb_links = get_schedule_links_keyboard(test_entries, subjs_map)
    links_callbacks = [btn.callback_data for row in kb_links.inline_keyboard for btn in row if btn.callback_data]
    assert "sched_link_detail:sched_1" in links_callbacks
    assert "sched_link_detail:sched_2" in links_callbacks

    # 6. Schedule slot detail keyboard (with URL)
    kb_detail_with_url = get_schedule_slot_detail_keyboard("sched_1", url="https://link.com")
    detail_cbs = [btn.callback_data for row in kb_detail_with_url.inline_keyboard for btn in row if btn.callback_data]
    detail_urls = [btn.url for row in kb_detail_with_url.inline_keyboard for btn in row if btn.url]
    assert "sched_link_edit:sched_1" in detail_cbs
    assert "sched_link_clear:sched_1" in detail_cbs
    assert "https://link.com" in detail_urls

    # 7. Schedule slot detail keyboard (without URL)
    kb_detail_no_url = get_schedule_slot_detail_keyboard("sched_2", url="")
    detail_cbs_no_url = [btn.callback_data for row in kb_detail_no_url.inline_keyboard for btn in row if btn.callback_data]
    assert "sched_link_edit:sched_2" in detail_cbs_no_url
    assert "sched_link_clear:sched_2" not in detail_cbs_no_url


def test_quick_pair_confirm_keyboard():
    kb = get_quick_pair_confirm_keyboard(subject_id="os", subject_short_title="ОС")
    callbacks = [btn.callback_data for row in kb.inline_keyboard for btn in row if btn.callback_data]
    assert "pair_quick:os" in callbacks
    assert "pair_quick_pick" in callbacks
    assert "pair_quick:unsorted" in callbacks
    assert kb.inline_keyboard[-1][0].style == "primary"


def test_back_button_and_settings_keyboards():
    # 1. make_back_button
    btn = make_back_button(text="⬅️ Назад", callback_data="main_menu")
    assert btn.text == "⬅️ Назад"
    assert btn.callback_data == "main_menu"
    assert btn.style == "primary"

    # 2. get_back_keyboard
    kb_back = get_back_keyboard(callback_data="settings_menu", text="⬅️ В настройки")
    assert len(kb_back.inline_keyboard) == 1
    assert kb_back.inline_keyboard[0][0].callback_data == "settings_menu"
    assert kb_back.inline_keyboard[0][0].style == "primary"

    # 3. get_settings_keyboard
    kb_settings = get_settings_keyboard()
    callbacks = [b.callback_data for row in kb_settings.inline_keyboard for b in row if b.callback_data]
    assert "settings_change_name" in callbacks
    assert "main_menu" in callbacks
    # Bottom button is blue back button
    assert kb_settings.inline_keyboard[-1][0].callback_data == "main_menu"
    assert kb_settings.inline_keyboard[-1][0].style == "primary"


def test_attendance_keyboard_and_card_button():
    # 1. get_attendance_keyboard
    kb_att = get_attendance_keyboard("session_456")
    assert len(kb_att.inline_keyboard) == 1
    assert kb_att.inline_keyboard[0][0].callback_data == "sess_view:session_456"
    assert kb_att.inline_keyboard[0][0].style == "primary"

    # 2. get_session_card_keyboard with participants
    kb_card_att = get_session_card_keyboard(
        session_id="session_456",
        has_audio=True,
        slides_count=3,
        participants_count=18,
    )
    callbacks = [btn.callback_data for row in kb_card_att.inline_keyboard for btn in row if btn.callback_data]
    assert "sess_attendance:session_456" in callbacks
    # Button text has student count
    att_btn = [btn for row in kb_card_att.inline_keyboard for btn in row if btn.callback_data == "sess_attendance:session_456"][0]
    assert "18" in att_btn.text

    # 3. get_session_card_keyboard without participants
    kb_card_no_att = get_session_card_keyboard(
        session_id="session_456",
        has_audio=True,
        slides_count=3,
        participants_count=0,
    )
    callbacks_no = [btn.callback_data for row in kb_card_no_att.inline_keyboard for btn in row if btn.callback_data]
    assert "sess_attendance:session_456" not in callbacks_no


def test_main_menu_keyboard_dynamic_recording_mode():
    # Idle mode
    kb_idle = get_main_menu_keyboard(is_recording=False)
    callbacks_idle = [btn.callback_data for row in kb_idle.inline_keyboard for btn in row if btn.callback_data]
    assert "btn_screenshot" not in callbacks_idle
    assert "btn_stop" not in callbacks_idle
    assert "sess_cats" in callbacks_idle

    # Recording mode
    kb_rec = get_main_menu_keyboard(is_recording=True)
    callbacks_rec = [btn.callback_data for row in kb_rec.inline_keyboard for btn in row if btn.callback_data]
    assert "btn_screenshot" in callbacks_rec
    assert "btn_stop" in callbacks_rec
    assert kb_rec.inline_keyboard[0][0].callback_data == "btn_screenshot"
    assert kb_rec.inline_keyboard[0][1].callback_data == "btn_stop"

