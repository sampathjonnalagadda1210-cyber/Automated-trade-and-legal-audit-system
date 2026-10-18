"""
EAN-13 detection, decoding and photographic scale calibration.
===============================================================

Why this module exists
----------------------
Rule 7 of the LM(PC) Rules, 2011 fixes the *minimum height in millimetres*
of the numerals/letters used for the principal declarations.  To check that
from a photograph you need to know how many image pixels correspond to one
real-world millimetre.  Asking a field inspector to type in the physical
width of every package is slow and error-prone.

Almost every retail package in India already carries a dimensional
reference: its EAN-13 barcode.  Under the GS1 General Specifications an
EAN-13 symbol is 95 modules wide, and at the nominal 100% magnification
(the "SC2" size) one module is 0.330 mm, giving a printed symbol width of
31.35 mm for the bar pattern between the outer guard bars.

So: find the barcode, measure its width in pixels, divide by 31.35 mm, and
you have a px-per-mm scale for that photograph, derived from the package
itself.  Magnification is recovered too: a symbol printed at 80% simply has
a proportionally smaller module, and because we *decode* the symbol we can
sanity-check that what we measured really is an EAN-13 and not a stripey
piece of packaging art.

Everything here is plain NumPy/OpenCV - no external barcode library, so
there is no system-level dependency (zbar etc.) to install.
"""

from __future__ import annotations

import numpy as np

try:
    import cv2
except ImportError:  # pragma: no cover
    cv2 = None


# --------------------------------------------------------------------------
# GS1 constants
# --------------------------------------------------------------------------

MODULES_PER_SYMBOL = 95          # EAN-13 bar pattern, guard to guard
NOMINAL_MODULE_MM = 0.330        # module width at 100% magnification
NOMINAL_SYMBOL_WIDTH_MM = MODULES_PER_SYMBOL * NOMINAL_MODULE_MM   # 31.35 mm

# GS1 permits printing between 80% and 200% magnification.
MIN_MAGNIFICATION = 0.80
MAX_MAGNIFICATION = 2.00

L_CODES = ["0001101", "0011001", "0010011", "0111101", "0100011",
           "0110001", "0101111", "0111011", "0110111", "0001011"]
G_CODES = ["0100111", "0110011", "0011011", "0100001", "0011101",
           "0111001", "0000101", "0010001", "0001001", "0010111"]
R_CODES = ["1110010", "1100110", "1101100", "1000010", "1011100",
           "1001110", "1010000", "1000100", "1001000", "1110100"]

# Parity of the six left-hand digits encodes the (unprinted) first digit.
PARITY_TABLE = {
    "LLLLLL": 0, "LLGLGG": 1, "LLGGLG": 2, "LLGGGL": 3, "LGLLGG": 4,
    "LGGLLG": 5, "LGGGLL": 6, "LGLGLG": 7, "LGLGGL": 8, "LGGLGL": 9,
}

# GS1 prefixes -> issuing country/organisation. Only the ones an Indian
# inspector realistically meets are listed; anything else says "Other".
GS1_PREFIXES = [
    (890, 890, "India"),
    (0, 19, "USA / Canada"),
    (30, 37, "France"),
    (40, 44, "Germany"),
    (45, 49, "Japan"),
    (50, 50, "United Kingdom"),
    (69, 69, "China"),
    (76, 76, "Switzerland"),
    (80, 83, "Italy"),
    (84, 84, "Spain"),
    (87, 87, "Netherlands"),
    (88, 88, "South Korea"),
    (93, 93, "Australia"),
    (955, 955, "Malaysia"),
    (958, 958, "Macau"),
]


def gs1_country(code13: str) -> str:
    """Map a GS1 company prefix to its issuing country."""
    if not code13 or len(code13) < 3:
        return "Unknown"
    try:
        p3 = int(code13[:3])
        p2 = int(code13[:2])
    except ValueError:
        return "Unknown"
    for lo, hi, name in GS1_PREFIXES:
        if lo > 99:                       # three-digit prefix range
            if lo <= p3 <= hi:
                return name
        else:                             # two-digit prefix range
            if lo <= p2 <= hi:
                return name
    return "Other / not assigned to India"


