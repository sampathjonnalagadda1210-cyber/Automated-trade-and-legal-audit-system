"""
Storage.
========

Plain ``sqlite3`` from the standard library, deliberately.  An ORM would
add two pip dependencies and one more thing to go wrong on a laptop that
has never run a Python web app before, and it would buy nothing here: the
schema is nine tables of flat rows.

Two databases exist on purpose.  ``atlas.db`` is the *device* database -
what a field phone would carry, including the outbound sync queue.
``central.db`` stands in for the departmental server the device syncs to.
Keeping them physically separate is what makes the offline-first story
demonstrable rather than merely claimed: you can switch the device
offline, work, and watch the two diverge and then reconcile.
"""

from __future__ import annotations

import datetime
import json
import os
import sqlite3

from werkzeug.security import generate_password_hash

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
DEVICE_DB = os.path.join(DATA_DIR, "atlas.db")
CENTRAL_DB = os.path.join(DATA_DIR, "central.db")

DEVICE_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    display_name  TEXT,
    role          TEXT NOT NULL DEFAULT 'inspector',
    jurisdiction  TEXT,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scans (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    uid              TEXT UNIQUE NOT NULL,
    user_id          INTEGER,
    product_name     TEXT,
    category         TEXT,
    manufacturer     TEXT,
    location         TEXT,
    source           TEXT NOT NULL DEFAULT 'single',
    image_file       TEXT,
    overlay_file     TEXT,
    frame_count      INTEGER DEFAULT 1,
    barcode          TEXT,
    barcode_country  TEXT,
    px_per_mm        REAL,
    calibration_mode TEXT,
    verdict          TEXT,
    major_count      INTEGER DEFAULT 0,
    minor_count      INTEGER DEFAULT 0,
    advisory_count   INTEGER DEFAULT 0,
    quality_score    REAL,
    result_json      TEXT,
    report_file      TEXT,
    created_at       TEXT NOT NULL,
    sync_state       TEXT NOT NULL DEFAULT 'pending',
    synced_at        TEXT
);

