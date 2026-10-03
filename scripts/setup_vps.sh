#!/usr/bin/env bash
# ==============================================================================
# StudentAssistant VPS Setup Script (Ubuntu Server 22.04 / 24.04 LTS)
# Fully autonomous, idempotent, and self-contained installer.
# ==============================================================================

set -e

echo "=========================================================="
echo "🚀 Настройка сервера StudentAssistant"
echo "=========================================================="

SUDO=""
if [ "$(id -u)" -ne 0 ]; then
    SUDO="sudo"
fi

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_ROOT"

# 1. System Packages Update & Installation
echo -e "\n📦 [1/5] Установка системных пакетов (Xvfb, PulseAudio, ffmpeg, Python)..."
$SUDO apt-get update -y

# Handle Ubuntu 24.04 (Noble) vs 22.04 (Jammy) asound package naming
ASOUND_PKG="libasound2"
if apt-cache show libasound2t64 >/dev/null 2>&1; then
    ASOUND_PKG="libasound2t64"
fi

$SUDO apt-get install -y \
    xvfb \
    pulseaudio \
    pulseaudio-utils \
    libasound2-plugins \
    alsa-utils \
    ffmpeg \
    python3 \
    python3-pip \
    python3-venv \
    git \
    curl \
    fonts-liberation \
    fonts-noto-color-emoji \
    "$ASOUND_PKG"

# 2. Python Virtual Environment & Full Dependencies
echo -e "\n🐍 [2/5] Подготовка виртуального окружения Python..."
if [ ! -f ".venv/bin/python" ]; then
    rm -rf .venv
    python3 -m venv .venv
fi

source .venv/bin/activate
pip install --upgrade pip

if [ -f "requirements.txt" ]; then
    pip install -r requirements.txt
fi

# Explicitly guarantee all required packages are installed
pip install \
    "playwright>=1.40.0" \
    "aiogram>=3.3.0" \
    "google-genai>=0.1.1" \
    "httpx>=0.26.0" \
    "pillow>=10.0.0" \
    "imagehash>=4.3.1" \
    "pydantic>=2.5.0" \
    "pydantic-settings>=2.0.0" \
    "python-dotenv>=1.0.0" \
    "pytest>=8.0.0" \
    "anyio>=4.0.0"

# 3. Playwright Chromium & Google Chrome Installation
echo -e "\n🌐 [3/5] Установка Chromium, Google Chrome и системных библиотек браузера..."
.venv/bin/python -m playwright install chromium chrome
$SUDO .venv/bin/python -m playwright install-deps chromium chrome || true

# 4. PulseAudio Virtual Audio Cable Setup
echo -e "\n🔊 [4/5] Настройка виртуального аудио-кабеля (VirtualSink)..."
mkdir -p ~/.config/pulse
cat << 'EOF' > ~/.config/pulse/default.pa
.include /etc/pulse/default.pa
load-module module-native-protocol-unix socket=/tmp/pulse-socket auth-anonymous=1
load-module module-null-sink sink_name=VirtualSink sink_properties=device.description=VirtualSink
set-default-sink VirtualSink
EOF

# Ensure pulseaudio is started with fixed socket
pulseaudio --start --exit-idle-time=-1 --disallow-exit >/dev/null 2>&1 || true
pactl load-module module-native-protocol-unix socket=/tmp/pulse-socket auth-anonymous=1 >/dev/null 2>&1 || true
pactl load-module module-null-sink sink_name=VirtualSink sink_properties=device.description=VirtualSink >/dev/null 2>&1 || true
pactl set-default-sink VirtualSink >/dev/null 2>&1 || true

# 5. Environment Template & Systemd Service Creation
echo -e "\n⚙️ [5/5] Создание конфигурации и systemd-сервиса..."
if [ ! -f ".env" ] && [ -f ".env.example" ]; then
    cp .env.example .env
    echo "   ℹ️ Создан файл .env (не забудь вписать свои API-ключи)."
fi

SERVICE_FILE="/etc/systemd/system/student-assistant.service"
CURRENT_USER="$(whoami)"

$SUDO bash -c "cat << EOF > '$SERVICE_FILE'
[Unit]
Description=StudentAssistant Telegram Bot & Meeting Recorder
After=network.target sound.target

[Service]
Type=simple
User=$CURRENT_USER
WorkingDirectory=$PROJECT_ROOT
EnvironmentFile=-$PROJECT_ROOT/.env
Environment=\"PATH=$PROJECT_ROOT/.venv/bin:/usr/local/bin:/usr/bin\"
Environment=\"PYTHONPATH=$PROJECT_ROOT\"
Environment=\"DISPLAY=:99\"
Environment=\"PULSE_SERVER=unix:/tmp/pulse-socket\"
Environment=\"PULSE_COOKIE=/dev/null\"
ExecStartPre=/bin/bash -c "pulseaudio --check || pulseaudio --start --exit-idle-time=-1 --disallow-exit >/dev/null 2>&1 || true; pactl load-module module-native-protocol-unix socket=/tmp/pulse-socket auth-anonymous=1 >/dev/null 2>&1 || true; pactl load-module module-null-sink sink_name=VirtualSink sink_properties=device.description=VirtualSink >/dev/null 2>&1 || true; pactl set-default-sink VirtualSink >/dev/null 2>&1 || true; pactl set-sink-mute VirtualSink 0 >/dev/null 2>&1 || true; pactl set-sink-volume VirtualSink 100% >/dev/null 2>&1 || true; pactl set-source-mute VirtualSink.monitor 0 >/dev/null 2>&1 || true; pactl set-source-volume VirtualSink.monitor 100% >/dev/null 2>&1 || true; pgrep -f 'Xvfb :99' >/dev/null 2>&1 || (rm -f /tmp/.X99-lock /tmp/.X11-unix/X99 && Xvfb :99 -screen 0 1920x1080x24 -ac +extension GLX +render -noreset -nolisten tcp &)"
ExecStart=$PROJECT_ROOT/.venv/bin/python bot/main.py
Restart=always
RestartSec=5
TimeoutStopSec=10

[Install]
WantedBy=multi-user.target
EOF"

$SUDO systemctl daemon-reload
$SUDO systemctl enable student-assistant

echo -e "\n=========================================================="
echo "🎉 Настройка завершена на 100%! Сервер готов к работе."
echo "=========================================================="
echo "Следующие шаги:"
echo "1. Открой .env и укажи свои ключи: nano .env"
echo "2. Запусти бота: systemctl enable --now student-assistant"
echo "3. Проверь статус: systemctl status student-assistant"
echo "=========================================================="

