"""
Offline-first queue and background sync.
========================================

The field reality this models: a Legal Metrology inspector works in
godowns, basements and rural markets where mobile data is unreliable.  If
the app needs a network round-trip to give a verdict, it is useless exactly
where it is most needed.

So every scan is decided entirely on-device and written to a local queue
with ``sync_state = 'pending'``.  A background thread wakes every few
seconds, and when the device is online it pushes pending rows into the
central database and marks them ``synced``.  Nothing about the inspector's
workflow changes either way - the only difference is the colour of a chip
in the header.

``central.db`` stands in for the departmental server.  The transfer is a
local database write rather than an HTTP call, which keeps the demo
self-contained and offline-capable; swapping ``_push_one`` for a POST to a
real endpoint is the only change a deployment would need.
"""

from __future__ import annotations

import json
import threading
import time

from . import db

_worker = None
_stop = threading.Event()
_lock = threading.Lock()

POLL_SECONDS = 4


def pending_counts():
    with db.connect() as conn:
        scans = conn.execute(
            "SELECT COUNT(*) c FROM scans WHERE sync_state = 'pending'"
        ).fetchone()["c"]
        complaints = conn.execute(
            "SELECT COUNT(*) c FROM complaints WHERE sync_state = 'pending'"
        ).fetchone()["c"]
        synced = conn.execute(
            "SELECT COUNT(*) c FROM scans WHERE sync_state = 'synced'"
        ).fetchone()["c"]
    return {"scans": scans, "complaints": complaints, "synced": synced}


def _push_scan(conn_device, conn_central, row, device_id):
    inspector = None
    if row["user_id"]:
        user = db.user_by_id(row["user_id"])
        inspector = user["display_name"] if user else None
    conn_central.execute(
        """INSERT OR IGNORE INTO scans
           (uid, device_id, inspector, product_name, category, manufacturer,
            location, barcode, verdict, major_count, minor_count,
            result_json, captured_at, received_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (row["uid"], device_id, inspector, row["product_name"],
         row["category"], row["manufacturer"], row["location"],
         row["barcode"], row["verdict"], row["major_count"],
         row["minor_count"], row["result_json"], row["created_at"], db.now()))
    conn_device.execute(
        "UPDATE scans SET sync_state = 'synced', synced_at = ? WHERE id = ?",
        (db.now(), row["id"]))


def _push_complaint(conn_device, conn_central, row):
    conn_central.execute(
        """INSERT OR IGNORE INTO complaints
           (reference, scan_uid, complainant, body, received_at)
           VALUES (?,?,?,?,?)""",
        (row["reference"], row["scan_uid"], row["complainant"],
         row["body"], db.now()))
    conn_device.execute(
        "UPDATE complaints SET sync_state = 'synced' WHERE id = ?",
        (row["id"],))


def flush(force=False):
    """Push everything pending. Returns a short report dict."""
    if not force and not db.is_online():
        return {"ran": False, "reason": "device is offline", "scans": 0,
                "complaints": 0}

    with _lock:
        device_id = db.get_setting("device_id", "UNKNOWN")
        pushed_scans = pushed_complaints = 0
        with db.connect(db.DEVICE_DB) as conn_device, \
                db.connect(db.CENTRAL_DB) as conn_central:
            scans = conn_device.execute(
                "SELECT * FROM scans WHERE sync_state = 'pending' "
                "ORDER BY id LIMIT 100").fetchall()
            for row in scans:
                _push_scan(conn_device, conn_central, row, device_id)
                pushed_scans += 1

            complaints = conn_device.execute(
                """SELECT c.*, s.uid AS scan_uid FROM complaints c
                   JOIN scans s ON s.id = c.scan_id
                   WHERE c.sync_state = 'pending' ORDER BY c.id LIMIT 100"""
            ).fetchall()
            for row in complaints:
                _push_complaint(conn_device, conn_central, row)
                pushed_complaints += 1

            conn_central.commit()
            conn_device.commit()

    if pushed_scans or pushed_complaints:
        db.log_sync(
            "PUSH",
            f"{pushed_scans} scan(s) and {pushed_complaints} complaint(s) "
            f"uploaded to the central server")
    return {"ran": True, "scans": pushed_scans,
            "complaints": pushed_complaints}


def central_stats():
    with db.connect(db.CENTRAL_DB) as conn:
        total = conn.execute("SELECT COUNT(*) c FROM scans").fetchone()["c"]
        complaints = conn.execute(
            "SELECT COUNT(*) c FROM complaints").fetchone()["c"]
        by_verdict = {r["verdict"]: r["c"] for r in conn.execute(
            "SELECT verdict, COUNT(*) c FROM scans GROUP BY verdict")}
        devices = [dict(r) for r in conn.execute(
            "SELECT device_id, COUNT(*) c, MAX(received_at) last "
            "FROM scans GROUP BY device_id")]
        recent = [dict(r) for r in conn.execute(
            "SELECT uid, device_id, inspector, product_name, verdict, "
            "received_at FROM scans ORDER BY id DESC LIMIT 10")]
    return {"total": total, "complaints": complaints,
            "by_verdict": by_verdict, "devices": devices, "recent": recent}


def _loop():
    while not _stop.is_set():
        try:
            if db.is_online():
                flush()
        except Exception as exc:                       # never kill the thread
            try:
                db.log_sync("ERROR", str(exc)[:200])
            except Exception:
                pass
        _stop.wait(POLL_SECONDS)


def start_worker():
    """Start the background sync thread once per process."""
    global _worker
    if _worker and _worker.is_alive():
        return
    _stop.clear()
    _worker = threading.Thread(target=_loop, name="atlas-sync", daemon=True)
    _worker.start()


def stop_worker():
    _stop.set()


def set_online(flag):
    db.set_setting("online", "1" if flag else "0")
    db.log_sync("LINK", "device went online" if flag else "device went offline")
    if flag:
        return flush()
    return {"ran": False, "reason": "offline", "scans": 0, "complaints": 0}
