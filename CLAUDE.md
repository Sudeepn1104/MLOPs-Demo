# Project memory

## SmartBusOCR (`smartbusocr/`)

Windows 11 desktop app (Tkinter) for bus-pass attendance at Mangalore Institute of Technology & Engineering (MITE).
A webcam reads the printed Reg. No. (USN) on the college bus pass, looks it up in SQLite, and records attendance.
RFID is not used even though the card says "RFID CARD". All demo data is fictional.

### Files
- `app.py`: the whole app (DB, card location, OCR, rules, auto-scanner, GUI, settings).
- `check_db.py`: helper that lists students/attendance/scan counts and inserts the fictional test record `4MT24AI054` (MITE-BUS-01, valid to 2027-06-30).
- `requirements.txt`: opencv-python, numpy, pillow, pytesseract, rapidocr-onnxruntime (optional).
- `pass_template.png`: clean 1000x630 image of the pass with the per-student areas blanked. Required at runtime for ORB template matching.
- `smartbus_ocr_demo.db`, daily backups `smartbus_ocr_demo.backup-YYYYMMDD-HHMMSS.db`, `scan_debug/`: created at runtime. They hold real pass text and images, so never commit or share them (they're gitignored).

### Core principle
OCR only *proposes* an ID. SQLite is authoritative and the lookup is exact (case-insensitive).
Look-alike correction (A/4, I/1, O/0, S/5, B/8, Z/2, M/N, plus one stray inserted char) is used only to reach an ID that already exists, and only when exactly one stored ID is reachable.

### Pipeline
1. **Locate card** (`locate_card`): ORB features of the fixed printed graphics (header, logo, bus picture) are matched at template scales (1.0, 0.6, 0.4, 0.28). Per-student areas are masked out (`BLANK_BOXES`). A RANSAC homography (at least 14 inliers) flattens the card to 1000x630. `deep=True` also searches a 1.8x upscaled frame for far cards. Fallback: contour/quad detection (`find_card_quads`, aspect 1.2–2.3, area 8–65% of the frame) plus a 180° retry.
2. **Distance**: card width `< MIN_PX=200` means "move closer"; `< NEAR_PX=290` means far, so up to `STACK_SIZE=6` frames are ECC-refined, phase-correlated, averaged, upscaled and sharpened before reading.
3. **OCR** (`read_flat`): RapidOCR (preferred, in-process, better on small print) reads `FIELDS_BOX`. Otherwise Tesseract `--psm 6` reads `STRIP_BOX` on an upscaled grayscale crop (no thresholding, which hurts this card's small print). Tesseract path: `C:\Program Files\Tesseract-OCR\tesseract.exe` if present, else PATH.
4. **Candidates** (`analyse_text`): `REG NO` label, then the configured pattern `[prefix]year+branch+suffix`, then generic 5–20 char tokens containing a digit.
5. **Rules** (`evaluate_student`): NO_ID / UNMATCHED / AMBIGUOUS / INACTIVE / BAD_DATE / EXPIRED / WRONG_BUS, else a match.
6. **Auto-scan** (`AutoScanner`, a background thread that sends events to a Tk queue): an exact read needs 1 read and a corrected read needs `AUTO_CONFIRM_READS=2` in a row. Duplicates (same usn + session + day) are rejected. Attendance source is `auto_scan`. After `AUTO_FAIL_LIMIT=2` unreadable reads it asks for a rescan and saves debug frames. The card must leave the view for 1.5 s before a re-scan.
7. **Manual mode**: "Capture + run OCR", then a visual check, then "Confirm attendance" (source `manual_confirmation`).
8. Optional name check (`require_name_match`): fuzzy match of the stored name against the OCR text (difflib ≥ 0.75).
9. UI colours: green = recorded, red = alert, orange = scan again / move closer. `winsound` beeps.

### Card layout fractions (flattened card)
`STRIP_BOX=(0.40,0.28,0.82,0.84)` Reg. No.→Date of Validity values; `NAME_BOX=(0.17,0.17,0.66,0.32)`; `DEST_BOX=(0.44,0.50,0.74,0.60)`; `FIELDS_BOX=(0.17,0.17,0.82,0.84)`.
Validity is the later of two `dd-Mon-yy` dates on the pass (the first is the issue date).

### SQLite schema (create-if-missing; existing students/scans are never altered)
- `students(usn PK, name, bus_number, valid_until ISO date, active)`
- `scans(id, usn, scanned_at, ocr_text, result)`: every attempt
- `attendance(id, usn, student_name, bus_number, session 'Pickup'|'Drop-off', recorded_at, source)`
- `admission_year_codes(code)`: seeded 25–30
- `branch_codes(code, branch_name)`: ci, cs, ai, ic, me, mt, ri, ec, is
- `app_settings`: `id_prefixes` (default `4MT`), `require_name_match` (`0`), `admin_pin`
- Demo students: 27ci001, 27cs002 (BUS-01), 27ai003 (BUS-02), 27ic004 (expired), 26me005 (inactive).
- The DB is backed up once per day before migrations.

### Admin
Driver/admin settings are PIN-protected. The PIN is PBKDF2-SHA256 with 200k iterations and a random salt. No default PIN: you set one on first use. The legacy plaintext `2468` is migrated or force-replaced. 5 failures lock it for 60 s. Settings edit year codes, branch codes, ID prefixes and the name check.

### Run (Windows, PowerShell)
```
py -m venv .venv; .venv\Scripts\activate; pip install -r requirements.txt; python app.py
```
