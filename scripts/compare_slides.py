#!/usr/bin/env python3
"""
CLI utility to compare lecture slides and examine pHash & dHash metrics.

Usage:
    python scripts/compare_slides.py path/to/slide1.png path/to/slide2.png
    python scripts/compare_slides.py path/to/slide*.png --bits 3
    python scripts/compare_slides.py path/to/slide1.png path/to/slide2.png --threshold 0.90
"""

import argparse
import sys
from pathlib import Path
from PIL import Image
import imagehash


def main():
    parser = argparse.ArgumentParser(
        description="Compare lecture slides using perceptual (pHash) and difference (dHash) hashing."
    )
    parser.add_argument(
        "images",
        nargs="+",
        help="Paths to slide images to compare (at least 2 images required)",
    )
    parser.add_argument(
        "--bits",
        type=int,
        default=3,
        help="Max bit difference to consider two frames as duplicates (default: 3 bits out of 64)",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=None,
        help="Similarity threshold (0.0 to 1.0). If set, overrides --bits. E.g. 0.95 means <=3 bits diff.",
    )

    args = parser.parse_args()

    # Expand any glob patterns in image arguments
    expanded_paths = []
    for arg in args.images:
        p = Path(arg)
        if "*" in arg or "?" in arg:
            expanded_paths.extend(sorted(p.parent.glob(p.name)))
        elif p.exists():
            expanded_paths.append(p)
        else:
            print(f"Warning: File not found: {arg}")

    if len(expanded_paths) < 2:
        print("Error: Please provide at least 2 valid image files to compare.")
        sys.exit(1)

    max_bits = args.bits
    if args.threshold is not None:
        max_bits = int(64 * (1.0 - args.threshold))

    print("=" * 70)
    print(f"SLIDE HASH COMPARISON TOOL (Total images: {len(expanded_paths)})")
    print(f"Duplicate criteria: <= {max_bits} bits difference (similarity >= {1.0 - (max_bits/64.0):.2%})")
    print("=" * 70)

    # 1. Compute hashes
    slide_data = []
    for idx, path in enumerate(expanded_paths):
        try:
            with Image.open(path) as img:
                w, h = img.size
                ph = imagehash.phash(img, hash_size=8)
                dh = imagehash.dhash(img, hash_size=8)
                slide_data.append({
                    "index": idx + 1,
                    "path": path,
                    "size": f"{w}x{h}",
                    "phash": ph,
                    "dhash": dh,
                })
                print(f"[{idx + 1}] {path.name} ({w}x{h}) | pHash={ph} | dHash={dh}")
        except Exception as e:
            print(f"Failed to load image {path.name}: {e}")

    print("-" * 70)
    print("PAIRWISE COMPARISONS:")
    print("-" * 70)

    for i in range(len(slide_data)):
        for j in range(i + 1, len(slide_data)):
            s1 = slide_data[i]
            s2 = slide_data[j]

            p_dist = s1["phash"] - s2["phash"]
            d_dist = s1["dhash"] - s2["dhash"]

            p_sim = 1.0 - (p_dist / 64.0)
            d_sim = 1.0 - (d_dist / 64.0)

            # dHash is the primary metric for content structure
            is_dhash_dup = d_dist <= max_bits
            is_phash_dup = p_dist <= max_bits

            if is_dhash_dup:
                status = "DUPLICATE (same slide, cursor/speaker noise)"
            elif d_dist <= max_bits + 2:
                status = "SIMILAR (minor animation or partial changes)"
            else:
                status = "NEW SLIDE (different lecture material)"

            print(f"Slide [{s1['index']}] vs [{s2['index']}]:")
            print(f"  * dHash: {d_dist} bits difference (similarity {d_sim:.1%}) -> {'DUPLICATE' if is_dhash_dup else 'DIFFERENT'}")
            print(f"  * pHash: {p_dist} bits difference (similarity {p_sim:.1%}) -> {'DUPLICATE' if is_phash_dup else 'DIFFERENT'}")
            print(f"  * Verdict: {status}")
            print()

    print("=" * 70)


if __name__ == "__main__":
    main()