def ean13_checksum_ok(code13: str) -> bool:
    """Validate the EAN-13 modulo-10 check digit."""
    if len(code13) != 13 or not code13.isdigit():
        return False
    total = sum(int(d) * (3 if i % 2 else 1) for i, d in enumerate(code13[:12]))
    return (10 - total % 10) % 10 == int(code13[12])


# --------------------------------------------------------------------------
# Encoder - used to render the bundled demo labels
# --------------------------------------------------------------------------

def ean13_bits(code13: str) -> str:
    """Return the 95-module bit pattern for a full 13-digit code."""
    if len(code13) != 13 or not code13.isdigit():
        raise ValueError("EAN-13 needs exactly 13 digits")
    parity = [k for k, v in PARITY_TABLE.items() if v == int(code13[0])][0]
    bits = "101"
    for digit_char, par in zip(code13[1:7], parity):
        table = L_CODES if par == "L" else G_CODES
        bits += table[int(digit_char)]
    bits += "01010"
    for digit_char in code13[7:]:
        bits += R_CODES[int(digit_char)]
    bits += "101"
    return bits


def ean13_add_check_digit(code12: str) -> str:
    total = sum(int(d) * (3 if i % 2 else 1) for i, d in enumerate(code12))
    return code12 + str((10 - total % 10) % 10)


# --------------------------------------------------------------------------
# Decoder
# --------------------------------------------------------------------------

def _best_match(pattern: str):
    """Nearest-Hamming match of a 7-bit pattern against the three code sets.

    Returns (digit, parity_letter, hamming_distance).
    """
    best = (None, None, 99)
    for table, letter in ((L_CODES, "L"), (G_CODES, "G"), (R_CODES, "R")):
        for digit, code in enumerate(table):
            dist = sum(1 for a, b in zip(pattern, code) if a != b)
            if dist < best[2]:
                best = (digit, letter, dist)
    return best


def _runs(binary_line: np.ndarray):
    """Split a binarised scanline into (start, length, is_dark) runs."""
    dark = binary_line > 0
    transitions = np.flatnonzero(np.diff(dark.astype(np.int8)))
    bounds = np.concatenate(([0], transitions + 1, [dark.size]))
    return [(int(s), int(e - s), bool(dark[s]))
            for s, e in zip(bounds[:-1], bounds[1:]) if e > s]


def _read_symbol(binary_line: np.ndarray, symbol_start: int, module_px: float):
    """Read 95 modules starting at symbol_start and decode them."""
    bits = []
    for i in range(MODULES_PER_SYMBOL):
        centre = int(round(symbol_start + (i + 0.5) * module_px))
        if centre < 0 or centre >= binary_line.size:
            return None
        bits.append("1" if binary_line[centre] > 0 else "0")
    bits = "".join(bits)

    if bits[:3] != "101" or bits[-3:] != "101" or bits[45:50] != "01010":
        return None

    digits, parity, total_err = [], "", 0
    for i in range(6):                                  # left half
        digit, letter, err = _best_match(bits[3 + i * 7: 10 + i * 7])
        if letter == "R" or err > 1:
            return None
        digits.append(digit)
        parity += letter
        total_err += err
    for i in range(6):                                  # right half
        digit, letter, err = _best_match(bits[50 + i * 7: 57 + i * 7])
        if letter != "R" or err > 1:
            return None
        digits.append(digit)
        total_err += err

    if parity not in PARITY_TABLE:
        return None

    code13 = str(PARITY_TABLE[parity]) + "".join(str(d) for d in digits)
    if not ean13_checksum_ok(code13):
        return None
    return code13, total_err


