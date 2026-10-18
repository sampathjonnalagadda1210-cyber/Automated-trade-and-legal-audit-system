"""End-to-end verification of the changed flows via the Flask test client.

Covers the upgrade surface: CSRF-protected forms, login throttling,
security headers, the private-media guard, the liveness probe,
repository pagination and CSV export, the complaints register, and
the role-separated audit log.
"""
import base64
import os
import re
import sys
import time
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app as app_module
from app import app

c = app.test_client()


def csrf_from(html):
    m = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert m, "no csrf_token field in form"
    return m.group(1)


def login(username, password):
    """Sign in the way a browser does: load the form (which seeds the
    session CSRF token), then post it back with the token."""
    r = c.get("/login")
    assert r.status_code == 200
    token = csrf_from(r.data.decode("utf-8"))
    return c.post("/login",
                  data={"username": username, "password": password,
                        "csrf_token": token},
                  follow_redirects=True)


def strip(html):
    return re.sub(r"<[^>]+>", " ", html)


print("== unauthenticated access is redirected, not served ==")
r = c.get("/repository")
assert r.status_code == 302 and "/login" in r.headers["Location"]
r = c.get("/api/status")
assert r.status_code == 302
r = c.get("/static/reports/anything.pdf")
assert r.status_code == 302 and "/login" in r.headers["Location"], \
    "reports must not be public"
print("ok - private pages and files redirect to sign-in")

print("\n== liveness probe is open and leaks nothing ==")
r = c.get("/healthz")
assert r.status_code == 200
assert r.get_json()["status"] == "ok"
print("ok - /healthz answers without a session")

print("\n== security headers on every response ==")
r = c.get("/login")
for h, v in [("X-Content-Type-Options", "nosniff"),
             ("X-Frame-Options", "DENY"),
             ("Referrer-Policy", "strict-origin-when-cross-origin")]:
    assert r.headers.get(h) == v, f"missing {h}"
print("ok - nosniff, frame deny, referrer policy set")

print("\n== CSRF: state-changing posts without a token are refused ==")
r = c.post("/sync/toggle", data={})
assert r.status_code == 400, r.status_code
r = c.post("/login", data={"username": "admin", "password": "x"})
assert r.status_code == 400, "login without token must be rejected"
print("ok - posts without a session token get 400")

print("\n== login as inspector ==")
r = login("inspector1", "inspector123")
assert r.status_code == 200, r.status_code
html = r.data.decode("utf-8")
assert "Repository" in html, "nav broken"
assert "Complaints" in html, "complaints register missing from nav"
assert "Audit log" not in html, "inspector must not see the audit log"
print("ok - inspector signed in; register in nav, audit log hidden")

print("\n== single scan (bundled non-compliant label) ==")
token = csrf_from(c.get("/scan").data.decode("utf-8"))
t0 = time.perf_counter()
r = c.post("/scan", data={"sample": "label_noncompliant.png",
                            "csrf_token": token},
           follow_redirects=True)
dt = time.perf_counter() - t0
html = r.data.decode("utf-8")
assert r.status_code == 200, r.status_code
assert "Official complaint" in html, "official complaint panel missing"
assert "Prepare the official complaint" in html, "draft button missing"
assert "Consumer view (English)" in html, "consumer view toggle missing"
assert "Your right" not in strip(html), "rights panel should not be on officer view"
scan_id = int(re.search(r"/scan/(\d+)", html).group(1))
print(f"ok - scan {scan_id} in {dt:.1f}s; official complaint panel present")

print("\n== consumer view toggle ==")
r = c.get(f"/scan/{scan_id}?consumer=1")
html2 = r.data.decode("utf-8")
assert r.status_code == 200
assert "Your right" in html2 and "Official complaint" not in html2
print("ok - consumer view renders rights panels, no complaint panel")

print("\n== official complaint draft form ==")
r = c.get(f"/scan/{scan_id}/official-complaint")
html = r.data.decode("utf-8")
assert r.status_code == 200, r.status_code
assert "Ground 1" in html, "grounds preview missing"
assert "Legal Metrology Inspector" in html, "designation missing"
print("ok - draft form renders with grounds")

print("\n== official complaint PDF generation ==")
token = csrf_from(c.get(
    f"/scan/{scan_id}/official-complaint").data.decode("utf-8"))
