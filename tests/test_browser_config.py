import pytest
from pathlib import Path

from config import settings
from recorder.browser import CHROMIUM_ARGS, MeetingRecorder
from recorder.audio import AudioRecorder


def test_chromium_launch_flags():
    """Verifies critical stealth and media-bypass flags are present."""
    assert "--disable-blink-features=AutomationControlled" in CHROMIUM_ARGS
    assert "--use-fake-ui-for-media-stream" in CHROMIUM_ARGS
    assert "--autoplay-policy=no-user-gesture-required" in CHROMIUM_ARGS
    assert "--no-sandbox" in CHROMIUM_ARGS
    assert "--disable-background-timer-throttling" in CHROMIUM_ARGS
    assert "--disable-backgrounding-occluded-windows" in CHROMIUM_ARGS
    assert "--disable-renderer-backgrounding" in CHROMIUM_ARGS
    assert "--disable-quic" in CHROMIUM_ARGS
    assert "--disable-features=PostQuantumKyber,PostQuantumMLKEM" in CHROMIUM_ARGS
    assert "--alsa-output-device=pulse" not in CHROMIUM_ARGS


def test_ensure_xvfb():
    """Verifies ensure_xvfb runs cleanly and returns empty string on non-Linux."""
    from recorder.browser import ensure_xvfb
    res = ensure_xvfb(":99")
    import platform
    if platform.system() != "Linux":
        assert res == ""


def test_pulseaudio_setup_methods():
    """Verifies PulseAudio ensure methods and server URI constants."""
    from recorder.audio import PULSE_SOCKET_PATH, PULSE_SERVER_URI
    assert PULSE_SOCKET_PATH == "/tmp/pulse-socket"
    assert PULSE_SERVER_URI == "unix:/tmp/pulse-socket"
    # Calling ensure_pulseaudio on non-Linux should be a graceful no-op
    AudioRecorder.ensure_pulseaudio()


def test_audio_recorder_ffmpeg_args(tmp_path):
    """Verifies ffmpeg command is built with speech-optimized parameters."""
    rec = AudioRecorder(output_path=tmp_path / "test.mp3", pulse_source="VirtualSink.monitor")
    cmd = rec._build_ffmpeg_command()

    assert "-ac" in cmd
    idx_ac = cmd.index("-ac")
    assert cmd[idx_ac + 1] == "1"  # Mono

    assert "-ar" in cmd
    idx_ar = cmd.index("-ar")
    assert cmd[idx_ar + 1] == "16000"  # 16 kHz

    assert "-b:a" in cmd
    idx_ba = cmd.index("-b:a")
    assert cmd[idx_ba + 1] == "64k"  # 64 kbps


def test_config_stage3_settings():
    """Verifies default values for Stage 3 settings."""
    assert isinstance(settings.student_name, str)
    assert settings.max_lecture_duration_min >= 60
    assert settings.min_lecture_duration_min >= 30
    assert settings.participant_drop_threshold >= 1


def test_student_name_customization_and_persistence(tmp_path, monkeypatch):
    """Verifies student name customization and JSON persistence."""
    from config import load_user_settings, save_user_setting, set_student_name
    test_settings_file = tmp_path / "user_settings.json"
    monkeypatch.setattr("config.USER_SETTINGS_FILE", test_settings_file)

    # Initial state
    assert load_user_settings() == {}

    # Change student name
    saved = set_student_name("Алексей Смирнов")
    assert saved == "Алексей Смирнов"
    assert settings.student_name == "Алексей Смирнов"
    assert test_settings_file.exists()

    # Re-reading settings from file
    loaded = load_user_settings()
    assert loaded.get("student_name") == "Алексей Смирнов"



