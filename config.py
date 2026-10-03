import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Union
from dotenv import load_dotenv
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
TEMP_DIR = BASE_DIR / "temp"
TEMP_DIR.mkdir(exist_ok=True, parents=True)

DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True, parents=True)
USER_SETTINGS_FILE = DATA_DIR / "user_settings.json"


def save_json_atomic(file_path: Union[str, Path], data: Any, indent: int = 2):
    """
    Atomically writes data to a JSON file.
    Writes to a unique temporary file first, flushes and syncs to disk,
    then replaces the target file atomically via os.replace.
    Prevents 0-byte file corruption on unexpected crashes or power outages.
    """
    target = Path(file_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp_file = target.with_suffix(f".tmp_{os.getpid()}_{int(time.time() * 1000)}")
    try:
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=indent)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_file, target)
    except Exception as e:
        if temp_file.exists():
            try:
                temp_file.unlink(missing_ok=True)
            except Exception:
                pass
        raise e


def load_user_settings() -> Dict[str, Any]:
    """Loads runtime user settings from disk."""
    if USER_SETTINGS_FILE.exists():
        try:
            with open(USER_SETTINGS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"Failed to load user settings: {e}")
            return {}
    return {}


def save_user_setting(key: str, value: Any):
    """Saves a runtime user setting to disk atomically."""
    data = load_user_settings()
    data[key] = value
    try:
        save_json_atomic(USER_SETTINGS_FILE, data)
    except Exception as e:
        logger.warning(f"Failed to save user setting {key}: {e}")


# Load environment variables from .env file by absolute path
load_dotenv(BASE_DIR / ".env")


class Settings(BaseModel):
    """Configuration settings for StudentAssistant application."""

    # Google Gemini AI Settings
    gemini_api_key: str = Field(
        default_factory=lambda: os.getenv("GEMINI_API_KEY", "")
    )
    gemini_model: str = Field(
        default_factory=lambda: os.getenv("GEMINI_MODEL", "gemini-flash-latest")
    )


    # Telegram Bot Settings
    telegram_bot_token: str = Field(
        default_factory=lambda: os.getenv("TELEGRAM_BOT_TOKEN", "")
    )
    telegram_channel_id: str = Field(
        default_factory=lambda: os.getenv("TELEGRAM_CHANNEL_ID", "")
    )
    telegram_group_id: str = Field(
        default_factory=lambda: os.getenv("TELEGRAM_GROUP_ID", os.getenv("TELEGRAM_CHANNEL_ID", "-1002443546271"))
    )
    allowed_telegram_users: str = Field(
        default_factory=lambda: os.getenv("ALLOWED_TELEGRAM_USERS", "1017867274")
    )
    telegram_api_server: Optional[str] = Field(
        default_factory=lambda: os.getenv("TELEGRAM_API_SERVER")
    )
    telegram_api_local_root: Optional[str] = Field(
        default_factory=lambda: os.getenv("TELEGRAM_API_LOCAL_ROOT")
    )

    # GitHub Settings
    github_repo_url: str = Field(
        default_factory=lambda: os.getenv("GITHUB_REPO_URL", "")
    )
    github_token: str = Field(
        default_factory=lambda: os.getenv("GITHUB_TOKEN", "")
    )
    github_branch: str = Field(
        default_factory=lambda: os.getenv("GITHUB_BRANCH", "main")
    )
    proxy: Optional[str] = Field(
        default_factory=lambda: os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY") or os.getenv("PROXY")
    )

    # Visual Slide Capture & Deduplication
    slide_similarity_threshold: float = Field(
        default_factory=lambda: float(os.getenv("SLIDE_SIMILARITY_THRESHOLD", "0.95"))
    )
    slide_capture_interval_sec: int = Field(
        default_factory=lambda: int(os.getenv("SLIDE_CAPTURE_INTERVAL_SEC", "5"))
    )

    # Meeting Capture & Platform Settings
    student_name: str = Field(
        default_factory=lambda: load_user_settings().get("student_name") or os.getenv("STUDENT_NAME", "Студент")
    )
    max_lecture_duration_min: int = Field(
        default_factory=lambda: int(os.getenv("MAX_LECTURE_DURATION_MIN", "180"))
    )
    min_lecture_duration_min: int = Field(
        default_factory=lambda: int(os.getenv("MIN_LECTURE_DURATION_MIN", "65"))
    )
    participant_drop_threshold: int = Field(
        default_factory=lambda: int(os.getenv("PARTICIPANT_DROP_THRESHOLD", "3"))
    )

    # Smart Exit 2.0 Settings
    smart_exit_min_duration_min: float = Field(
        default_factory=lambda: float(os.getenv("SMART_EXIT_MIN_DURATION_MIN", "95.3"))
    )
    smart_exit_silence_sample_sec: float = Field(
        default_factory=lambda: float(os.getenv("SMART_EXIT_SILENCE_SAMPLE_SEC", "10.0"))
    )
    smart_exit_consecutive_silence_threshold: int = Field(
        default_factory=lambda: int(os.getenv("SMART_EXIT_CONSECUTIVE_SILENCE_THRESHOLD", "3"))
    )
    smart_exit_next_pair_buffer_min: int = Field(
        default_factory=lambda: int(os.getenv("SMART_EXIT_NEXT_PAIR_BUFFER_MIN", "4"))
    )

    # Audio Silence Trimming Settings (Post-processing)
    min_leading_silence_sec: float = Field(
        default_factory=lambda: float(os.getenv("MIN_LEADING_SILENCE_SEC", "20.0"))
    )
    min_trailing_silence_sec: float = Field(
        default_factory=lambda: float(os.getenv("MIN_TRAILING_SILENCE_SEC", "25.0"))
    )
    silence_threshold_db: float = Field(
        default_factory=lambda: float(os.getenv("SILENCE_THRESHOLD_DB", "-38.0"))
    )

    # Timezone settings (e.g. Europe/Moscow for UTC+3)
    timezone: str = Field(
        default_factory=lambda: os.getenv("TIMEZONE", "Europe/Moscow")
    )

    # Simulation / Dry-run Mode
    simulation_mode: bool = Field(
        default_factory=lambda: os.getenv("SIMULATION_MODE", "false").lower() in ("true", "1", "yes")
    )


