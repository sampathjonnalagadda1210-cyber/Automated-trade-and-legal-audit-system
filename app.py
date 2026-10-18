"""
A.T.L.A.S. - Automated Trade & Legal Audit System
=================================================

Flask application: routes, auth and view assembly.  All the actual work
happens in ``core/`` - this file is deliberately thin, so that the parts
worth reading (calibration, glare recovery, the rule engine) are not buried
inside request handlers.

Auth is a plain signed session rather than Flask-Login, and storage is
plain ``sqlite3`` rather than an ORM.  That is two fewer pip packages to
install and two fewer things that can fail on a laptop that has never run
a Python web app.
"""

from __future__ import annotations

import concurrent.futures
import csv
import functools
import hmac
import io
import math
import os
import secrets
import shutil
import tempfile
import time
import traceback

from flask import (Flask, abort, flash, jsonify, redirect, render_template,
                   request, Response, send_from_directory, session, url_for)
from werkzeug.security import check_password_hash
from werkzeug.utils import secure_filename

from core import db, ecommerce, rights, sync
from core import pipeline
from core import report_generator as reports
from core.ocr_engine import ensure_tesseract
from core.rule_engine import RULES

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "static", "uploads")
REPORT_DIR = os.path.join(BASE_DIR, "static", "reports")
SAMPLE_DIR = os.path.join(BASE_DIR, "static", "samples")
ALLOWED = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}


# ------------------------------------------------------------ config --

def _resolve_secret_key():
    """Session signing key, in order of preference:
    1. the ATLAS_SECRET_KEY environment variable (production),
    2. a key generated once and kept in data/secret.key (so a
       restart does not log every device user out),
    3. a fresh random key per boot (last resort - sessions do
       not survive a restart, but nothing is ever hardcoded)."""
    env = os.environ.get("ATLAS_SECRET_KEY")
    if env:
        return env
    key_file = os.path.join(BASE_DIR, "data", "secret.key")
    try:
        if os.path.exists(key_file):
            with open(key_file, "r", encoding="utf-8") as fh:
                key = fh.read().strip()
            if len(key) >= 32:
                return key
    except OSError:
        pass
    key = secrets.token_urlsafe(48)
    try:
        os.makedirs(os.path.dirname(key_file), exist_ok=True)
        with open(key_file, "w", encoding="utf-8") as fh:
            fh.write(key)
        os.chmod(key_file, 0o600)
    except OSError:
        pass
    return key


app = Flask(__name__)
app.config["SECRET_KEY"] = _resolve_secret_key()
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024 * 1024
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = \
    os.environ.get("ATLAS_COOKIE_SECURE", "").lower() in ("1", "true", "yes")
app.config["PERMANENT_SESSION_LIFETIME"] = 12 * 3600   # 12 hours


# ------------------------------------------------------------ security --

# Brute-force guard: per username and per IP, sliding 15-minute window.
_login_failures = {}
_LOGIN_MAX_ATTEMPTS = 8
_LOGIN_WINDOW = 900


def _login_throttled(key):
    now = time.time()
    attempts = [t for t in _login_failures.get(key, ())
                if now - t < _LOGIN_WINDOW]
    _login_failures[key] = attempts
    return len(attempts) >= _LOGIN_MAX_ATTEMPTS


def _record_login_failure(key):
    _login_failures.setdefault(key, []).append(time.time())


def _clear_login_failures(key):
    _login_failures.pop(key, None)


def _csrf_token():
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_urlsafe(32)
    return session["_csrf"]


def csrf_protect(view):
    """Validate the per-session token on every state-changing
    request.  Tokens ride in the session cookie, which is
    SameSite=Lax and HttpOnly, so a cross-site form cannot read
    or forge one."""
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if request.method in ("POST", "PUT", "DELETE", "PATCH"):
            expected = session.get("_csrf")
            sent = request.form.get("csrf_token", "")
            if not expected or not sent or \
                    not hmac.compare_digest(expected, sent):
                abort(400, "Session token missing or invalid. "
                           "Reload the form and try again.")
        return view(*args, **kwargs)
    return wrapped