@pytest.mark.anyio
async def test_is_meeting_ended_detects_mts_link_markers():
    from unittest.mock import AsyncMock, MagicMock
    rec = MeetingRecorder()
    rec.is_connected = True
    mock_page = MagicMock()
    mock_page.url = "https://my.mts-link.ru/j/12345"
    mock_page.evaluate = AsyncMock(return_value=None)
    mock_page.content = AsyncMock(return_value="""
        <html><body>
            <h1>Создайте личный кабинет для доступа ко всем материалам ваших встреч</h1>
            <button>Новая встреча</button>
            <div>Запись встречи, заметки, расшифровка</div>
        </body></html>
    """)
    rec.page = mock_page

    assert await rec.is_meeting_ended() is True


@pytest.mark.anyio
async def test_is_meeting_ended_false_in_active_stream_room():
    from unittest.mock import AsyncMock, MagicMock
    rec = MeetingRecorder()
    rec.is_connected = True
    mock_page = MagicMock()
    mock_page.url = "https://my.mts-link.ru/j/12345/stream"
    # is_room_active is True, kick_modal is None
    mock_page.evaluate = AsyncMock(side_effect=[True, None])
    mock_page.content = AsyncMock(return_value="<html><body><div>расшифровка</div></body></html>")
    rec.page = mock_page

    assert await rec.is_meeting_ended() is False


@pytest.mark.anyio
async def test_is_meeting_ended_returns_false_on_transient_exception():
    from unittest.mock import AsyncMock, MagicMock
    rec = MeetingRecorder()
    rec.is_connected = True
    mock_page = MagicMock()
    mock_page.url = "https://my.mts-link.ru/j/12345/stream"
    mock_page.is_closed.return_value = False
    # Simulate CDP timeout or execution context destroyed in evaluate/content
    mock_page.evaluate = AsyncMock(side_effect=Exception("Execution context was destroyed"))
    mock_page.content = AsyncMock(side_effect=Exception("CDP request timed out"))
    rec.page = mock_page

    # Must return False rather than killing the meeting recording!
    assert await rec.is_meeting_ended() is False


@pytest.mark.anyio
async def test_join_meeting_retry_on_navigation_error(monkeypatch):
    from unittest.mock import AsyncMock, MagicMock
    rec = MeetingRecorder()
    rec.start = AsyncMock()
    mock_page = MagicMock()
    # First attempt raises net::ERR_TIMED_OUT, second attempt succeeds
    mock_page.goto = AsyncMock(side_effect=[Exception("net::ERR_TIMED_OUT"), None])
    rec.page = mock_page
    rec._join_mts_link = AsyncMock(return_value=True)

    monkeypatch.setattr("asyncio.sleep", AsyncMock())

    success = await rec.join_meeting(url="https://my.mts-link.ru/j/123/456", student_name="Павел")
    assert success is True
    assert mock_page.goto.call_count == 2
    rec._join_mts_link.assert_awaited_once_with("Павел")


@pytest.mark.anyio
async def test_join_mts_link_webinar_registration(monkeypatch):
    from unittest.mock import AsyncMock, MagicMock
    rec = MeetingRecorder()
    mock_page = MagicMock()
    rec.page = mock_page

    reg_btn = MagicMock()
    reg_btn.is_visible = AsyncMock(return_value=True)
    reg_btn.click = AsyncMock()

    email_input = MagicMock()
    email_input.wait_for = AsyncMock()
    email_input.fill = AsyncMock()

    name_input = MagicMock()
    name_input.fill = AsyncMock()

    second_name_input = MagicMock()
    second_name_input.is_visible = AsyncMock(return_value=True)
    second_name_input.fill = AsyncMock()

    submit_btn = MagicMock()
    submit_btn.click = AsyncMock()

    pre_join_btn = MagicMock()
    pre_join_btn.is_visible = AsyncMock(return_value=True)
    pre_join_btn.click = AsyncMock()

    def locator_side_effect(selector):
        loc = MagicMock()
        if "RegistrationLanding.mainButton" in selector or "ЗАРЕГИСТРИРОВАТЬСЯ" in selector:
            loc.first = reg_btn
            loc.last = submit_btn
        elif 'input[name="email"]' in selector:
            loc.first = email_input
        elif 'input[name="name"]' in selector:
            loc.first = name_input
        elif 'input[name="secondName"]' in selector:
            loc.first = second_name_input
        elif "ПРИСОЕДИНИТЬСЯ К ВСТРЕЧЕ" in selector:
            loc.first = pre_join_btn
        else:
            loc.first = MagicMock(is_visible=AsyncMock(return_value=False))
            loc.last = MagicMock(is_visible=AsyncMock(return_value=False))
        return loc

    mock_page.locator = MagicMock(side_effect=locator_side_effect)
    mock_page.evaluate = AsyncMock(return_value=True)
    mock_page.mouse = MagicMock(click=AsyncMock())
    rec.dismiss_popups = AsyncMock()

    monkeypatch.setattr("asyncio.sleep", AsyncMock())

    success = await rec._join_mts_link("Влад Коротов")
    assert success is True
    reg_btn.click.assert_awaited()
    name_input.fill.assert_awaited_with("Влад")
    second_name_input.fill.assert_awaited_with("Коротов")
    submit_btn.click.assert_awaited()
    pre_join_btn.click.assert_awaited()


