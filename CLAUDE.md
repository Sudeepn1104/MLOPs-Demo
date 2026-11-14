# Project memory

## SmartBusOCR (`smartbusocr/`)

Windows 11 desktop app (Tkinter) for bus-pass attendance at Mangalore Institute of Technology & Engineering (MITE).
A webcam reads the printed Reg. No. (USN) on the college bus pass, looks it up in SQLite, and records attendance.
RFID is not used even though the card says "RFID CARD". All demo data is fictional.

### Files
- `app.py`: the whole app (DB, card location, OCR, rules, auto-scanner, GUI, settings).
- `check_db.py`: helper that lists students/attendance/scan counts and inserts the fictional test record `4MT24AI054` (MITE-BUS-01, valid to 2027-06-30).
- `requirements.txt`: opencv-python, numpy, pillow, pytesseract, rapidocr-onnxruntime (optional).
- `pass_template.png`: clean 1000x630 image of the pass with the per-student areas blanked. Required at runtime for template matching.
- `smartbus_ocr_demo.db`, daily backups `smartbus_ocr_demo.backup-YYYYMMDD-HHMMSS.db`, `scan_debug/`: created at runtime. They hold real pass text and images, so never commit or share them (they're gitignored).

### Core principle
OCR only *proposes* an ID. SQLite is authoritative and the lookup is exact (case-insensitive).
Look-alike correction (A/4, I/1, O/0, S/5, B/8, Z/2, M/N, plus one stray inserted char or one missing char) is used only to reach an ID that already exists, and only when exactly one stored ID is reachable. If nothing matches, `/ | \ ! l` between ID characters are read as `I` and the text is tried again (OCR often turns the thin I into one of these, which splits the ID). Auto-scan accepts one read only when it is an exact read of a single sharp frame of a near card (>= `STACK_PX`). Every other read (small card, combined frames, corrected ID) must be repeated from separate frames: with many similar IDs a misread digit lands on another student, and reads that share frames repeat the same mistake.

### Pipeline
1. **Locate card** (`locate_card`): SIFT features of the fixed printed graphics (header, logo, bus picture) are matched against the frame (CLAHE, FLANN, ratio 0.8). SIFT is scale-invariant, so there is one template and no template scales. Per-student areas are masked out (`template_mask`: `BLANK_BOXES` widened by `TEMPLATE_MASK_MARGIN=12` px so the artificial edges of the blanked areas give no features). A MAGSAC homography (at least `MIN_INLIERS=12`) flattens the card to 1000x630. `plausible_card` rejects fits that are non-convex, mirrored, the wrong aspect, or too skewed. Frames are shrunk to `MAX_DETECT_SIDE=1280` for the search; `deep=True` also searches bigger frames at full size for far cards. Fallback: contour/quad detection (`find_card_quads`, aspect 1.2–2.3, area 8–65% of the frame) plus a 180° retry.
2. **Distance**: card width `< MIN_PX=200` means "move closer". `< STACK_PX=400` means small: frames are ECC-refined, phase-correlated and averaged (up to `STACK_SIZE=10`), then upscaled and sharpened before reading. From `NEAR_PX=290` up, the first frame is tried on its own before stacking. While the stack is filling it is read only at `STACK_READ_AT=(3, 6, 8)` frames, and a failed read does not count as a failure until the stack is full. The stack survives missed detections and is dropped only when the card has been gone for 1.5 s.
   - **Motion blur** (`frame_sharpness`): detail in the printed header (`HEADER_BOX`) relative to the template at the same size. A frame under `BLUR_RATIO=0.5` of the sharpest recent frame (or of `AutoScanner.sharp_ref`, the running sharpness of frames that read well; it starts at 0 and is learned from the camera, and until the first read a failed read counts as a soft failure) is not read and not stacked.
   - **Glare** (`glare_fraction`): share of the details area well above the card's own background brightness; over `GLARE_FRACTION=0.01` counts as glare. `stacked(dark=True)` then averages only the darker half of the values at each point, which removes glare that moves between frames. Near cards keep `RECENT_SIZE=4` frames for this and try it when a single-frame read fails.
   - Blur and glare are "soft" failures: the banner says hold steady / tilt the pass, and "scan again" is only asked after `SOFT_FAIL_SECONDS=4`.
3. **OCR** (`read_flat`): RapidOCR (preferred, in-process, better on small print) reads `FIELDS_BOX`. Otherwise Tesseract `--psm 6` reads `STRIP_BOX` on an upscaled grayscale crop (no thresholding, which hurts this card's small print). Tesseract path: `C:\Program Files\Tesseract-OCR\tesseract.exe` if present, else PATH.
4. **Candidates** (`analyse_text`): `REG NO` label, then the configured pattern `[prefix]year+branch+suffix`, then generic 5–20 char tokens containing a digit.
5. **Rules** (`evaluate_student`): NO_ID / UNMATCHED / AMBIGUOUS / INACTIVE / BAD_DATE / EXPIRED / WRONG_BUS, else a match.
6. **Auto-scan** (`AutoScanner`, a background thread that sends events to a Tk queue): an exact single-frame read of a near card needs 1 read; anything else needs `AUTO_CONFIRM_READS=2` in a row from separate frames (the stack is cleared after the first). Duplicates (same usn + session + day) are rejected. Attendance source is `auto_scan`. After `AUTO_FAIL_LIMIT=2` unreadable reads it asks for a rescan and saves debug frames. The card must leave the view for 1.5 s before a re-scan.
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
