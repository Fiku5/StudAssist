import json
import pytest
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from recorder.session_manager import SessionManager


@pytest.fixture
def temp_session_manager(tmp_path):
    mgr = SessionManager(base_dir=tmp_path / "sessions")
    return mgr


def test_create_and_get_session(temp_session_manager):
    mgr = temp_session_manager
    session_id = "session_test_01"
    session_dir = mgr.create_session(session_id=session_id, subject="Тестовая лекция")

    assert session_dir.exists()
    assert (session_dir / "slides").exists()
    assert (session_dir / "attachments").exists()

    meta = mgr.get_session_metadata(session_id)
    assert meta is not None
    assert meta["session_id"] == session_id
    assert meta["subject"] == "Тестовая лекция"
    assert meta["status"] == "recording"


def test_finalize_recording(temp_session_manager, tmp_path):
    mgr = temp_session_manager
    session_id = "session_test_02"
    mgr.create_session(session_id=session_id)

    dummy_audio = tmp_path / "audio.mp3"
    dummy_audio.write_bytes(b"dummy mp3 data 12345")

    meta = mgr.finalize_recording(
        session_id=session_id,
        duration_seconds=120.5,
        exit_reason="Normal exit",
        audio_path=dummy_audio,
        unique_slides=[],
        is_silent=False,
        audio_level={"max_volume": -3.5, "is_silent": False},
    )

    assert meta["status"] == "ready_for_review"
    assert meta["duration_seconds"] == 120.5
    assert meta["audio"]["exists"] is True
    assert meta["audio"]["max_volume"] == -3.5
    assert meta["audio"]["is_silent"] is False


def test_finalize_recording_with_attendance(temp_session_manager, tmp_path):
    mgr = temp_session_manager
    session_id = "session_test_att"
    mgr.create_session(session_id=session_id)

    attendance_data = {
        "meeting_duration_minutes": 45.0,
        "total_unique_participants": 2,
        "participants": [
            {"name": "Павел Николаев", "total_minutes": 44.5, "attendance_percentage": 98.9}
        ],
    }

    meta = mgr.finalize_recording(
        session_id=session_id,
        duration_seconds=2700.0,
        exit_reason="Normal exit",
        attendance=attendance_data,
    )

    assert meta["status"] == "ready_for_review"
    assert "attendance" in meta
    assert meta["attendance"]["total_unique_participants"] == 2
    assert meta["attendance"]["participants"][0]["name"] == "Павел Николаев"

    # Verify persisted in metadata.json file
    saved = mgr.get_session_metadata(session_id)
    assert saved["attendance"]["total_unique_participants"] == 2


def test_add_attachments(temp_session_manager):
    mgr = temp_session_manager
    session_id = "session_test_03"
    mgr.create_session(session_id=session_id)

    att_path = mgr.add_attachment(session_id, "script.py", b"print('hello world')")
    assert att_path.exists()
    assert att_path.read_text(encoding="utf-8") == "print('hello world')"

    attachments = mgr.get_attachments(session_id)
    assert len(attachments) == 1
    assert attachments[0].name == "script.py"

    meta = mgr.get_session_metadata(session_id)
    assert len(meta["attachments"]) == 1
    assert meta["attachments"][0]["filename"] == "script.py"


def test_delete_attachment(temp_session_manager):
    mgr = temp_session_manager
    session_id = "session_test_del_att"
    mgr.create_session(session_id=session_id)

    mgr.add_attachment(session_id, "file_1.txt", b"content 1")
    mgr.add_attachment(session_id, "file_2.txt", b"content 2")
    assert len(mgr.get_attachments(session_id)) == 2

    # Delete file_1.txt
    success = mgr.delete_attachment(session_id, "file_1.txt")
    assert success is True

    remaining = mgr.get_attachments(session_id)
    assert len(remaining) == 1
    assert remaining[0].name == "file_2.txt"

    # Verify metadata.json was updated
    meta = mgr.get_session_metadata(session_id)
    assert len(meta["attachments"]) == 1
    assert meta["attachments"][0]["filename"] == "file_2.txt"

    # Deleting non-existent file
    assert mgr.delete_attachment(session_id, "ghost.txt") is False


