"""
Image conditioning, OCR and physical measurement.
=================================================

Three jobs live here.

1. **Make a phone photo readable.**  Retail packaging is glossy, curved and
   photographed under whatever light the shop has.  CLAHE evens out patchy
   illumination; the multi-frame merge deals with specular glare, which no
   amount of single-image contrast work can recover because the pixels
   under a blown-out highlight carry no information at all.

2. **Read it.**  Tesseract gives word boxes with per-word confidence; those
   are grouped into lines using Tesseract's own layout analysis so that two
   panels sitting side by side in one photo don't get spliced together.

3. **Measure it.**  Rule 7 talks about the *height of the numerals* in
   millimetres.  Tesseract's bounding box is not that - it includes
   descenders and internal leading.  So for a declaration we locate the
   numeric token, crop it, and measure the actual vertical extent of ink.
   Combined with the px/mm scale from the barcode, that is a real
   millimetre measurement rather than a proxy.
"""

from __future__ import annotations

import os

import numpy as np

try:
    import cv2
except ImportError:  # pragma: no cover
    cv2 = None

try:
    import pytesseract
    from pytesseract import Output
except ImportError:  # pragma: no cover
    pytesseract = None
    Output = None


# --------------------------------------------------------------------------
# Tesseract discovery
# --------------------------------------------------------------------------

_WINDOWS_GUESSES = [
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
    os.path.expanduser(r"~\AppData\Local\Tesseract-OCR\tesseract.exe"),
    os.path.expanduser(r"~\AppData\Local\Programs\Tesseract-OCR\tesseract.exe"),
]


_TESSERACT_STATE = None          # (available, message), cached after first success


def ensure_tesseract():
    """Return (available, message). Tries PATH, then the usual install dirs.

    Windows installers routinely skip the "add to PATH" box, which is the
    single most common reason this app appears broken on a fresh laptop, so
    it is worth looking in the default locations before giving up.

    The answer is cached: checking used to spawn a tesseract process on
    every call, and the check runs on every page render, which made the
    whole site sluggish for no reason.
    """
    global _TESSERACT_STATE
    if _TESSERACT_STATE is not None:
        return _TESSERACT_STATE
    if pytesseract is None:
        return False, "The pytesseract package is not installed."
    try:
        version = pytesseract.get_tesseract_version()
        _TESSERACT_STATE = True, f"Tesseract {version}"
        return _TESSERACT_STATE
    except Exception:
        pass
    for guess in _WINDOWS_GUESSES:
        if os.path.exists(guess):
            pytesseract.pytesseract.tesseract_cmd = guess
            try:
                version = pytesseract.get_tesseract_version()
                _TESSERACT_STATE = True, f"Tesseract {version} (found at {guess})"
                return _TESSERACT_STATE
            except Exception:
                continue
    return False, (
        "Tesseract OCR engine not found. Install it and make sure it is on "
        "PATH - see the README for the one-line instructions per platform."
    )


# --------------------------------------------------------------------------
# Loading and conditioning
# --------------------------------------------------------------------------

def load_bgr(path):
    img = cv2.imread(path, cv2.IMREAD_COLOR)
    if img is None:
        from PIL import Image
        img = cv2.cvtColor(
            np.array(Image.open(path).convert("RGB")), cv2.COLOR_RGB2BGR)
    # Very large phone photos slow OCR down without helping it; cap the long
    # edge but stay generous enough that small print survives.
    h, w = img.shape[:2]
    longest = max(h, w)
    if longest > 2400:
        scale = 2400.0 / longest
        img = cv2.resize(img, None, fx=scale, fy=scale,
                         interpolation=cv2.INTER_AREA)
    return img


def apply_clahe(gray, clip=2.5, tile=8):
    """Contrast Limited Adaptive Histogram Equalisation.

    Plain histogram equalisation would blow out the bright half of an
    unevenly lit label; CLAHE works tile-by-tile with a contrast ceiling, so
    text in the shadowed corner of a pack becomes readable without
    destroying the well-lit part.
    """
    clahe = cv2.createCLAHE(clipLimit=clip, tileGridSize=(tile, tile))
    return clahe.apply(gray)


