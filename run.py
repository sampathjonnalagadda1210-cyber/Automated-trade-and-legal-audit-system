#!/usr/bin/env python3
"""
Start A.T.L.A.S.
================

Run this file, not app.py, unless you know you don't need the checks.

Its whole job is to fail helpfully.  A first-time setup breaks for a small
number of boring reasons - a missing package, Tesseract not on PATH, port
5000 already taken - and each of those produces a stack trace that tells a
beginner nothing.  This checks for them first and says what to do.
"""

import os
import socket
import sys
import webbrowser

BANNER = r"""
   _  _____ _      _    ___
  / \|_   _| |    / \  / __|    Automated Trade & Legal Audit System
 / _ \ | | | |__ / _ \ \__ \    Legal Metrology (Packaged Commodities)
/_/ \_\|_| |____/_/ \_\|___/    Rules, 2011
"""

REQUIRED = [
    ("flask", "Flask", "the web framework"),
    ("cv2", "opencv-python", "image processing"),
    ("numpy", "numpy", "numerical work"),
    ("PIL", "Pillow", "image loading"),
    ("pytesseract", "pytesseract", "the OCR wrapper"),
    ("reportlab", "reportlab", "PDF reports"),
]


def check_python():
    if sys.version_info < (3, 9):
        print(f"\n  Python 3.9 or newer is needed. You are running "
              f"{sys.version.split()[0]}.")
        print("  Install a newer Python from python.org and try again.\n")
        sys.exit(1)


def check_packages():
    missing = []
    for module, package, why in REQUIRED:
        try:
            __import__(module)
        except ImportError:
            missing.append((package, why))
    if missing:
        print("\n  Some Python packages are missing:\n")
        for package, why in missing:
            print(f"    - {package:20s} ({why})")
        print("\n  Install them all with this one command:\n")
        print("      pip install -r requirements.txt\n")
        sys.exit(1)


def check_tesseract():
    from core.ocr_engine import ensure_tesseract
    ok, message = ensure_tesseract()
    if ok:
        print(f"  OCR engine     : {message}")
        return
    print("\n  " + "!" * 66)
    print("  The Tesseract OCR engine was not found.")
    print()
    print("  pytesseract is only a wrapper - the actual OCR program is a")
    print("  separate install:")
    print()
    print("    Windows : download the installer from")
    print("              https://github.com/UB-Mannheim/tesseract/wiki")
    print("              and TICK the box that adds it to PATH.")
    print("    macOS   : brew install tesseract")
    print("    Linux   : sudo apt install tesseract-ocr")
    print()
    print("  The site will still start and every page will load, but a scan")
    print("  will report an error until Tesseract is installed.")
    print("  " + "!" * 66 + "\n")


def free_port(preferred=5000):
    for port in range(preferred, preferred + 12):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            if sock.connect_ex(("127.0.0.1", port)) != 0:
                return port
    return preferred


def main():
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, os.getcwd())
    print(BANNER)
    check_python()
    check_packages()
    print(f"  Python         : {sys.version.split()[0]}")
    check_tesseract()

    from app import app

    port = free_port()
    url = f"http://127.0.0.1:{port}"
    print(f"  Database       : data/atlas.db (created automatically)")
    print(f"  Address        : {url}")
    print()
    print("  Sign in as  inspector1 / inspector123")
    print("  Stop the server with Ctrl+C in this window.")
    print()

    try:
        webbrowser.open(url)
    except Exception:
        pass
    app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