# Global singleton settings instance
settings = Settings()


def set_student_name(new_name: str) -> str:
    """Updates student_name at runtime and persists it to user_settings.json."""
    clean = new_name.strip()
    if clean:
        settings.student_name = clean
        save_user_setting("student_name", clean)
        logger.info(f"Updated attendee student_name to '{clean}'")
    return settings.student_name


def get_local_now() -> "datetime":
    """
    Returns current datetime localized to settings.timezone (default Europe/Moscow).
    Falls back to UTC+3 fixed offset if ZoneInfo lookup fails.
    """
    from datetime import datetime, timezone, timedelta
    try:
        import zoneinfo
        tz = zoneinfo.ZoneInfo(settings.timezone)
        return datetime.now(tz)
    except Exception:
        # Fallback to fixed UTC+3 (Moscow)
        tz = timezone(timedelta(hours=3))
        return datetime.now(tz)


def get_owner_user_ids() -> List[int]:
    """
    Parses ALLOWED_TELEGRAM_USERS env variable into a list of integer owner IDs.
    Defaults to [1017867274] if not configured or empty.
    """
    raw = (settings.allowed_telegram_users or "").strip()
    if not raw:
        return [1017867274]
    ids = []
    for part in raw.replace(",", " ").split():
        try:
            ids.append(int(part.strip()))
        except ValueError:
            pass
    return ids or [1017867274]


def get_headman_telegram_id() -> Optional[int]:
    """
    Retrieves the Telegram user ID assigned as the headman (староста).
    First checks runtime user_settings.json, then falls back to HEADMAN_TELEGRAM_USERS env if present.
    """
    data = load_user_settings()
    hid = data.get("headman_telegram_id")
    if hid is not None:
        try:
            return int(hid)
        except (ValueError, TypeError):
            pass

    env_hid = os.getenv("HEADMAN_TELEGRAM_USERS", "").strip()
    if env_hid:
        try:
            return int(env_hid.split(",")[0].strip())
        except (ValueError, IndexError):
            pass
    return None


def set_headman_telegram_id(user_id: Optional[int]) -> Optional[int]:
    """
    Sets or clears the headman Telegram ID in user_settings.json.
    Pass None or 0 to revoke headman permissions.
    """
    if user_id:
        val = int(user_id)
        save_user_setting("headman_telegram_id", val)
        logger.info(f"Updated headman_telegram_id to {val}")
        return val
    else:
        data = load_user_settings()
        data.pop("headman_telegram_id", None)
        save_json_atomic(USER_SETTINGS_FILE, data)
        logger.info("Cleared headman_telegram_id")
        return None


def is_headman(user_id: int) -> bool:
    """Returns True if the user is the designated headman (староста)."""
    hid = get_headman_telegram_id()
    return hid is not None and user_id == hid


def is_owner(user_id: int) -> bool:
    """Returns True if the user is in the owner whitelist."""
    return user_id in get_owner_user_ids()


def get_allowed_user_ids() -> List[int]:
    """
    Parses allowed user IDs: includes both owners and the headman (if assigned).
    Ensures AuthMiddleware allows headman requests to proceed.
    """
    owners = get_owner_user_ids()
    hid = get_headman_telegram_id()
    if hid and hid not in owners:
        return owners + [hid]
    return owners