r = c.post(f"/scan/{scan_id}/official-complaint",
           data={"csrf_token": token,
                 "officer_name": "S. Meenakshi",
                 "designation": "Legal Metrology Inspector",
                 "jurisdiction": "Chengalpattu District",
                 "addressee": "The Controller, Department of Legal Metrology",
                 "place": "Test premises, Chennai",
                 "action": "prosecution",
                 "notes": "Verification smoke test."},
           follow_redirects=True)
assert r.status_code == 200, r.status_code
assert r.data.startswith(b"%PDF"), "POST did not return the PDF directly"
pdf_name = max((f for f in os.listdir("static/reports")
                if f.startswith("official_OC-")),
               key=lambda f: os.path.getmtime(
                   os.path.join("static", "reports", f)))
ref_m = re.search(r"official_(OC-[0-9A-Z]+)\.pdf", pdf_name)
assert ref_m, "reference not in filename"
reference = ref_m.group(1)
with open(os.path.join("static", "reports", pdf_name), "rb") as fh:
    pdf_head = fh.read(5)
assert pdf_head == b"%PDF-", "generated file is not a PDF"
print(f"ok - memo {reference} generated as a real PDF")

print("\n== memo content sanity ==")


def pdf_text(path):
    """Pull the text literals out of a ReportLab PDF without any third-
    party library: each content stream is ASCII85 + Flate compressed,
    so decode that chain and collect the parenthesised strings in order."""
    raw = open(path, "rb").read()
    out = []
    for m in re.finditer(rb"<<([^<>]*)>>\s*stream\r?\n(.*?)endstream",
                         raw, re.DOTALL):
        hdr, data = m.group(1), m.group(2).strip()
        if b"Image" in hdr:
            continue
        try:
            if b"ASCII85Decode" in hdr:
                data = base64.a85decode(data, adobe=True)
            if b"FlateDecode" in hdr:
                data = zlib.decompress(data)
        except Exception:
            continue
        i = 0
        while i < len(data):
            if data[i:i + 1] == b"(":
                depth, j, chunk = 1, i + 1, bytearray()
                while j < len(data) and depth:
                    ch = data[j:j + 1]
                    if ch == b"\\":
                        chunk += data[j + 1:j + 2]
                        j += 2
                        continue
                    if ch == b"(":
                        depth += 1
                    elif ch == b")":
                        depth -= 1
                        if not depth:
                            break
                    chunk += ch
                    j += 1
                out.append(chunk.decode("latin-1", "ignore"))
                i = j + 1
            else:
                i += 1
    return " ".join(out)


memo = pdf_text(os.path.join("static", "reports", pdf_name)).lower()
checks = ["section 36", "section 32", "section 18", "ground 1",
          "verification", "signature of the inspecting officer",
          "annexure a", "annexure b", "rule 18(2)",
          "inclusive of all taxes"]
missing = [n for n in checks if n not in memo]
assert not missing, f"memo missing: {missing}"
print("ok - memo carries grounds, statutory sections, verification, annexures")

print("\n== mark submitted ==")
token = csrf_from(c.get(f"/scan/{scan_id}").data.decode("utf-8"))
r = c.post(f"/scan/{scan_id}/official-complaint/submit",
           data={"reference": reference, "csrf_token": token},
           follow_redirects=True)
html = r.data.decode("utf-8")
assert r.status_code == 200
assert "SUBMITTED" in html, "submitted status not shown"
assert reference in html, "reference not listed on scan page"
print("ok - memo marked submitted on the scan page")

print("\n== complaint-filing flow (consumer letter) still works ==")
token = csrf_from(c.get(f"/scan/{scan_id}/complaint").data.decode("utf-8"))
r = c.post(f"/scan/{scan_id}/complaint",
           data={"complainant": "Verify Bot", "contact": "x@y.z",
                 "body": "Test complaint body", "lang": "en",
                 "csrf_token": token},
           follow_redirects=True)
assert r.status_code == 200
assert b"%PDF" in r.data or "CMP-" in r.data.decode("utf-8", "ignore")
print("ok - shopper complaint letter unchanged")

print("\n== demo batch (5 labels, threaded) ==")
t0 = time.perf_counter()
token = csrf_from(c.get("/batch").data.decode("utf-8"))
r = c.post("/batch", data={"use_samples": "1", "location": "Test godown",
                           "csrf_token": token},
           follow_redirects=True)