def glare_map(gray, sat_window=9):
    """Per-pixel glare score in 0..1 (1 = hopeless specular highlight).

    A glare patch is three things at once: brighter than the label's own
    paper white, locally flat (the highlight has erased the texture), and
    close to sensor saturation.  All three tests are needed - judging
    brightness against a fixed threshold would condemn every cream or white
    carton in the country as glare, which is exactly the false positive that
    would make a field tool useless.
    """
    g = gray.astype(np.float32)
    # Estimate the paper white from the image itself. The 60th percentile is
    # low enough that a large highlight can't drag it upwards, and high
    # enough to sit on the background rather than the print.
    paper_white = float(np.percentile(g, 60))
    bright_thresh = max(paper_white + 9.0, 238.0)
    bright = np.clip((g - bright_thresh) / max(255 - bright_thresh, 1), 0, 1)
    local_std = cv2.blur(gray.astype(np.float32) ** 2, (sat_window, sat_window)) - \
        cv2.blur(gray.astype(np.float32), (sat_window, sat_window)) ** 2
    local_std = np.sqrt(np.clip(local_std, 0, None))
    flat = 1.0 - np.clip(local_std / 18.0, 0, 1)
    score = bright * flat
    return cv2.GaussianBlur(score, (0, 0), 5)


def glare_fraction(gray):
    return float((glare_map(gray) > 0.45).mean())


def _align_to(reference_gray, moving_gray, moving_bgr):
    """Align one frame to the reference using ORB features + homography.

    Hand-held frames are never pixel-aligned, so merging them without
    registration would smear the text.  If not enough matches are found the
    frame is returned unchanged and the caller falls back to whatever
    overlap it already has.
    """
    try:
        orb = cv2.ORB_create(nfeatures=1500)
        kp1, des1 = orb.detectAndCompute(reference_gray, None)
        kp2, des2 = orb.detectAndCompute(moving_gray, None)
        if des1 is None or des2 is None or len(kp1) < 20 or len(kp2) < 20:
            return moving_bgr, False
        matcher = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True)
        matches = sorted(matcher.match(des1, des2), key=lambda m: m.distance)
        matches = matches[:200]
        if len(matches) < 15:
            return moving_bgr, False
        src = np.float32([kp2[m.trainIdx].pt for m in matches]).reshape(-1, 1, 2)
        dst = np.float32([kp1[m.queryIdx].pt for m in matches]).reshape(-1, 1, 2)
        H, mask = cv2.findHomography(src, dst, cv2.RANSAC, 4.0)
        if H is None or mask is None or int(mask.sum()) < 12:
            return moving_bgr, False
        h, w = reference_gray.shape[:2]
        warped = cv2.warpPerspective(moving_bgr, H, (w, h),
                                     borderMode=cv2.BORDER_REPLICATE)
        return warped, True
    except cv2.error:
        return moving_bgr, False


def merge_frames(frames_bgr):
    """Fuse several frames of the same label into one glare-free image.

    Each frame is registered to the first, scored for glare, and then every
    output pixel is taken from whichever frame saw that spot least
    reflectively.  The selection map is heavily blurred before use so the
    result cross-fades between frames instead of showing hard seams.

    Returns (merged_bgr, report).
    """
    if len(frames_bgr) == 1:
        gray = cv2.cvtColor(frames_bgr[0], cv2.COLOR_BGR2GRAY)
        return frames_bgr[0], {
            "frames": 1, "aligned": 0,
            "glare_before": round(glare_fraction(gray) * 100, 1),
            "glare_after": round(glare_fraction(gray) * 100, 1),
            "recovered_pct": 0.0,
        }

    reference = frames_bgr[0]
    ref_gray = cv2.cvtColor(reference, cv2.COLOR_BGR2GRAY)

    stack, aligned_count = [reference], 0
    for frame in frames_bgr[1:]:
        if frame.shape != reference.shape:
            frame = cv2.resize(frame, (reference.shape[1], reference.shape[0]))
        f_gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        warped, ok = _align_to(ref_gray, f_gray, frame)
        aligned_count += 1 if ok else 0
        stack.append(warped)

    scores = np.stack(
        [glare_map(cv2.cvtColor(f, cv2.COLOR_BGR2GRAY)) for f in stack])
    # Weight = how usable this frame is here. The exponent sharpens the
    # preference so a clean frame clearly wins over a glared one.
    weights = np.power(1.0 - np.clip(scores, 0, 1), 6) + 1e-4
    weights = np.stack([cv2.GaussianBlur(w, (0, 0), 9) for w in weights])
    weights /= weights.sum(axis=0, keepdims=True)

    merged = np.zeros_like(stack[0], dtype=np.float32)
    for frame, weight in zip(stack, weights):
        merged += frame.astype(np.float32) * weight[..., None]
    merged = np.clip(merged, 0, 255).astype(np.uint8)

    before = glare_fraction(ref_gray)
    after = glare_fraction(cv2.cvtColor(merged, cv2.COLOR_BGR2GRAY))
    return merged, {
        "frames": len(frames_bgr),
        "aligned": aligned_count,
        "glare_before": round(before * 100, 1),
        "glare_after": round(after * 100, 1),
        "recovered_pct": round(max(before - after, 0) * 100, 1),
    }


