"""
The scan pipeline.
==================

One function, ``run_scan``, drives the whole on-device chain so that the
single-image path, the multi-frame path and the batch path all behave
identically - there is exactly one place where a scan is decided, which is
what keeps the batch results consistent with what an inspector sees on
screen for the same photograph.

    frames -> merge (glare) -> calibrate (EAN-13) -> condition (CLAHE)
           -> OCR -> measure glyph heights in mm -> rule engine -> verdict

Everything above runs locally. Nothing in this module touches the network.
"""

from __future__ import annotations

import os
import uuid

import cv2

from . import barcode as bc
from . import ocr_engine as ocr
from .rule_engine import evaluate

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UPLOAD_DIR = os.path.join(BASE_DIR, "static", "uploads")


def new_uid():
    return uuid.uuid4().hex[:10].upper()


def run_scan(image_paths, manual_width_mm=None, category=None,
             embossed=False, save_prefix=None, write_overlay=True):
    """Run the full on-device chain over one or more frames of a label.

    ``image_paths``      - one path, or several frames of the same pack.
    ``manual_width_mm``  - fallback calibration: the real width of the pack
                           as it appears in the frame. Used only when no
                           barcode could be decoded.

    Returns a dict with the evaluation, the calibration, the merge report
    and the filenames written.
    """
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    uid = save_prefix or new_uid()

    frames = [ocr.load_bgr(p) for p in image_paths]
    if not frames:
        raise ValueError("no frames supplied")

    # 1. Glare recovery across frames -------------------------------------
    merged, merge_report = ocr.merge_frames(frames)

    # 2. Calibration from the barcode -------------------------------------
    calibration = bc.detect_and_calibrate(merged)
    if calibration.get("ok"):
        px_per_mm = calibration["px_per_mm"]
        calibration_mode = (
            f"EAN-13 reference ({calibration['code']}), "
            f"{calibration['symbol_width_px']:.0f} px across a "
            f"{calibration['reference_width_mm']} mm symbol")
    elif manual_width_mm:
        px_per_mm = merged.shape[1] / float(manual_width_mm)
        calibration_mode = (
            f"manual fallback - operator entered a pack width of "
            f"{manual_width_mm} mm")
    else:
        px_per_mm = None
        calibration_mode = "none - font-height checks skipped"

    # 3. Conditioning and OCR ---------------------------------------------
    gray, sharpened = ocr.prepare_for_ocr(merged)
    quality = ocr.assess_capture(gray)
    words = ocr.ocr_words(sharpened)
    lines = ocr.group_lines(words)

    # 4. Physical measurement ---------------------------------------------
    for line in lines:
        mm, token, method = ocr.measure_declaration_height_mm(
            gray, line, px_per_mm)
        line["height_mm"] = mm
        line["height_token"] = token
        line["height_method"] = method

    # 5. Rules -------------------------------------------------------------
    result = evaluate(lines, calibration=calibration, capture_quality=quality,
                      category=category, embossed=embossed)

    # 6. Persist the working image and an annotated copy --------------------
    image_file = f"{uid}.jpg"
    cv2.imwrite(os.path.join(UPLOAD_DIR, image_file), merged,
                [int(cv2.IMWRITE_JPEG_QUALITY), 88])

    overlay_file = None
    if write_overlay:
        failed_fields = {v["field"] for v in result["violations"]}
        highlights = {}
        for key, res in result["field_results"].items():
            if not res.get("matched_text"):
                continue
            tag = "fail" if key in failed_fields else "ok"
            highlights[res["matched_text"][:40]] = tag
        box = calibration.get("box") if calibration.get("ok") else None
        annotated = ocr.annotate(merged, lines, highlights, box)
        overlay_file = f"{uid}_overlay.jpg"
        cv2.imwrite(os.path.join(UPLOAD_DIR, overlay_file), annotated,
                    [int(cv2.IMWRITE_JPEG_QUALITY), 88])

    result["lines"] = [
        {k: v for k, v in line.items() if k != "words"} for line in lines
    ]

    return {
        "uid": uid,
        "result": result,
        "calibration": calibration,
        "calibration_mode": calibration_mode,
        "px_per_mm": px_per_mm,
        "merge_report": merge_report,
        "quality": quality,
        "image_file": image_file,
        "overlay_file": overlay_file,
        "frame_count": len(frames),
    }


def scan_to_record(scan, user_id=None, form=None, source="single"):
    """Shape a pipeline result into the row that goes into the database."""
    form = form or {}
    result = scan["result"]
    cal = scan["calibration"]
    summary = result["summary"]
    return {
        "uid": scan["uid"],
        "user_id": user_id,
        "product_name": form.get("product_name") or _guess_product(result),
        "category": form.get("category"),
        "manufacturer": form.get("manufacturer") or _guess_manufacturer(result),
        "location": form.get("location"),
        "source": source,
        "image_file": scan["image_file"],
        "overlay_file": scan["overlay_file"],
        "frame_count": scan["frame_count"],
        "barcode": cal.get("code") if cal.get("ok") else None,
        "barcode_country": cal.get("country") if cal.get("ok") else None,
        "px_per_mm": scan["px_per_mm"],
        "calibration_mode": scan["calibration_mode"],
        "verdict": result["verdict"],
        "major_count": summary["major"],
        "minor_count": summary["minor"],
        "advisory_count": summary["advisory"],
        "quality_score": result["gate"]["score"],
        "result": result,
    }


def _guess_product(result):
    field = result["field_results"].get("COMMODITY_NAME") or {}
    return (field.get("matched_text") or "Unidentified pack")[:120]


def _guess_manufacturer(result):
    field = result["field_results"].get("MANUFACTURER_NAME_ADDRESS") or {}
    text = field.get("matched_text")
    if not text:
        return None
    for marker in ("by:", "by ", "By:"):
        if marker in text:
            return text.split(marker, 1)[1].strip()[:120]
    return text[:120]