batch_dt = time.perf_counter() - t0
html = r.data.decode("utf-8")
assert r.status_code == 200, r.status_code
m = re.search(r'class="n">(\d+)</div>\s*<div class="l">Packs processed', html)
assert m and m.group(1) == "5", "batch did not process 5 packs"
assert "batch_02_font_violation.png" in html
print(f"ok - batch of 5 in {batch_dt:.1f}s")

print("\n== e-commerce sweeps ==")
token = csrf_from(c.get("/listings").data.decode("utf-8"))
t0 = time.perf_counter()
r = c.post("/listings", data={"csrf_token": token}, follow_redirects=True)
cold = time.perf_counter() - t0
assert r.status_code == 200
t0 = time.perf_counter()
r = c.post("/listings", data={"csrf_token": token}, follow_redirects=True)
warm = time.perf_counter() - t0
assert r.status_code == 200
html = r.data.decode("utf-8")
assert "last sweep:" in html and "next scheduled:" in html
print(f"ok - cold sweep {cold:.1f}s, warm sweep {warm:.1f}s; sweep schedule shown")

print("\n== repository: pagination and CSV export ==")
r = c.get("/repository")
html = r.data.decode("utf-8")
assert r.status_code == 200
assert "page 1 of" in html
r = c.get("/repository?page=999")          # clamps to the last page
assert r.status_code == 200
r = c.get("/repository?page=abc")          # junk is sanitised to 1
assert r.status_code == 200
r = c.get("/repository/export")
assert r.status_code == 200
assert r.headers["Content-Disposition"].startswith(
    "attachment; filename=atlas-repository-")
csv_text = r.data.decode("utf-8")
lines = csv_text.strip().split("\n")
assert lines[0].startswith("scan_uid,captured_at,product")
assert len(lines) >= 2, "export is empty"
print(f"ok - export returned {len(lines) - 1} rows as an attachment")

print("\n== complaints register ==")
r = c.get("/complaints")
html = r.data.decode("utf-8")
assert r.status_code == 200
assert "Official memos" in html and "Shopper letters" in html
assert reference in html, "memo missing from the register"
print("ok - register lists both complaint kinds")

print("\n== audit log: role-separated ==")
r = c.get("/admin/audit")
assert r.status_code == 403, "inspector must not read the audit log"
r = login("auditor", "auditor123")
assert r.status_code == 200
r = c.get("/admin/audit")
html = r.data.decode("utf-8")
assert r.status_code == 200, "auditor must read the audit log"
assert "Audit log" in html
r = c.get("/scan")                        # auditors are read-only
assert r.status_code == 403
c.get("/logout")
r = login("admin", "admin123")
assert r.status_code == 200
r = c.get("/admin/audit")
html = r.data.decode("utf-8")
assert r.status_code == 200
assert "LOGIN" in html, "audit entries missing"
assert "page 1 of" in html
print("ok - auditor reads the trail, cannot scan; admin sees entries")

print("\n== login throttling ==")
app_module._login_failures.clear()          # isolate this test
# a valid token: failed logins keep the session, so one token
# serves every attempt
r = c.get("/login")
valid_token = csrf_from(r.data.decode("utf-8"))
statuses = []
for _ in range(10):
    r = c.post("/login",
               data={"username": "lockme", "password": "wrong",
                     "csrf_token": valid_token},
               follow_redirects=False)
    statuses.append(r.status_code)
assert 429 in statuses, f"no throttle kicked in: {statuses}"
first_429 = statuses.index(429)
assert first_429 >= 7, f"throttled too early: {statuses}"
print(f"ok - bad passwords throttled after {first_429 + 1} attempts (429)")

print("\n== sync toggle round-trip (CSRF-protected) ==")
r = c.get("/sync")
token = csrf_from(r.data.decode("utf-8"))
r = c.post("/sync/toggle", data={"csrf_token": token},
           follow_redirects=True)
assert r.status_code == 200
r = c.post("/sync/toggle", data={"csrf_token": token},
           follow_redirects=True)
assert r.status_code == 200
print("ok - online/offline toggle works with a valid token")

print("\nALL CHECKS PASSED")
