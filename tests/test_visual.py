import pytest
from pathlib import Path
from PIL import Image, ImageDraw
from recorder.visual import SlideDeduplicator, format_timecode


def test_format_timecode():
    assert format_timecode(0) == "00m00s"
    assert format_timecode(45) == "00m45s"
    assert format_timecode(125) == "02m05s"
    assert format_timecode(3665) == "01h01m05s"


def test_slide_deduplication(tmp_path):
    output_dir = tmp_path / "unique_slides"
    dedup = SlideDeduplicator(output_dir=output_dir, similarity_threshold=0.95)

    # 1. Create first slide (Red background with text)
    img1 = Image.new("RGB", (400, 300), color=(200, 50, 50))
    d1 = ImageDraw.Draw(img1)
    d1.text((20, 20), "Slide 1: Intro", fill=(255, 255, 255))

    slide1 = dedup.process_frame(img1, timestamp_sec=0)
    assert slide1 is not None
    assert slide1.index == 1
    assert slide1.file_path.exists()
    assert len(dedup.saved_slides) == 1

    # 2. Create duplicate slide (almost identical, minor 1px change)
    img1_dup = img1.copy()
    d_dup = ImageDraw.Draw(img1_dup)
    d_dup.point((10, 10), fill=(254, 254, 254))

    slide_dup = dedup.process_frame(img1_dup, timestamp_sec=5)
    assert slide_dup is None  # Should be filtered out!
    assert len(dedup.saved_slides) == 1

    # 3. Create completely different slide (Blue background with different shapes)
    img2 = Image.new("RGB", (400, 300), color=(20, 50, 200))
    d2 = ImageDraw.Draw(img2)
    d2.rectangle([(50, 50), (350, 250)], fill=(255, 255, 0))

    slide2 = dedup.process_frame(img2, timestamp_sec=60)
    assert slide2 is not None
    assert slide2.index == 2
    assert slide2.file_path.exists()
    assert len(dedup.saved_slides) == 2


def test_slide_absolute_timestamp(tmp_path):
    from datetime import datetime
    output_dir = tmp_path / "slides"
    dedup = SlideDeduplicator(output_dir=output_dir)

    img = Image.new("RGB", (100, 100), color=(10, 20, 30))
    fixed_dt = datetime(2026, 9, 9, 19, 30, 45)
    slide = dedup.process_frame(img, timestamp_sec=10, captured_at=fixed_dt)

    assert slide is not None
    assert slide.filename == "slide_001_2026-09-09_19-30-45.png"
    assert slide.timecode_str == "2026-09-09_19-30-45"
    assert slide.file_path.name == "slide_001_2026-09-09_19-30-45.png"
    assert slide.file_path.exists()