@app.after_request
def _security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = \
        "camera=(), microphone=(), geolocation=()"
    return response


@app.before_request
def _guard_private_media():
    """Photographs, annotated evidence and generated reports are
    case files, not public assets.  An unauthenticated visitor is
    sent to the sign-in page rather than handed the file - the
    evidence behind a finding should be as protected as the
    finding itself."""
    path = request.path
    if (path.startswith("/static/uploads/")
            or path.startswith("/static/reports/")
            or path.startswith("/uploads/")):
        if not session.get("user_id"):
            return redirect(url_for("login", next=path))


# Repository and register pagination: one page size everywhere,
# so every list in the product behaves the same way.
PAGE_SIZE = 20


def _page_arg():
    """1-based page number from the query string, sanitised."""
    try:
        return max(1, int(request.args.get("page", 1)))
    except (TypeError, ValueError):
        return 1


def _pages(total):
    return max(1, math.ceil(total / PAGE_SIZE))


@app.context_processor
def inject_globals():
    ok, message = ensure_tesseract()
    return {
        "user": current_user(),
        "online": db.is_online(),
        "queue": sync.pending_counts(),
        "device_id": db.get_setting("device_id", "-"),
        "rule_version": RULES["rule_set_version"],
        "tesseract_ok": ok,
        "tesseract_msg": message,
        "languages": rights.LANGUAGES,
        "csrf_token": _csrf_token,
        "now_iso": db.now(),
    }


# ------------------------------------------------------------------ auth --

def current_user():
    uid = session.get("user_id")
    return db.user_by_id(uid) if uid else None


def login_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        user = current_user()
        if not user or user["role"] != "admin":
            abort(403)
        return view(*args, **kwargs)
    return wrapped


def roles_required(*roles):
    """Route guard for role-separated actions.  Inspectors (and admins) scan,
    file complaints and drive the sync link; auditors get read-only access to
    every view but cannot act - matching the role descriptions on the login
    card and the admin page."""
    def decorator(view):
        @functools.wraps(view)
        def wrapped(*args, **kwargs):
            user = current_user()
            if not user or user["role"] not in roles:
                abort(403)
            return view(*args, **kwargs)
        return wrapped
    return decorator


def inspector_required(view):
    return roles_required("inspector", "admin")(view)


@app.route("/login", methods=["GET", "POST"])
@csrf_protect
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        ip = request.remote_addr or "unknown"
        if _login_throttled(f"user:{username}") or \
                _login_throttled(f"ip:{ip}"):
            db.log_audit(None, "LOGIN_THROTTLED", f"user:{username} ip:{ip}")
            flash("Too many failed sign-ins. Wait 15 minutes and try "
                  "again, or ask your administrator.", "danger")
            return render_template("login.html"), 429
        user = db.user_by_username(username)
        if user and check_password_hash(user["password_hash"],
                                        request.form.get("password", "")):
            _clear_login_failures(f"user:{username}")
            _clear_login_failures(f"ip:{ip}")
            session.clear()
            session["user_id"] = user["id"]
            session.permanent = True
            db.log_audit(user["id"], "LOGIN", user["username"])
            return redirect(request.args.get("next") or url_for("dashboard"))
        _record_login_failure(f"user:{username}")
        _record_login_failure(f"ip:{ip}")
        db.log_audit(None, "LOGIN_FAILED", f"user:{username} ip:{ip}")
        flash("Those credentials were not recognised.", "danger")
    return render_template("login.html")


@app.route("/logout")
def logout():
    user = current_user()
    if user:
        db.log_audit(user["id"], "LOGOUT", user["username"])
    session.clear()
    return redirect(url_for("login"))


# ------------------------------------------------------------ dashboard --

