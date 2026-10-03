import asyncio
import logging
import os
import platform
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Optional, List
from playwright.async_api import async_playwright, Browser, Page, Playwright

from config import settings, TEMP_DIR
from recorder.audio import AudioRecorder, PULSE_SERVER_URI

logger = logging.getLogger(__name__)


def ensure_xvfb(display: str = ":99") -> str:
    """
    Ensures an Xvfb virtual display server is active on Linux.
    Sets os.environ["DISPLAY"] = display and launches Xvfb if not already running.
    Allows Chromium to run in headful mode on headless servers, activating the full desktop audio pipeline.
    """
    if platform.system() != "Linux":
        return ""

    target_display = os.environ.get("DISPLAY") or display
    display_num = target_display.lstrip(":")

    is_alive = False
    try:
        check = subprocess.run(
            ["xdpyinfo", "-display", target_display],
            capture_output=True,
            timeout=2,
        )
        if check.returncode == 0:
            is_alive = True
    except Exception:
        pass

    if not is_alive:
        try:
            pg = subprocess.run(["pgrep", "-f", f"Xvfb.*{target_display}"], capture_output=True, text=True, timeout=2)
            if pg.returncode == 0 and pg.stdout.strip():
                is_alive = True
        except Exception:
            pass

    if not is_alive:
        lock_file = Path(f"/tmp/.X{display_num}-lock")
        socket_file = Path(f"/tmp/.X11-unix/X{display_num}")
        try:
            if lock_file.exists():
                lock_file.unlink()
            if socket_file.exists():
                socket_file.unlink()
        except Exception as e:
            logger.debug(f"Cleaning Xvfb lock note: {e}")

        xvfb_bin = shutil.which("Xvfb")
        if not xvfb_bin:
            logger.warning("Xvfb binary not found. Attempting apt install xvfb...")
            try:
                subprocess.run(["apt-get", "update", "-y"], capture_output=True, timeout=30)
                subprocess.run(["apt-get", "install", "-y", "xvfb"], capture_output=True, timeout=60)
                xvfb_bin = shutil.which("Xvfb")
            except Exception as e:
                logger.error(f"Failed to auto-install xvfb: {e}")

        if xvfb_bin:
            logger.info(f"Starting Xvfb virtual display on {target_display}...")
            try:
                subprocess.Popen(
                    [
                        xvfb_bin, target_display,
                        "-screen", "0", "1920x1080x24",
                        "-ac",
                        "+extension", "GLX",
                        "+render",
                        "-noreset",
                        "-nolisten", "tcp",
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                time.sleep(1.0)
            except Exception as e:
                logger.warning(f"Could not start Xvfb: {e}")
        else:
            logger.error("Xvfb is not installed. Headful browser mode may fail.")

    os.environ["DISPLAY"] = target_display
    return target_display


def get_silent_audio_file() -> Path:
    """Creates a 1-second pure silence 16kHz mono WAV file for Chromium fake microphone."""
    silent_path = TEMP_DIR / "silent_mic.wav"
    silent_path.parent.mkdir(parents=True, exist_ok=True)
    if not silent_path.exists():
        import wave
        with wave.open(str(silent_path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(16000)
            wf.writeframes(b"\x00" * 32000)
    return silent_path


# Standard Chromium launch flags for silent WebRTC and unthrottled audio rendering
CHROMIUM_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--use-fake-ui-for-media-stream",
    "--use-fake-device-for-media-stream",
    f"--use-file-for-fake-audio-capture={get_silent_audio_file().resolve()}",
    "--autoplay-policy=no-user-gesture-required",
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--window-size=1920,1080",
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--disable-quic",
    "--disable-features=PostQuantumKyber,PostQuantumMLKEM",
]


def to_latin_slug(text: str) -> str:
    """Converts Russian/Cyrillic string to ASCII latin alphanumeric slug for valid email addresses."""
    charmap = {
        'а': 'a', 'б': 'b', 'в': 'v', 'г': 'g', 'д': 'd', 'е': 'e', 'ё': 'e',
        'ж': 'zh', 'з': 'z', 'и': 'i', 'й': 'y', 'к': 'k', 'л': 'l', 'м': 'm',
        'н': 'n', 'о': 'o', 'п': 'p', 'р': 'r', 'с': 's', 'т': 't', 'у': 'u',
        'ф': 'f', 'х': 'kh', 'ц': 'ts', 'ч': 'ch', 'ш': 'sh', 'щ': 'shch',
        'ъ': '', 'ы': 'y', 'ь': '', 'э': 'e', 'ю': 'yu', 'я': 'ya'
    }
    slug = ''.join(charmap.get(c, c) for c in text.lower())
    return ''.join(c for c in slug if c.isascii() and (c.isalnum() or c in '._-')) or "student"


class MeetingRecorder:
    """
    Playwright-based automated attendee for MTS Link and Yandex Telemost.
    Silently joins meetings under the student's name with cameras and microphones disabled.
    """

    def __init__(self, headless: bool = True):
        self.headless = headless
        self.playwright: Optional[Playwright] = None
        self.browser: Optional[Browser] = None
        self.page: Optional[Page] = None
        self.is_connected = False
        self.joined_name: Optional[str] = None

    async def start(self):
        """Initializes Playwright and launches Chromium with PulseAudio routing."""
        is_linux = platform.system() == "Linux"
        if is_linux:
            AudioRecorder.ensure_pulseaudio()
            ensure_xvfb()

        launch_env = dict(os.environ)
        if is_linux:
            launch_env["PULSE_SERVER"] = PULSE_SERVER_URI
            launch_env["PULSE_SINK"] = "VirtualSink"
            launch_env["PULSE_COOKIE"] = "/dev/null"
            launch_env["DISPLAY"] = os.environ.get("DISPLAY", ":99")

        # On Linux VPS, we run headful (headless=False) inside Xvfb (:99)
        # to ensure Chromium activates its full desktop audio engine and routes PCM to PulseAudio.
        use_headless = False if is_linux else self.headless
        launch_kwargs = {
            "headless": use_headless,
            "args": list(CHROMIUM_ARGS),
            "env": launch_env,
        }

        if is_linux:
            if shutil.which("google-chrome") or shutil.which("google-chrome-stable"):
                launch_kwargs["channel"] = "chrome"

        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(**launch_kwargs)
        context = await self.browser.new_context(
            viewport={"width": 1920, "height": 1080},
            locale="ru-RU",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            permissions=["microphone"],  # Only microphone permitted; camera is strictly excluded to prevent video leaks
        )

        # Track and auto-resume any Web Audio AudioContext instances created by the page
        await context.add_init_script("""
            window.__student_audio_contexts = [];
            const OrigAudioContext = window.AudioContext || window.webkitAudioContext;
            if (OrigAudioContext) {
                window.AudioContext = function(...args) {
                    const ctx = new OrigAudioContext(...args);
                    window.__student_audio_contexts.push(ctx);
                    return ctx;
                };
                window.AudioContext.prototype = OrigAudioContext.prototype;
            }

            // WebRTC MediaDevices hardening: hide camera hardware and mute outbound tracks
            if (navigator.mediaDevices) {
                if (navigator.mediaDevices.enumerateDevices) {
                    const origEnumerate = navigator.mediaDevices.enumerateDevices.bind(navigator.mediaDevices);
                    navigator.mediaDevices.enumerateDevices = async function() {
                        const devices = await origEnumerate();
                        return devices.filter(d => d.kind !== 'videoinput');
                    };
                }

                if (navigator.mediaDevices.getUserMedia) {
                    const origGUM = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
                    navigator.mediaDevices.getUserMedia = async function(constraints) {
                        let safeConstraints = constraints;
                        if (safeConstraints && typeof safeConstraints === 'object') {
                            safeConstraints = { ...safeConstraints };
                            if (safeConstraints.video) {
                                safeConstraints.video = false;
                            }
                        }
                        const hasAudio = !!(safeConstraints && safeConstraints.audio);
                        if (!hasAudio) {
                            return new MediaStream();
                        }
                        try {
                            const stream = await origGUM(safeConstraints);
                            stream.getVideoTracks().forEach(t => {
                                t.enabled = false;
                                t.stop();
                            });
                            stream.getAudioTracks().forEach(t => {
                                t.enabled = false;
                            });
                            return stream;
                        } catch (err) {
                            if (!hasAudio) {
                                return new MediaStream();
                            }
                            throw err;
                        }
                    };
                }
            }
        """)

        self.page = await context.new_page()

    async def join_meeting(self, url: str, student_name: Optional[str] = None) -> bool:
        """Determines platform and joins the meeting."""
        name = student_name or settings.student_name
        self.joined_name = name

        if not self.page:
            await self.start()

        # Retry navigation up to 3 times in case of transient network spikes/delays
        max_retries = 3
        last_error = None
        for attempt in range(1, max_retries + 1):
            try:
                logger.info(f"Navigating to meeting URL (attempt {attempt}/{max_retries}): {url}")
                await self.page.goto(url, wait_until="domcontentloaded", timeout=45000)
                last_error = None
                break
            except Exception as e:
                last_error = e
                logger.warning(f"Navigation attempt {attempt}/{max_retries} failed: {e}")
                if attempt < max_retries:
                    await asyncio.sleep(3.0)
                else:
                    raise last_error

        if "telemost" in url:
            success = await self._join_telemost(name)
        elif "mts" in url or "webinar" in url:
            success = await self._join_mts_link(name)
        else:
            logger.warning(f"Unknown meeting platform for URL: {url}. Trying generic join.")
            success = await self._join_generic(name)

        self.is_connected = success
        return success

    async def _join_telemost(self, student_name: str) -> bool:
        """Automates entry into Yandex Telemost (supporting both classic and new 360/Messenger iframe architecture)."""
        logger.info(f"Joining Yandex Telemost as '{student_name}'...")
        page = self.page

        try:
            # Step 1: Wait up to 45 seconds for Telemost SPA to initialize.
            # Handle onboarding modals ("Большое обновление в Телемосте"),
            # "Продолжить в браузере", device check warnings ("Понятно"),
            # and detect either the new iframe (private-join) or classic direct DOM.
            max_wait_seconds = 45
            is_iframe_mode = False
            t_frame = None

            for sec in range(1, max_wait_seconds + 1):
                try:
                    await page.evaluate("""() => {
                        // 1. Dismiss "Большое обновление в Телемосте" / onboarding modals
                        const onboardBtn = document.querySelector(
                            '[data-testid="telemost-3-onboarding-confirm"], [data-testid="telemost-3-onboarding-close"]'
                        );
                        if (onboardBtn) onboardBtn.click();

                        // 2. Click "Продолжить в браузере" if prompted to open desktop app
                        const contBtn = document.querySelector(
                            '[data-testid="meeting-continue-in-browser-continue"], button[class*="continueInBrowser" i]'
                        );
                        if (contBtn) contBtn.click();

                        // 3. Dismiss device warning dialogs ("Включить видео не удалось" / "Понятно")
                        const btns = Array.from(document.querySelectorAll('button'));
                        const poyatno = btns.find(b => (b.innerText || '').includes('Понятно') || (b.innerText || '').includes('Хорошо'));
                        if (poyatno) poyatno.click();
                    }""")
                except Exception:
                    pass

                # Check if new iframe architecture is loaded
                t_frame = next((f for f in page.frames if "private-join" in f.url), None)
                if t_frame:
                    is_iframe_mode = True
                    logger.info(f"Telemost iframe architecture detected after {sec}s.")
                    # Dismiss any initial modals/overlays inside the iframe
                    try:
                        await t_frame.evaluate("""() => {
                            const btns = Array.from(document.querySelectorAll('button'));
                            for (const b of btns) {
                                const t = (b.innerText || '').trim();
                                if (t === 'Понятно' || t === 'Хорошо' || t === 'Закрыть' || t.includes('Понятно')) {
                                    b.click();
                                }
                            }
                            document.querySelectorAll('.Orb-Overlay2, .Orb-Modal2, [data-testid="orb-modal-action-bar"]').forEach(el => {
                                el.style.display = 'none';
                                el.style.pointerEvents = 'none';
                            });
                        }""")
                    except Exception:
                        pass
                    break

                # Check if classic direct lobby is loaded
                try:
                    classic_lobby = await page.evaluate("""() => {
                        const hasJoin = Boolean(document.querySelector('button[data-testid="enter-conference-button"], button[class*="joinMeetingButton"]'));
                        const hasInput = Boolean(document.querySelector('input[data-testid="orb-textinput-input"], input.Orb-Textinput-input'));
                        return hasJoin || hasInput;
                    }""")
                    if classic_lobby:
                        logger.info(f"Telemost classic lobby detected after {sec}s.")
                        break
                except Exception:
                    pass

                await asyncio.sleep(1.0)

            # Target locator context: either iframe or direct page
            fl = page.frame_locator('iframe[src*="private-join"]') if is_iframe_mode else page

            # Step 2: Fill student name
            name_input = fl.locator(
                'input[data-testid="orb-textinput-input"], input.Orb-Textinput-input, input[placeholder*="Представьтесь" i], input[placeholder*="имя" i], input[type="text"]'
            ).first
            try:
                await name_input.wait_for(state="attached", timeout=15000)
                await name_input.fill("")
                await name_input.fill(student_name)
                logger.info(f"Filled student name '{student_name}' in Telemost.")
            except Exception as e:
                logger.debug(f"Name input fill note in Telemost: {e}")

            # Step 3: Turn off microphone and camera before entering
            # Use both direct DOM click and locator to guarantee toggle even if overlays exist
            if is_iframe_mode and t_frame:
                try:
                    await t_frame.evaluate("""() => {
                        const mic = document.querySelector('button[data-testid="turn-off-mic-button"]');
                        if (mic) mic.click();
                        const cam = document.querySelector('button[data-testid="turn-off-camera-button"]');
                        if (cam) cam.click();
                    }""")
                    logger.info("Toggled mic and camera mute via frame DOM in Telemost lobby.")
                except Exception as e:
                    logger.debug(f"DOM mic/cam mute note: {e}")
            else:
                try:
                    await page.evaluate("""() => {
                        const mic = document.querySelector('button[data-testid="turn-off-mic-button"]');
                        if (mic) mic.click();
                        const cam = document.querySelector('button[data-testid="turn-off-camera-button"]');
                        if (cam) cam.click();
                    }""")
                except Exception:
                    pass

            try:
                mic_btn = fl.locator('button[data-testid="turn-off-mic-button"]').first
                if await mic_btn.is_visible(timeout=1000):
                    await mic_btn.click()
            except Exception:
                pass

            try:
                cam_btn = fl.locator('button[data-testid="turn-off-camera-button"]').first
                if await cam_btn.is_visible(timeout=1000):
                    await cam_btn.click()
            except Exception:
                pass

            # Step 4: Click join button ('Подключиться' / data-testid="enter-conference-button")
            # Dismiss any lingering FAQ or promo overlays inside the frame before clicking
            if is_iframe_mode and t_frame:
                try:
                    await t_frame.evaluate("""() => {
                        const closeButtons = Array.from(document.querySelectorAll('button')).filter(b => {
                            const label = (b.getAttribute('aria-label') || '').toLowerCase();
                            const text = (b.innerText || '').toLowerCase();
                            const testid = (b.getAttribute('data-testid') || '').toLowerCase();
                            return label.includes('закрыть') || text.includes('понятно') || text.includes('хорошо') || text.includes('отлично') || testid === 'orb-button-close';
                        });
                        for (const b of closeButtons) {
                            b.click();
                        }
                    }""")
                except Exception:
                    pass

            # Click join button via direct DOM click AND Playwright locator click
            clicked_dom = False
            if is_iframe_mode and t_frame:
                try:
                    clicked_dom = await t_frame.evaluate("""() => {
                        const btn = document.querySelector('button[data-testid="enter-conference-button"], button.joinMeetingButton_M38VH, button[class*="joinMeetingButton"]');
                        if (btn) {
                            btn.click();
                            return true;
                        }
                        return false;
                    }""")
                    if clicked_dom:
                        logger.info("Clicked 'Подключиться' in Telemost via DOM.")
                except Exception as e:
                    logger.debug(f"DOM click join note: {e}")

            join_btn = fl.locator(
                'button[data-testid="enter-conference-button"], button[data-test-id="enter-conference-button"], '
                'button[class*="joinMeetingButton"], button:has-text("Подключиться"), '
                'button:has-text("Войти во встречу"), button:has-text("Присоединиться")'
            ).first
            try:
                if not clicked_dom:
                    await join_btn.wait_for(state="attached", timeout=10000)
                await join_btn.click(timeout=5000, force=True)
                logger.info("Clicked 'Подключиться' in Telemost via Playwright.")
            except Exception as e:
                if not clicked_dom:
                    logger.error(f"Could not find or click join/connect button in Telemost: {e}")
                    debug_path = Path("temp/telemost_join_failed.png")
                    debug_path.parent.mkdir(parents=True, exist_ok=True)
                    await page.screenshot(path=str(debug_path))
                    return False

            # Step 5: Verify meeting room entry (or waiting room admission)
            # Require genuine in-room elements (hangup button, video grid, or waiting room notification)
            # and do not falsely report entry if still on the lobby dialog.
            entered = False
            for attempt in range(25):
                await asyncio.sleep(1.0)
                try:
                    if is_iframe_mode:
                        t_frame = next((f for f in page.frames if "private-join" in f.url), None)
                        if t_frame:
                            # Dismiss any lingering device warning modal ("Включить видео или микрофон не удалось" / "Понятно")
                            try:
                                await t_frame.evaluate("""() => {
                                    const btns = Array.from(document.querySelectorAll('button'));
                                    for (const b of btns) {
                                        const t = (b.innerText || '').trim();
                                        if (t === 'Понятно' || t === 'Хорошо' || t === 'Закрыть' || t.includes('Понятно')) {
                                            b.click();
                                        }
                                    }
                                }""")
                            except Exception:
                                pass

                            in_room_cnt = await t_frame.evaluate("""() => {
                                const h = document.querySelector('button[data-testid*="hangup"], button[data-testid*="end-call"], button[aria-label*="Завершить"], button[aria-label*="Положить трубку"]');
                                return h ? 1 : 0;
                            }""")

                            frame_content = await t_frame.content()
                            in_waiting_room = (
                                "комнате ожидания" in frame_content or
                                "скоро вас впустит" in frame_content or
                                "видит ваш запрос" in frame_content
                            )

                            if in_room_cnt > 0 or in_waiting_room:
                                entered = True
                                break
                    else:
                        in_room = await page.evaluate("""() => {
                            const hasHangup = Boolean(document.querySelector(
                                'button[data-testid*="hangup"], button[data-testid*="end-call"], ' +
                                'button[aria-label*="Завершить"], button[aria-label*="Положить трубку"]'
                            ));
                            const bodyText = document.body ? (document.body.innerText || '') : '';
                            const inWaitingRoom = bodyText.includes('комнате ожидания') || 
                                                  bodyText.includes('скоро вас впустит') || 
                                                  bodyText.includes('видит ваш запрос');
                            return hasHangup || inWaitingRoom;
                        }""")
                        if in_room:
                            entered = True
                            break
                except Exception:
                    pass

            if entered:
                logger.info("Confirmed entry into Telemost meeting room.")
                try:
                    await page.keyboard.press("Control+d")
                except Exception:
                    pass
                await asyncio.sleep(1.0)
                await self.dismiss_popups()
                await self.ensure_participants_panel_closed()
                try:
                    await page.mouse.click(500, 500)
                except Exception:
                    pass
                return True
            else:
                logger.warning("Join button still visible or room entry unconfirmed after click in Telemost.")
                debug_path = Path("temp/telemost_room_unconfirmed.png")
                debug_path.parent.mkdir(parents=True, exist_ok=True)
                await page.screenshot(path=str(debug_path))
                return False

        except Exception as e:
            logger.error(f"Telemost automation error: {e}")
            try:
                debug_path = Path("temp/telemost_error.png")
                debug_path.parent.mkdir(parents=True, exist_ok=True)
                await page.screenshot(path=str(debug_path))
            except Exception:
                pass
            return False

    async def _join_mts_link(self, student_name: str) -> bool:
        """Automates entry into MTS Link (Webinar.ru), supporting both direct rooms and webinars requiring registration."""
        logger.info(f"Joining MTS Link as '{student_name}'...")
        page = self.page

        try:
            # Step 1: Wait up to 75 seconds for ANY actionable meeting element to appear in DOM.
            # On Linux VPS under Xvfb, heavy React SPAs like MTS Link can show
            # 'Еще немного' -> 'Хм, странно. Загрузка идет неожиданно долго.'
            # before rendering the registration landing or room controls (often takes 25-45 seconds).
            reg_landing_btn = page.locator(
                'button[data-testid="RegistrationLanding.mainButton"], '
                'button:has-text("ЗАРЕГИСТРИРОВАТЬСЯ"), button:has-text("Зарегистрироваться")'
            ).first
            name_input = page.locator(
                'input#name, input:not(.MuiSelect-nativeInput):not([aria-hidden="true"])'
            ).first
            pre_join_no_devices_btn = page.locator(
                'button:has-text("ПРИСОЕДИНИТЬСЯ БЕЗ КАМЕРЫ"), button:has-text("Присоединиться без камеры"), '
                'button:has-text("ПРИСОЕДИНИТЬСЯ БЕЗ УСТРОЙСТВ"), button:has-text("Присоединиться без устройств"), '
                'button:has-text("ПРОДОЛЖИТЬ БЕЗ КАМЕРЫ"), button:has-text("Продолжить без камеры"), '
                'button:has-text("ПРОДОЛЖИТЬ БЕЗ УСТРОЙСТВ"), button:has-text("Продолжить без устройств"), '
                'button:has-text("ВОЙТИ БЕЗ КАМЕРЫ"), button:has-text("Войти без камеры")'
            ).first
            pre_join_btn = page.locator(
                'button:has-text("ПРИСОЕДИНИТЬСЯ К ВСТРЕЧЕ"), button:has-text("Присоединиться к встрече")'
            ).first

            detected_mode = None
            max_wait_seconds = 75
            logger.info(f"Waiting up to {max_wait_seconds}s for MTS Link to finish loading SPA bundles...")

            for sec in range(1, max_wait_seconds + 1):
                try:
                    # 1. Registration landing button
                    if await reg_landing_btn.is_visible():
                        detected_mode = "registration"
                        logger.info(f"MTS Link webinar registration landing detected after {sec}s.")
                        break
                    # 2. Pre-join modal (device check)
                    if await pre_join_no_devices_btn.is_visible() or await pre_join_btn.is_visible():
                        detected_mode = "pre_join"
                        logger.info(f"MTS Link pre-join modal detected after {sec}s.")
                        break
                    # 3. Direct room lobby name input
                    if await name_input.is_visible():
                        detected_mode = "direct_lobby"
                        logger.info(f"MTS Link direct lobby name input detected after {sec}s.")
                        break
                    # 4. Stream room already active
                    if "/stream" in page.url:
                        has_media = await page.evaluate(
                            "() => document.querySelectorAll('video').length > 0 || !!document.querySelector('button[data-testid*=\"LeaveButton\"]')"
                        )
                        if has_media:
                            detected_mode = "in_stream"
                            logger.info(f"MTS Link stream room already active after {sec}s.")
                            break
                except Exception:
                    pass

                if sec % 10 == 0:
                    logger.info(f"Still waiting for MTS Link SPA to finish loading ({sec}/{max_wait_seconds}s)...")
                await asyncio.sleep(1.0)

            if not detected_mode:
                logger.error("MTS Link failed to load any recognizable meeting UI within timeout.")
                debug_path = Path("temp/mts_join_failed.png")
                debug_path.parent.mkdir(parents=True, exist_ok=True)
                await page.screenshot(path=str(debug_path))
                return False

            if detected_mode == "registration":
                logger.info("Clicking 'ЗАРЕГИСТРИРОВАТЬСЯ' on landing page...")
                await reg_landing_btn.click()
                await asyncio.sleep(1.0)

                # Split student_name into first name and surname
                name_parts = student_name.strip().split(maxsplit=1)
                first_name = name_parts[0] if name_parts else "Студент"
                last_name = name_parts[1] if len(name_parts) > 1 else ""

                # Email input (use unique timestamp and valid ASCII latin alias)
                ts = int(time.time())
                safe_alias = to_latin_slug(first_name)
                reg_email = f"{safe_alias}.{ts}@gmail.com"

                try:
                    email_input = page.locator('input[name="email"], input[type="email"]').first
                    await email_input.wait_for(state="visible", timeout=15000)
                    await email_input.fill(reg_email)
                    logger.info(f"Filled registration email: {reg_email}")

                    name_field = page.locator('input[name="name"]').first
                    await name_field.fill(first_name)
                    logger.info(f"Filled registration name: {first_name}")

                    second_name_input = page.locator('input[name="secondName"]').first
                    if await second_name_input.is_visible(timeout=1000):
                        await second_name_input.fill(last_name or first_name)
                        logger.info(f"Filled registration secondName: {last_name or first_name}")

                    reg_submit_btn = page.locator(
                        'button[data-testid="RegistrationDialog.submit"], '
                        'button:has-text("ЗАРЕГИСТРИРОВАТЬСЯ"), button:has-text("Зарегистрироваться")'
                    ).last
                    await reg_submit_btn.click()
                    logger.info("Submitted MTS Link webinar registration form.")

                    await asyncio.sleep(1.0)
                    err_text = await page.evaluate("""() => {
                        const el = document.querySelector('.Mui-error, [class*="error" i], [role="alert"]');
                        return el && el.innerText ? el.innerText.trim() : null;
                    }""")
                    if err_text:
                        logger.warning(f"MTS Link registration dialog note: {err_text}")
                except Exception as e:
                    logger.warning(f"Error during MTS Link registration form fill: {e}")

            elif detected_mode == "direct_lobby":
                # Standard meeting entry (direct room lobby)
                try:
                    await name_input.fill("")
                    await name_input.fill(student_name)
                    logger.info(f"Filled name '{student_name}' in MTS Link.")
                except Exception as e:
                    logger.warning(f"MTS Link name input fill note: {e}")

                # Email field if requested by host
                email_input = page.locator('input[name="email"], input[type="email"], input[placeholder*="email" i]').first
                try:
                    if await email_input.is_visible(timeout=2000):
                        await email_input.fill("student.recorder@university.local")
                except Exception:
                    pass

                # Wait for Join / Submit button to become enabled and click
                submit_selector = (
                    'button[data-testid*="enter-to-event"], button[type="submit"], '
                    'button:has-text("ПРИСОЕДИНИТЬСЯ"), button:has-text("JOIN"), '
                    'button:has-text("ВОЙТИ"), button:has-text("Присоединиться")'
                )
                try:
                    await page.wait_for_selector(f"{submit_selector}:not([disabled]):not(.Mui-disabled)", timeout=10000)
                except Exception:
                    pass

                submit_btn = page.locator(submit_selector).first
                if await submit_btn.is_visible(timeout=5000):
                    await submit_btn.click()
                    logger.info("Submitted join request in MTS Link.")
                else:
                    logger.error("Could not find submit/join button in MTS Link.")
                    debug_path = Path("temp/mts_join_failed.png")
                    debug_path.parent.mkdir(parents=True, exist_ok=True)
                    await page.screenshot(path=str(debug_path))
                    return False

            # Step 2: Transition into stream room and handle device check / in-room confirmation
            entered_stream = False
            for _ in range(35):
                # Check for pre-meeting device check modal ("ПРИСОЕДИНИТЬСЯ БЕЗ КАМЕРЫ" / "БЕЗ УСТРОЙСТВ" first, fallback to "ПРИСОЕДИНИТЬСЯ К ВСТРЕЧЕ")
                no_devices_btn = page.locator(
                    'button:has-text("ПРИСОЕДИНИТЬСЯ БЕЗ КАМЕРЫ"), button:has-text("Присоединиться без камеры"), '
                    'button:has-text("ПРИСОЕДИНИТЬСЯ БЕЗ УСТРОЙСТВ"), button:has-text("Присоединиться без устройств"), '
                    'button:has-text("ПРОДОЛЖИТЬ БЕЗ КАМЕРЫ"), button:has-text("Продолжить без камеры"), '
                    'button:has-text("ПРОДОЛЖИТЬ БЕЗ УСТРОЙСТВ"), button:has-text("Продолжить без устройств"), '
                    'button:has-text("ВОЙТИ БЕЗ КАМЕРЫ"), button:has-text("Войти без камеры")'
                ).first
                standard_join_btn = page.locator(
                    'button:has-text("ПРИСОЕДИНИТЬСЯ К ВСТРЕЧЕ"), button:has-text("Присоединиться к встрече")'
                ).first

                try:
                    if await no_devices_btn.is_visible():
                        logger.info("Found 'ПРИСОЕДИНИТЬСЯ БЕЗ КАМЕРЫ/УСТРОЙСТВ' in MTS Link. Clicking...")
                        await no_devices_btn.click(force=True)
                        await asyncio.sleep(1.0)
                    elif await standard_join_btn.is_visible():
                        await self._mute_mts_prejoin_devices()
                        logger.info("Found standard pre-meeting join button in MTS Link. Clicking...")
                        await standard_join_btn.click(force=True)
                        await asyncio.sleep(1.0)
                except Exception as e:
                    logger.debug(f"Pre-join button click note: {e}")

                # Check if already in stream room with controls or media
                in_stream = await page.evaluate("""() => {
                    const isStreamUrl = window.location.href.includes('/stream');
                    const hasControls = !!document.querySelector('button[data-testid*="LeaveButton"], button[aria-label*="Покинуть встречу"], button[aria-label*="Завершить"]');
                    const hasVideos = document.querySelectorAll('video').length > 0;
                    return isStreamUrl && (hasControls || hasVideos);
                }""")
                if in_stream:
                    logger.info("Confirmed entry into MTS Link stream room.")
                    entered_stream = True
                    break
                await asyncio.sleep(1.0)

            if not entered_stream:
                logger.error("Could not confirm entry into MTS Link stream room within timeout.")
                debug_path = Path("temp/mts_join_failed.png")
                debug_path.parent.mkdir(parents=True, exist_ok=True)
                await page.screenshot(path=str(debug_path))
                return False

            await asyncio.sleep(1.0)
            await self._mute_mts_in_room_devices()

            # Dismiss any lingering device check modal on top of stream (e.g. "ПРИСОЕДИНИТЬСЯ БЕЗ КАМЕРЫ")
            for _ in range(5):
                lingering_btn = page.locator(
                    'button:has-text("ПРИСОЕДИНИТЬСЯ БЕЗ КАМЕРЫ"), button:has-text("Присоединиться без камеры"), '
                    'button:has-text("ПРИСОЕДИНИТЬСЯ БЕЗ УСТРОЙСТВ"), button:has-text("Присоединиться без устройств"), '
                    'button:has-text("ПРОДОЛЖИТЬ БЕЗ КАМЕРЫ"), button:has-text("Продолжить без камеры"), '
                    'button:has-text("ВОЙТИ БЕЗ КАМЕРЫ"), button:has-text("Войти без камеры")'
                ).first
                try:
                    if await lingering_btn.is_visible(timeout=1000):
                        logger.info("Dismissing lingering MTS Link device modal by clicking 'ПРИСОЕДИНИТЬСЯ БЕЗ КАМЕРЫ'...")
                        await lingering_btn.click(force=True)
                        await asyncio.sleep(0.5)
                    else:
                        break
                except Exception:
                    break

            await self.dismiss_popups()
            await self.ensure_participants_panel_closed()
            try:
                await page.mouse.click(500, 500)
            except Exception:
                pass
            return True

        except Exception as e:
            logger.error(f"MTS Link automation error: {e}")
            try:
                debug_path = Path("temp/mts_error.png")
                debug_path.parent.mkdir(parents=True, exist_ok=True)
                await page.screenshot(path=str(debug_path))
            except Exception:
                pass
            return False

    async def _mute_mts_prejoin_devices(self):
        """Attempts to turn off mic and camera toggles on the MTS Link pre-join screen."""
        if not self.page:
            return
        try:
            await self.page.evaluate("""() => {
                const buttons = Array.from(document.querySelectorAll('button, [role="switch"], [role="button"]'));
                for (const b of buttons) {
                    const label = (b.getAttribute('aria-label') || b.title || b.innerText || '').toLowerCase();
                    const testid = (b.getAttribute('data-testid') || '').toLowerCase();
                    const isMic = label.includes('микрофон') || label.includes('звук') || testid.includes('mic');
                    const isCam = label.includes('камер') || label.includes('видео') || testid.includes('cam');
                    if (isMic || isCam) {
                        const isOff = label.includes('включить') || label.includes('off') || 
                                      b.getAttribute('aria-checked') === 'false' || 
                                      b.getAttribute('aria-pressed') === 'false';
                        if (!isOff) {
                            b.click();
                        }
                    }
                }
            }""")
        except Exception as e:
            logger.debug(f"_mute_mts_prejoin_devices note: {e}")

    async def _mute_mts_in_room_devices(self):
        """
        Safety sweep after joining MTS Link stream room.
        Ensures microphone and camera are muted in the bottom toolbar.
        """
        if not self.page:
            return
        try:
            await self.page.evaluate("""() => {
                const buttons = Array.from(document.querySelectorAll('button'));
                for (const b of buttons) {
                    const label = (b.getAttribute('aria-label') || b.title || b.innerText || '').toLowerCase();
                    const testid = (b.getAttribute('data-testid') || '').toLowerCase();
                    
                    const isMic = (label.includes('микрофон') || testid.includes('mic')) && !label.includes('настройк');
                    const isCam = (label.includes('камер') || testid.includes('cam')) && !label.includes('настройк');

                    if (isMic) {
                        const isTurnOn = label.includes('включить') || b.getAttribute('aria-pressed') === 'false';
                        if (!isTurnOn && (label.includes('выключить') || b.getAttribute('aria-pressed') === 'true' || b.classList.contains('active'))) {
                            b.click();
                        }
                    }
                    if (isCam) {
                        const isTurnOn = label.includes('включить') || b.getAttribute('aria-pressed') === 'false';
                        if (!isTurnOn && (label.includes('выключить') || b.getAttribute('aria-pressed') === 'true' || b.classList.contains('active'))) {
                            b.click();
                        }
                    }
                }
            }""")
        except Exception as e:
            logger.debug(f"_mute_mts_in_room_devices note: {e}")

    async def _join_generic(self, student_name: str) -> bool:
        """Generic fallback for unknown meeting platforms."""
        logger.info(f"Attempting generic join as '{student_name}'...")
        page = self.page

        try:
            name_input = page.locator('input[type="text"]').first
            if await name_input.is_visible(timeout=3000):
                await name_input.fill(student_name)
            btn = page.locator('button:has-text("Войти"), button:has-text("Join")').first
            if await btn.is_visible(timeout=3000):
                await btn.click()
            return True
        except Exception:
            return False

    async def dismiss_popups(self):
        """Dismisses any promo, device modal, onboarding, or notification popups in the meeting room."""
        if not self.page:
            return
        try:
            await self.page.evaluate("""
                () => {
                    const all = Array.from(document.querySelectorAll('*'));

                    // 1. Click 'ПРИСОЕДИНИТЬСЯ БЕЗ КАМЕРЫ' / 'БЕЗ УСТРОЙСТВ' or 'ПРИСОЕДИНИТЬСЯ К ВСТРЕЧЕ' leaf element and walk up to button
                    const noDev = all.find(e => e.innerText && (
                        e.innerText.toLowerCase().includes('без камеры') ||
                        e.innerText.toLowerCase().includes('без устройств') || 
                        e.innerText.toLowerCase().includes('без микрофона') ||
                        e.innerText.toLowerCase().includes('присоединиться к встрече')
                    ) && e.children.length === 0);
                    if (noDev) {
                        let curr = noDev;
                        while (curr && curr !== document.body) {
                            curr.click();
                            if (curr.tagName === 'BUTTON') break;
                            curr = curr.parentElement;
                        }
                    }

                    // 2. Click 'БОЛЬШЕ НЕ ПОКАЗЫВАТЬ' banner leaf element
                    const bannerLeaves = all.filter(e => e.innerText && e.innerText.toLowerCase().includes('больше не показывать') && e.children.length === 0);
                    for (const b of bannerLeaves) {
                        let curr = b;
                        while (curr && curr !== document.body) {
                            curr.click();
                            if (curr.tagName === 'BUTTON') break;
                            curr = curr.parentElement;
                        }
                    }

                    // 3. Click close buttons (✕) or standard dismiss buttons
                    const dismissTexts = ['отлично', 'понятно', 'хорошо', 'закрыть', 'close', 'пропустить'];
                    for (const el of all) {
                        if (el.children.length === 0) {
                            const text = (el.innerText || '').trim().toLowerCase();
                            const aria = (el.getAttribute('aria-label') || '').trim().toLowerCase();
                            if (dismissTexts.includes(text) || dismissTexts.some(t => aria.includes(t))) {
                                el.click();
                                if (el.parentElement && el.parentElement.tagName === 'BUTTON') {
                                    el.parentElement.click();
                                }
                            }
                        }
                    }

                    // 4. Forcefully remove lingering top warning banners (VPN warning bar)
                    const vpnBanners = Array.from(document.querySelectorAll('*')).filter(e => {
                        const t = e.innerText || '';
                        return (t.includes('Отключите VPN') || t.includes('VPN')) && e.offsetHeight > 0 && e.offsetHeight < 120 && e.children.length > 0;
                    });
                    for (const b of vpnBanners) {
                        b.remove();
                    }

                    // 5. Safely hide lingering backdrop or promo modals without breaking React DOM
                    const poyatno = Array.from(document.querySelectorAll('button')).find(b => (b.innerText || '').includes('Понятно'));
                    if (poyatno) poyatno.click();

                    const modals = document.querySelectorAll('.Orb-Modal, .Orb-Modal2, .Modal, [role="dialog"], .Modal-Overlay, .Orb-Overlay2, .MuiDialog-root, .MuiBackdrop-root');
                    for (const m of modals) {
                        const text = m.innerText || '';
                        if (text.includes('Алиса Про') || text.includes('Конспектирование') || text.includes('камере и микрофону') || text.includes('без устройств') || text.includes('видео или микрофон') || text.includes('видео не удалось')) {
                            m.style.display = 'none';
                            m.style.pointerEvents = 'none';
                        }
                    }

                    // 6. Guarantee WebRTC speaker playback is active & unmuted
                    try {
                        if (window.__student_audio_contexts) {
                            window.__student_audio_contexts.forEach(ctx => {
                                if (ctx.state === 'suspended') {
                                    ctx.resume().catch(() => {});
                                }
                            });
                        }
                        document.querySelectorAll('audio, video').forEach(el => {
                            el.muted = false;
                            el.volume = 1.0;
                            if (el.paused) {
                                el.play().catch(() => {});
                            }
                        });
                        const speakerButtons = all.filter(e => {
                            const aria = (e.getAttribute('aria-label') || '').toLowerCase();
                            const testId = (e.getAttribute('data-testid') || '').toLowerCase();
                            return (aria.includes('звук') || aria.includes('динамик') || testId.includes('volume') || testId.includes('speaker')) &&
                                   (aria.includes('включ') || aria.includes('выключ') || aria.includes('отключ'));
                        });
                        for (const s of speakerButtons) {
                            const aria = (s.getAttribute('aria-label') || '').toLowerCase();
                            if (aria.includes('включить звук') || aria.includes('включить динамик')) {
                                s.click();
                            }
                        }

                        // 7. Yandex Telemost (360 / Messenger): Hide sidebars and expand conference iframe to full screen
                        const sb = document.querySelector('.yamb-sidebar, [class*="yamb-sidebar"]');
                        if (sb) sb.style.display = 'none';
                        const leftBar = document.querySelector('.yamb-global-bar, [class*="GlobalBarRoot"]');
                        if (leftBar) leftBar.style.display = 'none';
                        const content = document.querySelector('.yamb-root__content');
                        if (content) {
                            content.style.padding = '0';
                            content.style.margin = '0';
                            content.style.width = '100vw';
                            content.style.height = '100vh';
                        }
                        const telemostIframe = document.querySelector('iframe[src*="private-join"]');
                        if (telemostIframe) {
                            telemostIframe.style.width = '100vw';
                            telemostIframe.style.height = '100vh';
                            telemostIframe.style.position = 'fixed';
                            telemostIframe.style.top = '0';
                            telemostIframe.style.left = '0';
                            telemostIframe.style.zIndex = '999999';
                        }
                    } catch (err) {}
                }
            """)

            # Also dismiss popups inside Telemost iframe if present
            telemost_frame = next((f for f in self.page.frames if "private-join" in f.url), None)
            if telemost_frame:
                await telemost_frame.evaluate("""() => {
                    try {
                        // 1. Click dismiss buttons in promo modals: 'Отлично', 'Понятно', 'Хорошо', 'Закрыть', etc.
                        const dismissTexts = ['отлично', 'понятно', 'хорошо', 'закрыть', 'close', 'пропустить'];
                        const btns = Array.from(document.querySelectorAll('button'));
                        for (const b of btns) {
                            const text = (b.innerText || '').trim().toLowerCase();
                            const aria = (b.getAttribute('aria-label') || '').trim().toLowerCase();
                            const testId = (b.getAttribute('data-testid') || '').toLowerCase();
                            if (dismissTexts.some(t => text === t || aria.includes(t)) || testId === 'orb-button-close') {
                                b.click();
                            }
                        }

                        // 2. Dismiss cookie consent banner
                        const cookieBtns = Array.from(document.querySelectorAll('button')).filter(b => {
                            const t = (b.innerText || '').toLowerCase();
                            return t.includes('essential') || t.includes('allow all') || t.includes('разрешить');
                        });
                        for (const cb of cookieBtns) {
                            cb.click();
                        }

                        // 3. Forcefully hide any lingering modals, overlays, or cookie bars via CSS
                        document.querySelectorAll('.Orb-Overlay2, .Orb-Modal2, [role="dialog"], [class*="cookie" i], [class*="Cookie"]').forEach(el => {
                            el.style.display = 'none';
                            el.style.pointerEvents = 'none';
                        });
                    } catch (err) {}
                }""")
        except Exception as e:
            logger.debug(f"dismiss_popups error (ignored): {e}")

    async def capture_screenshot(self) -> Optional[bytes]:
        """Captures a high-resolution screenshot of the meeting page, ensuring promo modals and sidebars are closed."""
        if not self.page:
            return None
        try:
            await self.dismiss_popups()
            await self.ensure_participants_panel_closed()

            # Safety net: directly ensure promo modals and cookie banners are visually hidden
            telemost_frame = self._get_telemost_frame()
            if telemost_frame:
                await telemost_frame.evaluate("""() => {
                    document.querySelectorAll('.Orb-Overlay2, .Orb-Modal2, [role="dialog"], [class*="cookie" i], [class*="Cookie"]').forEach(el => {
                        el.style.display = 'none';
                        el.style.pointerEvents = 'none';
                    });
                }""")

            return await self.page.screenshot(type="png")
        except Exception as e:
            logger.error(f"Failed to capture meeting screenshot: {e}")
            return None

    def _get_telemost_frame(self):
        """Returns the active Telemost WebRTC meeting iframe if running in 360/Messenger iframe architecture."""
        if not self.page:
            return None
        return next((f for f in self.page.frames if "private-join" in f.url), None)

    async def is_participants_panel_open(self) -> bool:
        """Checks if the participants list/sidebar is currently open."""
        if not self.page:
            return False
        try:
            target = self._get_telemost_frame() or self.page
            return await target.evaluate("""() => {
                const url = window.location.href.toLowerCase();
                if (url.includes('telemost') || url.includes('private-join')) {
                    const sb = document.querySelector('[class*="teleMessengerSidebar"], [class*="sidebar_"], [class*="participantsSidebar"]');
                    return !!sb && sb.offsetWidth > 0;
                }
                if (url.includes('mts') || url.includes('webinar')) {
                    const aside = document.querySelector('aside.stream-sidebar, [class*="StreamSideBar" i], [class*="StreamLayout__sidebar" i], [class*="StreamPeople" i]');
                    if (!aside) return false;
                    const isClosed = aside.className.includes('closed') || 
                                     aside.classList.contains('StreamLayout__closed___scvx6') || 
                                     aside.offsetWidth === 0;
                    return !isClosed;
                }
                return false;
            }""")
        except Exception:
            return False

    async def ensure_participants_panel_open(self) -> bool:
        """
        Checks if the participants list/sidebar is currently open.
        If not, attempts to open it via platform-specific buttons.
        """
        if not self.page:
            return False

        try:
            target = self._get_telemost_frame() or self.page
            return await target.evaluate("""() => {
                const url = window.location.href.toLowerCase();
                
                // 1. Yandex Telemost
                if (url.includes('telemost') || url.includes('private-join')) {
                    const sb = document.querySelector('[class*="teleMessengerSidebar"], [class*="sidebar_"], [class*="participantsSidebar"]');
                    if (sb) {
                        sb.style.display = '';
                    }
                    if (sb && sb.offsetWidth > 0) {
                        return true;
                    }

                    const btn = document.querySelector(
                        'button[data-testid="participants-button"], button[data-testid*="participants"], ' +
                        'button[aria-label*="Участники"], button[title*="Участники"]'
                    );
                    if (btn) {
                        btn.click();
                        return true;
                    }
                }

                // 2. MTS Link (Webinar.ru)
                if (url.includes('mts') || url.includes('webinar')) {
                    const aside = document.querySelector('aside.stream-sidebar, [class*="StreamSideBar" i], [class*="StreamLayout__sidebar" i], [class*="StreamPeople" i]');
                    if (aside && !aside.className.includes('closed') && aside.offsetWidth > 0) {
                        return true;
                    }

                    const btn = document.querySelector('button[data-testid*="People" i], button[aria-label*="Участники" i], button[aria-label*="участники" i]');
                    if (btn) {
                        btn.click();
                        return true;
                    }
                }

                return false;
            }""")
        except Exception as e:
            logger.debug(f"ensure_participants_panel_open error: {e}")
            return False

    async def ensure_participants_panel_closed(self) -> bool:
        """
        Closes the participants sidebar if it is currently open,
        ensuring slides and presentations remain completely unobstructed.
        """
        if not self.page:
            return False

        try:
            target = self._get_telemost_frame() or self.page
            return await target.evaluate("""() => {
                const url = window.location.href.toLowerCase();

                // 1. Yandex Telemost
                if (url.includes('telemost') || url.includes('private-join')) {
                    const sb = document.querySelector('aside[class*="sidebar" i], div[class*="teleMessengerSidebar"], div[class*="participantsSidebar"]');
                    if (sb && sb.offsetWidth > 100 && sb.tagName !== 'BUTTON') {
                        // Priority 1: Sidebar top close button (aria-label="Закрыть окно", class closeButton_*)
                        const closeBtn = sb.querySelector(
                            'button[aria-label*="Закрыть"], button[class*="closeButton"], button[title*="Закрыть"], [class*="Orb-Modal2-NavigationBar-Close"]'
                        );
                        if (closeBtn) {
                            closeBtn.click();
                            return true;
                        }

                        // Priority 2: Toggle button in bottom toolbar (only if active/pressed)
                        const toggleBtn = document.querySelector(
                            'button[data-testid="participants-button"], button[data-testid*="participants"], button[aria-label*="Участники"]'
                        );
                        if (toggleBtn && (toggleBtn.getAttribute('aria-pressed') === 'true' || (toggleBtn.className && toggleBtn.className.includes('active')))) {
                            toggleBtn.click();
                            return true;
                        }
                    }
                }

                // 2. MTS Link (Webinar.ru)
                if (url.includes('mts') || url.includes('webinar')) {
                    const aside = document.querySelector('aside.stream-sidebar, [class*="StreamSideBar" i], [class*="StreamLayout__sidebar" i], [class*="StreamPeople" i]');
                    const isClosed = !aside || aside.className.includes('closed') || aside.offsetWidth === 0;
                    if (!isClosed) {
                        // Priority 1: Inside close button (chevron or close icon in sidebar header)
                        const closeBtn = aside.querySelector('button[class*="closeBtn" i], [class*="StreamPeople__closeBtn" i], button[aria-label*="Свернуть" i], button[aria-label*="Закрыть" i]');
                        if (closeBtn) {
                            closeBtn.click();
                            return true;
                        }

                        // Priority 2: Toggle button in bottom toolbar
                        const toggleBtn = document.querySelector('button[data-testid*="People" i], button[aria-label*="Участники" i]');
                        if (toggleBtn) {
                            toggleBtn.click();
                            return true;
                        }
                    }
                }

                return false;
            }""")
        except Exception as e:
            logger.debug(f"ensure_participants_panel_closed error: {e}")
            return False

    async def get_active_participants(self) -> List[str]:
        """
        Extracts the list of active participant names currently in the meeting room.
        Handles both Yandex Telemost and MTS Link with fallbacks to video tile overlays.
        Filters out self name, roles, and system markers.
        Renders the sidebar invisibly (opacity: 0) while reading, then immediately closes it,
        preventing any visual obstruction of lecture slides and code presentations.
        """
        if not self.page:
            return []

        was_open = False
        try:
            was_open = await self.is_participants_panel_open()
            target = self._get_telemost_frame() or self.page
            if not was_open:
                # Inject invisible style so opening the panel never obstructs slides in screenshots
                try:
                    await target.evaluate("""() => {
                        let style = document.getElementById('hide-participants-style');
                        if (!style) {
                            style = document.createElement('style');
                            style.id = 'hide-participants-style';
                            style.textContent = `
                                aside.stream-sidebar, [class*="StreamSideBar" i], [class*="StreamLayout__sidebar" i], [class*="participantsSidebar"], [class*="teleMessengerSidebar"], [class*="sidebar_"], .StreamPeople, .StreamPeople__streamPeople {
                                    position: fixed !important;
                                    right: -9999px !important;
                                    opacity: 0 !important;
                                    pointer-events: none !important;
                                }
                            `;
                            document.head.appendChild(style);
                        }
                    }""")
                except Exception:
                    pass

                await self.ensure_participants_panel_open()
                await asyncio.sleep(0.3)

            raw_names: List[str] = await target.evaluate("""() => {
                const url = window.location.href.toLowerCase();
                const names = [];

                const addName = (n) => {
                    if (!n) return;
                    let clean = n.trim();
                    if (!clean) return;
                    clean = clean.replace(/\\s*\\((?:я|вы|организатор|ведущий|гость)\\)$/i, '').trim();
                    if (clean && clean.toLowerCase() !== 'я' && clean.toLowerCase() !== 'вы') {
                        names.push(clean);
                    }
                };

                // --- 1. Yandex Telemost ---
                if (url.includes('telemost') || url.includes('private-join')) {
                    // Method A: Sidebar participant rows (clean display names)
                    const sidebarRows = document.querySelectorAll(
                        '[class*="participantsSidebar"] [class*="Participant_"], ' +
                        '[class*="teleMessengerSidebar"] [class*="Participant_"], ' +
                        '[class*="sidebar_"] [class*="Participant_"]'
                    );
                    if (sidebarRows.length > 0) {
                        sidebarRows.forEach(row => {
                            const nameEl = row.querySelector('[class*="DisplayName_"], [class*="TextName"]');
                            if (nameEl && nameEl.innerText) {
                                addName(nameEl.innerText);
                            }
                        });
                    }

                    // Method B: Video tile overlays (fallback or supplement)
                    if (names.length === 0) {
                        const tileNames = document.querySelectorAll(
                            '[class*="participantName" i] [class*="TextName"], ' +
                            '[class*="DisplayName_"], [class*="TextName"]'
                        );
                        tileNames.forEach(el => {
                            if (el.innerText) addName(el.innerText);
                        });
                    }
                }

                // --- 2. MTS Link (Webinar.ru) ---
                else if (url.includes('mts') || url.includes('webinar')) {
                    // Method A: User rows in participants panel
                    const userRows = document.querySelectorAll('[class*="UserRowName__name"], [class*="UserRow__name"]');
                    if (userRows.length > 0) {
                        userRows.forEach(row => {
                            const isMe = !!row.closest('[class*="UserRow__me"]');
                            const nameText = row.getAttribute('title') || row.innerText;
                            if (nameText && !isMe) {
                                addName(nameText);
                            }
                        });
                    }

                    // Method B: Video tile overlays
                    if (names.length === 0) {
                        const tileOverlays = document.querySelectorAll('[class*="VcsItemOverlay__title"]');
                        tileOverlays.forEach(overlay => {
                            const title = overlay.getAttribute('title') || overlay.innerText;
                            if (title) addName(title);
                        });
                    }
                }

                // --- 3. Generic fallback ---
                else {
                    const possibleUserElements = document.querySelectorAll('[class*="user-name" i], [class*="participant-name" i]');
                    possibleUserElements.forEach(el => {
                        if (el.innerText) addName(el.innerText);
                    });
                }

                return names;
            }""")

            bot_name = (self.joined_name or settings.student_name or "").strip().lower()
            unique_participants = []
            seen = set()

            for n in raw_names:
                cleaned = n.strip()
                if not cleaned:
                    continue
                if bot_name and cleaned.lower() == bot_name:
                    continue
                if cleaned.lower() not in seen:
                    seen.add(cleaned.lower())
                    unique_participants.append(cleaned)

            return sorted(unique_participants)

        except Exception as e:
            logger.error(f"Error fetching active participants from DOM: {e}")
            return []
        finally:
            if not was_open:
                try:
                    await self.ensure_participants_panel_closed()
                    target = self._get_telemost_frame() or self.page
                    if target:
                        await target.evaluate("""() => {
                            const style = document.getElementById('hide-participants-style');
                            if (style) style.remove();
                        }""")
                except Exception:
                    pass

    async def get_participant_count(self) -> Optional[int]:
        """
        Parses active participant count from page indicators (badges/counters or DOM participants list).
        Replaces regex-only approaches with exact DOM extraction.
        """
        if not self.page:
            return None

        try:
            # 1. Try exact DOM counters
            target = self._get_telemost_frame() or self.page
            count = await target.evaluate("""() => {
                const url = window.location.href.toLowerCase();

                // Yandex Telemost
                if (url.includes('telemost') || url.includes('private-join')) {
                    const sideCount = document.querySelector('[class*="participantsCount"]');
                    if (sideCount && sideCount.innerText) {
                        const num = parseInt(sideCount.innerText.trim(), 10);
                        if (!isNaN(num)) return num;
                    }
                    const btn = document.querySelector('button[data-testid*="participants"], button[aria-label*="Участники"]');
                    if (btn && btn.innerText) {
                        const m = btn.innerText.match(/\\d+/);
                        if (m) return parseInt(m[0], 10);
                    }
                }

                // MTS Link
                if (url.includes('mts') || url.includes('webinar')) {
                    const badge = document.querySelector('div.badge[class*="StreamPeople__badge"], [class*="StreamPeople"] .badge');
                    if (badge && badge.innerText) {
                        const num = parseInt(badge.innerText.trim(), 10);
                        if (!isNaN(num)) return num;
                    }
                }

                return null;
            }""")

            if count is not None and count > 0:
                return count

            # 2. If badge not present, calculate from active participants list (+1 for bot itself)
            active = await self.get_active_participants()
            if active:
                return len(active) + 1

            # 3. Fallback regex on target DOM content if DOM structure changed completely
            target = self._get_telemost_frame() or self.page
            text = await target.content()
            matches = re.findall(r"(?<!до\s)(?:участник[а-я]*|людей|people|participants)[^\d]{1,10}(\d+)", text, re.IGNORECASE)
            if matches:
                return int(matches[0])
        except Exception as e:
            logger.debug(f"get_participant_count error: {e}")

        return None

    async def is_meeting_ended(self) -> bool:
        """Detects if the meeting has been terminated, closed, or the bot was removed/kicked."""
        if not self.page:
            return True

        try:
            if hasattr(self.page, "is_closed") and self.page.is_closed() is True:
                logger.info("Meeting page is closed.")
                return True
            if self.browser and hasattr(self.browser, "is_connected") and self.browser.is_connected() is False:
                logger.info("Meeting browser is disconnected.")
                return True
        except Exception:
            pass

        try:
            # 1. Check URL redirect away from the meeting room
            current_url = self.page.url.lower()
            if self.is_connected and "telemost" in current_url and "/j/" not in current_url:
                logger.info(f"Telemost meeting redirect detected (left room): {current_url}")
                return True
            if self.is_connected and "my.mts-link.ru" in current_url and "/stream" not in current_url and "/j/" not in current_url:
                logger.info(f"MTS Link meeting redirect detected (left room): {current_url}")
                return True

            # If we are actively in an MTS Link /stream room, check if the room is actively alive first!
            if self.is_connected and "my.mts-link.ru" in current_url and "/stream" in current_url:
                is_room_active = await self.page.evaluate("""() => {
                    const hasHangup = !!document.querySelector(
                        'button[data-testid*="LeaveButton"], button[aria-label*="Покинуть"], button[aria-label*="Завершить"], button[class*="hangup" i], button[class*="leave" i]'
                    );
                    const hasVideos = document.querySelectorAll('video').length > 0;
                    const hasParticipants = document.querySelectorAll('[class*="participant" i], [class*="speaker" i]').length > 0;
                    return (hasHangup || hasVideos || hasParticipants);
                }""")
                if is_room_active:
                    # The room is actively running. Check ONLY explicit kick/banned modal dialogs, NOT general page HTML keywords!
                    kick_modal = await self.page.evaluate("""() => {
                        const modal = document.querySelector('.Orb-Modal, .Modal, [role="dialog"], .MuiDialog-root');
                        if (!modal) return null;
                        const text = (modal.innerText || '').toLowerCase();
                        if (text.includes('удалил') || text.includes('исключ') || text.includes('заверш') || text.includes('вы были удалены')) {
                            return text.slice(0, 100);
                        }
                        return null;
                    }""")
                    if kick_modal:
                        logger.info(f"Meeting kick/ended dialog detected inside stream room: {kick_modal}")
                        return True
                    # If room is active and no kick modal is visible, the meeting is definitely NOT ended!
                    return False

            # 2. Check text markers for call end or participant removal
            content = (await self.page.content()).lower()
            end_markers = [
                "встреча завершена",
                "звонок завершен",
                "вебинар окончен",
                "вебинар завершен",
                "конференция завершена",
                "мероприятие завершено",
                "вебинар был завершен",
                "организатор завершил мероприятие",
                "создайте личный кабинет",
                "ко всем материалам ваших встреч",
                "вас удалили",
                "вас исключили",
                "исключен из встречи",
                "удален из встречи",
                "вы покинули встречу",
                "вы были удалены",
                "организатор исключил",
                "the meeting has ended",
                "you were removed",
                "you have been removed",
            ]
            for marker in end_markers:
                if marker in content:
                    logger.info(f"Meeting end/kick marker detected: '{marker}'")
                    return True

            # 3. Check MTS Link post-meeting dashboard elements
            if self.is_connected and "mts-link" in current_url:
                try:
                    mts_end = self.page.locator('button:has-text("Новая встреча"), text="ко всем материалам ваших встреч"').first
                    if await mts_end.is_visible(timeout=500):
                        logger.info("MTS Link post-meeting dashboard/summary detected.")
                        return True
                except Exception:
                    pass

            # 4. Check if in-call controls disappeared completely while supposedly connected
            if self.is_connected:
                in_call_btn = self.page.locator(
                    'button[data-test-id="hangup-button"], button[data-test-id="end-call-button"], '
                    'button[data-testid*="LeaveButton"], button[aria-label*="Покинуть встречу" i], '
                    'button[aria-label*="Завершить" i], button[aria-label*="Положить трубку" i]'
                ).first
                if not await in_call_btn.is_visible(timeout=1000):
                    # Check if modal dialog appeared
                    kick_modal = self.page.locator('.Orb-Modal, .Modal, [role="dialog"], .MuiDialog-root').first
                    if await kick_modal.is_visible(timeout=1000):
                        modal_text = (await kick_modal.inner_text()).lower()
                        if any(w in modal_text for w in ["удалил", "исключ", "заверш", "законч"]):
                            logger.info(f"Meeting kick/close dialog detected: {modal_text[:60]}")
                            return True
        except Exception as e:
            logger.debug(f"is_meeting_ended transient check error (safely ignored): {e}")
            return False
        return False

    async def close(self):
        """Cleanly terminates browser session."""
        self.is_connected = False
        if self.page:
            try:
                await self.page.close()
            except Exception:
                pass
            self.page = None
        if self.browser:
            try:
                await self.browser.close()
            except Exception:
                pass
            self.browser = None
        if self.playwright:
            try:
                await self.playwright.stop()
            except Exception:
                pass
            self.playwright = None