def test_clear_attachments(temp_session_manager):
    mgr = temp_session_manager
    session_id = "session_test_clear_att"
    mgr.create_session(session_id=session_id)

    mgr.add_attachment(session_id, "doc1.pdf", b"pdf1")
    mgr.add_attachment(session_id, "doc2.pdf", b"pdf2")
    mgr.add_attachment(session_id, "photo.png", b"png")
    assert len(mgr.get_attachments(session_id)) == 3

    cleared = mgr.clear_attachments(session_id)
    assert cleared == 3
    assert len(mgr.get_attachments(session_id)) == 0

    meta = mgr.get_session_metadata(session_id)
    assert meta["attachments"] == []


def test_list_and_delete_sessions(temp_session_manager):
    mgr = temp_session_manager
    mgr.create_session(session_id="session_01")
    mgr.create_session(session_id="session_02")

    sessions = mgr.list_sessions()
    assert len(sessions) == 2
    session_ids = [s["session_id"] for s in sessions]
    assert "session_01" in session_ids
    assert "session_02" in session_ids

    deleted = mgr.delete_session("session_01")
    assert deleted is True

    sessions_after = mgr.list_sessions()
    assert len(sessions_after) == 1
    assert sessions_after[0]["session_id"] == "session_02"


def test_session_id_generation_and_subject_grouping(temp_session_manager):
    mgr = temp_session_manager
    from datetime import datetime
    fixed_dt = datetime(2026, 9, 10, 9, 0)

    # 1. Create with subject slug
    sdir1 = mgr.create_session(
        subject="Архитектура ПО",
        subject_slug="arhitektura_po",
        dt=fixed_dt,
    )
    assert sdir1.name == "2026-09-10_09-00_arhitektura_po"

    # 2. Create another under different subject
    sdir2 = mgr.create_session(
        subject="Базы данных",
        subject_slug="bazy_dannykh",
        dt=fixed_dt,
    )
    assert sdir2.name == "2026-09-10_09-00_bazy_dannykh"

    # 3. Filter by subject
    arch_sessions = mgr.list_sessions(subject_slug="arhitektura_po")
    assert len(arch_sessions) == 1
    assert arch_sessions[0]["session_id"] == sdir1.name

    db_sessions = mgr.list_sessions(subject_slug="bazy_dannykh")
    assert len(db_sessions) == 1
    assert db_sessions[0]["session_id"] == sdir2.name

    # 4. Subjects summary
    summary = mgr.get_subjects_summary()
    assert len(summary) == 2
    slugs = [s["slug"] for s in summary]
    assert "arhitektura_po" in slugs
    assert "bazy_dannykh" in slugs

    # 5. Re-assign subject
    mgr.assign_session_subject(sdir1.name, "Компьютерные сети", "seti")
    updated_meta = mgr.get_session_metadata(sdir1.name)
    assert updated_meta["subject"] == "Компьютерные сети"
    assert updated_meta["subject_slug"] == "seti"


def test_slides_backup_lifecycle_and_restore(temp_session_manager):
    import json
    from datetime import datetime, timedelta
    from PIL import Image, ImageDraw

    mgr = temp_session_manager
    session_id = "session_backup_test"
    sdir = mgr.create_session(session_id=session_id)
    slides_dir = sdir / "slides"

    # Create 3 visually distinct dummy slides
    img1 = Image.new("RGB", (400, 300), color=(255, 255, 255))
    ImageDraw.Draw(img1).rectangle([(20, 20), (200, 200)], fill=(0, 0, 255))

    img2 = Image.new("RGB", (400, 300), color=(0, 0, 0))
    ImageDraw.Draw(img2).rectangle([(200, 100), (380, 280)], fill=(255, 255, 0))

    img3 = Image.new("RGB", (400, 300), color=(100, 100, 100))
    ImageDraw.Draw(img3).line([(0, 0), (400, 300)], fill=(255, 0, 0), width=10)

    img1.save(slides_dir / "slide_001_2026-09-17_15-00-10.png")
    img2.save(slides_dir / "slide_002_2026-09-17_15-05-00.png")
    img3.save(slides_dir / "slide_003_2026-09-17_15-10-00.png")

    assert mgr.has_slides_backup(session_id) is False

    # 1. Run postprocessing -> creates slides_raw backup
    res = mgr.run_session_postprocess(session_id)
    assert res["success"] is True
    assert mgr.has_slides_backup(session_id) is True
    assert res["clean_slides_count"] == 3

    raw_dir = sdir / "slides_raw"
    assert raw_dir.exists()
    assert len(list(raw_dir.glob("*.png"))) == 3
    assert (raw_dir / ".backup_meta.json").exists()

    # 2. Simulate subsequent postprocessing within 7 days:
    # Modify slides_dir by removing 2 slides
    (slides_dir / "slide_002_2026-09-17_15-05-00.png").unlink(missing_ok=True)
    (slides_dir / "slide_003_2026-09-17_15-10-00.png").unlink(missing_ok=True)
    assert len(list(slides_dir.glob("*.png"))) == 1

    # Run postprocess again -> should NOT overwrite backup because it is < 7 days old
    mgr.run_session_postprocess(session_id)
    assert len(list(raw_dir.glob("*.png"))) == 3

    # 3. Restore from backup
    restored = mgr.restore_slides_from_backup(session_id)
    assert restored is True
    assert len(list(slides_dir.glob("*.png"))) == 3
    meta = mgr.get_session_metadata(session_id)
    assert meta["slides_count"] == 3

    # 4. Test expiration: set backup date to 8 days ago
    expired_dt = datetime.now() - timedelta(days=8)
    with open(raw_dir / ".backup_meta.json", "w", encoding="utf-8") as f:
        json.dump({"created_at": expired_dt.isoformat()}, f)

    # has_slides_backup should detect expiration and purge the directory
    assert mgr.has_slides_backup(session_id) is False
    assert not raw_dir.exists()

    # 5. Subsequent postprocess after expiration creates fresh backup
    mgr.run_session_postprocess(session_id)
    assert mgr.has_slides_backup(session_id) is True
    assert raw_dir.exists()