@pytest.mark.anyio
async def test_meeting_recorder_context_grants_microphone_only_and_hardens_webrtc(monkeypatch):
    """Verifies MeetingRecorder initializes context with microphone only (NO camera) and injects WebRTC hardening script."""
    from unittest.mock import AsyncMock, MagicMock
    rec = MeetingRecorder()
    mock_browser = MagicMock()
    mock_context = MagicMock()
    mock_context.new_page = AsyncMock()
    mock_context.add_init_script = AsyncMock()
    mock_browser.new_context = AsyncMock(return_value=mock_context)
    
    mock_playwright = MagicMock()
    mock_playwright.chromium.launch = AsyncMock(return_value=mock_browser)
    
    monkeypatch.setattr("recorder.browser.async_playwright", lambda: MagicMock(start=AsyncMock(return_value=mock_playwright)))
    
    await rec.start()
    
    mock_browser.new_context.assert_awaited_once()
    _, kwargs = mock_browser.new_context.call_args
    assert "permissions" in kwargs
    assert "microphone" in kwargs["permissions"]
    assert "camera" not in kwargs.get("permissions", [])

    # Verify WebRTC hardening script was injected via add_init_script
    assert mock_context.add_init_script.call_count >= 1
    injected_scripts = [call.args[0] for call in mock_context.add_init_script.call_args_list]
    combined_script = "\n".join(injected_scripts)
    assert "videoinput" in combined_script
    assert "enumerateDevices" in combined_script
    assert "getUserMedia" in combined_script


@pytest.mark.anyio
async def test_join_mts_link_prioritizes_no_devices_over_standard_join(monkeypatch):
    """Verifies MTS Link join strictly prioritizes 'ПРИСОЕДИНИТЬСЯ БЕЗ УСТРОЙСТВ' over full device join."""
    from unittest.mock import AsyncMock, MagicMock
    rec = MeetingRecorder()
    mock_page = MagicMock()
    rec.page = mock_page

    no_dev_btn = MagicMock(is_visible=AsyncMock(return_value=True), click=AsyncMock())
    standard_join_btn = MagicMock(is_visible=AsyncMock(return_value=True), click=AsyncMock())

    def locator_side_effect(selector):
        loc = MagicMock()
        if "БЕЗ УСТРОЙСТВ" in selector or "без устройств" in selector:
            loc.first = no_dev_btn
        elif "ПРИСОЕДИНИТЬСЯ К ВСТРЕЧЕ" in selector or "Присоединиться к встрече" in selector:
            loc.first = standard_join_btn
        else:
            loc.first = MagicMock(is_visible=AsyncMock(return_value=False))
            loc.last = MagicMock(is_visible=AsyncMock(return_value=False))
        return loc

    mock_page.locator = MagicMock(side_effect=locator_side_effect)
    mock_page.evaluate = AsyncMock(return_value=True)
    mock_page.mouse = MagicMock(click=AsyncMock())
    rec.dismiss_popups = AsyncMock()
    rec.ensure_participants_panel_closed = AsyncMock()
    rec._mute_mts_in_room_devices = AsyncMock()

    monkeypatch.setattr("asyncio.sleep", AsyncMock())

    success = await rec._join_mts_link("Павел Репецкий")
    assert success is True
    # 'БЕЗ УСТРОЙСТВ' button must be clicked
    no_dev_btn.click.assert_awaited()
    # 'ПРИСОЕДИНИТЬСЯ К ВСТРЕЧЕ' must NEVER be clicked when 'БЕЗ УСТРОЙСТВ' is present
    standard_join_btn.click.assert_not_awaited()
    # Post-join in-room safety mute must be invoked
    rec._mute_mts_in_room_devices.assert_awaited()