@app.route("/")
@login_required
def dashboard():
    scans = db.list_scans(limit=500)
    total = len(scans)
    verdicts = {}
    for s in scans:
        verdicts[s["verdict"]] = verdicts.get(s["verdict"], 0) + 1

    with db.connect() as conn:
        by_rule = [dict(r) for r in conn.execute(
            "SELECT citation, severity, COUNT(*) c FROM violations "
            "GROUP BY citation ORDER BY c DESC LIMIT 8")]
        by_category = [dict(r) for r in conn.execute(
            "SELECT COALESCE(NULLIF(category,''),'Uncategorised') category, "
            "COUNT(*) total, SUM(CASE WHEN verdict='COMPLIANT' THEN 1 ELSE 0 END) ok "
            "FROM scans GROUP BY category ORDER BY total DESC LIMIT 8")]
        by_day = [dict(r) for r in conn.execute(
            "SELECT substr(created_at,1,10) d, COUNT(*) total, "
            "SUM(CASE WHEN verdict='COMPLIANT' THEN 1 ELSE 0 END) ok "
            "FROM scans GROUP BY d ORDER BY d DESC LIMIT 10")]

    max_rule = max([r["c"] for r in by_rule], default=1)
    return render_template(
        "dashboard.html", scans=scans[:8], total=total, verdicts=verdicts,
        by_rule=by_rule, max_rule=max_rule, by_category=by_category,
        by_day=list(reversed(by_day)), central=sync.central_stats(),
        sync_log=db.recent_sync_log(8))


# ----------------------------------------------------------------- scan --

def _save_uploads(files, uid):
    """Persist uploaded frames to a temp dir; returns the saved paths."""
    tmp = tempfile.mkdtemp(prefix=f"atlas_{uid}_")
    paths = []
    for f in files:
        if not f or not f.filename:
            continue
        ext = os.path.splitext(f.filename)[1].lower()
        if ext not in ALLOWED:
            continue
        dest = os.path.join(tmp, secure_filename(f.filename) or f"f{len(paths)}{ext}")
        f.save(dest)
        paths.append(dest)
    return tmp, paths


@app.route("/scan", methods=["GET", "POST"])
@csrf_protect
@inspector_required
def scan_new():
    samples = sorted(f for f in os.listdir(SAMPLE_DIR)
                     if f.lower().endswith(".png"))
    if request.method == "GET":
        return render_template("scan_new.html", samples=samples)

    user = current_user()
    uid = pipeline.new_uid()
    tmp = None
    try:
        chosen = request.form.getlist("sample")
        if chosen:
            paths = [os.path.join(SAMPLE_DIR, secure_filename(c))
                     for c in chosen]
            paths = [p for p in paths if os.path.exists(p)]
        else:
            tmp, paths = _save_uploads(request.files.getlist("frames"), uid)

        if not paths:
            flash("No usable image was supplied. Pick a bundled sample or "
                  "upload a JPG/PNG photo of a label.", "warning")
            return redirect(url_for("scan_new"))

        width = request.form.get("package_width_mm", "").strip()
        scan = pipeline.run_scan(
            paths,
            manual_width_mm=float(width) if width else None,
            category=request.form.get("category") or None,
            embossed=bool(request.form.get("embossed")),
            save_prefix=uid,
        )
        record = pipeline.scan_to_record(
            scan, user_id=user["id"], form=request.form,
            source="multi-frame" if len(paths) > 1 else "single")
        scan_id = db.insert_scan(record, scan["result"]["violations"])
        db.log_audit(user["id"], "SCAN", f"scan:{scan['uid']}")
        session["last_merge"] = scan["merge_report"]
        return redirect(url_for("scan_result", scan_id=scan_id))

    except RuntimeError as exc:
        flash(str(exc), "danger")
        return redirect(url_for("scan_new"))
    except Exception as exc:                          # keep the demo alive
        traceback.print_exc()
        flash(f"The scan could not be completed: {exc}", "danger")
        return redirect(url_for("scan_new"))
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)


@app.route("/scan/<int:scan_id>")
@login_required
def scan_result(scan_id):
    scan = db.get_scan(scan_id)
    if not scan:
        abort(404)
    lang = request.args.get("lang", "en")
    consumer = request.args.get("consumer") == "1"
    panels = rights.explain_all(scan["result"].get("violations", []), lang) \
        if consumer else []
    with db.connect() as conn:
        official_drafts = [dict(r) for r in conn.execute(
            "SELECT * FROM official_complaints WHERE scan_id = ? "
            "ORDER BY id DESC", (scan_id,))]
    return render_template(
        "scan_result.html", scan=scan, result=scan["result"],
        panels=panels, lang=rights.normalise(lang), ui=rights.ui(lang),
        consumer=consumer, official_drafts=official_drafts,
        merge=session.pop("last_merge", None))