def test_create_slides_backup_direct(temp_session_manager):
    mgr = temp_session_manager
    session_id = "session_direct_backup_test"

    # Non-existent session
    assert mgr.create_slides_backup("non_existent_session") is False

    sdir = mgr.create_session(session_id=session_id)
    slides_dir = sdir / "slides"
    raw_slides_dir = sdir / "slides_raw"

    # No slides present
    assert mgr.create_slides_backup(session_id) is False
    assert not raw_slides_dir.exists()

    # Add 2 slides
    (slides_dir / "slide_001.png").write_bytes(b"slide1")
    (slides_dir / "slide_002.png").write_bytes(b"slide2")

    # Fresh backup creation
    assert mgr.create_slides_backup(session_id) is True
    assert raw_slides_dir.exists()
    assert len(list(raw_slides_dir.glob("*.png"))) == 2
    assert (raw_slides_dir / ".backup_meta.json").exists()

    # Preserves existing backup when force=False
    (slides_dir / "slide_003.png").write_bytes(b"slide3")
    assert mgr.create_slides_backup(session_id, force=False) is True
    assert len(list(raw_slides_dir.glob("*.png"))) == 2  # Not overwritten

    # Overwrites when force=True
    assert mgr.create_slides_backup(session_id, force=True) is True
    assert len(list(raw_slides_dir.glob("*.png"))) == 3



def test_dynamic_slide_timecodes_against_silence(temp_session_manager, monkeypatch):
    from PIL import Image, ImageDraw

    mgr = temp_session_manager
    session_id = "session_timecode_test"
    sdir = mgr.create_session(session_id=session_id)
    slides_dir = sdir / "slides"

    # Set session created_at to 14:58:00
    mgr.update_session_metadata(session_id, {"created_at": "2026-09-16 14:58:00"})

    # Slide 1: lobby slide at 14:58:30 (relative 30s)
    img_lobby = Image.new("RGB", (400, 300), color=(10, 10, 10))
    img_lobby.save(slides_dir / "slide_001_2026-09-16_14-58-30.png")

    # Slide 2: lecture slide at 15:02:00 (relative 240s)
    img_lec1 = Image.new("RGB", (400, 300), color=(255, 255, 255))
    ImageDraw.Draw(img_lec1).rectangle([(20, 20), (200, 200)], fill=(0, 0, 255))
    img_lec1.save(slides_dir / "slide_002_2026-09-16_15-02-00.png")

    # Slide 3: lecture slide at 15:10:00 (relative 720s)
    img_lec2 = Image.new("RGB", (400, 300), color=(0, 0, 0))
    ImageDraw.Draw(img_lec2).rectangle([(200, 100), (380, 280)], fill=(255, 255, 0))
    img_lec2.save(slides_dir / "slide_003_2026-09-16_15-10-00.png")

    # Create dummy audio (> 1000 bytes)
    audio_file = sdir / "audio.mp3"
    audio_file.write_bytes(b"dummy audio" * 200)

    # Mock trim_silence_boundaries to return start_cutoff = 196.2s (matching user's bug scenario)
    from recorder.audio import AudioRecorder
    monkeypatch.setattr(
        AudioRecorder,
        "trim_silence_boundaries",
        lambda p, **kw: (196.2, 5181.0),
    )

    res = mgr.run_session_postprocess(session_id)
    assert res["success"] is True
    # Lobby slide (at 30s < 196.2s) should be purged, lecture slides (240s, 720s) survive!
    assert res["initial_slides_count"] == 3
    assert res["clean_slides_count"] == 2
    assert not (slides_dir / "slide_001_2026-09-16_14-58-30.png").exists()
    assert (slides_dir / "slide_002_2026-09-16_15-02-00.png").exists()
    assert (slides_dir / "slide_003_2026-09-16_15-10-00.png").exists()

    # Verify backup retained all 3 slides including the lobby slide
    raw_dir = sdir / "slides_raw"
    assert (raw_dir / "slide_001_2026-09-16_14-58-30.png").exists()