@pytest.mark.anyio
async def test_join_mts_link_dismisses_without_camera_modal(monkeypatch):
    """Verifies MTS Link join specifically clicks 'ПРИСОЕДИНИТЬСЯ БЕЗ КАМЕРЫ' on device check modal."""
    from unittest.mock import AsyncMock, MagicMock
    rec = MeetingRecorder()
    mock_page = MagicMock()
    rec.page = mock_page

    no_cam_btn = MagicMock(is_visible=AsyncMock(return_value=True), click=AsyncMock())
    standard_join_btn = MagicMock(is_visible=AsyncMock(return_value=False), click=AsyncMock())

    def locator_side_effect(selector):
        loc = MagicMock()
        if "БЕЗ КАМЕРЫ" in selector or "без камеры" in selector:
            loc.first = no_cam_btn
        elif "ПРИСОЕДИНИТЬСЯ К ВСТРЕЧЕ" in selector:
            loc.first = standard_join_btn
        else:
            loc.first = MagicMock(is_visible=AsyncMock(return_value=False))
            loc.last = MagicMock(is_visible=AsyncMock(return_value=False))
        return loc

    mock_page.locator = MagicMock(side_effect=locator_side_effect)
    mock_page.evaluate = AsyncMock(return_value=True)
    mock_page.mouse = MagicMock(click=AsyncMock())
    rec.dismiss_popups = AsyncMock()
    rec.ensure_participants_panel_closed = AsyncMock()
    rec._mute_mts_in_room_devices = AsyncMock()

    monkeypatch.setattr("asyncio.sleep", AsyncMock())

    success = await rec._join_mts_link("Павел Репецкий")
    assert success is True
    # 'БЕЗ КАМЕРЫ' button must be clicked
    no_cam_btn.click.assert_awaited()


@pytest.mark.anyio
async def test_mute_mts_in_room_and_prejoin_devices():
    """Verifies _mute_mts_prejoin_devices and _mute_mts_in_room_devices execute safe DOM scripts."""
    from unittest.mock import AsyncMock, MagicMock
    rec = MeetingRecorder()
    mock_page = MagicMock()
    mock_page.evaluate = AsyncMock()
    rec.page = mock_page

    await rec._mute_mts_prejoin_devices()
    mock_page.evaluate.assert_awaited_once()

    mock_page.evaluate.reset_mock()
    await rec._mute_mts_in_room_devices()
    mock_page.evaluate.assert_awaited_once()


