"""
The compliance rule engine.
===========================

Takes the lines OCR'd off a label - each optionally carrying a measured
glyph height in millimetres - and decides which declarations under the
LM(PC) Rules, 2011 are present, which are missing, and which are present
but defective.

Two design choices are worth stating up front.

*Nothing about the law is hardcoded here.*  Keywords, regexes, citations,
severities, the Rule 7 height table and the exemptions all live in
``rules.json``.  When the Rules are amended, the JSON changes and this file
does not.  That matters because the Rules genuinely do change - the 2017
amendment and the 2022 QR-code amendment both moved the goalposts.

*Every finding carries its citation.*  A verdict of "non-compliant" is
useless to an inspector; "Rule 18(2) - MRP declared without the 'inclusive
of all taxes' qualifier" is something that can go into a notice.
"""

from __future__ import annotations

import datetime
import json
import os
import re

RULES_PATH = os.path.join(os.path.dirname(__file__), "rules.json")

with open(RULES_PATH, "r", encoding="utf-8") as fh:
    RULES = json.load(fh)

FIELDS = {f["field"]: f for f in RULES["mandatory_fields"]}
CROSS = {c["rule_id"]: c for c in RULES["cross_field_rules"]}

IMPORTER_PATTERN = re.compile(
    r"\bimported\s*(&|and)?\s*(marketed|packed|distributed)?\s*by\b|\bimporter\b",
    re.IGNORECASE)

MONTH_NAMES = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}

# Phrases that satisfy Rule 18(2)'s "inclusive of all taxes" qualifier, with
# an edit-distance tolerance.  Phone-camera OCR mangles punctuation freely -
# '(Incl. of all taxes)' routinely comes back as '(Incl: of all taxes)', and
# 'inclusive' often loses a letter - so a strict keyword match produces a
# false MAJOR violation from a single misread glyph.  The pattern is checked
# first (cheap, exact); only if it fails does this tolerant fallback run:
# an incl- word (distance 1 - allowing distance 2 would also accept
# 'exclusive', which is the opposite declaration) followed by a consecutive
# 'of all taxes' / 'all tax' style phrase, each word within distance 1.
_INCL_TARGETS = (("inclusive", 1), ("incl", 1))
_TAX_PHRASES = (
    (("of", 1), ("all", 1), ("taxes", 1)),
    (("of", 1), ("all", 1), ("tax", 1)),
    (("all", 1), ("taxes", 1)),
    (("all", 1), ("tax", 1)),
)


def _word_within(word, target, max_errors):
    """True if ``word`` equals ``target`` within ``max_errors`` edits."""
    if abs(len(word) - len(target)) > max_errors:
        return False
    prev = list(range(len(target) + 1))
    for i, cw in enumerate(word, 1):
        cur = [i]
        for j, ct in enumerate(target, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1,
                           prev[j - 1] + (cw != ct)))
        prev = cur
    return prev[-1] <= max_errors


_COMMODITY_REJECT = tuple(
    bad.lower() for bad in RULES.get("commodity_name_reject", []))


def _tax_qualifier_found(low_all):
    """Rule 18(2) qualifier detection: exact regex, then tolerant fallback."""
    if re.search(FIELDS["MRP"]["inclusive_tax_regex"], low_all):
        return True
    words = re.findall(r"[a-z]+", low_all)
    if not any(_word_within(w, t, e) for w in words for t, e in _INCL_TARGETS):
        return False
    for i in range(len(words)):
        for phrase in _TAX_PHRASES:
            if all(i + j < len(words)
                   and _word_within(words[i + j], target, max_errors)
                   for j, (target, max_errors) in enumerate(phrase)):
                return True
    return False


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _match_strength(line_text, field):
    """0 = no match, 1 = pattern-only match, 2 = explicit keyword match.

    The distinction matters.  The net-quantity regex happily matches
    "Rs. 18.50 per 100 g" on the unit-price line, so a line that actually
    says "Net Quantity:" must outrank one that merely contains a number and
    a unit - otherwise the engine reads the wrong pack size and applies the
    wrong Rule 7 height band.
    """
    low = line_text.lower()
    for kw in field.get("keywords", []):
        if kw in low:
            return 2
    rx = field.get("regex")
    if rx and re.search(rx, low, re.IGNORECASE):
        return 1
    return 0


