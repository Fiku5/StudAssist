import json
import logging
import os
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Union
from PIL import Image
import imagehash
from config import get_local_now

logger = logging.getLogger(__name__)


@dataclass
class SavedSlide:
    """Represents a uniquely captured lecture slide."""
    index: int
    timestamp_sec: float
    timecode_str: str
    file_path: Path
    phash: str
    captured_at: Optional[datetime] = None

    @property
    def filename(self) -> str:
        return self.file_path.name


def format_timecode(seconds: float) -> str:
    """Converts seconds into human-readable MMmSSs or HHhMMmSSs format."""
    total_seconds = int(seconds)
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    secs = total_seconds % 60

    if hours > 0:
        return f"{hours:02d}h{minutes:02d}m{secs:02d}s"
    return f"{minutes:02d}m{secs:02d}s"


class SlideDeduplicator:
    """
    Intelligent keyframe extractor using Perceptual Hashing (pHash).
    Filters out static video frames (where the teacher stays on the same slide)
    and preserves only unique visual slides and code snippets.
    """

    def __init__(
        self,
        output_dir: Union[str, Path],
        similarity_threshold: float = 0.95,
        hash_size: int = 8,
    ):
        """
        :param output_dir: Directory where unique slides will be saved.
        :param similarity_threshold: Threshold (0.0 to 1.0) above which frames
                                     are considered identical duplicates.
                                     Default 0.95 means <= 3 bits difference in 64-bit pHash.
        :param hash_size: pHash grid size (8 gives 64-bit hash).
        """
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.similarity_threshold = similarity_threshold
        self.hash_size = hash_size
        self.total_bits = hash_size * hash_size  # Typically 64 bits

        self.saved_slides: List[SavedSlide] = []
        self._last_hash: Optional[imagehash.ImageHash] = None

    def calculate_similarity(
        self, hash1: imagehash.ImageHash, hash2: imagehash.ImageHash
    ) -> float:
        """Calculates normalized similarity between two perceptual hashes."""
        hamming_distance = hash1 - hash2
        return 1.0 - (hamming_distance / float(self.total_bits))

    def process_frame(
        self,
        frame: Union[Image.Image, Path, str],
        timestamp_sec: float = 0.0,
        captured_at: Optional[datetime] = None,
    ) -> Optional[SavedSlide]:
        """
        Evaluates a frame. If it is sufficiently distinct from the previous slide,
        saves it and returns SavedSlide. Otherwise, returns None.
        """
        # Load image if path was provided
        if isinstance(frame, (str, Path)):
            img = Image.open(frame)
            source_path = Path(frame)
        else:
            img = frame
            source_path = None

        # Compute perceptual hash
        current_hash = imagehash.phash(img, hash_size=self.hash_size)

        # Check for duplication with previous slide
        if self._last_hash is not None:
            similarity = self.calculate_similarity(self._last_hash, current_hash)
            if similarity >= self.similarity_threshold:
                # Frame is a duplicate (same slide)
                return None

        # New unique slide detected!
        slide_idx = len(self.saved_slides) + 1
        current_dt = captured_at or get_local_now()
        dt_str = current_dt.strftime("%Y-%m-%d_%H-%M-%S")
        dest_filename = f"slide_{slide_idx:03d}_{dt_str}.png"
        dest_path = self.output_dir / dest_filename

        if source_path and source_path.exists():
            shutil.copy2(source_path, dest_path)
        else:
            img.save(dest_path, "PNG")

        slide = SavedSlide(
            index=slide_idx,
            timestamp_sec=timestamp_sec,
            timecode_str=dt_str,
            file_path=dest_path,
            phash=str(current_hash),
            captured_at=current_dt,
        )

        self._last_hash = current_hash
        self.saved_slides.append(slide)
        logger.info(f"Captured new unique slide #{slide.index} at {slide.timecode_str} (file: {dest_filename})")
        return slide

    def postprocess(
        self,
        cutoff_sec: Optional[float] = None,
        start_cutoff_sec: Optional[float] = None,
        dhash_threshold_bits: int = 3,
        create_backup: bool = True,
    ) -> List[SavedSlide]:
        """
        Runs post-processing on this session's saved slides:
        - Creates a backup copy in slides_raw/ before modifying or unlinking any slide.
        - Drops slides captured before start_cutoff_sec (early silence / lobby).
        - Drops slides captured after cutoff_sec (silence tail).
        - Shifts timestamps of surviving slides to match trimmed audio.
        - Uses dHash to collapse duplicates, retaining the latest/most complete version.
        """
        if create_backup and self.output_dir.exists():
            session_dir = self.output_dir.parent
            raw_slides_dir = session_dir / "slides_raw"
            raw_slide_files = sorted(self.output_dir.glob("*.png"))
            if raw_slide_files and not (raw_slides_dir.exists() and any(raw_slides_dir.glob("*.png"))):
                try:
                    raw_slides_dir.mkdir(parents=True, exist_ok=True)
                    for f in raw_slide_files:
                        shutil.copy2(f, raw_slides_dir / f.name)
                    backup_meta = {
                        "created_at": get_local_now().replace(tzinfo=None).isoformat(),
                        "slides_count": len(raw_slide_files),
                    }
                    with open(raw_slides_dir / ".backup_meta.json", "w", encoding="utf-8") as bf:
                        json.dump(backup_meta, bf, ensure_ascii=False, indent=2)
                    logger.info(f"Created slides_raw backup ({len(raw_slide_files)} slides) for {session_dir.name} before post-processing.")
                except Exception as e:
                    logger.warning(f"Could not create slides_raw backup: {e}")

        self.saved_slides = postprocess_session_slides(
            self.saved_slides,
            cutoff_sec=cutoff_sec,
            start_cutoff_sec=start_cutoff_sec,
            dhash_threshold_bits=dhash_threshold_bits,
        )
        return self.saved_slides

    def get_summary(self) -> dict:
        """Returns statistics on processed slides."""
        return {
            "total_unique_slides": len(self.saved_slides),
            "output_directory": str(self.output_dir),
            "slides": [
                {
                    "index": s.index,
                    "timecode": s.timecode_str,
                    "filename": s.filename,
                }
                for s in self.saved_slides
            ],
        }