@app.route("/scan/<int:scan_id>/report")
@login_required
def scan_report(scan_id):
    scan = db.get_scan(scan_id)
    if not scan:
        abort(404)
    officer = db.user_by_id(scan["user_id"]) or current_user()
    filename = reports.generate_report(scan, officer)
    db.set_report_file(scan_id, filename)
    return redirect(url_for("static", filename=f"reports/{filename}"))


# ---------------------------------------------------------------- batch --

@app.route("/batch", methods=["GET", "POST"])
@csrf_protect
@inspector_required
def batch():
    sample_batch_dir = os.path.join(SAMPLE_DIR, "batch")
    available = sorted(os.listdir(sample_batch_dir)) \
        if os.path.isdir(sample_batch_dir) else []

    if request.method == "GET":
        return render_template("batch.html", available=available,
                               results=None, summary=None, report=None)

    user = current_user()
    tmp = None
    try:
        if request.form.get("use_samples"):
            paths = [os.path.join(sample_batch_dir, f) for f in available]
        else:
            tmp, paths = _save_uploads(request.files.getlist("frames"),
                                       pipeline.new_uid())
        if not paths:
            flash("No images to process.", "warning")
            return redirect(url_for("batch"))

        # Each image runs the identical single-scan pipeline - same
        # calibration, same rule engine - so a batch result can never
        # disagree with the on-screen result for the same pack.  The packs
        # are independent, so they run in a small thread pool: Tesseract and
        # OpenCV release the GIL while working, so four workers cut the wall
        # time roughly fourfold without changing a single finding.
        location = request.form.get("location")

        def _process_one(path):
            try:
                scan = pipeline.run_scan([path], category=None)
                record = pipeline.scan_to_record(
                    scan, user_id=user["id"],
                    form={"location": location}, source="batch")
                scan_id = db.insert_scan(record, scan["result"]["violations"])
                return {
                    "filename": os.path.basename(path),
                    "scan_id": scan_id,
                    "verdict": scan["result"]["verdict"],
                    "barcode": record["barcode"],
                    "product": record["product_name"],
                    "violations": scan["result"]["violations"],
                    "summary": scan["result"]["summary"],
                }
            except Exception as exc:
                return {
                    "filename": os.path.basename(path), "scan_id": None,
                    "verdict": "ERROR", "barcode": None,
                    "product": str(exc)[:90], "violations": [],
                    "summary": {"major": 0, "minor": 0, "advisory": 0},
                }

        with concurrent.futures.ThreadPoolExecutor(
                max_workers=min(4, max(1, len(paths)))) as pool:
            results = list(pool.map(_process_one, paths))

        ok = sum(1 for r in results if r["verdict"] == "COMPLIANT")
        summary = {
            "total": len(results), "compliant": ok,
            "flagged": len(results) - ok,
            "findings": sum(len(r["violations"]) for r in results),
        }
        report = None
        usable = [r for r in results if r["scan_id"]]
        if usable:
            report = reports.generate_batch_report(usable, user)
        db.log_audit(user["id"], "BATCH", f"{len(results)} images")
        return render_template("batch.html", available=available,
                               results=results, summary=summary, report=report)
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)


# ----------------------------------------------------------- repository --

@app.route("/repository")
@login_required
def repository():
    search = request.args.get("q", "").strip()
    verdict = request.args.get("verdict", "").strip()
    total = db.count_scans(search=search or None, verdict=verdict or None)
    pages = _pages(total)
    page = min(_page_arg(), pages)
    scans = db.list_scans(search=search or None, verdict=verdict or None,
                          limit=PAGE_SIZE, offset=(page - 1) * PAGE_SIZE)
    return render_template("repository.html", scans=scans, search=search,
                           verdict=verdict, page=page, pages=pages,
                           total=total)