def prepare_for_ocr(bgr):
    """Grayscale + CLAHE + gentle sharpening, tuned for photographed print."""
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    enhanced = apply_clahe(gray)
    blur = cv2.GaussianBlur(enhanced, (0, 0), 1.6)
    sharp = cv2.addWeighted(enhanced, 1.55, blur, -0.55, 0)
    return gray, sharp


# --------------------------------------------------------------------------
# Capture quality (drives confidence-gated capture)
# --------------------------------------------------------------------------

def assess_capture(gray):
    """Score how usable a capture is *before* trusting anything read from it.

    - sharpness: variance of the Laplacian, the standard focus measure
    - glare: fraction of the frame lost to specular highlights
    - exposure: how much of the histogram is pinned at either end
    """
    lap_var = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    sharpness = float(np.clip(lap_var / 220.0, 0, 1))
    glare = glare_fraction(gray)
    clipped = float(((gray < 6) | (gray > 250)).mean())
    exposure = float(np.clip(1.0 - clipped * 3.0, 0, 1))
    return {
        "laplacian_variance": round(lap_var, 1),
        "sharpness": round(sharpness, 3),
        "glare_pct": round(glare * 100, 1),
        "exposure": round(exposure, 3),
    }


# --------------------------------------------------------------------------
# OCR
# --------------------------------------------------------------------------

def ocr_words(image_for_ocr, lang="eng", psm=3):
    """Run Tesseract and return word-level boxes with confidences."""
    available, message = ensure_tesseract()
    if not available:
        raise RuntimeError(message)

    data = pytesseract.image_to_data(
        image_for_ocr, lang=lang, output_type=Output.DICT,
        config=f"--psm {psm}",
    )
    words = []
    for i in range(len(data["text"])):
        text = data["text"][i].strip()
        if not text:
            continue
        try:
            conf = float(data["conf"][i])
        except (TypeError, ValueError):
            conf = -1.0
        if conf < 25:
            continue
        words.append({
            "text": text,
            "x": int(data["left"][i]), "y": int(data["top"][i]),
            "w": int(data["width"][i]), "h": int(data["height"][i]),
            "conf": conf,
            "block": int(data["block_num"][i]),
            "par": int(data["par_num"][i]),
            "line": int(data["line_num"][i]),
        })
    return words


