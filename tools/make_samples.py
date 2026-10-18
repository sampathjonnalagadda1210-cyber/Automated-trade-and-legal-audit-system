"""
Builds the demo label images that ship with A.T.L.A.S.
======================================================

These are *synthetic but dimensionally honest* labels: every image is
rendered at a known pixels-per-millimetre scale, the barcode is a real
EAN-13 symbol drawn at GS1 nominal magnification, and each declaration is
drawn at a stated cap-height in millimetres.  That means the ground truth
is known, so the barcode calibration and the Rule 7 font-height check can
actually be verified rather than just demonstrated.

The rendered PNGs are committed under static/samples/, so this script only
needs re-running if you want to change the demo set.

    python tools/make_samples.py
"""

import os
import random
import sys

from PIL import Image, ImageDraw, ImageFilter, ImageFont

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.barcode import ean13_add_check_digit, ean13_bits   # noqa: E402

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(BASE, "static", "samples")
BATCH = os.path.join(OUT, "batch")

PX_PER_MM = 12.0          # render scale: every image uses this

FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "C:/Windows/Fonts/arial.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
]
FONT_BOLD_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
]


def _font_path(candidates):
    for p in candidates:
        if os.path.exists(p):
            return p
    raise SystemExit("No usable TrueType font found on this machine.")


REGULAR = _font_path(FONT_CANDIDATES)
BOLD = _font_path(FONT_BOLD_CANDIDATES)


def font_for_cap_height_mm(mm, bold=False):
    """Return a PIL font whose *cap height* is the requested millimetres.

    Rule 7 measures the height of the numerals/letters themselves, not the
    font's nominal point size, so we solve for the size that renders a
    capital 'M' at the target height.
    """
    target_px = mm * PX_PER_MM
    path = BOLD if bold else REGULAR
    size = max(int(target_px), 4)
    for _ in range(60):
        f = ImageFont.truetype(path, size)
        box = f.getbbox("M")
        cap = box[3] - box[1]
        if cap == 0:
            break
        err = target_px - cap
        if abs(err) <= 0.6:
            return f
        size += 1 if err > 0 else -1
        size = max(size, 4)
    return ImageFont.truetype(path, size)


def draw_ean13(img, code13, x_mm, y_mm, height_mm=18.0, magnification=1.0,
               with_digits=True):
    """Draw a real EAN-13 symbol at a true GS1 magnification."""
    bits = ean13_bits(code13)
    module_mm = 0.330 * magnification
    module_px = module_mm * PX_PER_MM
    x0 = x_mm * PX_PER_MM
    y0 = y_mm * PX_PER_MM
    h = height_mm * PX_PER_MM
    d = ImageDraw.Draw(img)

    # Quiet zone (11 modules left, 7 right) kept white.
    quiet_l = 11 * module_px
    d.rectangle(
        [x0 - quiet_l, y0 - 2, x0 + 95 * module_px + 7 * module_px,
         y0 + h + (26 if with_digits else 2)],
        fill="white",
    )

    guard_extra = 5 * module_px            # guard bars run longer
    for i, bit in enumerate(bits):
        if bit == "1":
            long_bar = i < 3 or i >= 92 or 45 <= i < 50
            bx = x0 + i * module_px
            d.rectangle(
                [bx, y0, bx + module_px - 0.05,
                 y0 + h + (guard_extra if long_bar else 0)],
                fill="black",
            )

    if with_digits:
        f = ImageFont.truetype(REGULAR, max(int(2.2 * PX_PER_MM), 8))
        d.text((x0 - quiet_l + 2, y0 + h + guard_extra - 4), code13[0],
               font=f, fill="black")
        d.text((x0 + 4 * module_px, y0 + h + guard_extra - 4), code13[1:7],
               font=f, fill="black")
        d.text((x0 + 51 * module_px, y0 + h + guard_extra - 4), code13[7:],
               font=f, fill="black")


def add_glare(img, spots):
    """Overlay soft blown-out highlights, the way foil/plastic reflects."""
    glare = Image.new("L", img.size, 0)
    gd = ImageDraw.Draw(glare)
    for (cx_mm, cy_mm, rx_mm, ry_mm, strength) in spots:
        cx, cy = cx_mm * PX_PER_MM, cy_mm * PX_PER_MM
        rx, ry = rx_mm * PX_PER_MM, ry_mm * PX_PER_MM
        gd.ellipse([cx - rx, cy - ry, cx + rx, cy + ry], fill=int(255 * strength))
    glare = glare.filter(ImageFilter.GaussianBlur(radius=18))
    white = Image.new("RGB", img.size, "white")
    return Image.composite(white, img, glare)