def parse_net_quantity(text):
    """Return net quantity normalised to grams / millilitres, or None."""
    m = re.search(
        r"(\d+(?:\.\d+)?)\s*(kg|kilogram|kilograms|g|gm|gms|gram|grams|"
        r"l|ltr|litre|liter|litres|liters|ml|millilitre|milliliter)\b",
        text.lower())
    if not m:
        return None
    value, unit = float(m.group(1)), m.group(2)
    if unit.startswith(("kg", "kilogram")) or unit in ("l", "ltr", "litre",
                                                       "liter", "litres",
                                                       "liters"):
        value *= 1000.0
    return value


def parse_mrp(text):
    m = re.search(r"(?:rs\.?|inr|₹)\s*(\d+(?:\.\d{1,2})?)", text.lower())
    if not m:
        m = re.search(r"(?:mrp|m\.?r\.?p\.?)[^0-9]{0,20}(\d+(?:\.\d{1,2})?)",
                      text.lower())
    return float(m.group(1)) if m else None


def parse_mfg_date(text):
    """Return (year, month) from a declaration, or None."""
    low = text.lower()
    m = re.search(r"\b(0[1-9]|1[0-2])[\/\-\.](19|20)(\d{2})\b", low)
    if m:
        return int(m.group(2) + m.group(3)), int(m.group(1))
    m = re.search(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)"
                  r"[a-z]*\.?\s*,?\s*(19|20)(\d{2})\b", low)
    if m:
        return int(m.group(2) + m.group(3)), MONTH_NAMES[m.group(1)]
    return None


def required_height_mm(net_qty_g_ml, embossed=False):
    """Look up the Rule 7 minimum height for this pack size."""
    table = RULES["font_height_table_mm"]["by_net_quantity"]
    if net_qty_g_ml is None:
        # Unknown pack size: apply the most lenient band so the engine never
        # invents a violation it cannot substantiate.
        row = table[0]
        return (row["embossed_mm"] if embossed else row["normal_mm"]), \
            row["band"] + " (assumed - net quantity not read)"
    for row in table:
        if row["max_qty_g_ml"] is None or net_qty_g_ml <= row["max_qty_g_ml"]:
            return (row["embossed_mm"] if embossed else row["normal_mm"]), \
                row["band"]
    return RULES["font_height_table_mm"]["general_minimum_mm"], "general"


# --------------------------------------------------------------------------
# Main evaluation
# --------------------------------------------------------------------------