def test_postprocess_session_slides(tmp_path):
    from recorder.visual import postprocess_session_slides, SavedSlide

    output_dir = tmp_path / "postprocess_slides"
    output_dir.mkdir()

    # Create 3 frames of slide A (with minor cursor/noise changes)
    img_a1 = Image.new("RGB", (400, 300), color=(255, 255, 255))
    d1 = ImageDraw.Draw(img_a1)
    d1.text((30, 30), "IPv6 Address Representation", fill=(0, 0, 0))
    p_a1 = output_dir / "slide_001.png"
    img_a1.save(p_a1)

    img_a2 = img_a1.copy()
    d2 = ImageDraw.Draw(img_a2)
    d2.point((100, 100), fill=(255, 0, 0))  # red dot/cursor
    p_a2 = output_dir / "slide_002.png"
    img_a2.save(p_a2)

    img_a3 = img_a1.copy()
    d3 = ImageDraw.Draw(img_a3)
    d3.point((120, 100), fill=(255, 0, 0))  # moved cursor
    p_a3 = output_dir / "slide_003.png"
    img_a3.save(p_a3)

    # Create slide B (completely different slide)
    img_b = Image.new("RGB", (400, 300), color=(10, 10, 80))
    db = ImageDraw.Draw(img_b)
    db.text((30, 30), "Different Topic: BGP Routing", fill=(255, 255, 255))
    p_b = output_dir / "slide_004.png"
    img_b.save(p_b)

    # Create late slide C past cutoff (e.g. at 4500s when cutoff is 4200s)
    img_c = Image.new("RGB", (400, 300), color=(0, 0, 0))
    p_c = output_dir / "slide_005.png"
    img_c.save(p_c)

    slides = [
        SavedSlide(index=1, timestamp_sec=10, timecode_str="00m10s", file_path=p_a1, phash=""),
        SavedSlide(index=2, timestamp_sec=20, timecode_str="00m20s", file_path=p_a2, phash=""),
        SavedSlide(index=3, timestamp_sec=30, timecode_str="00m30s", file_path=p_a3, phash=""),
        SavedSlide(index=4, timestamp_sec=60, timecode_str="01m00s", file_path=p_b, phash=""),
        SavedSlide(index=5, timestamp_sec=4500, timecode_str="75m00s", file_path=p_c, phash=""),
    ]

    # Run postprocess with cutoff at 4200s (70 min)
    clean = postprocess_session_slides(slides, cutoff_sec=4200, dhash_threshold_bits=3)

    # 1. Slide C past cutoff should be deleted from disk and excluded
    assert not p_c.exists()
    assert len(clean) == 2

    # 2. Slide A1 and A2 should be deleted from disk, keeping the latest A3
    assert not p_a1.exists()
    assert not p_a2.exists()
    assert p_a3.exists()

    # 3. Slide B should be preserved
    assert p_b.exists()

    # 4. Clean list contains exactly [A3, B] with sequential indices 1 and 2
    assert clean[0].file_path == p_a3
    assert clean[0].index == 1
    assert clean[1].file_path == p_b
    assert clean[1].index == 2


def test_postprocess_session_slides_leading_silence(tmp_path):
    from recorder.visual import postprocess_session_slides, SavedSlide

    output_dir = tmp_path / "leading_slides"
    output_dir.mkdir()

    # Pre-lecture slides (e.g. lobby, loading screen, students joining at 5s, 15s)
    p_lobby1 = output_dir / "lobby_01.png"
    p_lobby2 = output_dir / "lobby_02.png"
    Image.new("RGB", (200, 200), color=(10, 10, 10)).save(p_lobby1)
    Image.new("RGB", (200, 200), color=(20, 20, 20)).save(p_lobby2)

    # Actual lecture slides (started at 300s) with distinct content
    p_lec1 = output_dir / "lec_01.png"
    p_lec2 = output_dir / "lec_02.png"
    img_lec1 = Image.new("RGB", (200, 200), color=(10, 50, 150))
    d1 = ImageDraw.Draw(img_lec1)
    d1.text((10, 20), "Architecture Patterns", fill=(255, 255, 255))
    d1.rectangle([(20, 50), (150, 150)], fill=(255, 200, 0))
    img_lec1.save(p_lec1)

    img_lec2 = Image.new("RGB", (200, 200), color=(20, 120, 30))
    d2 = ImageDraw.Draw(img_lec2)
    d2.text((10, 20), "Database Sharding", fill=(255, 255, 255))
    d2.line([(0, 190), (200, 10)], fill=(255, 0, 150), width=6)
    img_lec2.save(p_lec2)

    slides = [
        SavedSlide(index=1, timestamp_sec=10.0, timecode_str="00m10s", file_path=p_lobby1, phash=""),
        SavedSlide(index=2, timestamp_sec=120.0, timecode_str="02m00s", file_path=p_lobby2, phash=""),
        SavedSlide(index=3, timestamp_sec=310.0, timecode_str="05m10s", file_path=p_lec1, phash=""),
        SavedSlide(index=4, timestamp_sec=450.0, timecode_str="07m30s", file_path=p_lec2, phash=""),
    ]

    # Preacher started at 300s (start_cutoff_sec = 298.5s)
    clean = postprocess_session_slides(slides, start_cutoff_sec=298.5, dhash_threshold_bits=3)

    # Lobby slides should be unlinked from disk
    assert not p_lobby1.exists()
    assert not p_lobby2.exists()

    # Lecture slides should survive
    assert p_lec1.exists()
    assert p_lec2.exists()
    assert len(clean) == 2

    # Timestamps should be shifted by -298.5s
    assert clean[0].file_path == p_lec1
    assert clean[0].index == 1
    assert abs(clean[0].timestamp_sec - (310.0 - 298.5)) < 0.01
    assert clean[0].timecode_str == "00m11s"

    assert clean[1].file_path == p_lec2
    assert clean[1].index == 2
    assert abs(clean[1].timestamp_sec - (450.0 - 298.5)) < 0.01
    assert clean[1].timecode_str == "02m31s"