def postprocess_session_slides(
    saved_slides: List[SavedSlide],
    cutoff_sec: Optional[float] = None,
    start_cutoff_sec: Optional[float] = None,
    dhash_threshold_bits: int = 3,
) -> List[SavedSlide]:
    """
    Post-processes captured slides immediately after session concludes:
    1. Purges slides captured before audio start cutoff timestamp (silence at beginning / lobby).
    2. Purges slides captured after audio cutoff timestamp (silence tail).
    3. Shifts timestamps of surviving slides to match trimmed audio.
    4. Uses difference hash (dHash) to identify duplicates/near-duplicates.
    5. For each group of duplicate slides, deletes older files from disk and retains
       only the latest (most complete) version.
    """
    if not saved_slides:
        return []

    # 1. Purge slides outside [start_cutoff_sec, cutoff_sec]
    surviving = []
    for slide in saved_slides:
        # Purge slides captured before speech began
        if start_cutoff_sec is not None and slide.timestamp_sec < start_cutoff_sec:
            try:
                if slide.file_path.exists():
                    slide.file_path.unlink(missing_ok=True)
                    logger.info(f"Purged early pre-lecture slide before cutoff {start_cutoff_sec}s: {slide.filename}")
            except Exception as e:
                logger.warning(f"Failed to delete early slide {slide.file_path}: {e}")
            continue

        # Purge slides captured after lecture ended
        if cutoff_sec is not None and slide.timestamp_sec > cutoff_sec:
            try:
                if slide.file_path.exists():
                    slide.file_path.unlink(missing_ok=True)
                    logger.info(f"Purged late slide past cutoff {cutoff_sec}s: {slide.filename}")
            except Exception as e:
                logger.warning(f"Failed to delete late slide {slide.file_path}: {e}")
            continue

        if slide.file_path.exists():
            # Shift timestamp to match trimmed audio
            if start_cutoff_sec is not None and start_cutoff_sec > 0:
                slide.timestamp_sec = max(0.0, slide.timestamp_sec - start_cutoff_sec)
                slide.timecode_str = format_timecode(slide.timestamp_sec)
            surviving.append(slide)

    if not surviving:
        return []

    # 2. Compute dHash for each surviving slide
    slide_hashes = []
    for s in surviving:
        try:
            with Image.open(s.file_path) as img:
                h = imagehash.dhash(img, hash_size=8)
                slide_hashes.append((s, h))
        except Exception as e:
            logger.warning(f"Could not compute dHash for {s.file_path}: {e}")
            slide_hashes.append((s, None))

    # 3. Deduplicate and retain the latest version of each slide
    final_slides: List[SavedSlide] = []
    final_hashes: List[Optional[imagehash.ImageHash]] = []

    for slide, d_hash in slide_hashes:
        if d_hash is None:
            final_slides.append(slide)
            final_hashes.append(None)
            continue

        matched_idx = -1
        # Check from most recent to oldest
        for idx in range(len(final_slides) - 1, -1, -1):
            ref_hash = final_hashes[idx]
            if ref_hash is not None and (d_hash - ref_hash) <= dhash_threshold_bits:
                matched_idx = idx
                break

        if matched_idx != -1:
            old_slide = final_slides[matched_idx]
            logger.info(
                f"Deduplicator: replacing older duplicate #{old_slide.index} ({old_slide.filename}) "
                f"with newer version #{slide.index} ({slide.filename})"
            )
            try:
                if old_slide.file_path.exists() and old_slide.file_path != slide.file_path:
                    old_slide.file_path.unlink(missing_ok=True)
            except Exception as e:
                logger.warning(f"Failed to delete older duplicate file {old_slide.file_path}: {e}")

            # Keep the newer slide representing this slide
            final_slides[matched_idx] = slide
            final_hashes[matched_idx] = d_hash
        else:
            final_slides.append(slide)
            final_hashes.append(d_hash)

    # Re-index slides sequentially
    for new_idx, s in enumerate(final_slides, 1):
        s.index = new_idx

    logger.info(
        f"Slide post-processing complete: {len(saved_slides)} raw slides -> "
        f"{len(final_slides)} unique clean slides retained."
    )
    return final_slides
