"""
E-commerce listing compliance.
==============================

Rule 6(10), inserted by the 2017 amendment, extends the mandatory
declarations to online listings: a marketplace must display the same
information on the product page that the law requires on the pack.  The
CCPA has issued enforcement notices to platforms over exactly this.

Two practical wrinkles this module handles.

*Declarations are often pictures.*  Sellers frequently upload the "product
information" block as a JPEG rather than as page text, which defeats a
plain HTML scraper.  So a listing snapshot can carry image assets, and
those get pushed through the same OCR pipeline as a physical label before
the rule engine sees them.

*Scraping belongs on the server, on a schedule.*  Hitting marketplaces
live from a handset would be slow, rate-limited and legally awkward.  The
architecture is: a backend job fetches and caches listing snapshots on a
schedule, and the checker runs against the cache.  What ships here is the
cache and the checker; the fetcher is a scheduled job stub, so the whole
thing runs with no network at all.  A pasted listing can also be checked
live, which is what an official reviewing a specific complaint would do.
"""

from __future__ import annotations

import datetime
import json
import os
import threading
import time

from . import db
from .rule_engine import FIELDS, RULES, _match_strength

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORPUS_PATH = os.path.join(BASE_DIR, "data", "listings.json")
LISTING_IMAGE_DIR = os.path.join(BASE_DIR, "static", "samples", "listings")

REQUIRED = RULES["ecommerce_required_fields"]
CITATION = RULES["ecommerce_citation"]


def load_corpus():
    if not os.path.exists(CORPUS_PATH):
        return []
    with open(CORPUS_PATH, "r", encoding="utf-8") as fh:
        return json.load(fh)["listings"]


def check_text(blob, is_imported_hint=None):
    """Run the mandatory-declaration check over a block of listing text."""
    lines = [{"text": part.strip()} for part in blob.splitlines()
             if part.strip()]
    low = blob.lower()
    imported = is_imported_hint
    if imported is None:
        imported = ("imported by" in low or "importer" in low
                    or "country of origin" in low)

    found, missing = {}, []
    for key in REQUIRED:
        field = FIELDS[key]
        hit = None
        for line in lines:
            if _match_strength(line["text"], field):
                hit = line["text"]
                break
        if key == "COMMODITY_NAME" and hit is None and lines:
            hit = lines[0]["text"]          # the listing title is the name
        found[key] = hit
        if hit is None:
            missing.append({
                "field": key,
                "label": field["label"],
                "citation": field["citation"],
                "plain": field.get("plain", ""),
            })

    return {
        "found": found,
        "missing": missing,
        "verdict": "COMPLIANT" if not missing else "NON_COMPLIANT",
        "citation": CITATION,
        "checked_at": db.now(),
    }


_OCR_CACHE_PATH = os.path.join(BASE_DIR, "data", "listing_ocr_cache.json")