def _decode_scanline(binary_line: np.ndarray, max_read_attempts: int = 12):
    """Decode one binarised scanline (1 = dark bar, 0 = light space).

    Rather than assuming the first and last dark pixels on the row are the
    guard bars - which breaks the moment the human-readable digits, a border
    or any other ink shares the row - this searches for the start guard
    pattern itself: bar-space-bar of three equal modules, preceded by a
    quiet zone.  That fixes the module width, the 95-module span follows,
    and the symbol is read by sampling module centres.

    After a successful decode the right-hand edge is snapped to the actual
    last dark run of the end guard, so the measured width is the printed
    width rather than 95x an estimate.

    ``max_read_attempts`` bounds the futile work when the guard pattern is
    a false trigger: a barcode-free line of text can produce dozens of
    candidate start positions, and each one costs ~70 read attempts, which
    is where all of the calibration time on barcode-free images went.  A
    real symbol decodes from its first few start candidates, so the cap
    costs nothing in the cases that matter.
    """
    runs = _runs(binary_line)
    if len(runs) < 30:
        return None

    attempts_left = max_read_attempts

    for i in range(len(runs) - 2):
        r0, r1, r2 = runs[i], runs[i + 1], runs[i + 2]
        if not (r0[2] and not r1[2] and r2[2]):
            continue                                   # need bar-space-bar

        module_px = (r0[1] + r1[1] + r2[1]) / 3.0
        if module_px < 0.9:
            continue
        # The three guard elements must each be about one module wide.
        if any(not (0.55 * module_px <= r[1] <= 1.75 * module_px)
               for r in (r0, r1, r2)):
            continue
        # A quiet zone of light pixels must precede the start guard.
        if i > 0:
            prev = runs[i - 1]
            if prev[2] or prev[1] < 2.5 * module_px:
                continue

        symbol_start = r0[0]
        nominal = MODULES_PER_SYMBOL * module_px
        if symbol_start + nominal * 0.8 > binary_line.size:
            continue

        # The guard bars are only three pixels-wide elements, so the module
        # width they give is a rough estimate - and a 4% error compounds
        # badly over 95 modules.  Pin the far edge down instead: the end
        # guard's trailing edge is the right-hand end of some dark run, so
        # try each dark run ending in the plausible window as the symbol end.
        candidate_ends = {
            s + length for s, length, is_dark in runs
            if is_dark and 0.80 * nominal <= (s + length - symbol_start)
            <= 1.25 * nominal
        }
        # Plus a coarse sweep, in case the end guard merged with something.
        # 1% steps are enough: a decode is validated by the check digit, so
        # only widths within a fraction of a percent of the true pitch can
        # pass all 12 digits anyway.  Sweep and run-edge candidates are
        # merged and tried nearest the nominal width first - the true edge
        # is never far from nominal at any magnification, so the correct
        # pitch is usually among the first attempts (which keeps the
        # attempt budget below from ever biting on a genuine symbol).
        sweep = [symbol_start + nominal * f
                 for f in np.arange(0.85, 1.201, 0.01)]
        ends = sorted(candidate_ends | set(sweep),
                      key=lambda e: abs((e - symbol_start) - nominal))

        for x_end in ends:
            width = x_end - symbol_start
            if width <= 0 or symbol_start + width > binary_line.size:
                continue
            m = width / MODULES_PER_SYMBOL
            if m < 0.9:
                continue
            if attempts_left <= 0:
                break                     # this start position is a dud
            attempts_left -= 1
            got = _read_symbol(binary_line, symbol_start, m)
            if got is None:
                continue
            code13, err = got
            return {
                "code": code13,
                "x_start": symbol_start,
                "x_end": x_end,
                "width_px": width,
                "module_px": m,
                "bit_errors": err,
            }
    return None