def evaluate(lines, calibration=None, capture_quality=None,
             category=None, inspection_date=None, embossed=False):
    """Evaluate one label.

    ``lines``       - list of dicts with at least ``text``; optionally
                      ``height_mm`` (measured glyph height) and ``conf``.
    ``calibration`` - the dict returned by barcode.detect_and_calibrate().
    ``capture_quality`` - dict from ocr_engine.assess_capture().

    Returns a structured result including field-by-field findings, a list
    of violations with citations, applied exemptions, and a verdict.
    """
    inspection_date = inspection_date or datetime.date.today()
    all_text = "\n".join(l["text"] for l in lines)
    low_all = all_text.lower()
    is_imported = bool(IMPORTER_PATTERN.search(all_text))

    # ---------------------------------------------------------- fields ----
    field_results = {}
    for key, field in FIELDS.items():
        matched = []
        for line in lines:
            strength = _match_strength(line["text"], field)
            if strength:
                matched.append((strength, line))

        if key == "COMMODITY_NAME" and not matched and lines:
            # A generic name rarely carries a "label:" prefix; on a real pack
            # it is simply the most prominent text. Take the tallest line
            # that isn't the brand banner as a best-effort match - but never
            # a nutrition-table header or other boilerplate, which is often
            # the tallest text on a back panel and once produced "Energy"
            # as a product name. The reject list is data in rules.json.
            ranked = sorted(lines, key=lambda l: l.get("h", 0), reverse=True)
            for cand in ranked[:5]:
                low_c = cand["text"].lower()
                if any(bad in low_c for bad in _COMMODITY_REJECT):
                    continue
                if len(cand["text"]) >= 4 and not cand["text"].isupper():
                    matched = [(1, cand)]
                    break

        best = None
        if matched:
            # Keyword matches first, then the most confident reading.
            best = sorted(matched,
                          key=lambda sl: (sl[0], sl[1].get("conf", 0)),
                          reverse=True)[0][1]

        field_results[key] = {
            "label": field["label"],
            "citation": field["citation"],
            "severity": field["severity"],
            "plain": field.get("plain", ""),
            "found": bool(matched),
            "matched_text": best["text"] if best else None,
            "measured_height_mm": best.get("height_mm") if best else None,
            "height_token": best.get("height_token") if best else None,
            "ocr_confidence": round(best.get("conf", 0), 1) if best else None,
            "conditional": field.get("conditional"),
        }

    net_qty = None
    if field_results["NET_QUANTITY"]["found"]:
        net_qty = parse_net_quantity(field_results["NET_QUANTITY"]["matched_text"])
    if net_qty is None:
        net_qty = parse_net_quantity(all_text)

    mrp_value = parse_mrp(all_text)
    mfg = parse_mfg_date(all_text)

    # ------------------------------------------------------ exemptions ----
    exemptions = []
    for ex in RULES["exemptions"]:
        trigger = ex["trigger"]
        hit = (
            (trigger == "net_qty_below_10" and net_qty is not None and net_qty < 10)
            or (trigger == "net_qty_above_25kg" and net_qty is not None and net_qty > 25000)
            or (trigger == "category_electronics" and (category or "").lower()
                in ("electronics", "electrical", "appliance"))
        )
        if hit:
            exemptions.append(ex)
    exempt_ids = {e["rule_id"] for e in exemptions}
    broadly_exempt = bool(exempt_ids & {"PACK_TOO_SMALL", "WHOLESALE_LARGE_PACK"})

    violations = []

    def add(rule_id, field, description, citation, severity, detail=None):
        violations.append({
            "rule_id": rule_id, "field": field, "description": description,
            "citation": citation, "severity": severity, "detail": detail,
        })

    # ------------------------------------------- 1. missing declarations --
    if not broadly_exempt:
        for key, res in field_results.items():
            if res["found"]:
                continue
            if res["conditional"] == "imported_only" and not is_imported:
                res["skipped_reason"] = "Not an imported pack - not required."
                continue
            if key == "UNIT_SALE_PRICE" and (net_qty is None or net_qty <= 200):
                res["skipped_reason"] = "Small pack - unit sale price not required."
                continue
            if key == "MANUFACTURER_NAME_ADDRESS" and "ELECTRONICS_QR" in exempt_ids:
                res["skipped_reason"] = "May be declared via QR code (2022 amendment)."
                continue
            note = ""
            if key == "COUNTRY_OF_ORIGIN" and is_imported:
                note = (" The pack names an importer, so the country of "
                        "origin is mandatory.")
            add(f"MISSING_{key}", key,
                f"Mandatory declaration not found on the pack: "
                f"{res['label']}.{note}",
                res["citation"], res["severity"])

    # ------------------------------------------------ 2. MRP formatting --
    if field_results["MRP"]["found"] and not broadly_exempt:
        if not _tax_qualifier_found(low_all):
            rule = CROSS["MRP_MISSING_INCLUSIVE_TAX_WORDING"]
            add(rule["rule_id"], "MRP", rule["description"],
                rule["citation"], rule["severity"])

    # ------------------------------------------- 3. Rule 7 font heights --
    tolerance = RULES["font_height_table_mm"]["measurement_tolerance_mm"]
    minimum, band = required_height_mm(net_qty, embossed)
    for key in ("MRP", "NET_QUANTITY"):
        res = field_results[key]
        measured = res["measured_height_mm"]
        if not res["found"] or measured is None:
            continue
        if measured < minimum - tolerance:
            add("FONT_TOO_SMALL_" + key, key,
                f"{res['label']} is printed at {measured:.2f} mm; "
                f"Rule 7(3) requires at least {minimum:.1f} mm for a pack "
                f"in the '{band}' band.",
                CROSS["FONT_TOO_SMALL"]["citation"], "major",
                detail={"measured_mm": measured, "required_mm": minimum,
                        "band": band, "token": res.get("height_token")})

    # ---------------------------------- 5. barcode origin cross-check ----
    origin_note = None
    if calibration and calibration.get("ok"):
        gs1_country = calibration.get("country", "")
        declared_india = bool(re.search(r"origin[^a-z]{0,6}india|made in india",
                                        low_all))
        declared_other = re.search(r"(?:country of origin|made in|product of)"
                                   r"[^a-z]{0,6}([a-z ]{3,20})", low_all)
        if declared_india and gs1_country not in ("India", "Unknown"):
            rule = CROSS["ORIGIN_PREFIX_MISMATCH"]
            add(rule["rule_id"], "COUNTRY_OF_ORIGIN",
                f"Pack declares Indian origin but the barcode's GS1 prefix "
                f"({calibration['code'][:3]}) was issued to {gs1_country}. "
                + rule["description"].split(". ", 1)[1],
                rule["citation"], "advisory")
        elif declared_other and not declared_india and gs1_country == "India":
            origin_note = ("Barcode prefix is Indian while the pack declares a "
                           "foreign origin - consistent with an Indian packer "
                           "of imported goods.")

    # ------------------------------------------- 6. impossible mfg date --
    if mfg:
        year, month = mfg
        if (year, month) > (inspection_date.year, inspection_date.month):
            rule = CROSS["MFG_DATE_IN_FUTURE"]
            add(rule["rule_id"], "MFG_DATE",
                f"Declared month/year of manufacture ({month:02d}/{year}) is "
                f"after the inspection date.",
                rule["citation"], rule["severity"])

    # --------------------------------------- 7. confidence-gated capture --
    gate = capture_gate(lines, calibration, capture_quality)
    if gate["retake_recommended"]:
        rule = CROSS["LOW_CONFIDENCE_CAPTURE"]
        add(rule["rule_id"], "CAPTURE",
            "Capture quality below the reliability threshold: "
            + "; ".join(gate["reasons"]) + ". "
            "Findings below are provisional until a better capture is taken.",
            rule["citation"], "advisory")

    majors = [v for v in violations if v["severity"] == "major"]
    minors = [v for v in violations if v["severity"] == "minor"]
    advisories = [v for v in violations if v["severity"] == "advisory"]

    if gate["retake_recommended"]:
        verdict = "INCONCLUSIVE"
    elif majors:
        verdict = "NON_COMPLIANT"
    elif minors:
        verdict = "NON_COMPLIANT_MINOR"
    else:
        verdict = "COMPLIANT"

    return {
        "verdict": verdict,
        "field_results": field_results,
        "violations": violations,
        "exemptions": exemptions,
        "gate": gate,
        "context": {
            "net_quantity_g_ml": net_qty,
            "mrp_value": mrp_value,
            "mfg_month_year": f"{mfg[1]:02d}/{mfg[0]}" if mfg else None,
            "is_imported": is_imported,
            "required_height_mm": minimum,
            "height_band": band,
            "origin_note": origin_note,
            "category": category,
        },
        "summary": {
            "major": len(majors),
            "minor": len(minors),
            "advisory": len(advisories),
            "total": len(violations),
            "fields_checked": len(field_results),
            "fields_found": sum(1 for r in field_results.values() if r["found"]),
            "is_compliant": verdict == "COMPLIANT",
        },
        "rule_set_version": RULES["rule_set_version"],
        "rule_source": RULES["source"],
    }


