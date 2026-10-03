import hashlib
import io
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, Union

from PIL import Image, ImageStat
import imagehash

logger = logging.getLogger(__name__)

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}


def is_meaningful_image(
    img: Image.Image,
    min_width: int = 250,
    min_height: int = 180,
    max_aspect_ratio: float = 4.5,
    min_variance: float = 80.0,
) -> bool:
    """
    Filters out decorative icons, bullets, tiny borders, and flat background placeholders.
    Returns True if image has meaningful resolution, normal aspect ratio, and color variance.
    """
    w, h = img.size
    # Check minimum dimensions (either landscape or portrait)
    if not ((w >= min_width and h >= min_height) or (w >= min_height and h >= min_width)):
        return False

    # Check aspect ratio (eliminate long thin divider lines or tall margin strips)
    aspect = max(w / h, h / w)
    if aspect > max_aspect_ratio:
        return False

    # Check color variance / entropy (eliminate solid single-color rectangles or empty white boxes)
    try:
        stat = ImageStat.Stat(img.convert("L"))
        variance = stat.var[0] if stat.var else 0.0
        if variance < min_variance:
            return False
    except Exception:
        pass

    return True


class AttachmentExtractor:
    """
    Intelligent processor for student attachments (PPTX, PDF, PNG, JPG).
    - Extracts native PPTX tables directly into Markdown.
    - Extracts charts and diagrams from PDF and PPTX, filtering logos and icons.
    - Performs pHash matching against webinar stream frames to upgrade blurry slides to pristine originals.
    """

    def __init__(self, output_dir: Optional[Union[str, Path]] = None):
        self.output_dir = Path(output_dir) if output_dir else None
        if self.output_dir:
            self.output_dir.mkdir(parents=True, exist_ok=True)

    def process_attachments(
        self,
        attachment_paths: List[Union[str, Path]],
        existing_slide_paths: Optional[List[Union[str, Path]]] = None,
        dest_slides_dir: Optional[Union[str, Path]] = None,
        attachments_dir: Optional[Union[str, Path]] = None,
    ) -> Dict[str, Any]:
        """
        Analyzes attachments and integrates them into the lecture materials.
        Returns:
            {
                "extracted_images": List[Path],       # New or upgraded illustrations
                "upgraded_slides_count": int,         # How many webinar slides were replaced by pristine originals
                "extracted_tables": List[Dict],       # Native tables formatted in Markdown
                "illustrations_info": List[Dict],     # Metadata descriptions for Gemini prompt
                "extracted_texts": List[Dict],        # Extracted text from attachments
            }
        """
        out_dir = Path(dest_slides_dir) if dest_slides_dir else (self.output_dir or Path("temp/extracted_slides"))
        out_dir.mkdir(parents=True, exist_ok=True)

        att_dir = None
        if attachments_dir:
            att_dir = Path(attachments_dir)
        elif attachment_paths and Path(attachment_paths[0]).parent.exists():
            att_dir = Path(attachment_paths[0]).parent

        existing_slides = [Path(p) for p in (existing_slide_paths or []) if Path(p).exists()]

        # Compute pHashes of existing webinar slides for pristine replacement
        existing_hashes = []
        for s_path in existing_slides:
            try:
                with Image.open(s_path) as img:
                    h = imagehash.phash(img, hash_size=8)
                    existing_hashes.append((s_path, h))
            except Exception as e:
                logger.debug(f"Could not hash existing slide {s_path.name}: {e}")

        extracted_images: List[Path] = []
        upgraded_slides_count = 0
        extracted_tables: List[Dict[str, str]] = []
        illustrations_info: List[Dict[str, str]] = []

        # Track image hashes to eliminate university logos repeated across multiple slides
        seen_image_hashes: Dict[str, int] = {}
        # (PIL Image, suggested_name, description, original_path_if_already_on_disk)
        candidate_images: List[Tuple[Image.Image, str, str, Optional[Path]]] = []

        extracted_texts: List[Dict[str, str]] = []

        for att in attachment_paths:
            att_path = Path(att)
            if not att_path.exists():
                continue

            ext = att_path.suffix.lower()

            # 1. Plain standalone images (.png, .jpg, .jpeg, .webp)
            if ext in IMAGE_EXTENSIONS:
                try:
                    with Image.open(att_path) as img:
                        loaded_img = img.convert("RGB")
                        candidate_images.append(
                            (loaded_img, att_path.name, f"Прикрепленное изображение: {att_path.name}", att_path)
                        )
                except Exception as e:
                    logger.warning(f"Failed to read image attachment {att_path.name}: {e}")

            # 2. PowerPoint presentations (.pptx)
            elif ext == ".pptx":
                pptx_res = self._process_pptx(att_path)
                extracted_tables.extend(pptx_res.get("tables", []))
                if pptx_res.get("text"):
                    extracted_texts.append({
                        "filename": att_path.name,
                        "content": pptx_res["text"][:50000],
                    })
                for c_img, c_name, c_desc in pptx_res.get("images", []):
                    candidate_images.append((c_img, c_name, c_desc, None))

            # 3. PDF documents (.pdf)
            elif ext == ".pdf":
                pdf_res = self._process_pdf(att_path)
                for c_img, c_name, c_desc in pdf_res.get("images", []):
                    candidate_images.append((c_img, c_name, c_desc, None))

        # Count occurrences of hashes to detect repeating logo templates
        for img, _, _, _ in candidate_images:
            try:
                md5_sig = hashlib.md5(img.tobytes()).hexdigest()
                seen_image_hashes[md5_sig] = seen_image_hashes.get(md5_sig, 0) + 1
            except Exception:
                pass

        # Filter candidates and perform pHash upgrade or register as new illustrations
        for img, suggested_name, description, orig_path in candidate_images:
            # Check logo repeater
            try:
                md5_sig = hashlib.md5(img.tobytes()).hexdigest()
                if seen_image_hashes.get(md5_sig, 0) > 2:
                    # Repeating logo or template border, discard
                    continue
            except Exception:
                pass

            if not is_meaningful_image(img):
                continue

            # Compare against existing webinar frames via pHash
            cand_hash = imagehash.phash(img, hash_size=8)
            matched_webinar_slide: Optional[Path] = None

            for s_path, s_hash in existing_hashes:
                dist = cand_hash - s_hash
                # Hamming distance <= 12 (similarity >= 0.82) -> upgrade webinar frame
                if dist <= 12:
                    matched_webinar_slide = s_path
                    break

            if matched_webinar_slide:
                # Upgrade blurry stream screenshot in-place with pristine original!
                try:
                    img.save(matched_webinar_slide, format="PNG")
                    upgraded_slides_count += 1
                    logger.info(
                        f"Upgraded stream slide {matched_webinar_slide.name} with pristine original ({suggested_name})"
                    )
                except Exception as e:
                    logger.warning(f"Could not overwrite {matched_webinar_slide}: {e}")
            else:
                # Standalone unique diagram/scheme
                # If already an existing attachment on disk, keep it in attachments without moving to slides/
                if orig_path and Path(orig_path).exists():
                    clean_target = Path(orig_path)
                else:
                    # Extracted from inside PPTX/PDF: save to attachments folder (or out_dir)
                    target_dir = att_dir if att_dir else out_dir
                    target_dir.mkdir(parents=True, exist_ok=True)
                    clean_target = target_dir / suggested_name
                    if clean_target.exists():
                        clean_target = target_dir / f"{clean_target.stem}_{int(len(extracted_images))}.png"
                    try:
                        img.save(clean_target, format="PNG")
                    except Exception as e:
                        logger.warning(f"Could not save illustration {clean_target.name}: {e}")
                        continue

                extracted_images.append(clean_target)
                illustrations_info.append({
                    "filename": clean_target.name,
                    "description": description,
                })
                logger.info(f"Registered attachment illustration: {clean_target.name}")

        return {
            "extracted_images": extracted_images,
            "upgraded_slides_count": upgraded_slides_count,
            "extracted_tables": extracted_tables,
            "illustrations_info": illustrations_info,
            "extracted_texts": extracted_texts,
        }

    def _process_pptx(self, pptx_path: Path) -> Dict[str, Any]:
        """Extracts native tables into Markdown, text content, and embedded images from a PPTX file."""
        tables = []
        images: List[Tuple[Image.Image, str, str]] = []
        slide_text_blocks: List[str] = []

        try:
            from pptx import Presentation
            prs = Presentation(str(pptx_path))
        except Exception as e:
            logger.warning(f"Could not parse PPTX {pptx_path.name} via python-pptx: {e}")
            return {"tables": tables, "images": images, "text": ""}

        for slide_idx, slide in enumerate(prs.slides, start=1):
            slide_title = ""
            texts: List[str] = []
            for shape in slide.shapes:
                # 1. Extract text and slide title if present
                if shape.has_text_frame and shape.text_frame.text:
                    t = shape.text_frame.text.strip()
                    if t:
                        texts.append(t)
                    if not slide_title:
                        slide_title = t.split("\n")[0].strip()

                # 2. Extract native tables as Markdown
                if shape.has_table:
                    tbl = shape.table
                    md_lines = []
                    header_done = False
                    for r_idx, row in enumerate(tbl.rows):
                        cells = [re.sub(r"\s+", " ", cell.text.strip()) for cell in row.cells]
                        if any(cells):
                            md_lines.append("| " + " | ".join(cells) + " |")
                            if not header_done:
                                md_lines.append("| " + " | ".join([":---"] * len(cells)) + " |")
                                header_done = True
                    if md_lines:
                        table_md = "\n".join(md_lines)
                        tables.append({
                            "source_file": pptx_path.name,
                            "slide_num": slide_idx,
                            "title": slide_title or f"Слайд {slide_idx}",
                            "markdown": table_md,
                        })

                # 3. Extract embedded pictures
                if shape.shape_type == 13:  # MSO_SHAPE_TYPE.PICTURE
                    try:
                        blob = shape.image.blob
                        with Image.open(io.BytesIO(blob)) as pil_img:
                            loaded_img = pil_img.convert("RGB")
                            images.append((
                                loaded_img,
                                f"pptx_{pptx_path.stem}_s{slide_idx}_img.png",
                                f"Иллюстрация из слайда {slide_idx} ('{slide_title or pptx_path.name}')",
                            ))
                    except Exception:
                        pass

            if texts:
                slide_text_blocks.append(f"--- Слайд {slide_idx}: {slide_title or 'Без заголовка'} ---\n" + "\n".join(texts))

        return {
            "tables": tables,
            "images": images,
            "text": "\n\n".join(slide_text_blocks),
        }

    def _process_pdf(self, pdf_path: Path) -> Dict[str, Any]:
        """Extracts diagrams and captioned images from a PDF file using PyMuPDF."""
        images: List[Tuple[Image.Image, str, str]] = []
        try:
            import fitz
        except ImportError:
            logger.warning("PyMuPDF (fitz) is not installed; skipping PDF extraction.")
            return {"images": images}

        caption_keywords = ["рис.", "рисунок", "схема", "диаграмма", "таблица", "fig.", "architecture"]

        try:
            with fitz.open(str(pdf_path)) as doc:
                for page_idx in range(len(doc)):
                    page = doc[page_idx]
                    page_text = page.get_text("text") or ""
                    page_lower = page_text.lower()

                    # Find matching caption on this page
                    caption = ""
                    for kw in caption_keywords:
                        match = re.search(rf"({kw}\s*[\d\.\-]+[^\n\.\;]*)", page_lower)
                        if match:
                            caption = match.group(1).strip()
                            break

                    desc = f"Иллюстрация из {pdf_path.name} (стр. {page_idx + 1})"
                    if caption:
                        desc += f": {caption}"

                    # Extract embedded images
                    img_list = page.get_images(full=True)
                    for img_idx, img_info in enumerate(img_list):
                        xref = img_info[0]
                        try:
                            base_image = doc.extract_image(xref)
                            image_bytes = base_image["image"]
                            with Image.open(io.BytesIO(image_bytes)) as pil_img:
                                loaded_img = pil_img.convert("RGB")
                                images.append((
                                    loaded_img,
                                    f"pdf_{pdf_path.stem}_p{page_idx + 1}_i{img_idx + 1}.png",
                                    desc,
                                ))
                        except Exception:
                            pass
        except Exception as e:
            logger.warning(f"Could not parse PDF {pdf_path.name} via PyMuPDF: {e}")
            return {"images": images}

        return {"images": images}