def test_postprocess_session_slides_both_boundaries(tmp_path):
    from recorder.visual import postprocess_session_slides, SavedSlide

    output_dir = tmp_path / "both_boundaries"
    output_dir.mkdir()

    p_early = output_dir / "early.png"
    p_mid = output_dir / "mid.png"
    p_late = output_dir / "late.png"

    Image.new("RGB", (200, 200), color=(1, 1, 1)).save(p_early)
    Image.new("RGB", (200, 200), color=(100, 100, 100)).save(p_mid)
    Image.new("RGB", (200, 200), color=(200, 200, 200)).save(p_late)

    slides = [
        SavedSlide(index=1, timestamp_sec=50.0, timecode_str="00m50s", file_path=p_early, phash=""),
        SavedSlide(index=2, timestamp_sec=600.0, timecode_str="10m00s", file_path=p_mid, phash=""),
        SavedSlide(index=3, timestamp_sec=5500.0, timecode_str="91m40s", file_path=p_late, phash=""),
    ]

    # Trim start < 300s and end > 5000s
    clean = postprocess_session_slides(slides, start_cutoff_sec=300.0, cutoff_sec=5000.0)

    assert not p_early.exists()
    assert not p_late.exists()
    assert p_mid.exists()

    assert len(clean) == 1
    assert clean[0].file_path == p_mid
    assert clean[0].index == 1
    assert clean[0].timestamp_sec == 300.0  # 600 - 300
    assert clean[0].timecode_str == "05m00s"


def test_slide_deduplicator_auto_backup_on_postprocess(tmp_path):
    import json
    session_dir = tmp_path / "2026-09-24_session_test"
    slides_dir = session_dir / "slides"
    dedup = SlideDeduplicator(output_dir=slides_dir, similarity_threshold=0.8)

    # Frame 1: lobby slide (t=10s)
    img_lobby = Image.new("RGB", (300, 200), color=(10, 10, 10))
    dedup.process_frame(img_lobby, timestamp_sec=10.0)

    # Frame 2: lecture slide (t=120s)
    img_lec = Image.new("RGB", (300, 200), color=(200, 200, 200))
    d = ImageDraw.Draw(img_lec)
    d.rectangle([(20, 20), (100, 100)], fill=(255, 0, 0))
    dedup.process_frame(img_lec, timestamp_sec=120.0)

    # Frame 3: outro slide (t=3600s)
    img_outro = Image.new("RGB", (300, 200), color=(0, 0, 255))
    dedup.process_frame(img_outro, timestamp_sec=3600.0)

    assert len(dedup.saved_slides) == 3
    assert len(list(slides_dir.glob("*.png"))) == 3

    raw_slides_dir = session_dir / "slides_raw"
    assert not raw_slides_dir.exists()

    # Run postprocess with cutoffs that drop frame 1 (lobby) and frame 3 (outro)
    clean_slides = dedup.postprocess(start_cutoff_sec=60.0, cutoff_sec=1800.0)

    # 1. slides_raw backup must have been created BEFORE pruning
    assert raw_slides_dir.exists()
    raw_files = list(raw_slides_dir.glob("*.png"))
    assert len(raw_files) == 3

    meta_file = raw_slides_dir / ".backup_meta.json"
    assert meta_file.exists()
    meta = json.loads(meta_file.read_text(encoding="utf-8"))
    assert meta["slides_count"] == 3
    assert "created_at" in meta

    # 2. slides_dir should now contain only the surviving slide
    surviving_files = list(slides_dir.glob("*.png"))
    assert len(surviving_files) == 1
    assert len(clean_slides) == 1

    # 3. Running postprocess again must NOT overwrite the backup
    dedup.postprocess(start_cutoff_sec=60.0, cutoff_sec=1800.0)
    assert len(list(raw_slides_dir.glob("*.png"))) == 3