def _load_ocr_cache():
    try:
        with open(_OCR_CACHE_PATH, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _save_ocr_cache(cache):
    try:
        with open(_OCR_CACHE_PATH, "w", encoding="utf-8") as fh:
            json.dump(cache, fh, ensure_ascii=False, indent=1)
    except OSError:
        pass


def _ocr_assets(assets):
    """OCR any declaration images attached to a listing snapshot.

    Snapshots in the cache rarely change between sweeps, so the OCR result
    is memoised per asset, keyed by the file's size and modification time.
    A re-run of the sweep - which is exactly what a demo or a manual check
    does - then costs a JSON read instead of a Tesseract run per image.
    """
    if not assets:
        return "", []
    from . import ocr_engine as ocr
    available, _ = ocr.ensure_tesseract()
    if not available:
        return "", []

    cache = _load_ocr_cache()
    text_parts, used, dirty = [], [], False
    for asset in assets:
        path = os.path.join(LISTING_IMAGE_DIR, asset)
        if not os.path.exists(path):
            continue
        try:
            stat = os.stat(path)
            key = f"{asset}:{stat.st_size}:{int(stat.st_mtime)}"
            cached = cache.get(key)
            if cached is not None:
                text_parts.append(cached)
                used.append(asset)
                continue
            bgr = ocr.load_bgr(path)
            _, sharp = ocr.prepare_for_ocr(bgr)
            lines = ocr.group_lines(ocr.ocr_words(sharp))
            text = "\n".join(l["text"] for l in lines)
            cache[key] = text
            dirty = True
            text_parts.append(text)
            used.append(asset)
        except Exception:
            continue
    if dirty:
        _save_ocr_cache(cache)
    return "\n".join(text_parts), used


def check_listing(listing, use_ocr=True):
    """Check one cached listing snapshot, OCR'ing image assets if present."""
    blob = "\n".join([listing.get("title", "")] + listing.get("page_text", []))
    ocr_text, ocr_assets_used = "", []
    if use_ocr and listing.get("declaration_images"):
        ocr_text, ocr_assets_used = _ocr_assets(listing["declaration_images"])
        if ocr_text:
            blob += "\n" + ocr_text

    result = check_text(blob)
    result.update({
        "platform": listing["platform"],
        "title": listing["title"],
        "url": listing.get("url"),
        "seller": listing.get("seller"),
        "snapshot_date": listing.get("snapshot_date"),
        "source_kind": ("page text + OCR of declaration image"
                        if ocr_assets_used else "page text only"),
        "ocr_assets": ocr_assets_used,
        "ocr_text": ocr_text,
        "listing_id": listing.get("id"),
    })
    return result


def run_scheduled_sweep(use_ocr=True):
    """The job a backend scheduler would run: check every cached listing."""
    results = [check_listing(l, use_ocr=use_ocr) for l in load_corpus()]
    with db.connect() as conn:
        for r in results:
            conn.execute(
                """INSERT INTO listing_checks
                   (platform, title, url, seller, verdict, missing_json,
                    source_kind, checked_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (r["platform"], r["title"], r["url"], r["seller"],
                 r["verdict"], json.dumps(r["missing"]), r["source_kind"],
                 db.now()))
        conn.commit()
    db.log_sync("LISTING_SWEEP",
                f"{len(results)} cached listings re-checked")
    return results


def sweep_summary(results):
    non_compliant = [r for r in results if r["verdict"] != "COMPLIANT"]
    by_platform = {}
    for r in results:
        p = by_platform.setdefault(r["platform"], {"total": 0, "bad": 0})
        p["total"] += 1
        if r["verdict"] != "COMPLIANT":
            p["bad"] += 1
    field_tally = {}
    for r in non_compliant:
        for m in r["missing"]:
            field_tally[m["label"]] = field_tally.get(m["label"], 0) + 1
    return {
        "total": len(results),
        "non_compliant": len(non_compliant),
        "rate": round(100 * len(non_compliant) / max(len(results), 1), 1),
        "by_platform": by_platform,
        "field_tally": dict(sorted(field_tally.items(),
                                   key=lambda kv: -kv[1])),
    }


def last_sweep_time():
    with db.connect() as conn:
        row = conn.execute(
            "SELECT MAX(checked_at) t FROM listing_checks").fetchone()
    return row["t"] if row and row["t"] else None


def sweep_interval_hours():
    """How often the scheduled sweep runs. Editable at runtime via
    the settings table so an operator can tighten it without a code
    change; defaults to every 6 hours."""
    raw = db.get_setting("sweep_interval_hours", "")
    try:
        return max(1.0, float(raw)) if raw else 6.0
    except (TypeError, ValueError):
        return 6.0


def next_sweep_due(hours=None):
    if hours is None:
        hours = sweep_interval_hours()
    last = last_sweep_time()
    if not last:
        return "not yet run"
    try:
        dt = datetime.datetime.fromisoformat(last)
    except ValueError:
        return "unknown"
    return (dt + datetime.timedelta(hours=hours)).strftime("%d %b %Y, %H:%M")


# ------------------------------------------------------- scheduler ---

_scheduler = None
_stop = threading.Event()
_CHECK_EVERY = 30          # seconds between due-checks


def _scheduler_loop():
    """Wake up periodically and run the sweep when it is due.  A real
    deployment would use cron or a task queue; this keeps the whole
    system self-contained, exactly like the sync worker."""
    while not _stop.is_set():
        try:
            last = last_sweep_time()
            due = False
            if not last:
                due = True
            else:
                dt = datetime.datetime.fromisoformat(last)
                due = (datetime.datetime.now() - dt) >= datetime.timedelta(
                    hours=sweep_interval_hours())
            if due:
                run_scheduled_sweep()
        except Exception as exc:      # never kill the thread
            try:
                db.log_sync("SWEEP_ERROR", str(exc)[:200])
            except Exception:
                pass
        _stop.wait(_CHECK_EVERY)


def start_scheduler():
    """Start the background sweep scheduler once per process."""
    global _scheduler
    if _scheduler and _scheduler.is_alive():
        return
    _stop.clear()
    _scheduler = threading.Thread(
        target=_scheduler_loop, name="atlas-sweep", daemon=True)
    _scheduler.start()


def stop_scheduler():
    _stop.set()