CREATE TABLE IF NOT EXISTS violations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_id     INTEGER NOT NULL,
    rule_id     TEXT,
    field       TEXT,
    description TEXT,
    citation    TEXT,
    severity    TEXT,
    FOREIGN KEY (scan_id) REFERENCES scans(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS complaints (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    reference    TEXT UNIQUE NOT NULL,
    scan_id      INTEGER NOT NULL,
    complainant  TEXT,
    contact      TEXT,
    language     TEXT DEFAULT 'en',
    body         TEXT,
    pdf_file     TEXT,
    created_at   TEXT NOT NULL,
    sync_state   TEXT NOT NULL DEFAULT 'pending',
    FOREIGN KEY (scan_id) REFERENCES scans(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS listing_checks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    platform      TEXT,
    title         TEXT,
    url           TEXT,
    seller        TEXT,
    verdict       TEXT,
    missing_json  TEXT,
    source_kind   TEXT,
    checked_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS official_complaints (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    reference     TEXT UNIQUE NOT NULL,
    scan_id       INTEGER NOT NULL,
    officer_name  TEXT,
    designation   TEXT,
    jurisdiction  TEXT,
    addressee     TEXT,
    place_seized  TEXT,
    action_prayed TEXT NOT NULL DEFAULT 'PROSECUTION',
    notes         TEXT,
    pdf_file      TEXT,
    status        TEXT NOT NULL DEFAULT 'DRAFT',
    created_at    TEXT NOT NULL,
    submitted_at  TEXT,
    sync_state    TEXT NOT NULL DEFAULT 'pending',
    FOREIGN KEY (scan_id) REFERENCES scans(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_offcomp_scan ON official_complaints(scan_id);

CREATE TABLE IF NOT EXISTS sync_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    event      TEXT,
    detail     TEXT,
    at         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    action  TEXT,
    entity  TEXT,
    at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE INDEX IF NOT EXISTS idx_scans_created ON scans(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_scans_sync    ON scans(sync_state);
CREATE INDEX IF NOT EXISTS idx_viol_scan     ON violations(scan_id);
"""

CENTRAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS scans (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    uid             TEXT UNIQUE NOT NULL,
    device_id       TEXT,
    inspector       TEXT,
    product_name    TEXT,
    category        TEXT,
    manufacturer    TEXT,
    location        TEXT,
    barcode         TEXT,
    verdict         TEXT,
    major_count     INTEGER,
    minor_count     INTEGER,
    result_json     TEXT,
    captured_at     TEXT,
    received_at     TEXT
);

CREATE TABLE IF NOT EXISTS complaints (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    reference   TEXT UNIQUE NOT NULL,
    scan_uid    TEXT,
    complainant TEXT,
    body        TEXT,
    received_at TEXT
);
"""


def now():
    return datetime.datetime.now().isoformat(timespec="seconds")


def connect(path=DEVICE_DB):
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(path, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init(seed=True):
    """Create both databases and seed demo accounts on first run."""
    os.makedirs(DATA_DIR, exist_ok=True)
    with connect(DEVICE_DB) as conn:
        conn.executescript(DEVICE_SCHEMA)
        conn.commit()
    with connect(CENTRAL_DB) as conn:
        conn.executescript(CENTRAL_SCHEMA)
        conn.commit()
    if seed:
        seed_users()
        set_setting("online", get_setting("online", "1"))
        set_setting("device_id", get_setting("device_id", "LMD-TN-0117"))


def seed_users():
    accounts = [
        ("admin", "admin123", "K. Ramanathan",
         "admin", "State HQ - Tamil Nadu"),
        ("inspector1", "inspector123", "S. Meenakshi",
         "inspector", "Chengalpattu District"),
        ("auditor", "auditor123", "Audit Cell",
         "auditor", "State HQ - Tamil Nadu"),
    ]
    with connect() as conn:
        for username, password, display, role, jurisdiction in accounts:
            exists = conn.execute(
                "SELECT 1 FROM users WHERE username = ?", (username,)).fetchone()
            if exists:
                continue
            conn.execute(
                "INSERT INTO users (username, password_hash, display_name, "
                "role, jurisdiction, created_at) VALUES (?,?,?,?,?,?)",
                (username, generate_password_hash(password), display,
                 role, jurisdiction, now()))
        conn.commit()


# ------------------------------------------------------------- settings ---

def get_setting(key, default=None):
    with connect() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?",
                           (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key, value):
    with connect() as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)))
        conn.commit()


def is_online():
    return get_setting("online", "1") == "1"


# ---------------------------------------------------------------- scans ---

def insert_scan(record, violations):
    """Persist a scan and its violations; returns the new row id."""
    with connect() as conn:
        cur = conn.execute(
            """INSERT INTO scans
               (uid, user_id, product_name, category, manufacturer, location,
                source, image_file, overlay_file, frame_count, barcode,
                barcode_country, px_per_mm, calibration_mode, verdict,
                major_count, minor_count, advisory_count, quality_score,
                result_json, created_at, sync_state)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (record["uid"], record.get("user_id"), record.get("product_name"),
             record.get("category"), record.get("manufacturer"),
             record.get("location"), record.get("source", "single"),
             record.get("image_file"), record.get("overlay_file"),
             record.get("frame_count", 1), record.get("barcode"),
             record.get("barcode_country"), record.get("px_per_mm"),
             record.get("calibration_mode"), record.get("verdict"),
             record.get("major_count", 0), record.get("minor_count", 0),
             record.get("advisory_count", 0), record.get("quality_score"),
             json.dumps(record.get("result", {})), now(), "pending"))
        scan_id = cur.lastrowid
        for v in violations:
            conn.execute(
                "INSERT INTO violations (scan_id, rule_id, field, description,"
                " citation, severity) VALUES (?,?,?,?,?,?)",
                (scan_id, v["rule_id"], v["field"], v["description"],
                 v["citation"], v["severity"]))
        conn.commit()
    return scan_id


def get_scan(scan_id):
    with connect() as conn:
        row = conn.execute("SELECT * FROM scans WHERE id = ?",
                           (scan_id,)).fetchone()
        if not row:
            return None
        scan = dict(row)
        scan["result"] = json.loads(scan["result_json"] or "{}")
        scan["violations"] = [dict(v) for v in conn.execute(
            "SELECT * FROM violations WHERE scan_id = ? ORDER BY "
            "CASE severity WHEN 'major' THEN 0 WHEN 'minor' THEN 1 ELSE 2 END",
            (scan_id,))]
        return scan


def list_scans(search=None, verdict=None, limit=200, offset=0):
    sql = "SELECT * FROM scans WHERE 1=1"
    args = []
    if search:
        sql += (" AND (product_name LIKE ? OR manufacturer LIKE ? OR "
                "barcode LIKE ? OR location LIKE ?)")
        args += [f"%{search}%"] * 4
    if verdict:
        sql += " AND verdict = ?"
        args.append(verdict)
    sql += " ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?"
    args += [limit, offset]
    with connect() as conn:
        return [dict(r) for r in conn.execute(sql, args)]


def count_scans(search=None, verdict=None):
    sql = "SELECT COUNT(*) c FROM scans WHERE 1=1"
    args = []
    if search:
        sql += (" AND (product_name LIKE ? OR manufacturer LIKE ? OR "
                "barcode LIKE ? OR location LIKE ?)")
        args += [f"%{search}%"] * 4
    if verdict:
        sql += " AND verdict = ?"
        args.append(verdict)
    with connect() as conn:
        return conn.execute(sql, args).fetchone()["c"]


def set_report_file(scan_id, filename):
    with connect() as conn:
        conn.execute("UPDATE scans SET report_file = ? WHERE id = ?",
                     (filename, scan_id))
        conn.commit()


# ------------------------------------------------------------ audit/log ---

def log_audit(user_id, action, entity):
    with connect() as conn:
        conn.execute(
            "INSERT INTO audit_log (user_id, action, entity, at) VALUES (?,?,?,?)",
            (user_id, action, entity, now()))
        conn.commit()


def log_sync(event, detail=""):
    with connect() as conn:
        conn.execute("INSERT INTO sync_log (event, detail, at) VALUES (?,?,?)",
                     (event, detail, now()))
        conn.commit()


def recent_sync_log(limit=12):
    with connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM sync_log ORDER BY id DESC LIMIT ?", (limit,))]


def user_by_username(username):
    with connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE username = ?",
                           (username,)).fetchone()
    return dict(row) if row else None


def user_by_id(user_id):
    with connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?",
                           (user_id,)).fetchone()
    return dict(row) if row else None


def list_users():
    with connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT id, username, display_name, role, jurisdiction, created_at "
            "FROM users ORDER BY id")]


def create_user(username, password, display_name, role, jurisdiction):
    with connect() as conn:
        conn.execute(
            "INSERT INTO users (username, password_hash, display_name, role,"
            " jurisdiction, created_at) VALUES (?,?,?,?,?,?)",
            (username, generate_password_hash(password), display_name, role,
             jurisdiction, now()))
        conn.commit()


# ------------------------------------------------------- registers ---

def list_complaints(limit=20, offset=0):
    with connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT c.*, s.uid AS scan_uid, s.product_name "
            "FROM complaints c JOIN scans s ON s.id = c.scan_id "
            "ORDER BY c.id DESC LIMIT ? OFFSET ?", (limit, offset))]


def count_complaints():
    with connect() as conn:
        return conn.execute(
            "SELECT COUNT(*) c FROM complaints").fetchone()["c"]


def list_official_complaints(limit=20, offset=0):
    with connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT o.*, s.uid AS scan_uid, s.product_name "
            "FROM official_complaints o JOIN scans s ON s.id = o.scan_id "
            "ORDER BY o.id DESC LIMIT ? OFFSET ?", (limit, offset))]


def count_official_complaints():
    with connect() as conn:
        return conn.execute(
            "SELECT COUNT(*) c FROM official_complaints").fetchone()["c"]


def list_audit(action=None, limit=25, offset=0):
    sql = "SELECT * FROM audit_log"
    args = []
    if action:
        sql += " WHERE action = ?"
        args.append(action)
    sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
    args += [limit, offset]
    with connect() as conn:
        return [dict(r) for r in conn.execute(sql, args)]


def count_audit(action=None):
    sql = "SELECT COUNT(*) c FROM audit_log"
    args = []
    if action:
        sql += " WHERE action = ?"
        args.append(action)
    with connect() as conn:
        return conn.execute(sql, args).fetchone()["c"]