@app.route("/repository/export")
@login_required
def repository_export():
    """The register as a CSV - for the case file, the weekly
    return, or the spreadsheet the deputy keeper already has."""
    search = request.args.get("q", "").strip()
    verdict = request.args.get("verdict", "").strip()
    scans = db.list_scans(search=search or None, verdict=verdict or None,
                          limit=100000)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["scan_uid", "captured_at", "product", "manufacturer",
                "premises", "barcode", "gs1_prefix_country", "verdict",
                "major", "minor", "advisory", "quality_score",
                "calibration_mode", "sync_state"])
    for s in scans:
        w.writerow([s["uid"], s["created_at"], s["product_name"],
                    s["manufacturer"] or "", s["location"] or "",
                    s["barcode"] or "", s["barcode_country"] or "",
                    s["verdict"], s["major_count"], s["minor_count"],
                    s["advisory_count"], s["quality_score"] or "",
                    s["calibration_mode"], s["sync_state"]])
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition":
                             f"attachment; filename=atlas-repository-"
                             f"{db.now()[:10]}.csv"})


# ------------------------------------------------------------ complaint --

@app.route("/scan/<int:scan_id>/complaint", methods=["GET", "POST"])
@csrf_protect
@inspector_required
def complaint(scan_id):
    scan = db.get_scan(scan_id)
    if not scan:
        abort(404)
    lang = request.args.get("lang", request.form.get("lang", "en"))
    panels = rights.explain_all(scan["result"].get("violations", []), lang)

    if request.method == "POST":
        reference = "CMP-" + pipeline.new_uid()
        record = {
            "reference": reference,
            "complainant": request.form.get("complainant", "").strip(),
            "contact": request.form.get("contact", "").strip(),
            "body": request.form.get("body", "").strip(),
            "created_at": db.now(),
        }
        pdf = reports.generate_complaint(scan, record, panels)
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO complaints (reference, scan_id, complainant, "
                "contact, language, body, pdf_file, created_at, sync_state) "
                "VALUES (?,?,?,?,?,?,?,?, 'pending')",
                (reference, scan_id, record["complainant"], record["contact"],
                 rights.normalise(lang), record["body"], pdf,
                 record["created_at"]))
            conn.commit()
        db.log_audit(session.get("user_id"), "COMPLAINT", reference)
        flash(f"Complaint {reference} prepared and queued for upload.",
              "success")
        return redirect(url_for("static", filename=f"reports/{pdf}"))

    return render_template("complaint.html", scan=scan, panels=panels,
                           lang=rights.normalise(lang), ui=rights.ui(lang))


# ------------------------------------------------- official complaint --

_ROMAN = {1: "(i)", 2: "(ii)", 3: "(iii)"}


@app.route("/scan/<int:scan_id>/official-complaint", methods=["GET", "POST"])
@csrf_protect
@inspector_required
def official_complaint(scan_id):
    """Draft, generate and (on the POST review step) submit the formal
    complaint memo that an inspector files with the department."""
    scan = db.get_scan(scan_id)
    if not scan:
        abort(404)
    officer = current_user()

    if request.method == "POST":
        reference = "OC-" + pipeline.new_uid()
        draft = {
            "reference": reference,
            "officer_name": (request.form.get("officer_name", "").strip()
                             or officer.get("display_name")
                             or officer.get("username", "")),
            "designation": (request.form.get("designation", "").strip()
                            or "Legal Metrology Inspector"),
            "jurisdiction": (request.form.get("jurisdiction", "").strip()
                             or officer.get("jurisdiction") or "-"),
            "addressee": (request.form.get("addressee", "").strip()
                          or "The Controller, Department of Legal Metrology"),
            "place_seized": request.form.get("place", "").strip()
                            or scan.get("location"),
            "action_prayed": ("SEIZURE_ONLY"
                              if request.form.get("action") == "seizure"
                              else "PROSECUTION"),
            "notes": request.form.get("notes", "").strip(),
            "device_id": db.get_setting("device_id", "-"),
            "created_at": db.now(),
        }
        pdf = reports.generate_official_complaint(scan, officer, draft)
        with db.connect() as conn:
            conn.execute(
                """INSERT INTO official_complaints
                   (reference, scan_id, officer_name, designation,
                    jurisdiction, addressee, place_seized, action_prayed,
                    notes, pdf_file, status, created_at, sync_state)
                   VALUES (?,?,?,?,?,?,?,?,?,?, 'DRAFT', ?, 'pending')""",
                (reference, scan_id, draft["officer_name"],
                 draft["designation"], draft["jurisdiction"],
                 draft["addressee"], draft["place_seized"],
                 draft["action_prayed"], draft["notes"], pdf,
                 draft["created_at"]))
            conn.commit()
        db.log_audit(officer["id"], "OFFICIAL_COMPLAINT_DRAFT", reference)
        # Straight to the PDF: the officer reviews, prints, signs, submits.
        return redirect(url_for("static", filename=f"reports/{pdf}"))

    violations = [v for v in scan.get("violations", [])
                  if v["severity"] in ("major", "minor")]
    return render_template("official_complaint.html", scan=scan,
                           violations=violations, officer=officer)