def group_lines(words, x_gap_factor=3.0):
    """Group words into lines, respecting Tesseract's own layout analysis.

    Clustering purely by y-coordinate would merge two side-by-side panels
    into one nonsense line, so the block/paragraph/line numbers Tesseract
    already computed are used instead - then a line is split further if two
    consecutive words are separated by an unusually wide gap, which is a
    reliable sign of two unrelated columns.
    """
    if not words:
        return []

    groups = {}
    for word in words:
        groups.setdefault((word["block"], word["par"], word["line"]), []).append(word)

    raw = [sorted(v, key=lambda b: b["x"])
           for _, v in sorted(groups.items(),
                              key=lambda kv: (kv[1][0]["y"], kv[1][0]["x"]))]

    segments = []
    for line in raw:
        if len(line) <= 1:
            segments.append(line)
            continue
        char_widths = sorted(b["w"] / max(len(b["text"]), 1) for b in line)
        median_char = char_widths[len(char_widths) // 2]
        threshold = max(median_char * x_gap_factor, 25)
        current = [line[0]]
        for prev, cur in zip(line, line[1:]):
            if cur["x"] - (prev["x"] + prev["w"]) > threshold:
                segments.append(current)
                current = [cur]
            else:
                current.append(cur)
        segments.append(current)

    lines = []
    for seg in segments:
        x = min(b["x"] for b in seg)
        y = min(b["y"] for b in seg)
        lines.append({
            "text": " ".join(b["text"] for b in seg),
            "x": x, "y": y,
            "w": max(b["x"] + b["w"] for b in seg) - x,
            "h": max(b["h"] for b in seg),
            "conf": sum(b["conf"] for b in seg) / len(seg),
            "words": seg,
        })
    return lines


# --------------------------------------------------------------------------
# Physical measurement
# --------------------------------------------------------------------------

def measure_ink_height_px(gray, box, pad=3):
    """Measure the true vertical extent of ink inside a word box.

    Tesseract's box height is not the glyph height: it carries descenders
    and a little padding.  Rule 7 is about the height of the numerals
    themselves, so we crop, binarise locally, discard near-empty rows as
    noise, and return the distance between the first and last inked row.
    """
    x, y, w, h = box["x"], box["y"], box["w"], box["h"]
    y0 = max(y - pad, 0)
    y1 = min(y + h + pad, gray.shape[0])
    x0 = max(x - pad, 0)
    x1 = min(x + w + pad, gray.shape[1])
    crop = gray[y0:y1, x0:x1]
    if crop.size == 0 or crop.shape[0] < 3:
        return None

    _, binary = cv2.threshold(crop, 0, 255,
                              cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    row_ink = (binary > 0).sum(axis=1)
    if row_ink.max() == 0:
        return None
    # Ignore rows with only a trace of ink - usually antialiasing or a stray
    # speck rather than part of a glyph.
    threshold = max(row_ink.max() * 0.12, 1)
    inked = np.flatnonzero(row_ink >= threshold)
    if inked.size == 0:
        return None
    return float(inked[-1] - inked[0] + 1)


def measure_declaration_height_mm(gray, line, px_per_mm, prefer_numeric=True):
    """Measure the printed height of a declaration in millimetres.

    Prefers a numeric token (Rule 7 speaks of the height of *numerals*) and
    falls back to the tallest word on the line.  Returns (mm, token, method)
    or (None, None, reason).
    """
    if not px_per_mm or px_per_mm <= 0:
        return None, None, "no calibration"

    words = line.get("words") or []
    if not words:
        return None, None, "no word boxes"

    candidates = []
    if prefer_numeric:
        candidates = [w for w in words if any(ch.isdigit() for ch in w["text"])]
    if not candidates:
        candidates = words

    best_mm, best_token = None, None
    for word in candidates:
        px = measure_ink_height_px(gray, word)
        if px is None:
            continue
        mm = px / px_per_mm
        # Rule 7 is a *minimum* height, so the governing measurement is the
        # smallest glyph height in the declaration, not the average.
        if best_mm is None or mm < best_mm:
            best_mm, best_token = mm, word["text"]

    if best_mm is None:
        return None, None, "could not isolate glyphs"
    method = "numeral ink height" if prefer_numeric else "glyph ink height"
    return round(best_mm, 2), best_token, method


def annotate(bgr, lines, highlights=None, barcode_box=None):
    """Draw an evidence overlay: detected lines, flagged lines, barcode."""
    out = bgr.copy()
    highlights = highlights or {}
    for line in lines:
        # OpenCV takes BGR, so these tuples read blue-green-red.
        colour = (150, 150, 150)          # neutral grey: read, not a declaration
        thickness = 1
        key = line["text"][:40]
        if key in highlights:
            colour = (45, 45, 190) if highlights[key] == "fail" else (70, 150, 60)
            thickness = 3
        cv2.rectangle(out, (line["x"], line["y"]),
                      (line["x"] + line["w"], line["y"] + line["h"]),
                      colour, thickness)
    if barcode_box:
        x, y, w, h = barcode_box
        cv2.rectangle(out, (x, y), (x + w, y + h), (0, 150, 245), 3)
        scale = max(out.shape[1] / 1400.0, 0.5)
        cv2.putText(out, "EAN-13 scale reference", (x, max(y - 10, 18)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.65 * scale, (0, 150, 245),
                    max(int(2 * scale), 2))
    return out