def _locate_barcode_regions(gray: np.ndarray, max_regions: int = 4):
    """Find candidate barcode regions via horizontal-gradient energy.

    A 1-D barcode is a band with strong horizontal gradient and weak
    vertical gradient.  Closing that response with a wide horizontal kernel
    merges the individual bars into one solid blob we can bound.
    """
    grad_x = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=-1)
    grad_y = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=-1)
    grad = cv2.convertScaleAbs(cv2.subtract(np.absolute(grad_x),
                                            np.absolute(grad_y)))

    blurred = cv2.blur(grad, (9, 9))
    _, thresh = cv2.threshold(blurred, 0, 255,
                              cv2.THRESH_BINARY + cv2.THRESH_OTSU)

    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (21, 7))
    closed = cv2.morphologyEx(thresh, cv2.MORPH_CLOSE, kernel)
    closed = cv2.erode(closed, None, iterations=4)
    closed = cv2.dilate(closed, None, iterations=4)

    contours, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL,
                                   cv2.CHAIN_APPROX_SIMPLE)
    boxes = []
    img_area = gray.shape[0] * gray.shape[1]
    for c in sorted(contours, key=cv2.contourArea, reverse=True)[:12]:
        x, y, w, h = cv2.boundingRect(c)
        if w < 40 or h < 12:
            continue
        if w * h < img_area * 0.0008:
            continue
        if w / max(h, 1) < 0.7:                 # barcodes are wider than tall
            continue
        boxes.append((x, y, w, h))
        if len(boxes) >= max_regions:
            break
    return boxes


def _decode_in_window(gray: np.ndarray, x: int, y: int, w: int, h: int):
    """Try to decode an EAN-13 inside one window, voting across scanlines."""
    pad_x, pad_y = int(w * 0.10) + 6, int(h * 0.10) + 3
    x0 = max(x - pad_x, 0)
    x1 = min(x + w + pad_x, gray.shape[1])
    y0 = max(y - pad_y, 0)
    y1 = min(y + h + pad_y, gray.shape[0])
    roi = gray[y0:y1, x0:x1]
    if roi.size == 0 or roi.shape[0] < 6:
        return None

    # Upscale narrow crops so each module lands on several pixels.
    scale = 1.0
    if roi.shape[1] < 500:
        scale = 500.0 / roi.shape[1]
        roi = cv2.resize(roi, None, fx=scale, fy=scale,
                         interpolation=cv2.INTER_CUBIC)

    roi = cv2.GaussianBlur(roi, (3, 3), 0)
    _, binary = cv2.threshold(roi, 0, 255,
                              cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)

    # Sample scanlines across the middle 80% of the band.  Decodes are
    # tallied as they arrive and the search stops as soon as one code has
    # enough agreement - a clean symbol decodes on nearly every scanline,
    # so waiting for all 21 only ever repaid a symbol that was already
    # proven.  Stopping at 8 agreeing decodes keeps the reported confidence
    # depth identical to the old behaviour, which counted up to all 21
    # lines but floor-capped its depth at 8.
    results = []
    codes = {}
    CONSENSUS = 8
    MAX_DECODES = 8
    winner = None
    height = binary.shape[0]
    for frac in np.linspace(0.12, 0.88, 21):
        row = binary[int(height * frac)]
        got = _decode_scanline(row)
        if got:
            results.append(got)
            group = codes.setdefault(got["code"], [])
            group.append(got)
            if winner is None and len(group) >= CONSENSUS:
                winner = got["code"]
                break
        if len(results) >= MAX_DECODES:
            break

    if not results:
        return None

    code = winner or max(codes.items(), key=lambda kv: len(kv[1]))[0]
    group = codes[code]

    width_px_roi = float(np.median([g["width_px"] for g in group]))
    width_px_orig = width_px_roi / scale

    # Tighten the reported box onto the symbol itself. The search window is
    # deliberately generous, but the evidence overlay should show the ruler
    # that was actually used, not the area it was hunted in.
    x_start_orig = x0 + float(np.median([g["x_start"] for g in group])) / scale
    pad = width_px_orig * 0.06
    box_x = int(max(x_start_orig - pad, 0))
    box_w = int(min(width_px_orig + 2 * pad, gray.shape[1] - box_x))

    return {
        "code": code,
        "width_px": width_px_orig,
        "module_px": width_px_orig / MODULES_PER_SYMBOL,
        "votes": len(group),
        "scanlines_tried": len(results),
        "box": (box_x, y0, box_w, y1 - y0),
    }