@app.route("/scan/<int:scan_id>/official-complaint/submit", methods=["POST"])
@csrf_protect
@inspector_required
def official_complaint_submit(scan_id):
    """Record that the signed memo was physically submitted."""
    ref = request.form.get("reference", "").strip()
    with db.connect() as conn:
        conn.execute(
            """UPDATE official_complaints SET status = 'SUBMITTED',
               submitted_at = ? WHERE reference = ? AND scan_id = ?""",
            (db.now(), ref, scan_id))
        conn.commit()
    db.log_audit(session.get("user_id"), "OFFICIAL_COMPLAINT_SUBMIT", ref)
    flash(f"Complaint {ref} marked as submitted.", "success")
    return redirect(url_for("scan_result", scan_id=scan_id))


# ------------------------------------------------- complaints register --

@app.route("/complaints")
@login_required
def complaints_register():
    """One register for both kinds of complaint the device
    produces: the formal memos filed with the department, and
    the shopper letters generated at the counter."""
    page = _page_arg()
    letters_total = db.count_complaints()
    letters_pages = _pages(letters_total)
    memos_total = db.count_official_complaints()
    memos_pages = _pages(memos_total)
    page = min(page, max(letters_pages, memos_pages))
    letters = db.list_complaints(limit=PAGE_SIZE,
                                 offset=(page - 1) * PAGE_SIZE)
    memos = db.list_official_complaints(limit=PAGE_SIZE,
                                        offset=(page - 1) * PAGE_SIZE)
    with db.connect() as conn:
        submitted = conn.execute(
            "SELECT COUNT(*) c FROM official_complaints "
            "WHERE status = 'SUBMITTED'").fetchone()["c"]
        queued = conn.execute(
            "SELECT COUNT(*) c FROM official_complaints "
            "WHERE sync_state = 'pending'").fetchone()["c"] \
            + conn.execute(
                "SELECT COUNT(*) c FROM complaints "
                "WHERE sync_state = 'pending'").fetchone()["c"]
    return render_template(
        "complaints.html", letters=letters, memos=memos,
        letters_total=letters_total, letters_pages=letters_pages,
        memos_total=memos_total, memos_pages=memos_pages,
        submitted=submitted, queued=queued, page=page)


# ------------------------------------------------------------ e-commerce --

@app.route("/listings", methods=["GET", "POST"])
@csrf_protect
@login_required
def listings():
    results = summary = pasted = None
    if request.method == "POST":
        # Reading the monitor is open to every role; running sweeps and the
        # paste-checker change cached state, so they are inspector actions.
        user = current_user()
        if user["role"] not in ("inspector", "admin"):
            abort(403)
    if request.method == "POST" and request.form.get("pasted"):
        pasted = ecommerce.check_text(request.form["pasted"])
    elif request.method == "POST":
        results = ecommerce.run_scheduled_sweep()
        summary = ecommerce.sweep_summary(results)
        db.log_audit(session.get("user_id"), "LISTING_SWEEP",
                     f"{len(results)} listings")
    return render_template(
        "listings.html", results=results, summary=summary, pasted=pasted,
        corpus=ecommerce.load_corpus(),
        last_sweep=ecommerce.last_sweep_time(),
        next_sweep=ecommerce.next_sweep_due(),
        citation=ecommerce.CITATION)


