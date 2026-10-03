#!/usr/bin/env python3
r"""
Diagnostic & testing utility for session post-processing.
Runs audio trailing silence trimming and dHash slide deduplication on a test session folder.

Usage:
    python scripts/test_postprocess_session.py "C:\StudentAssistant\seti_1"
    python scripts/test_postprocess_session.py "C:\StudentAssistant\seti_1" --bits 3 --dry-run
    python scripts/test_postprocess_session.py "C:\StudentAssistant\seti_1" --output-dir "C:\StudentAssistant\seti_1\slides_cleaned"
"""

import argparse
import shutil
import sys
from pathlib import Path
from PIL import Image
import imagehash

# Add project root to sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from recorder.audio import AudioRecorder
from recorder.visual import SavedSlide


def main():
    parser = argparse.ArgumentParser(description="Test session postprocessing on real lecture recordings.")
    parser.add_argument("session_dir", help="Path to session directory (containing slides/ and audio.mp3)")
    parser.add_argument("--bits", type=int, default=3, help="Max dHash bit difference for duplicate frames (default: 3)")
    parser.add_argument("--dry-run", action="store_true", help="Simulate without deleting or modifying any files")
    parser.add_argument("--output-dir", type=str, default=None, help="Save cleaned slides into a separate directory")
    parser.add_argument("--skip-audio", action="store_true", help="Skip audio trailing silence detection")

    args = parser.parse_args()
    session_path = Path(args.session_dir)

    if not session_path.exists():
        print(f"Error: Session directory does not exist: {session_path}")
        sys.exit(1)

    slides_dir = session_path / "slides" if (session_path / "slides").exists() else session_path
    slide_files = sorted(list(slides_dir.glob("*.png")) + list(slides_dir.glob("*.jpg")))

    if not slide_files:
        print(f"Error: No slide images found in {slides_dir}")
        sys.exit(1)

    print("=" * 70)
    print(f"POST-PROCESSING TEST: {session_path.name}")
    print(f"Found {len(slide_files)} raw slide images in: {slides_dir}")
    print(f"dHash tolerance: <= {args.bits} bits difference")
    print("=" * 70)

    # 1. Audio silence analysis
    audio_path = session_path / "audio.mp3"
    cutoff_sec = None
    if not args.skip_audio and audio_path.exists():
        print(f"\nAnalyzing audio file: {audio_path.name} ({audio_path.stat().st_size / 1024 / 1024:.1f} MB)...")
        if args.dry_run:
            # Non-destructive check
            temp_copy = session_path / "temp_audio_check.mp3"
            shutil.copy2(audio_path, temp_copy)
            cutoff_sec = AudioRecorder.trim_trailing_silence(temp_copy)
            if temp_copy.exists():
                temp_copy.unlink()
        else:
            cutoff_sec = AudioRecorder.trim_trailing_silence(audio_path)

        if cutoff_sec is not None:
            print(f"Trailing silence detected! Speech ended at: {cutoff_sec / 60:.1f} min ({cutoff_sec:.1f}s)")
        else:
            print("No prolonged trailing silence detected (or audio active until the end).")
    elif not audio_path.exists():
        print("\nNo audio.mp3 found in session folder (skipping audio trim).")

    # 2. Build SavedSlide list
    # Parse approximate timestamps from filename if available (e.g. slide_001_YYYY-MM-DD_HH-MM-SS.png)
    raw_slides = []
    for idx, f in enumerate(slide_files, 1):
        # Default timestamp estimation based on 5-second interval if not in filename
        raw_slides.append(SavedSlide(
            index=idx,
            timestamp_sec=idx * 5.0,
            timecode_str=f.stem,
            file_path=f,
            phash="",
        ))

    # 3. Simulate or execute slide deduplication
    print(f"\nComputing dHash for {len(raw_slides)} slides...")
    hashes = []
    for s in raw_slides:
        try:
            with Image.open(s.file_path) as img:
                h = imagehash.dhash(img, hash_size=8)
                hashes.append((s, h))
        except Exception as e:
            print(f"Failed to open {s.filename}: {e}")
            hashes.append((s, None))

    clusters = []  # List of lists of slides belonging to the same slide content
    cluster_hashes = []

    for slide, d_hash in hashes:
        if d_hash is None:
            clusters.append([slide])
            cluster_hashes.append(None)
            continue

        matched_cluster = -1
        # Check from most recent cluster backwards
        for c_idx in range(len(clusters) - 1, -1, -1):
            ref_hash = cluster_hashes[c_idx]
            if ref_hash is not None and (d_hash - ref_hash) <= args.bits:
                matched_cluster = c_idx
                break

        if matched_cluster != -1:
            clusters[matched_cluster].append(slide)
            # Update hash to newest slide in cluster
            cluster_hashes[matched_cluster] = d_hash
        else:
            clusters.append([slide])
            cluster_hashes.append(d_hash)

    print("\n" + "-" * 70)
    print(f"DEDUPLICATION RESULTS:")
    print(f"Total raw slides:     {len(raw_slides)}")
    print(f"Unique slides found:  {len(clusters)} (reduction: {(1.0 - len(clusters) / len(raw_slides)):.1%})")
    print("-" * 70)

    # Display clusters
    for c_idx, cluster in enumerate(clusters, 1):
        latest_slide = cluster[-1]
        if len(cluster) > 1:
            print(f"  * Слайд #{c_idx:02d}: объединено {len(cluster):2d} кадров [{cluster[0].filename} -> {latest_slide.filename}]")
            print(f"      -> Сохраняем актуальный: {latest_slide.filename}")
        else:
            print(f"  * Слайд #{c_idx:02d}: одиночный кадр {latest_slide.filename}")

    # If output directory specified, copy unique slides there
    if args.output_dir:
        out_path = Path(args.output_dir)
        out_path.mkdir(parents=True, exist_ok=True)
        for new_idx, cluster in enumerate(clusters, 1):
            latest = cluster[-1]
            dest = out_path / f"clean_slide_{new_idx:03d}_{latest.filename}"
            shutil.copy2(latest.file_path, dest)
        print(f"\nSaved {len(clusters)} clean unique slides to: {out_path}")
    elif not args.dry_run:
        print("\nNote: Run with --output-dir <path> to export the cleaned slides into a separate folder,")
        print("or use the script in dry-run mode to inspect cluster mappings.")

    print("=" * 70)


if __name__ == "__main__":
    main()
