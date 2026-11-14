# SmartBus Pass Attendance (Windows 11)

Webcam reads the printed Reg. No. on a MITE bus pass, checks it against SQLite, and records attendance.
RFID is not used. All demo data is fictional.

## One-time setup
1. Install Tesseract OCR (https://github.com/UB-Mannheim/tesseract/wiki), default path is fine.
2. In PowerShell, inside this folder:
   py -m venv .venv
   .venv\Scripts\activate
   python -m pip install --upgrade pip
   pip install -r requirements.txt
   (If rapidocr-onnxruntime fails to install on your Python version, run: pip install opencv-python numpy pillow pytesseract
    The app still works with Tesseract, but its reading range is shorter.)

## Files that must sit together
app.py, pass_template.png (needed to find the pass at any angle/distance), smartbus_ocr_demo.db (created on first run)

## Run
   python app.py
Driver/admin settings: set a new PIN on first use, then edit year codes, branch codes, ID prefixes, name check.
Start auto-scan: hold the pass in view. Green = recorded, red = alert, orange = scan again / move closer.

## Helper
   python check_db.py     (lists students/attendance; adds a fictional test record)

## Privacy
smartbus_ocr_demo.db, its backups, and the scan_debug folder contain real pass text/images. Do not share them.