@pytest.mark.anyio
async def test_join_telemost_rejects_false_positive_when_in_lobby(monkeypatch):
    """Verifies _join_telemost returns False instead of falsely reporting success when room is not entered."""
    from unittest.mock import AsyncMock, MagicMock
    rec = MeetingRecorder()
    mock_page = MagicMock()
    rec.page = mock_page
    
    # Simulate iframe mode
    mock_frame = MagicMock()
    mock_frame.url = "https://telemost.360.yandex.ru/private-join/12345"
    mock_frame.evaluate = AsyncMock(return_value=None)
    mock_frame.content = AsyncMock(return_value="<html><body>Lobby device check</body></html>")
    mock_page.frames = [mock_page.main_frame, mock_frame]
    
    fl = MagicMock()
    name_input = MagicMock(wait_for=AsyncMock(), fill=AsyncMock())
    mic_btn = MagicMock(is_visible=AsyncMock(return_value=False))
    cam_btn = MagicMock(is_visible=AsyncMock(return_value=False))
    join_btn = MagicMock(wait_for=AsyncMock(), click=AsyncMock())
    
    # In-room controls are 0 (not in room)
    async def frame_eval_lobby(script, *args):
        if "hangup" in script:
            return 0
        return None
        
    mock_frame.evaluate = AsyncMock(side_effect=frame_eval_lobby)
    
    def fl_locator_side_effect(selector):
        loc = MagicMock()
        if "orb-textinput" in selector or "имя" in selector or 'input[type="text"]' in selector:
            loc.first = name_input
        elif "turn-off-mic" in selector:
            loc.first = mic_btn
        elif "turn-off-camera" in selector:
            loc.first = cam_btn
        elif "enter-conference-button" in selector or "Подключиться" in selector:
            loc.first = join_btn
        else:
            loc.first = MagicMock(is_visible=AsyncMock(return_value=False))
            loc.count = AsyncMock(return_value=0)
        return loc
        
    fl.locator = MagicMock(side_effect=fl_locator_side_effect)
    mock_page.frame_locator = MagicMock(return_value=fl)
    mock_page.evaluate = AsyncMock(return_value=False)
    mock_page.screenshot = AsyncMock()
    
    monkeypatch.setattr("asyncio.sleep", AsyncMock())
    
    success = await rec._join_telemost("Влад Коротов")
    assert success is False
    assert rec.is_connected is False


@pytest.mark.anyio
async def test_join_telemost_confirms_entry_when_in_room(monkeypatch):
    """Verifies _join_telemost returns True when in-room hangup button is detected."""
    from unittest.mock import AsyncMock, MagicMock
    rec = MeetingRecorder()
    mock_page = MagicMock()
    rec.page = mock_page
    
    mock_frame = MagicMock()
    mock_frame.url = "https://telemost.360.yandex.ru/private-join/12345"
    async def frame_eval_room(script, *args):
        if "hangup" in script:
            return 1
        return None
    mock_frame.evaluate = AsyncMock(side_effect=frame_eval_room)
    mock_frame.content = AsyncMock(return_value="<html><body>In call room</body></html>")
    mock_page.frames = [mock_page.main_frame, mock_frame]
    
    fl = MagicMock()
    name_input = MagicMock(wait_for=AsyncMock(), fill=AsyncMock())
    join_btn = MagicMock(wait_for=AsyncMock(), click=AsyncMock())
    
    def fl_locator_side_effect(selector):
        loc = MagicMock()
        if "orb-textinput" in selector or "имя" in selector or 'input[type="text"]' in selector:
            loc.first = name_input
        elif "enter-conference-button" in selector or "Подключиться" in selector:
            loc.first = join_btn
        else:
            loc.first = MagicMock(is_visible=AsyncMock(return_value=False))
            loc.count = AsyncMock(return_value=0)
        return loc
        
    fl.locator = MagicMock(side_effect=fl_locator_side_effect)
    mock_page.frame_locator = MagicMock(return_value=fl)
    mock_page.evaluate = AsyncMock(return_value=False)
    mock_page.keyboard = MagicMock(press=AsyncMock())
    mock_page.mouse = MagicMock(click=AsyncMock())
    rec.dismiss_popups = AsyncMock()
    rec.ensure_participants_panel_closed = AsyncMock()
    
    monkeypatch.setattr("asyncio.sleep", AsyncMock())
    
    success = await rec._join_telemost("Влад Коротов")
    assert success is True



