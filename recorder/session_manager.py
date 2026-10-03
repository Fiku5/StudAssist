import json
import logging
import re
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Union

from config import TEMP_DIR, get_local_now, save_json_atomic
from recorder.schedule_manager import slugify_subject

logger = logging.getLogger(__name__)

SLIDES_BACKUP_RETENTION_SECONDS: int = 7 * 24 * 3600  # 7 days


def generate_session_id(subject_slug: str = "unsorted", dt: Optional[datetime] = None) -> str:
    """
    Generates a clean, human-readable session ID:
    Format: YYYY-MM-DD_HH-MM_subject_slug
    e.g. 2026-09-10_09-00_arhitektura_po
    """
    target_dt = dt or get_local_now()
    date_str = target_dt.strftime("%Y-%m-%d_%H-%M")
    clean_slug = slugify_subject(subject_slug)
    return f"{date_str}_{clean_slug}"


class SessionManager:
    """
    Manages structured storage and metadata for recorded lecture sessions.
    
    Structure:
    temp/sessions/
    └── {YYYY-MM-DD}_{HH-MM}_{subject_slug}/
        ├── metadata.json
        ├── audio.mp3
        ├── slides/
        └── attachments/
    """

    def __init__(self, base_dir: Optional[Path] = None):
        self.base_dir = base_dir or (TEMP_DIR / "sessions")
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def create_session(
        self,
        session_id: Optional[str] = None,
        subject: str = "Онлайн-пара",
        subject_slug: Optional[str] = None,
        dt: Optional[datetime] = None,
    ) -> Path:
        """Initializes a new session directory with metadata."""
        now = dt or get_local_now()
        slug = subject_slug or (slugify_subject(subject) if subject != "Онлайн-пара" else "unsorted")

        if not session_id:
            session_id = generate_session_id(subject_slug=slug, dt=now)

        session_dir = self.base_dir / session_id
        session_dir.mkdir(parents=True, exist_ok=True)
        (session_dir / "slides").mkdir(parents=True, exist_ok=True)
        (session_dir / "attachments").mkdir(parents=True, exist_ok=True)

        metadata = {
            "session_id": session_id,
            "created_at": now.strftime("%Y-%m-%d %H:%M:%S"),
            "subject": subject,
            "subject_slug": slug,
            "duration_seconds": 0,
            "exit_reason": "",
            "status": "recording",
            "postprocess_pending": False,
            "audio": {
                "exists": False,
                "size_bytes": 0,
                "max_volume": -91.0,
                "is_silent": True,
            },
            "slides_count": 0,
            "attachments": [],
            "published": None,
        }
        self.save_session_metadata(session_id, metadata)
        logger.info(f"Created new session: {session_id} for subject '{subject}' at {session_dir}")

        # Clean up any expired slide backups (> 7 days old) automatically
        try:
            self.cleanup_expired_backups()
        except Exception as e:
            logger.warning(f"Error running expired backups cleanup in create_session: {e}")

        return session_dir

    def get_session_dir(self, session_id: str) -> Path:
        return self.base_dir / session_id

    def get_session_metadata(self, session_id: str) -> Optional[dict]:
        meta_file = self.get_session_dir(session_id) / "metadata.json"
        if not meta_file.exists():
            return None
        try:
            with open(meta_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Failed to read metadata for session {session_id}: {e}")
            return None

    def save_session_metadata(self, session_id: str, metadata: dict):
        meta_file = self.get_session_dir(session_id) / "metadata.json"
        save_json_atomic(meta_file, metadata)

    def update_session_metadata(self, session_id: str, updates: dict) -> dict:
        meta = self.get_session_metadata(session_id) or {
            "session_id": session_id,
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "subject": "Онлайн-пара",
            "subject_slug": "unsorted",
            "status": "unknown",
            "attachments": [],
        }
        meta.update(updates)
        self.save_session_metadata(session_id, meta)
        return meta

    def assign_session_subject(
        self,
        session_id: str,
        subject_title: str,
        subject_slug: Optional[str] = None,
    ) -> dict:
        """Assigns an academic subject to a session and updates metadata."""
        slug = subject_slug or slugify_subject(subject_title)
        updates = {
            "subject": subject_title,
            "subject_slug": slug,
        }
        meta = self.update_session_metadata(session_id, updates)
        logger.info(f"Assigned session {session_id} to subject '{subject_title}' ({slug})")
        return meta

    def finalize_recording(
        self,
        session_id: str,
        duration_seconds: float,
        exit_reason: str,
        audio_path: Optional[Path] = None,
        unique_slides: Optional[List[Path]] = None,
        is_silent: bool = False,
        audio_level: Optional[dict] = None,
        attendance: Optional[dict] = None,
    ) -> dict:
        """Finalizes recorded data and updates metadata status to 'ready_for_review'."""
        audio_exists = False
        size_bytes = 0
        max_vol = -91.0
        if audio_path and audio_path.exists():
            audio_exists = True
            size_bytes = audio_path.stat().st_size
            if audio_level:
                max_vol = audio_level.get("max_volume", -91.0)
                is_silent = audio_level.get("is_silent", is_silent)

        slides_count = len(unique_slides) if unique_slides is not None else len(self.get_slides(session_id))

        updates = {
            "duration_seconds": round(duration_seconds, 1),
            "exit_reason": exit_reason,
            "status": "ready_for_review",
            "postprocess_pending": False,
            "postprocessed_at": get_local_now().strftime("%Y-%m-%d %H:%M:%S"),
            "audio": {
                "exists": audio_exists,
                "size_bytes": size_bytes,
                "max_volume": max_vol,
                "is_silent": is_silent,
            },
            "slides_count": slides_count,
        }
        if attendance is not None:
            updates["attendance"] = attendance

        return self.update_session_metadata(session_id, updates)

    def get_attachment_target_path(self, session_id: str, filename: str) -> Path:
        """Returns the safe destination Path for an attachment in the session directory."""
        session_dir = self.get_session_dir(session_id)
        att_dir = session_dir / "attachments"
        att_dir.mkdir(parents=True, exist_ok=True)

        # Sanitize filename against Path Traversal attacks
        clean_name = Path(filename).name.strip()
        clean_name = re.sub(r'^[.\s]+', '', clean_name)
        clean_name = clean_name.replace("/", "_").replace("\\", "_")
        safe_filename = clean_name if clean_name else f"attachment_{int(time.time())}.bin"
        return att_dir / safe_filename

    def add_attachment(self, session_id: str, filename: str, content: Union[bytes, str, Path]) -> Path:
        """
        Saves or registers a supplementary file uploaded by the student into attachments/.
        Supports content as bytes or as an existing file Path (avoiding memory loading).
        """
        target_file = self.get_attachment_target_path(session_id, filename)
        safe_filename = target_file.name

        if isinstance(content, (str, Path)):
            src_path = Path(content)
            if src_path.resolve() != target_file.resolve():
                shutil.copy2(src_path, target_file)
            size_bytes = target_file.stat().st_size
        else:
            with open(target_file, "wb") as f:
                f.write(content)
            size_bytes = len(content)

        meta = self.get_session_metadata(session_id) or {}
        attachments = meta.get("attachments", [])
        # Avoid duplicate entries for the same filename
        attachments = [a for a in attachments if a.get("filename") != safe_filename]
        attachments.append({
            "filename": safe_filename,
            "size_bytes": size_bytes,
            "uploaded_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })
        self.update_session_metadata(session_id, {"attachments": attachments})
        logger.info(f"Attached {safe_filename} ({size_bytes} bytes) to session {session_id}")
        return target_file

    def get_attachments(self, session_id: str) -> List[Path]:
        att_dir = self.get_session_dir(session_id) / "attachments"
        if not att_dir.exists():
            return []
        # Filter out 0-byte orphan files from broken downloads
        return sorted([f for f in att_dir.iterdir() if f.is_file() and f.stat().st_size > 0])

    def delete_attachment(self, session_id: str, filename: str) -> bool:
        """
        Deletes a specific attachment file from attachments/ and updates metadata.json.
        """
        att_dir = self.get_session_dir(session_id) / "attachments"
        clean_name = Path(filename).name.strip()
        target_file = att_dir / clean_name
        deleted = False
        if target_file.exists() and target_file.is_file():
            try:
                target_file.unlink()
                deleted = True
                logger.info(f"Deleted attachment {clean_name} from session {session_id}")
            except Exception as e:
                logger.error(f"Failed to delete attachment {clean_name}: {e}")
                return False

        meta = self.get_session_metadata(session_id) or {}
        attachments = meta.get("attachments", [])
        new_attachments = [a for a in attachments if a.get("filename") != clean_name]
        if len(new_attachments) != len(attachments) or deleted:
            self.update_session_metadata(session_id, {"attachments": new_attachments})
            return True
        return False

    def clear_attachments(self, session_id: str) -> int:
        """
        Deletes all attachment files from attachments/ and clears metadata.json.
        Returns the number of deleted files.
        """
        att_dir = self.get_session_dir(session_id) / "attachments"
        count = 0
        if att_dir.exists() and att_dir.is_dir():
            for f in list(att_dir.iterdir()):
                if f.is_file():
                    try:
                        f.unlink()
                        count += 1
                    except Exception as e:
                        logger.warning(f"Could not delete {f.name}: {e}")
        self.update_session_metadata(session_id, {"attachments": []})
        logger.info(f"Cleared {count} attachment(s) for session {session_id}")
        return count

    def get_slides(self, session_id: str) -> List[Path]:
        slides_dir = self.get_session_dir(session_id) / "slides"
        if not slides_dir.exists():
            return []
        return sorted([f for f in slides_dir.glob("slide_*.png")])

    def has_slides_backup(self, session_id: str) -> bool:
        """
        Checks if a valid, unexpired backup of raw slides exists in slides_raw/.
        If the backup is >= 7 days old, it is automatically purged and False is returned.
        """
        session_dir = self.get_session_dir(session_id)
        raw_dir = session_dir / "slides_raw"
        if not raw_dir.exists() or not raw_dir.is_dir():
            return False

        meta_file = raw_dir / ".backup_meta.json"
        created_at: Optional[datetime] = None
        if meta_file.exists():
            try:
                with open(meta_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    created_at = datetime.fromisoformat(data["created_at"])
            except Exception as e:
                logger.warning(f"Failed to read backup metadata in {raw_dir}: {e}")

        if not created_at:
            try:
                created_at = datetime.fromtimestamp(raw_dir.stat().st_mtime)
            except Exception:
                created_at = None

        if created_at:
            if hasattr(created_at, "tzinfo") and created_at.tzinfo is not None:
                created_at = created_at.replace(tzinfo=None)
            now_dt = get_local_now().replace(tzinfo=None)
            age_sec = (now_dt - created_at).total_seconds()
            if age_sec >= SLIDES_BACKUP_RETENTION_SECONDS:
                logger.info(
                    f"Slides backup for session {session_id} expired ({age_sec / 86400:.1f} days old >= 7 days). Purging."
                )
                shutil.rmtree(raw_dir, ignore_errors=True)
                return False

        raw_slides = list(raw_dir.glob("*.png"))
        return len(raw_slides) > 0

    def cleanup_expired_backups(self, retention_seconds: Optional[int] = None) -> int:
        """
        Scans all sessions in base_dir and automatically purges slides_raw/ backups
        that are older than retention_seconds (defaults to SLIDES_BACKUP_RETENTION_SECONDS = 7 days).
        Returns the number of purged backup directories.
        """
        retention = retention_seconds or SLIDES_BACKUP_RETENTION_SECONDS
        purged_count = 0
        if not self.base_dir.exists():
            return purged_count

        for sdir in self.base_dir.iterdir():
            if not sdir.is_dir():
                continue
            raw_dir = sdir / "slides_raw"
            if not raw_dir.is_dir():
                continue

            meta_file = raw_dir / ".backup_meta.json"
            created_at: Optional[datetime] = None
            if meta_file.exists():
                try:
                    with open(meta_file, "r", encoding="utf-8") as f:
                        data = json.load(f)
                        created_at = datetime.fromisoformat(data["created_at"])
                except Exception as e:
                    logger.warning(f"Failed to read backup metadata in {raw_dir}: {e}")

            if not created_at:
                try:
                    created_at = datetime.fromtimestamp(raw_dir.stat().st_mtime)
                except Exception:
                    created_at = None

            if created_at:
                if hasattr(created_at, "tzinfo") and created_at.tzinfo is not None:
                    created_at = created_at.replace(tzinfo=None)
                now_dt = get_local_now().replace(tzinfo=None)
                age_sec = (now_dt - created_at).total_seconds()
                if age_sec >= retention:
                    logger.info(
                        f"Purging expired slides backup for session {sdir.name} "
                        f"({age_sec / 86400:.1f} days old >= {retention / 86400:.1f} days)."
                    )
                    shutil.rmtree(raw_dir, ignore_errors=True)
                    purged_count += 1

        return purged_count

    def get_disk_space_info(self, target_path: Optional[Path] = None) -> Dict[str, float]:
        """
        Returns total, used, and free disk space for the partition containing target_path (or base_dir).
        """
        path = target_path or self.base_dir
        try:
            total, used, free = shutil.disk_usage(str(path))
            gb = 1024 ** 3
            return {
                "total_gb": round(total / gb, 2),
                "used_gb": round(used / gb, 2),
                "free_gb": round(free / gb, 2),
                "free_percent": round((free / total) * 100, 1) if total > 0 else 0.0,
            }
        except Exception as e:
            logger.warning(f"Could not determine disk usage for {path}: {e}")
            return {
                "total_gb": 0.0,
                "used_gb": 0.0,
                "free_gb": 999.0,
                "free_percent": 100.0,
            }

    def get_free_disk_space_gb(self, target_path: Optional[Path] = None) -> float:
        """Returns free disk space in Gigabytes."""
        return self.get_disk_space_info(target_path).get("free_gb", 999.0)


    def create_slides_backup(self, session_id: str, force: bool = False) -> bool:
        """
        Creates a full copy of slides/ into slides_raw/ before any postprocessing or alteration.
        Preserves existing backup unless force=True or backup is expired.
        Writes .backup_meta.json with ISO timestamp and slides count.
        """
        session_dir = self.get_session_dir(session_id)
        slides_dir = session_dir / "slides"
        raw_slides_dir = session_dir / "slides_raw"

        if not slides_dir.exists():
            return False

        if not force and self.has_slides_backup(session_id):
            logger.info(f"Slides backup for {session_id} already exists (< 7 days old). Preserving existing backup.")
            return True

        raw_slide_files = sorted(slides_dir.glob("*.png"))
        if not raw_slide_files:
            return False

        try:
            logger.info(f"Creating fresh slides backup ({len(raw_slide_files)} slides) for {session_id}")
            if raw_slides_dir.exists():
                shutil.rmtree(raw_slides_dir, ignore_errors=True)
            raw_slides_dir.mkdir(parents=True, exist_ok=True)

            for f in raw_slide_files:
                shutil.copy2(f, raw_slides_dir / f.name)

            backup_meta = {
                "created_at": get_local_now().replace(tzinfo=None).isoformat(),
                "slides_count": len(raw_slide_files),
            }
            with open(raw_slides_dir / ".backup_meta.json", "w", encoding="utf-8") as bf:
                json.dump(backup_meta, bf, ensure_ascii=False, indent=2)

            return True
        except Exception as e:
            logger.warning(f"Could not create slides backup for {session_id}: {e}")
            return False

    def restore_slides_from_backup(self, session_id: str) -> bool:
        """
        Restores slides from slides_raw/ into slides/ if a valid backup exists.
        Updates session metadata accordingly.
        """
        session_dir = self.get_session_dir(session_id)
        raw_dir = session_dir / "slides_raw"
        slides_dir = session_dir / "slides"

        if not self.has_slides_backup(session_id):
            logger.warning(f"No valid backup found for session {session_id} to restore from.")
            return False

        try:
            if slides_dir.exists():
                shutil.rmtree(slides_dir, ignore_errors=True)
            slides_dir.mkdir(parents=True, exist_ok=True)

            count = 0
            for f in sorted(raw_dir.glob("*.png")):
                shutil.copy2(f, slides_dir / f.name)
                count += 1

            self.update_session_metadata(
                session_id,
                {
                    "slides_count": count,
                    "status": "ready_for_review",
                },
            )
            logger.info(f"Restored {count} slides from backup for session {session_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to restore slides from backup for session {session_id}: {e}", exc_info=True)
            return False


    def get_audio_path(self, session_id: str) -> Optional[Path]:
        audio_file = self.get_session_dir(session_id) / "audio.mp3"
        if audio_file.exists():
            return audio_file
        return None

    def delete_session(self, session_id: str) -> bool:
        session_dir = self.get_session_dir(session_id)
        if session_dir.exists() and session_dir.is_dir():
            shutil.rmtree(session_dir, ignore_errors=True)
            logger.info(f"Deleted session directory {session_dir}")
            return True
        return False

    def list_sessions(self, subject_slug: Optional[str] = None) -> List[dict]:
        """
        Lists available sessions, ordered by created_at descending.
        Optionally filtered by subject_slug.
        """
        self._migrate_legacy_sessions()

        sessions = []
        if not self.base_dir.exists():
            return sessions

        for sdir in self.base_dir.iterdir():
            if not sdir.is_dir():
                continue
            meta = self.get_session_metadata(sdir.name)
            if not meta:
                # Synthesize basic metadata if missing
                audio_f = sdir / "audio.mp3"
                slides = list((sdir / "slides").glob("slide_*.png")) if (sdir / "slides").exists() else []
                mtime_dt = datetime.fromtimestamp(sdir.stat().st_mtime)
                meta = {
                    "session_id": sdir.name,
                    "created_at": mtime_dt.strftime("%Y-%m-%d %H:%M:%S"),
                    "subject": "Онлайн-пара",
                    "subject_slug": "unsorted",
                    "status": "ready_for_review",
                    "duration_seconds": 0,
                    "exit_reason": "Импортировано",
                    "audio": {
                        "exists": audio_f.exists(),
                        "size_bytes": audio_f.stat().st_size if audio_f.exists() else 0,
                        "max_volume": 0.0,
                        "is_silent": False,
                    },
                    "slides_count": len(slides),
                    "attachments": [],
                    "published": None,
                }
                self.save_session_metadata(sdir.name, meta)

            # Ensure subject_slug field is present
            if "subject_slug" not in meta:
                meta["subject_slug"] = slugify_subject(meta.get("subject", "unsorted"))

            # Sync actual slide and attachment counts dynamically
            meta["slides_count"] = len(self.get_slides(sdir.name))
            meta["attachments_count"] = len(self.get_attachments(sdir.name))
            audio_path = self.get_audio_path(sdir.name)
            if audio_path:
                meta["audio"]["exists"] = True
                meta["audio"]["size_bytes"] = audio_path.stat().st_size
            else:
                meta["audio"]["exists"] = False

            # Filter if subject_slug was provided
            if subject_slug and meta.get("subject_slug") != subject_slug:
                continue

            sessions.append(meta)

        # Sort newest first
        sessions.sort(key=lambda s: s.get("created_at", ""), reverse=True)
        return sessions

    def get_subjects_summary(self) -> List[dict]:
        """
        Groups all sessions by subject and returns a summary list:
        [{slug, title, count, latest_at}]
        """
        sessions = self.list_sessions()
        grouped: Dict[str, dict] = {}

        for s in sessions:
            slug = s.get("subject_slug") or "unsorted"
            title = s.get("subject", "Онлайн-пара")
            if slug not in grouped:
                grouped[slug] = {
                    "slug": slug,
                    "title": title,
                    "count": 0,
                    "latest_at": s.get("created_at", ""),
                }
            grouped[slug]["count"] += 1
            # Keep newest created_at
            if s.get("created_at", "") > grouped[slug]["latest_at"]:
                grouped[slug]["latest_at"] = s.get("created_at", "")

        return sorted(grouped.values(), key=lambda g: (g["slug"] == "unsorted", -g["count"]))

    def _migrate_legacy_sessions(self):
        """Discovers and renames legacy ugly folder names (tg_session_*, session_tg_session_*)."""
        try:
            # 1. Check root TEMP_DIR for lingering tg_session_*
            for old_dir in TEMP_DIR.glob("tg_session_*"):
                if not old_dir.is_dir():
                    continue
                mtime_dt = datetime.fromtimestamp(old_dir.stat().st_mtime)
                new_id = generate_session_id("unsorted", mtime_dt)
                target_dir = self.base_dir / new_id
                if not target_dir.exists():
                    logger.info(f"Migrating legacy temp {old_dir.name} -> {new_id}")
                    shutil.move(str(old_dir), str(target_dir))
                    (target_dir / "attachments").mkdir(parents=True, exist_ok=True)

            # 2. Check self.base_dir for session_tg_session_* or double session prefixes
            for sdir in self.base_dir.iterdir():
                if not sdir.is_dir():
                    continue
                if "tg_session" in sdir.name:
                    mtime_dt = datetime.fromtimestamp(sdir.stat().st_mtime)
                    meta = self.get_session_metadata(sdir.name) or {}
                    slug = meta.get("subject_slug") or slugify_subject(meta.get("subject", "unsorted"))
                    new_id = generate_session_id(slug, mtime_dt)
                    target_dir = self.base_dir / new_id
                    if not target_dir.exists():
                        logger.info(f"Cleaning ugly session folder name {sdir.name} -> {new_id}")
                        shutil.move(str(sdir), str(target_dir))
                        meta["session_id"] = new_id
                        meta_f = target_dir / "metadata.json"
                        save_json_atomic(meta_f, meta)
        except Exception as e:
            logger.warning(f"Legacy session migration check note: {e}")

    def run_session_postprocess(self, session_id: str) -> dict:
        """
        Executes deferred post-processing on a session on-demand:
        1. Trims silence boundaries from audio.mp3 using AudioRecorder.trim_silence_boundaries.
        2. Gathers slides from slides/ directory, trims pre-lecture lobby slides and late slides,
           shifts timestamps and collapses dHash duplicates.
        3. Updates metadata: clears postprocess_pending flag, updates status, duration, and audio stats.
        """
        session_dir = self.get_session_dir(session_id)
        meta = self.get_session_metadata(session_id)
        if not meta or not session_dir.exists():
            return {"success": False, "error": f"Session {session_id} not found"}

        audio_path = session_dir / "audio.mp3"
        slides_dir = session_dir / "slides"

        from recorder.audio import AudioRecorder
        from recorder.visual import SavedSlide, postprocess_session_slides

        # 1. Trim audio silence boundaries (leading silence + trailing silence)
        start_cutoff: Optional[float] = None
        end_cutoff: Optional[float] = None
        if audio_path.exists() and audio_path.stat().st_size > 1000:
            start_cutoff, end_cutoff = AudioRecorder.trim_silence_boundaries(audio_path)

        # Check / create backup in slides_raw before modifying slides
        self.create_slides_backup(session_id)

        # 2. Gather existing slides from slides directory
        raw_slide_files = sorted(slides_dir.glob("*.png"))
        initial_slides_count = len(raw_slide_files)

        # Parse session start time
        session_start_dt: Optional[datetime] = None
        if meta and meta.get("created_at"):
            try:
                session_start_dt = datetime.strptime(meta["created_at"], "%Y-%m-%d %H:%M:%S")
            except Exception:
                pass
        if not session_start_dt:
            try:
                session_start_dt = datetime.strptime(session_id[:16], "%Y-%m-%d_%H-%M")
            except Exception:
                pass

        # First pass: parse timestamps from filenames (YYYY-MM-DD_HH-MM-SS)
        slide_dt_pattern = re.compile(r"(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})")
        parsed_slide_info = []
        all_dts = []
        for idx, sf in enumerate(raw_slide_files, 1):
            m_dt = slide_dt_pattern.search(sf.stem)
            slide_dt = None
            if m_dt:
                try:
                    slide_dt = datetime.strptime(m_dt.group(1), "%Y-%m-%d_%H-%M-%S")
                    all_dts.append(slide_dt)
                except Exception:
                    pass
            parsed_slide_info.append((idx, sf, slide_dt))

        # Determine effective baseline for relative timestamps
        if all_dts:
            min_slide_dt = min(all_dts)
            if not session_start_dt or session_start_dt > min_slide_dt or (min_slide_dt - session_start_dt).total_seconds() > 7200:
                effective_start_dt = min_slide_dt
            else:
                effective_start_dt = session_start_dt
        else:
            effective_start_dt = session_start_dt

        saved_slides: List[SavedSlide] = []
        for idx, sf, s_dt in parsed_slide_info:
            if s_dt and effective_start_dt:
                ts = max(0.0, (s_dt - effective_start_dt).total_seconds())
            else:
                # Fallback to legacy formats: (\d+)m(\d+)s or _(\d+)s
                m_ms = re.search(r"(\d+)m(\d+)s", sf.stem)
                m_s = re.search(r"_(\d+)s(?:$|\b)", sf.stem)
                if m_ms:
                    ts = float(int(m_ms.group(1)) * 60 + int(m_ms.group(2)))
                elif m_s:
                    ts = float(m_s.group(1))
                else:
                    ts = float((idx - 1) * 5)

            saved_slides.append(
                SavedSlide(
                    index=idx,
                    timestamp_sec=ts,
                    timecode_str=sf.stem,
                    file_path=sf,
                    phash="",
                    captured_at=s_dt,
                )
            )

        # 3. Postprocess slides (purge early lobby slides + late slides + dHash duplicates)
        clean_slides = postprocess_session_slides(
            saved_slides,
            cutoff_sec=end_cutoff,
            start_cutoff_sec=start_cutoff,
        )

        # 4. Check audio level
        audio_level = (
            AudioRecorder.check_audio_level(audio_path)
            if audio_path.exists()
            else {"max_volume": -91.0, "is_silent": True}
        )

        # 5. Calculate new duration
        total_sec = meta.get("duration_seconds", 0.0)
        if start_cutoff is not None or end_cutoff is not None:
            eff_start = start_cutoff or 0.0
            eff_end = end_cutoff or total_sec
            total_sec = max(0.0, eff_end - eff_start)

        updates = {
            "status": "ready_for_review",
            "postprocess_pending": False,
            "duration_seconds": round(total_sec, 1),
            "slides_count": len(clean_slides),
            "audio": {
                "exists": audio_path.exists() and audio_path.stat().st_size > 1000,
                "size_bytes": audio_path.stat().st_size if audio_path.exists() else 0,
                "max_volume": audio_level.get("max_volume", -91.0),
                "is_silent": audio_level.get("is_silent", False),
            },
            "postprocessed_at": get_local_now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        updated_meta = self.update_session_metadata(session_id, updates)

        logger.info(
            f"Session {session_id} on-demand postprocessing finished: "
            f"slides: {initial_slides_count} -> {len(clean_slides)}, "
            f"duration: {total_sec:.1f}s (start_cut: {start_cutoff}, end_cut: {end_cutoff})."
        )

        return {
            "success": True,
            "session_id": session_id,
            "start_cutoff_sec": start_cutoff,
            "end_cutoff_sec": end_cutoff,
            "initial_slides_count": initial_slides_count,
            "clean_slides_count": len(clean_slides),
            "duration_seconds": total_sec,
            "metadata": updated_meta,
        }

    def recover_stale_sessions(self) -> List[dict]:
        """
        Scans all sessions on startup to recover any that were left in 'recording' status
        due to sudden power failure or ungraceful shutdown (kill -9) and marks them as interrupted
        with postprocess_pending=True.
        Returns list of sessions that require post-processing.
        """
        recovered = []
        for session in self.list_sessions():
            status = session.get("status")
            is_pending = session.get("postprocess_pending", False)
            sid = session.get("session_id")
            if not sid:
                continue

            if status == "recording":
                logger.warning(
                    f"Recovering stale 'recording' session {sid}: marking as interrupted with postprocess_pending=True."
                )
                sdir = self.get_session_dir(sid)
                audio_f = sdir / "audio.mp3"
                slides_d = sdir / "slides"
                dur = session.get("duration_seconds", 0)
                if dur == 0 and audio_f.exists():
                    dur = max(0.0, audio_f.stat().st_size / 8000.0)
                slide_count = len(list(slides_d.glob("*.png"))) if slides_d.exists() else 0
                up = self.update_session_metadata(
                    sid,
                    {
                        "status": "interrupted",
                        "postprocess_pending": True,
                        "duration_seconds": round(dur, 1),
                        "slides_count": slide_count,
                        "exit_reason": "Сервер был выключен или перезагружен во время записи",
                    },
                )
                recovered.append(up)
            elif is_pending:
                recovered.append(session)

        return recovered


# Global default singleton instance
session_mgr = SessionManager()