def capture_gate(lines, calibration=None, capture_quality=None):
    """Decide whether this capture is good enough to give a verdict on.

    A wrong "compliant" is worse than asking the inspector to take the photo
    again, so the engine refuses to commit when the evidence is weak.  Each
    signal contributes to a single 0-1 score and any hard failure triggers a
    retake regardless of the score.
    """
    reasons, signals = [], {}

    if lines:
        confs = [l.get("conf", 0) for l in lines]
        mean_conf = sum(confs) / len(confs)
    else:
        mean_conf = 0.0
    signals["ocr_confidence"] = round(mean_conf, 1)
    if mean_conf < 60:
        reasons.append(f"mean OCR confidence only {mean_conf:.0f}%")

    if len(lines) < 3:
        reasons.append(f"only {len(lines)} text lines detected")
    signals["lines_detected"] = len(lines)

    if capture_quality:
        signals.update(capture_quality)
        if capture_quality.get("sharpness", 1) < 0.25:
            reasons.append("image is out of focus")
        if capture_quality.get("glare_pct", 0) > 18:
            reasons.append(
                f"{capture_quality['glare_pct']:.0f}% of the frame lost to glare")
        if capture_quality.get("exposure", 1) < 0.45:
            reasons.append("exposure is clipped")

    cal_conf = calibration.get("confidence", 0.0) if calibration and \
        calibration.get("ok") else 0.0
    signals["calibration_confidence"] = cal_conf

    score = (
        0.45 * min(mean_conf / 85.0, 1.0)
        + 0.20 * min(len(lines) / 8.0, 1.0)
        + 0.20 * (capture_quality.get("sharpness", 0.6) if capture_quality else 0.6)
        + 0.15 * (1.0 - min((capture_quality.get("glare_pct", 0) if capture_quality
                             else 0) / 40.0, 1.0))
    )
    return {
        "score": round(score, 3),
        "signals": signals,
        "reasons": reasons,
        "retake_recommended": bool(reasons) or score < 0.55,
        "calibration_available": bool(calibration and calibration.get("ok")),
    }