# ------------------------------------------------------------------ sync --

@app.route("/sync")
@login_required
def sync_view():
    return render_template("sync.html", counts=sync.pending_counts(),
                           central=sync.central_stats(),
                           log=db.recent_sync_log(20))


@app.route("/sync/toggle", methods=["POST"])
@csrf_protect
@inspector_required
def sync_toggle():
    report = sync.set_online(not db.is_online())
    if report.get("scans") or report.get("complaints"):
        flash(f"Back online - uploaded {report['scans']} scan(s) and "
              f"{report['complaints']} complaint(s).", "success")
    elif db.is_online():
        flash("Device is online. Nothing was waiting in the queue.", "info")
    else:
        flash("Device switched to offline mode. Scans will queue locally.",
              "warning")
    return redirect(request.referrer or url_for("sync_view"))


@app.route("/sync/flush", methods=["POST"])
@csrf_protect
@inspector_required
def sync_flush():
    report = sync.flush(force=True)
    flash(f"Pushed {report['scans']} scan(s) and {report['complaints']} "
          f"complaint(s) to the central server.", "success")
    return redirect(url_for("sync_view"))


@app.route("/api/status")
@login_required
def api_status():
    return jsonify({"online": db.is_online(), "queue": sync.pending_counts()})


@app.route("/healthz")
def healthz():
    """Unauthenticated liveness probe for a reverse proxy or
    a process manager - reveals nothing but the state."""
    return jsonify({"status": "ok", "rule_set": RULES["rule_set_version"]})


# ----------------------------------------------------------------- rules --

@app.route("/rules")
@login_required
def rules_view():
    return render_template("rules.html", rules=RULES)


@app.route("/admin/users", methods=["GET", "POST"])
@csrf_protect
@admin_required
def admin_users():
    if request.method == "POST":
        try:
            db.create_user(
                request.form["username"].strip(), request.form["password"],
                request.form.get("display_name", "").strip(),
                request.form.get("role", "inspector"),
                request.form.get("jurisdiction", "").strip())
            flash("User created.", "success")
        except Exception as exc:
            flash(f"Could not create the user: {exc}", "danger")
        return redirect(url_for("admin_users"))
    return render_template("admin_users.html", users=db.list_users())


@app.route("/admin/audit")
@roles_required("admin", "auditor")
def audit_log():
    """The append-only trail, filterable by action.  Auditors get
    read access to exactly this view - that is the point of the
    role - so the trail no longer hides behind the admin page."""
    action = request.args.get("action", "").strip()
    total = db.count_audit(action or None)
    pages = _pages(total)
    page = min(_page_arg(), pages)
    entries = db.list_audit(action=action or None, limit=PAGE_SIZE,
                            offset=(page - 1) * PAGE_SIZE)
    with db.connect() as conn:
        actions = [r["action"] for r in conn.execute(
            "SELECT DISTINCT action FROM audit_log ORDER BY action")]
        users = {r["id"]: r["username"]
                 for r in conn.execute("SELECT id, username FROM users")}
    return render_template("audit_log.html", entries=entries,
                           action=action, actions=actions, users=users,
                           page=page, pages=pages, total=total)


@app.route("/uploads/<path:name>")
@login_required
def uploads(name):
    return send_from_directory(UPLOAD_DIR, name)


@app.errorhandler(403)
def forbidden(_):
    return render_template("error.html", code=403,
                           message="Your role does not allow that."), 403


@app.errorhandler(400)
def bad_request(_):
    return render_template("error.html", code=400,
                           message="The session token was missing or "
                                   "invalid - reload the form and try "
                                   "again."), 400


@app.errorhandler(404)
def not_found(_):
    return render_template("error.html", code=404,
                           message="No such page."), 404


@app.errorhandler(413)
def too_large(_):
    return render_template("error.html", code=413,
                           message="Those files are larger than the 64 MB "
                                   "upload limit."), 413


def bootstrap():
    db.init()
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    os.makedirs(REPORT_DIR, exist_ok=True)
    sync.start_worker()
    ecommerce.start_scheduler()


bootstrap()

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