def add_camera_noise(img, blur=0.4, noise=3, seed=0):
    """A little blur and sensor noise so it is not a pristine render."""
    random.seed(seed)
    img = img.filter(ImageFilter.GaussianBlur(radius=blur))
    px = img.load()
    w, h = img.size
    for _ in range((w * h) // 40):
        x, y = random.randrange(w), random.randrange(h)
        r, g, b = px[x, y]
        n = random.randint(-noise * 6, noise * 6)
        px[x, y] = (max(0, min(255, r + n)), max(0, min(255, g + n)),
                    max(0, min(255, b + n)))
    return img


# --------------------------------------------------------------------------

def render_label(spec):
    """Render one label from a declarative spec dict."""
    w_mm, h_mm = spec["size_mm"]
    W, H = int(w_mm * PX_PER_MM), int(h_mm * PX_PER_MM)
    img = Image.new("RGB", (W, H), spec.get("bg", "#f4f1e8"))
    d = ImageDraw.Draw(img)

    # Header band
    d.rectangle([0, 0, W, int(26 * PX_PER_MM)], fill=spec.get("brand_bg", "#1d4e5f"))

    title_font = font_for_cap_height_mm(spec.get("title_mm", 9), bold=True)
    d.text((6 * PX_PER_MM, 6 * PX_PER_MM), spec["brand"],
           font=title_font, fill="white")
    sub_font = font_for_cap_height_mm(3.0)
    d.text((6 * PX_PER_MM, 18 * PX_PER_MM), spec["tagline"],
           font=sub_font, fill="#cfe3ea")

    y = 31.0
    name_font = font_for_cap_height_mm(spec.get("commodity_mm", 5.5), bold=True)
    d.text((6 * PX_PER_MM, y * PX_PER_MM), spec["commodity"],
           font=name_font, fill="#111111")
    y += spec.get("commodity_mm", 5.5) + 6

    for line, mm, bold in spec["declarations"]:
        f = font_for_cap_height_mm(mm, bold=bold)
        d.text((6 * PX_PER_MM, y * PX_PER_MM), line, font=f, fill="#111111")
        y += mm + 3.2

    if spec.get("barcode"):
        code = ean13_add_check_digit(spec["barcode"][:12])
        bx = spec.get("barcode_xy", (10, h_mm - 30))
        draw_ean13(img, code, bx[0], bx[1],
                   height_mm=spec.get("barcode_h_mm", 16),
                   magnification=spec.get("barcode_mag", 1.0))
        spec["_full_code"] = code

    if spec.get("glare"):
        img = add_glare(img, spec["glare"])
    img = add_camera_noise(img, blur=spec.get("blur", 0.4),
                           seed=spec.get("seed", 1))
    return img


COMPLIANT = dict(
    size_mm=(164, 152), brand="SURYA FOODS", brand_bg="#1d4e5f",
    tagline="Pure . Packed . Trusted", title_mm=8.0,
    commodity="Roasted Chana Dal", commodity_mm=6.0,
    barcode="890123456789", barcode_xy=(12, 112), barcode_h_mm=16,
    declarations=[
        ("Net Quantity: 1 kg", 4.4, True),
        ("MRP Rs. 185.00 (inclusive of all taxes)", 4.4, True),
        ("Mfd. by: Surya Foods Pvt. Ltd.,", 2.4, False),
        ("Plot 14, MIDC Industrial Area, Pune 411019", 2.4, False),
        ("Month & Year of Manufacture: 03/2026", 2.4, False),
        ("Consumer Care: care@suryafoods.co.in", 2.4, False),
        ("Toll Free 1800 200 1199", 2.4, False),
        ("Country of Origin: India", 2.4, False),
        ("Unit Sale Price: Rs. 18.50 per 100 g", 2.4, False),
    ],
    seed=11,
)

NONCOMPLIANT = dict(
    size_mm=(164, 152), brand="GLOWPACK SNACKS", brand_bg="#7a2230",
    tagline="Crunch Everyday", bg="#fdf6ec", title_mm=7.5,
    commodity="Masala Potato Wafers", commodity_mm=6.0,
    barcode="890998877665", barcode_xy=(12, 112), barcode_h_mm=16,
    declarations=[
        # net quantity + MRP both printed far below the Rule 7 minimum for
        # a pack of this size, and the MRP carries no tax qualifier
        ("Net Quantity: 750 g", 1.6, False),
        ("MRP Rs. 99.00", 1.7, False),
        ("Mfd. by: Glowpack Snacks LLP, Unit 7, Surat 395003", 2.3, False),
        ("Mfg: 01/2026", 2.3, False),
        # no consumer care, no country of origin, no unit sale price
    ],
    seed=23,
)

IMPORTED = dict(
    size_mm=(164, 146), brand="ALPINE BREW", brand_bg="#2b3a55",
    tagline="Imported Instant Coffee", bg="#f6f3ee", title_mm=8.0,
    commodity="Instant Coffee Granules", commodity_mm=5.6,
    barcode="690112233445", barcode_xy=(12, 106), barcode_h_mm=16,
    declarations=[
        ("Net Quantity: 200 g", 3.0, True),
        ("MRP Rs. 549.00 (inclusive of all taxes)", 3.0, True),
        ("Imported & Marketed by: Alpine Trading Co.,", 2.3, False),
        ("22 Harbour Road, Mumbai 400001", 2.3, False),
        ("Month & Year of Import: 02/2026", 2.3, False),
        ("Consumer Care: help@alpinebrew.in", 2.3, False),
        # country of origin deliberately absent on an imported good
    ],
    seed=31,
)


def build_glare_series():
    """Three frames of the same label, glare moving as the phone tilts."""
    base = dict(COMPLIANT)
    base["brand"] = "SURYA FOODS"
    frames = []
    glare_sets = [
        [(45, 48, 40, 11, 0.97), (115, 92, 26, 10, 0.8)],
        [(50, 64, 38, 10, 0.97), (30, 88, 28, 9, 0.85)],
        [(95, 44, 34, 12, 0.92), (70, 78, 40, 10, 0.9)],
    ]
    for i, spots in enumerate(glare_sets, 1):
        spec = dict(base)
        spec["glare"] = spots
        spec["seed"] = 40 + i
        spec["blur"] = 0.6
        frames.append((f"glare_frame_{i}.png", render_label(spec)))
    return frames


BATCH_SPECS = [
    ("batch_01_compliant.png", COMPLIANT),
    ("batch_02_font_violation.png", NONCOMPLIANT),
    ("batch_03_imported.png", IMPORTED),
    ("batch_04_missing_mrp.png", dict(
        size_mm=(160, 140), brand="VEDIC HERBALS", brand_bg="#33562f",
        tagline="Ayurvedic Wellness", bg="#f2f6ee", title_mm=7.5,
        commodity="Triphala Churna", commodity_mm=5.6,
        barcode="890445566778", barcode_xy=(12, 100), barcode_h_mm=16,
        declarations=[
            ("Net Quantity: 100 g", 2.6, True),
            ("Mfd. by: Vedic Herbals, Haridwar 249401", 2.2, False),
            ("Mfg: 12/2025", 2.2, False),
            ("Consumer Care: 1800 300 4455", 2.2, False),
            ("Country of Origin: India", 2.2, False),
        ],
        seed=57)),
    ("batch_05_compliant_small.png", dict(
        size_mm=(158, 146), brand="NILGIRI TEA CO.", brand_bg="#4a3423",
        tagline="Estate Fresh", bg="#f7f1e5", title_mm=7.0,
        commodity="Black Tea Leaves", commodity_mm=5.4,
        barcode="890667788990", barcode_xy=(12, 106), barcode_h_mm=16,
        declarations=[
            ("Net Quantity: 250 g", 3.0, True),
            ("MRP Rs. 240.00 (inclusive of all taxes)", 3.0, True),
            ("Packed by: Nilgiri Tea Co., Coonoor 643101", 2.2, False),
            ("Month & Year of Packing: 02/2026", 2.2, False),
            ("Consumer Care: care@nilgiritea.in, 1800 123 7788", 2.2, False),
            ("Country of Origin: India", 2.2, False),
            ("Unit Sale Price: Rs. 96.00 per 100 g", 2.2, False),
        ],
        seed=63)),
]


def main():
    os.makedirs(OUT, exist_ok=True)
    os.makedirs(BATCH, exist_ok=True)

    singles = [
        ("label_compliant.png", COMPLIANT),
        ("label_noncompliant.png", NONCOMPLIANT),
        ("label_imported.png", IMPORTED),
    ]
    for name, spec in singles:
        img = render_label(dict(spec))
        img.save(os.path.join(OUT, name), quality=95)
        print("wrote", name, img.size)

    for name, img in build_glare_series():
        img.save(os.path.join(OUT, name), quality=95)
        print("wrote", name, img.size)

    for name, spec in BATCH_SPECS:
        img = render_label(dict(spec))
        img.save(os.path.join(BATCH, name), quality=95)
        print("wrote batch/", name, img.size)

    with open(os.path.join(OUT, "GROUND_TRUTH.txt"), "w") as f:
        f.write(
            "Demo labels are rendered at %.1f pixels per millimetre.\n"
            "EAN-13 symbols are drawn at GS1 nominal magnification (1.0),\n"
            "so the true symbol width is 31.35 mm -> %.1f px.\n"
            "The calibration step should therefore recover ~%.1f px/mm.\n"
            % (PX_PER_MM, 31.35 * PX_PER_MM, PX_PER_MM)
        )
    print("done")


if __name__ == "__main__":
    main()
