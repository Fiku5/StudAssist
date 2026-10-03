#!/usr/bin/env python3
"""
Test script to verify end-to-end audio recording from Playwright Chromium into PulseAudio VirtualSink.
Run on VPS: python scripts/test_vps_audio.py
"""
import base64
import math
import os
import struct
import sys
import wave
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Auto-switch to project .venv python if running in global/system python
if sys.prefix == sys.base_prefix:
    venv_python = PROJECT_ROOT / ".venv" / "bin" / "python"
    if venv_python.exists():
        os.execv(str(venv_python), [str(venv_python)] + sys.argv)

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

print("🚀 Инициализация окружения теста...", flush=True)

import asyncio
import platform
import subprocess
import time

from recorder.audio import AudioRecorder, PULSE_SERVER_URI, PULSE_SOCKET_PATH
from recorder.browser import CHROMIUM_ARGS, ensure_xvfb


def generate_test_wav(file_path: Path, duration_sec: float = 5.0, freq: float = 440.0):
    """Generates a loud 440 Hz 16-bit mono sine wave WAV file."""
    sample_rate = 44100
    n_samples = int(sample_rate * duration_sec)
    with wave.open(str(file_path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        data = bytearray()
        for i in range(n_samples):
            val = int(30000 * math.sin(2.0 * math.pi * freq * i / sample_rate))
            data.extend(struct.pack("<h", val))
        wf.writeframes(data)


async def run_audio_pipeline():
    print("==========================================================", flush=True)
    print("🔊 Тест сквозного захвата звука (Chromium -> PulseAudio -> ffmpeg)", flush=True)
    print("==========================================================", flush=True)

    current_os = platform.system()
    print(f"• Операционная система: {current_os}", flush=True)

    temp_dir = PROJECT_ROOT / "temp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    test_wav = temp_dir / "sine440.wav"
    generate_test_wav(test_wav, duration_sec=5.0, freq=440.0)

    if current_os == "Linux":
        print(f"• Настройка PulseAudio (сокет: {PULSE_SOCKET_PATH})...", flush=True)
        AudioRecorder.ensure_pulseaudio()

        # Unload suspend-on-idle module
        subprocess.run(["pactl", f"--server={PULSE_SERVER_URI}", "unload-module", "module-suspend-on-idle"], capture_output=True, timeout=2)
        subprocess.run(["pactl", f"--server={PULSE_SERVER_URI}", "suspend-sink", "VirtualSink", "0"], capture_output=True, timeout=2)
        subprocess.run(["pactl", f"--server={PULSE_SERVER_URI}", "suspend-source", "VirtualSink.monitor", "0"], capture_output=True, timeout=2)

        # ----------------------------------------------------------------------
        # Step 1: Test PulseAudio loopback with paplay
        # ----------------------------------------------------------------------
        print("\n--- [Этап 1] Проверка прямого PulseAudio loopback через paplay ---", flush=True)
        paplay_mp3 = temp_dir / "test_paplay_capture.mp3"
        if paplay_mp3.exists():
            paplay_mp3.unlink()

        paplay_recorder = AudioRecorder(output_path=paplay_mp3, pulse_source="VirtualSink.monitor")
        paplay_recorder.start()
        time.sleep(0.5)

        print("• Проигрываю тестовый звук через paplay в VirtualSink...", flush=True)
        subprocess.run(
            ["paplay", f"--server={PULSE_SERVER_URI}", "--device=VirtualSink", str(test_wav)],
            capture_output=True,
            timeout=6,
        )
        paplay_recorder.stop()
        paplay_level = AudioRecorder.check_audio_level(paplay_mp3)
        print(f"• Результат paplay: max_volume={paplay_level['max_volume']} dB, is_silent={paplay_level['is_silent']}", flush=True)
        if not paplay_level["is_silent"]:
            print("✅ PulseAudio VirtualSink и ffmpeg работают безупречно!", flush=True)
        else:
            print("⚠️ paplay записал тишину.", flush=True)

    # ----------------------------------------------------------------------
    # Step 2: Test Chromium Web Audio API & Media playback
    # ----------------------------------------------------------------------
    print("\n--- [Этап 2] Проверка воспроизведения из Chromium в VirtualSink ---", flush=True)
    test_mp3 = temp_dir / "test_chromium_capture.mp3"
    if test_mp3.exists():
        test_mp3.unlink()

    recorder = AudioRecorder(output_path=test_mp3, pulse_source="VirtualSink.monitor")
    print("• Запускаю запись ffmpeg (VirtualSink.monitor)...", flush=True)
    recorder.start()

    from playwright.async_api import async_playwright

    print("• Запускаю Playwright Chromium...", flush=True)
    launch_env = dict(os.environ)
    if current_os == "Linux":
        ensure_xvfb()
        launch_env["PULSE_SERVER"] = PULSE_SERVER_URI
        launch_env["PULSE_SINK"] = "VirtualSink"
        launch_env["PULSE_COOKIE"] = "/dev/null"
        launch_env["DISPLAY"] = os.environ.get("DISPLAY", ":99")

    import shutil
    has_chrome = False
    if current_os == "Linux":
        if shutil.which("google-chrome") or shutil.which("google-chrome-stable"):
            has_chrome = True
            print("• Обнаружен установленный системный Google Chrome!", flush=True)
        else:
            print("• Google Chrome не найден в системе. Устанавливаю официальный Google Chrome через Playwright...", flush=True)
            res = subprocess.run([sys.executable, "-m", "playwright", "install", "chrome"], capture_output=False)
            if res.returncode == 0:
                has_chrome = True
                print("• Google Chrome успешно установлен!", flush=True)
            else:
                print("⚠️ Не удалось установить Google Chrome через Playwright. Пробую запуск стандартного chromium.", flush=True)

    # Clean Chromium args without conflicting flags
    args = [a for a in CHROMIUM_ARGS if a != "--disable-features=AudioServiceOutOfProcess"]

    # Base64 encoded test WAV for HTML5 <audio> element testing
    wav_bytes = test_wav.read_bytes()
    wav_b64 = base64.b64encode(wav_bytes).decode("ascii")

    async with async_playwright() as p:
        use_headless = False if current_os == "Linux" else True
        launch_kwargs = {
            "headless": use_headless,
            "args": args,
            "env": launch_env,
        }
        if has_chrome:
            launch_kwargs["channel"] = "chrome"
            print(f"• Запускаю полноценный Google Chrome (channel='chrome', headless={use_headless})...", flush=True)
        else:
            print(f"• Запускаю стандартный Chromium (headless={use_headless})...", flush=True)

        browser = await p.chromium.launch(**launch_kwargs)
        context = await browser.new_context()
        page = await context.new_page()

        # HTML with both HTML5 <audio> tag and Web Audio tone generator
        html_content = f"""
        <!DOCTYPE html>
        <html>
        <head><title>Sound Generator</title></head>
        <body style="background: #222; color: #fff; padding: 20px;">
            <h1>Web Audio & HTML5 Audio Player</h1>
            <audio id="audioElem" src="data:audio/wav;base64,{wav_b64}" autoplay loop></audio>
            <div id="status">Starting...</div>
            <script>
                let ctx = null;
                async function startSound() {{
                    const res = {{}};
                    try {{
                        const audio = document.getElementById('audioElem');
                        audio.volume = 1.0;
                        await audio.play();
                        res.audio_elem = 'playing';
                    }} catch (e) {{
                        res.audio_elem = 'error: ' + e;
                    }}
                    try {{
                        ctx = new (window.AudioContext || window.webkitAudioContext)();
                        await ctx.resume();
                        const osc = ctx.createOscillator();
                        osc.type = 'sine';
                        osc.frequency.setValueAtTime(440, ctx.currentTime);
                        const gain = ctx.createGain();
                        gain.gain.setValueAtTime(0.9, ctx.currentTime);
                        osc.connect(gain);
                        gain.connect(ctx.destination);
                        osc.start();
                        res.web_audio = {{ state: ctx.state, rate: ctx.sampleRate }};
                    }} catch (e) {{
                        res.web_audio = {{ error: String(e) }};
                    }}
                    document.getElementById('status').innerText = JSON.stringify(res);
                    return res;
                }}
                window.startSound = startSound;
            </script>
        </body>
        </html>
        """
        await page.set_content(html_content)
        await page.mouse.click(100, 100)

        audio_status = await page.evaluate("window.startSound()")
        print(f"• Статус генераторов звука в браузере: {audio_status}", flush=True)

        if current_os == "Linux":
            await asyncio.sleep(1.0)
            res_inputs = subprocess.run(
                ["pactl", f"--server={PULSE_SERVER_URI}", "list", "sink-inputs"],
                capture_output=True,
                text=True,
                timeout=3,
            )
            print(f"• Детальный статус sink-inputs:\n{res_inputs.stdout.strip() or '  (нет активных потоков)'}", flush=True)

        print("• Воспроизведение звука в течение 5 секунд...", flush=True)
        await asyncio.sleep(5.0)

        await browser.close()

    print("• Останавливаю запись ffmpeg...", flush=True)
    recorder.stop()

    print("\n----------------------------------------------------------", flush=True)
    print("📊 Результаты анализа аудиозаписи Chromium:", flush=True)
    level_info = AudioRecorder.check_audio_level(test_mp3)
    print(f"• Файл: {test_mp3.resolve()}", flush=True)
    print(f"• Размер: {test_mp3.stat().st_size if test_mp3.exists() else 0} байт", flush=True)
    print(f"• Максимальная громкость: {level_info['max_volume']} dB", flush=True)
    print(f"• Средняя громкость: {level_info['mean_volume']} dB", flush=True)
    print(f"• Детектор тишины: {'⚠️ ТИШИНА' if level_info['is_silent'] else '✅ ЕСТЬ ЗВУК'}", flush=True)
    print("----------------------------------------------------------", flush=True)

    if not level_info["is_silent"] and level_info["max_volume"] > -40.0:
        print("🎉 ТЕСТ УСПЕШЕН! Звук из Chromium успешно дошел до PulseAudio и записан в MP3.", flush=True)
        return 0
    elif current_os != "Linux":
        print("ℹ️ На Windows/macOS виртуальный аудио-кабель не проверялся (тест актуален для Linux VPS).", flush=True)
        return 0
    else:
        print("❌ ОШИБКА: Записана тишина. Звук из Chromium не попал в VirtualSink.", flush=True)
        return 1


if __name__ == "__main__":
    code = asyncio.run(run_audio_pipeline())
    sys.exit(code)
