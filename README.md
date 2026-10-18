# A.T.L.A.S. — Automated Trade & Legal Audit System

A software system to check compliance of packaged commodities
under the Legal Metrology (Packaged Commodities) Rules, 2011, by scanning
products, images and labels.

Photograph a pack. The system reads the label, measures the printed
declarations in real millimetres, checks them against the Rules, and tells
you which provision was contravened — entirely on the device, with no
network. An inspector gets a citation and a signed PDF; a shopper gets the
same finding in plain Tamil, Hindi or English and a one-tap complaint.

---

## Running it

### 1. Install Python 3.9 or newer

From [python.org](https://www.python.org/downloads/). On Windows, tick
**"Add Python to PATH"** on the first screen of the installer.

### 2. Install the Tesseract OCR engine

`pytesseract` is only a wrapper — the OCR program itself installs separately.

| Platform | Command |
|---|---|
| Windows | Download from [UB-Mannheim](https://github.com/UB-Mannheim/tesseract/wiki) and **tick the "add to PATH" box** |
| macOS | `brew install tesseract` |
| Ubuntu / Debian | `sudo apt install tesseract-ocr` |

If the PATH box gets missed on Windows, the app looks in the usual install
folders anyway and will usually find it.

### 3. Install the Python packages

Open a terminal **in this folder** and run:

```bash
pip install -r requirements.txt
```

### 4. Start it

```bash
python run.py
```

A browser opens at `http://127.0.0.1:5000`. Sign in as
**`inspector1` / `inspector123`**.

Stop the server with `Ctrl+C`.

### Deploying it

The built-in server is for the field laptop and the demo. For a
shared or HTTPS deployment, put a production WSGI server
(Gunicorn, Waitress) in front of `app:app` and set:

| Variable | Effect |
|---|---|
| `ATLAS_SECRET_KEY` | session-signing key (otherwise a key is generated once into `data/secret.key`, mode 600) |
| `ATLAS_COOKIE_SECURE` | `1` to mark session cookies Secure (required behind HTTPS) |

`/healthz` is an unauthenticated liveness probe for a proxy or a
process manager. Photographs, generated reports and every state-changing
form are protected: files under `static/uploads/` and `static/reports/`
require sign-in, all POSTs carry a per-session CSRF token, login is
throttled after repeated failures, and every response carries
anti-clickjacking and content-type headers.

> `run.py` checks Python version, packages, Tesseract and port availability
> before starting, and prints what to do about each rather than a stack
> trace. If a scan ever fails, the message on screen says why.

**Demo accounts**

| Username | Password | Role |
|---|---|---|
| `inspector1` | `inspector123` | Field inspector |
| `admin` | `admin123` | State HQ — user management, audit log |
| `auditor` | `auditor123` | Read-only audit cell |

Databases are created on first run. Deleting `data/atlas.db` and
`data/central.db` resets everything to a clean state.

To check everything still works end to end:

```bash
python verify_changes.py
```

---

## What it does

**1 · Barcode-derived scale calibration.** Rule 7(3) fixes declaration
heights in millimetres, so any check on font size needs to know how many
pixels make a millimetre. Nearly every retail pack already carries a
dimensional reference: its EAN-13 barcode. Under the GS1 General
Specifications the symbol is 95 modules wide — 31.35 mm at nominal
magnification. A.T.L.A.S. finds the barcode, decodes it, measures its width
in pixels and derives the scale from the pack itself. Nothing is typed in,
and because the symbol is *decoded* rather than merely detected, the check
digit confirms that what was measured really is an EAN-13.

The decoder is written from scratch in NumPy (`core/barcode.py`), which is
why there is no `pyzbar` and therefore no system library to install.
Measured against the bundled labels, which are rendered at a known 12.0
px/mm, it recovers 11.95–12.03 px/mm on 11 of 11 — under 0.5% error.

**2 · Multi-frame glare recovery.** Glossy packaging under shop lighting
produces specular highlights, and no amount of single-image contrast work
recovers a blown-out patch, because those pixels carry no information at
all. Supply two or three frames of the same pack and A.T.L.A.S. registers
them with ORB feature matching, scores each for glare per pixel, and takes
every output pixel from whichever frame saw that spot least reflectively.

The three bundled `glare_frame_*` labels demonstrate this: frame 1 loses
the MRP line entirely, frame 2 misreads it as `Rs. 125.90` instead of
`185.00`, frame 3 garbles the tax qualifier. The merged frame reads all
twelve lines correctly.

**3 · Measurement, not approximation.** Tesseract's bounding box is not the
glyph height — it carries descenders and internal leading. Rule 7(3) speaks
of the height of the *numerals*, so the engine isolates the numeric token,
crops it, and measures the actual vertical extent of ink before converting
to millimetres.

**4 · A rule engine with nothing hardcoded.** Keywords, citations,
severities, the Rule 7(3) height table and the exemptions all live in
`core/rules.json`. The Rules genuinely do change — 2017 and 2022 both moved
the goalposts — so an amendment is a data edit, not a code change. Every
finding carries the provision it rests on, because "non-compliant" is
useless in a notice and "Rule 18(2)" is not. Browse the encoded set at
**/rules**.

**5 · Confidence-gated capture.** A wrong *compliant* is worse than asking
for a better photograph. OCR confidence, focus, glare and exposure feed one
score; below threshold the scan returns **INCONCLUSIVE** and asks for a
retake instead of guessing.

**6 · Offline-first with background sync.** Every scan is decided on-device
and written to a local queue. A background thread drains it whenever the
link returns. `data/atlas.db` is the handset; `data/central.db` stands in
for the departmental server. Use **Go offline** in the header, run scans,
and watch the two diverge and reconcile at **/sync**.

**7 · Batch processing.** A warehouse audit produces a folder, not a photo.
Every image runs through the identical pipeline, so a batch result can never
disagree with the on-screen result for the same pack. One consolidated PDF,
plus a full report per pack.

**8 · E-commerce listing monitoring.** Rule 6(10), inserted in 2017,
extends the mandatory declarations to online listings. Sellers often upload
the declarations block as a JPEG, which defeats an ordinary scraper — so
image assets go through the same OCR pipeline before the rule engine sees
them. Fetching is a scheduled backend job; the checker reads a cache, so it
runs with no network. Any listing text can also be pasted in and checked
live.

**9 · Consumer rights in three languages.** Each finding maps to a
plain-language explanation of the right engaged, keyed by rule ID, in
English, Hindi and Tamil. Adding Kannada is a column in `core/rights.json`.
The shopper-facing view is a toggle on the scan result, so the officer's
screen stays an officer's screen.

**10 · One-tap complaint.** The scan already holds the photograph, the
declarations and the provisions. The complaint form asks only who is
complaining; everything else is carried over into a dated PDF, queued for
upload like any other record.

**11 · Reports, repository, register, roles and audit.** PDF inspection
reports with photographic evidence and per-finding citations; a searchable,
paginated repository of every scan with a CSV export for the case file; a
complaints register covering both the shopper letters and the official
memos; inspector / auditor / admin roles; and an append-only audit log the
audit cell can filter and review read-only — because a tool whose output
may be relied on in proceedings has to say who did what and when.

**12 · Official complaint memo, ready to sign.** An inspection report records
what was found; a *complaint* is what sets the law in motion. On any
non-compliant scan, one click drafts the formal complaint an inspector files
with the Controller's office - office heading, subject line, pack particulars,
numbered grounds each citing the provision breached, the statutory powers
invoked (prosecution under s.32, seizure under s.18, offences under s.36 of
the Act), a verification clause, a signature block and two annexures
(annotated photograph; declaration-wise examination). Everything substantive
is auto-drafted from the stored scan; the officer confirms addressee and
place, prints, signs and submits - and marks the memo submitted from the scan
page so the device record matches the file.

---

## Layout

```
atlas/
├── run.py                      start here — pre-flight checks, then serve
├── app.py                      Flask routes (deliberately thin)
├── requirements.txt
├── core/
│   ├── barcode.py              EAN-13 decode + px/mm calibration
│   ├── ocr_engine.py           CLAHE, glare fusion, OCR, ink-height measurement
│   ├── rule_engine.py          evaluates declarations against rules.json
│   ├── rules.json              the encoded Rules — edit this, not the code
│   ├── rights.json / rights.py consumer rights, EN / HI / TA
│   ├── pipeline.py             the one place a scan is decided
│   ├── ecommerce.py            Rule 6(10) listing checks
│   ├── sync.py                 offline queue + background worker
│   ├── db.py                   sqlite3 storage, device + central
│   └── report_generator.py     inspection, complaint, official-memo and batch PDFs
├── templates/                  Jinja2
├── static/samples/             bundled demo labels (known ground truth)
├── data/                       databases + cached listing snapshots
├── tools/make_samples.py       regenerates the demo labels
└── verify_changes.py           smoke test - exercises every flow end to end
```

---

## A five-minute demo run

1. Sign in as `inspector1`.
2. **Scan** → tick the three **glare frame** labels together → *Run
   compliance scan*. The blue banner reports how much of the label was
   recovered that no single frame could read; the verdict is **COMPLIANT**.
3. **Scan** again → tick **label noncompliant** → scan. Four major findings
   and one minor, each with its provision. The evidence image shows green
   boxes on declarations that passed, red on the two printed below the Rule
   7(3) minimum, and orange on the barcode used as the ruler. Below the
   findings, the **official complaint panel** prepares the formal memo to the
   department, pre-filled from the scan, ready to print, sign and submit.
4. On the non-compliant scan, click **Prepare the official complaint** — check
   the addressee and place, generate, and the formal memo opens as a PDF.
   Once it is physically filed, use **mark submitted** on the scan page.
5. **Consumer view** (toggled at the bottom of the findings column) — switch
   to **தமிழ்**, then open the shopper complaint form and generate that PDF.
6. Header → **Go offline**. Run another scan: the verdict still appears, and
   the chip shows it queued. **Go online** — the queue drains and the scan
   appears on the central server at **/sync**.
7. **Batch** → *Run the demo batch* — five packs, mixed verdicts, one
   consolidated PDF.
8. **E-commerce** → *Run the scheduled sweep* — including one listing whose
   declarations only exist inside a JPEG.
9. **Rule book** — every check the engine performs, with its citation.

---

## Honest limitations

This is a working prototype, not a deployed enforcement system.

- **Classification is keyword and regex based**, not a trained NER model. It
  handles clearly labelled fields well and will miss creatively formatted or
  wholly non-English labels. Production should fine-tune a transformer NER
  model on annotated Indian label photographs.
- **No principal-display-panel detection.** The whole image is OCR'd, with
  column splitting to keep adjacent panels apart. A production version wants
  a YOLO-style detector and perspective correction; accuracy drops on
  angled, cluttered or multi-panel photographs.
- **OCR is English-only.** The consumer-facing explanations are trilingual,
  but reading Devanagari or Tamil *off the pack* needs the corresponding
  Tesseract language packs and re-tuned preprocessing.
- **Barcode calibration assumes a flat, front-on symbol.** A steeply angled
  or badly curved barcode will fail to decode, at which point the app falls
  back to a manually entered pack width rather than guessing.
- **The sync target is a local database**, not an HTTP endpoint. That keeps
  the demo offline-capable; a deployment replaces one function.
- **The official complaint memo follows the standard departmental format**
  (heading, subject, particulars, numbered grounds, prayer, verification,
  signature block) but is not a notified statutory form; have the format
  checked by the department or a legal SME before real submissions.
- **E-commerce fetching is a scheduled job stub.** The checker is real and
  runs against cached snapshots.
- **Security is production-minded but self-hosted**: per-session CSRF
  tokens, login throttling, security headers, private evidence files and a
  generated (or `ATLAS_SECRET_KEY`-supplied) signing key — but passwords
  are seeded demo values and SQLite is a file on disk. A large deployment
  still wants HTTPS termination, a managed secret store and a server
  database behind the sync layer.
- **`core/rules.json` should be validated by a legal SME** against the
  currently notified Rules before any enforcement use. The GS1-prefix origin
  check is deliberately marked *advisory*: a prefix records where a number
  was licensed, not where goods were made, so it is a lead to verify, never
  a finding on its own.

---

## Regulatory basis

- Legal Metrology Act, 2009
- Legal Metrology (Packaged Commodities) Rules, 2011 — Rule 6 (mandatory
  declarations), Rule 7(3) and the Third Schedule (minimum heights), Rule 18
  (retail sale price), Rules 26–27 (exemptions)
- LM(PC) (Amendment) Rules, 2017 — Rule 6(10), e-commerce declarations
- LM(PC) (Second Amendment) Rules, 2022 — QR-code declarations for
  electronics
- GS1 General Specifications — EAN-13 symbol dimensions