def test_cleanup_expired_backups(temp_session_manager):
    mgr = temp_session_manager

    # Create both sessions first
    s1_dir = mgr.create_session(session_id="session_expired")
    s2_dir = mgr.create_session(session_id="session_fresh")

    # Session 1: Expired backup (10 days old)
    raw1_dir = s1_dir / "slides_raw"
    raw1_dir.mkdir(parents=True, exist_ok=True)
    (raw1_dir / "slide_001.png").write_bytes(b"dummy_png_bytes")
    meta1 = {"created_at": (datetime.now() - timedelta(days=10)).isoformat()}
    with open(raw1_dir / ".backup_meta.json", "w", encoding="utf-8") as f:
        json.dump(meta1, f)

    # Session 2: Fresh backup (1 day old)
    raw2_dir = s2_dir / "slides_raw"
    raw2_dir.mkdir(parents=True, exist_ok=True)
    (raw2_dir / "slide_001.png").write_bytes(b"dummy_png_bytes")
    meta2 = {"created_at": (datetime.now() - timedelta(days=1)).isoformat()}
    with open(raw2_dir / ".backup_meta.json", "w", encoding="utf-8") as f:
        json.dump(meta2, f)

    assert raw1_dir.exists()
    assert raw2_dir.exists()

    # Run cleanup
    purged_count = mgr.cleanup_expired_backups(retention_seconds=7 * 86400)
    assert purged_count == 1
    assert not raw1_dir.exists()
    assert raw2_dir.exists()


def test_disk_space_methods(temp_session_manager, monkeypatch):
    mgr = temp_session_manager

    # Normal usage
    info = mgr.get_disk_space_info()
    assert "total_gb" in info
    assert "used_gb" in info
    assert "free_gb" in info
    assert "free_percent" in info
    assert info["total_gb"] > 0
    assert info["free_gb"] > 0

    free_gb = mgr.get_free_disk_space_gb()
    assert isinstance(free_gb, float)
    assert free_gb > 0

    # Test error fallback
    monkeypatch.setattr("shutil.disk_usage", lambda p: (_ for _ in ()).throw(OSError("Mock disk error")))
    fallback_info = mgr.get_disk_space_info()
    assert fallback_info["free_gb"] == 999.0
    assert fallback_info["total_gb"] == 0.0
    assert mgr.get_free_disk_space_gb() == 999.0


def test_create_session_triggers_cleanup(temp_session_manager):
    mgr = temp_session_manager

    # Create session with expired backup
    s1_dir = mgr.base_dir / "session_with_old_raw"
    s1_dir.mkdir(parents=True, exist_ok=True)
    raw_dir = s1_dir / "slides_raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    (raw_dir / "slide.png").write_bytes(b"img")
    old_meta = {"created_at": (datetime.now() - timedelta(days=12)).isoformat()}
    with open(raw_dir / ".backup_meta.json", "w", encoding="utf-8") as f:
        json.dump(old_meta, f)

    assert raw_dir.exists()

    # Creating a new session should trigger cleanup of the expired backup
    mgr.create_session(session_id="new_session_trigger")
    assert not raw_dir.exists()