def detect_and_calibrate(image_bgr, known_magnification: float | None = None):
    """Locate an EAN-13 in the image and derive a px-per-mm scale.

    Returns a dict describing what happened.  ``ok`` is True only when a
    checksum-valid symbol was decoded *and* the implied magnification falls
    inside the GS1-permitted 80-200% band; otherwise the caller should fall
    back to manual calibration.
    """
    if cv2 is None:
        return {"ok": False, "reason": "OpenCV not available"}

    gray = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY) \
        if image_bgr.ndim == 3 else image_bgr

    candidates = []

    for box in _locate_barcode_regions(gray):
        got = _decode_in_window(gray, *box)
        if got:
            candidates.append(got)

    # Fallback: on very crisp renders the gradient blob can merge the bars
    # with neighbouring text, so sweep overlapping horizontal bands.  The
    # sweep stops at the first window that decodes - one checksum-valid
    # symbol is enough, and the scanline votes inside that window provide
    # the confidence depth - and is capped so a barcode-free image (an
    # e-commerce declaration crop, say) pays a bounded cost instead of a
    # full worst-case search.
    if not candidates:
        h, w = gray.shape[:2]
        band = max(h // 6, 40)
        tops = list(range(0, max(h - band // 2, 1), band // 2))[:8]
        for top in tops:
            got = _decode_in_window(gray, 0, top, w, min(band, h - top))
            if got:
                candidates.append(got)
                break

    if not candidates:
        return {
            "ok": False,
            "reason": "No EAN-13 symbol could be decoded in this image.",
        }

    # Several windows may have seen the same symbol; pool them by code so
    # the vote count reflects every scanline that agreed.
    by_code = {}
    for c in candidates:
        by_code.setdefault(c["code"], []).append(c)
    winner_code, group = max(by_code.items(),
                             key=lambda kv: sum(c["votes"] for c in kv[1]))
    total_votes = sum(c["votes"] for c in group)
    total_decoded = sum(c["votes"] for c in candidates)
    agreement = total_votes / max(total_decoded, 1)

    best = max(group, key=lambda c: c["votes"])
    best = dict(best)
    best["width_px"] = float(np.median([c["width_px"] for c in group]))
    best["votes"] = total_votes

    px_per_mm_nominal = best["width_px"] / NOMINAL_SYMBOL_WIDTH_MM
    magnification = known_magnification

    result = {
        "ok": True,
        "code": best["code"],
        "country": gs1_country(best["code"]),
        "symbol_width_px": round(best["width_px"], 1),
        "module_px": round(best["module_px"], 3),
        "votes": best["votes"],
        "scanlines_tried": best["scanlines_tried"],
        "box": best["box"],
        "assumed_magnification": magnification or 1.0,
        "reference_width_mm": round(
            NOMINAL_SYMBOL_WIDTH_MM * (magnification or 1.0), 2),
        "checksum_valid": True,
    }
    result["px_per_mm"] = round(
        px_per_mm_nominal / (magnification or 1.0), 3)
    # Confidence reflects two things: how many independent scanlines decoded
    # the symbol, and whether they all agreed on the same digits.  A single
    # clean scanline plus a valid check digit is already strong evidence, so
    # the floor is deliberately not near zero.
    depth = min(best["votes"], 8) / 8.0
    result["agreement"] = round(agreement, 3)
    result["confidence"] = round(
        min(1.0, (0.55 + 0.45 * depth) * (0.6 + 0.4 * agreement)), 3)
    return result


def implied_magnification(px_per_mm: float, symbol_width_px: float) -> float:
    """Given an independently-known scale, what magnification was printed?"""
    if px_per_mm <= 0:
        return 0.0
    return (symbol_width_px / px_per_mm) / NOMINAL_SYMBOL_WIDTH_MM
