"""SmartBus OCR starter. OCR proposes identifiers; SQLite is authoritative."""

import difflib
from collections import namedtuple
import hashlib
import hmac
import os
import queue
import re
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog

import cv2
import numpy as np
import pytesseract

try:  # optional: far more accurate on small print, runs in-process (no per-call start-up cost)
    from rapidocr_onnxruntime import RapidOCR
except Exception:  # not installed or unsupported Python: Tesseract is used instead
    RapidOCR = None
from PIL import Image, ImageTk

# Keep the database beside app.py, even when launched from a shortcut.
DB_PATH = Path(__file__).resolve().with_name("smartbus_ocr_demo.db")
DEBUG_DIR = DB_PATH.parent / "scan_debug"  # last unreadable frame is saved here to help tune the camera

# Use the standard install path if present; otherwise rely on PATH.
_TESS = Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe")
if _TESS.exists():
    pytesseract.pytesseract.tesseract_cmd = str(_TESS)

AUTO_CONFIRM_READS = 2     # same stored ID must be read this many times in a row before auto-marking
AUTO_FAIL_LIMIT = 2        # unreadable reads in a row before asking the student to scan again
CARD_GONE_SECONDS = 1.5    # card must leave the view this long before it can be scanned again
# Field positions on the standard college bus pass, as fractions of the flattened card.
STRIP_BOX = (0.40, 0.28, 0.82, 0.84)   # Reg. No. down to Date of Validity (value column)
NAME_BOX = (0.17, 0.17, 0.66, 0.32)    # student name line
DEST_BOX = (0.44, 0.50, 0.74, 0.60)    # destination line
# Per-student areas of the pass (photo, details, bus number, barcode): ignored when matching the template.
TEMPLATE_PATH = Path(__file__).resolve().with_name("pass_template.png")
BLANK_BOXES = [(0.00, 0.26, 0.20, 0.62), (0.17, 0.17, 0.80, 0.84), (0.84, 0.48, 0.99, 0.73), (0.00, 0.78, 0.30, 1.00)]
FIELDS_BOX = (0.17, 0.17, 0.82, 0.84)  # name line down to Date of Validity
TEMPLATE_MASK_MARGIN = 12  # px kept clear around the blanked areas: their artificial edges never appear on a real pass
MIN_INLIERS = 12  # template features that must agree on the card's position
STACK_PX = 400    # card narrower than this (px): aligned frames are averaged, which makes small print readable
NEAR_PX = 290     # card at least this wide (px): a single frame is tried first
MIN_PX = 200      # narrower than this: too far to read, ask the student to move closer
STACK_SIZE = 10   # small cards: average up to this many aligned frames
HEADER_BOX = (0.22, 0.03, 0.99, 0.15)  # printed college name: the same on every pass, so its sharpness measures blur
START_SHARP_REF = 0.25  # sharpness assumed for a good frame until the first pass is read (it then adapts)
BLUR_RATIO = 0.5       # a frame under this fraction of the sharpest recent frame's sharpness is smeared: it is not read
GLARE_FRACTION = 0.01  # share of washed-out pixels in the details area that counts as glare
RECENT_SIZE = 4        # near cards: recent sharp frames kept, to combine when glare hides the text
SOFT_FAIL_SECONDS = 4.0  # blur or glare for this long without a read: ask the student to scan again
STACK_READ_AT = (3, 6, 8)  # while the stack is still filling, read only at these sizes (an early read is often enough)
MAX_DETECT_SIDE = 1280  # frames larger than this are shrunk for locating the card only (deep search uses 2x)
PassRead = namedtuple("PassRead", "text cand known exact flat quads lines shape card_px",
                      defaults=(None, 0, None, None, 0.0))
Located = namedtuple("Located", "flat card_px inliers deep")
LEGACY_DEFAULT_PIN = "2468"  # Only used to detect and force-replace the old default.
PBKDF2_ITERS = 200_000
MAX_PIN_FAILS = 5
LOCKOUT_SECONDS = 60


# ---------------------------------------------------------------- database
@contextmanager
def db():
    """Commit on success, roll back on error, always close (avoids Windows file locks)."""
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    try:
        with con:
            yield con
    finally:
        con.close()


def backup_db_once_per_day():
    """Copy an existing DB before any migration. Skipped if today's backup exists."""
    if not DB_PATH.exists():
        return
    stamp = datetime.now().strftime("%Y%m%d")
    if any(DB_PATH.parent.glob(f"smartbus_ocr_demo.backup-{stamp}*.db")):
        return
    target = DB_PATH.with_name(f"smartbus_ocr_demo.backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}.db")
    src = sqlite3.connect(DB_PATH)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


def hash_pin(pin):
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", pin.encode(), salt, PBKDF2_ITERS)
    return f"pbkdf2${PBKDF2_ITERS}${salt.hex()}${digest.hex()}"


def verify_pin(pin, stored):
    try:
        scheme, iters, salt, digest = stored.split("$")
        if scheme != "pbkdf2":
            return False
        calc = hashlib.pbkdf2_hmac("sha256", pin.encode(), bytes.fromhex(salt), int(iters))
        return hmac.compare_digest(calc.hex(), digest)
    except (ValueError, AttributeError):
        return False


def get_stored_pin():
    with db() as con:
        row = con.execute("SELECT setting_value FROM app_settings WHERE setting_key='admin_pin'").fetchone()
    return row[0] if row else None


def set_pin(pin):
    with db() as con:
        con.execute("INSERT INTO app_settings(setting_key, setting_value) VALUES ('admin_pin', ?) "
                    "ON CONFLICT(setting_key) DO UPDATE SET setting_value=excluded.setting_value",
                    (hash_pin(pin),))


def init_db():
    """Create-if-missing only. Existing students and scans rows are never altered or deleted."""
    backup_db_once_per_day()
    with db() as con:
        con.execute("""CREATE TABLE IF NOT EXISTS students (
            usn TEXT PRIMARY KEY, name TEXT NOT NULL, bus_number TEXT NOT NULL,
            valid_until TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1)""")
        con.execute("""CREATE TABLE IF NOT EXISTS scans (
            id INTEGER PRIMARY KEY AUTOINCREMENT, usn TEXT, scanned_at TEXT NOT NULL,
            ocr_text TEXT NOT NULL, result TEXT NOT NULL)""")
        con.execute("""CREATE TABLE IF NOT EXISTS admission_year_codes (
            code TEXT PRIMARY KEY COLLATE NOCASE)""")
        con.execute("""CREATE TABLE IF NOT EXISTS branch_codes (
            code TEXT PRIMARY KEY COLLATE NOCASE, branch_name TEXT NOT NULL)""")
        con.execute("""CREATE TABLE IF NOT EXISTS app_settings (
            setting_key TEXT PRIMARY KEY, setting_value TEXT NOT NULL)""")
        con.execute("""CREATE TABLE IF NOT EXISTS attendance (
            id INTEGER PRIMARY KEY AUTOINCREMENT, usn TEXT NOT NULL,
            student_name TEXT NOT NULL, bus_number TEXT NOT NULL,
            session TEXT NOT NULL, recorded_at TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'manual_confirmation')""")

        # Seed codes only when the tables are empty (never overwrite admin edits).
        if con.execute("SELECT COUNT(*) FROM admission_year_codes").fetchone()[0] == 0:
            con.executemany("INSERT OR IGNORE INTO admission_year_codes(code) VALUES (?)",
                            [(str(y),) for y in range(25, 31)])
        if con.execute("SELECT COUNT(*) FROM branch_codes").fetchone()[0] == 0:
            con.executemany("INSERT OR IGNORE INTO branch_codes(code, branch_name) VALUES (?, ?)", [
                ("ci", "CSE (IoT)"), ("cs", "CSE"), ("ai", "AI & ML"),
                ("ic", "CSE (AI & ML)"), ("me", "Mechanical"),
                ("mt", "Mechatronics"), ("ri", "Robotics & AI"),
                ("ec", "Electronics & Communication"), ("is", "Information Science"),
            ])

        # College ID prefix (e.g. 4MT) lives in SQLite too. Added only if missing.
        con.execute("INSERT OR IGNORE INTO app_settings(setting_key, setting_value) VALUES ('id_prefixes', '4MT')")

        con.execute("INSERT OR IGNORE INTO app_settings(setting_key, setting_value) VALUES ('require_name_match', '0')")

        # PIN migration: legacy plaintext -> PBKDF2 hash. No default PIN is created.
        row = con.execute("SELECT setting_value FROM app_settings WHERE setting_key='admin_pin'").fetchone()
        if row and not str(row[0]).startswith("pbkdf2$"):
            con.execute("UPDATE app_settings SET setting_value=? WHERE setting_key='admin_pin'",
                        (hash_pin(str(row[0])),))

        # Fictional examples only. Named columns; INSERT OR IGNORE never overwrites.
        demo = [
            ("27ci001", "Demo Student One", "MITE-BUS-01", "2027-06-30", 1),
            ("27cs002", "Demo Student Two", "MITE-BUS-01", "2027-06-30", 1),
            ("27ai003", "Demo Student Other Bus", "MITE-BUS-02", "2027-06-30", 1),
            ("27ic004", "Demo Student Expired", "MITE-BUS-01", "2025-06-30", 1),
            ("26me005", "Demo Student Inactive", "MITE-BUS-01", "2027-06-30", 0),
        ]
        con.executemany("INSERT OR IGNORE INTO students(usn, name, bus_number, valid_until, active) "
                        "VALUES (?, ?, ?, ?, ?)", demo)


# --------------------------------------------------------------------- OCR
def preprocess_for_ocr(frame, mode="adaptive"):
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    scale = min(2.0, 1800.0 / max(gray.shape[:2]))
    gray = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    gray = cv2.GaussianBlur(gray, (3, 3), 0)
    if mode == "otsu":
        return cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]
    return cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                 cv2.THRESH_BINARY, 31, 11)


def rotate_bound(img, angle):
    """Rotate by angle degrees on an enlarged canvas so nothing is cropped."""
    h, w = img.shape[:2]
    m = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
    cos, sin = abs(m[0, 0]), abs(m[0, 1])
    nw, nh = int(h * sin + w * cos), int(h * cos + w * sin)
    m[0, 2] += nw / 2 - w / 2
    m[1, 2] += nh / 2 - h / 2
    return cv2.warpAffine(img, m, (nw, nh), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)


def _quad_from_contour(c):
    hull = cv2.convexHull(c)
    approx = cv2.approxPolyDP(hull, 0.02 * cv2.arcLength(hull, True), True)
    if len(approx) == 4:
        return approx.reshape(4, 2).astype("float32"), cv2.contourArea(hull)
    rect = cv2.minAreaRect(hull)  # fallback: a rotated rectangle that the hull fills well
    box_area = rect[1][0] * rect[1][1]
    if box_area > 0 and cv2.contourArea(hull) > 0.85 * box_area:
        return cv2.boxPoints(rect).astype("float32"), cv2.contourArea(hull)
    return None, 0.0


def _card_shaped(q, frame_area):
    area = cv2.contourArea(q)
    if area < 0.08 * frame_area or area > 0.65 * frame_area:
        return False
    e = [float(np.linalg.norm(q[i] - q[(i + 1) % 4])) for i in range(4)]
    a, b = (e[0] + e[2]) / 2, (e[1] + e[3]) / 2
    if min(a, b) < 1:
        return False
    return 1.2 <= max(a, b) / min(a, b) <= 2.3  # bus pass is about 1.6:1


def find_card_quads(frame, limit=3):
    """Candidate pass outlines (4 corners each), best first. Several methods, since desks differ."""
    h, w = frame.shape[:2]
    scale = 640.0 / max(h, w)
    small = cv2.resize(frame, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    area = small.shape[0] * small.shape[1]
    gray = cv2.GaussianBlur(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY), (5, 5), 0)
    masks = []
    for lo, hi in ((40, 120), (20, 60), (70, 180)):
        edges = cv2.dilate(cv2.Canny(gray, lo, hi), np.ones((3, 3), np.uint8), iterations=2)
        masks.append(cv2.morphologyEx(edges, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8)))
    otsu = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]
    masks.append(cv2.morphologyEx(otsu, cv2.MORPH_CLOSE, np.ones((15, 15), np.uint8)))  # bright card on darker desk
    masks.append(cv2.morphologyEx(255 - otsu, cv2.MORPH_CLOSE, np.ones((15, 15), np.uint8)))
    hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)  # pale, low-colour surface = the card body
    pale = cv2.inRange(hsv, (0, 0, 140), (180, 70, 255))
    pale = cv2.morphologyEx(pale, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))
    masks.append(cv2.morphologyEx(pale, cv2.MORPH_CLOSE, np.ones((21, 21), np.uint8)))
    found = []
    for m in masks:
        contours, _ = cv2.findContours(m, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        for c in sorted(contours, key=cv2.contourArea, reverse=True)[:6]:
            if cv2.contourArea(c) < 0.08 * area:
                break
            q, hull_area = _quad_from_contour(c)
            if q is None or not _card_shaped(q, area):
                continue
            fit = hull_area / max(cv2.contourArea(q), 1.0)  # ~1.0 when the quad hugs the shape
            centre = q.mean(axis=0)
            if any(np.linalg.norm(centre - o[3]) < 0.06 * max(small.shape[:2]) and
                   0.75 < cv2.contourArea(q) / cv2.contourArea(o[2]) < 1.33 for o in found):
                continue
            found.append((abs(1.0 - fit), -cv2.contourArea(q), q, centre))
    found.sort(key=lambda t: (round(t[0], 1), t[1]))
    return [(t[2] / scale).astype("float32") for t in found[:limit]]


def find_card_quad(frame):
    quads = find_card_quads(frame, 1)
    return quads[0] if quads else None


_TEMPLATES = None


_CLAHE = cv2.createCLAHE(2.0, (8, 8))


def template_mask(h, w, margin=TEMPLATE_MASK_MARGIN):
    """255 where the pass looks the same on every card (header, logo, bus picture), 0 elsewhere.
    The blanked per-student areas are widened by margin so features on their artificial edges are left out."""
    mask = np.full((h, w), 255, np.uint8)
    for x0, y0, x1, y1 in BLANK_BOXES:
        mask[max(0, int(y0 * h) - margin):int(y1 * h) + margin, max(0, int(x0 * w) - margin):int(x1 * w) + margin] = 0
    mask[:, :int(0.04 * w) + margin] = 0  # dark photo border on the left
    mask[:int(0.015 * h) + margin, :] = 0
    mask[:, w - margin:] = 0  # the outer edge meets a different background in every frame
    mask[h - margin:, :] = 0
    return mask


def _load_templates():
    """SIFT features of the fixed printed graphics. SIFT is scale-invariant, so one template covers near and far cards."""
    global _TEMPLATES, _ECC_REF
    if _TEMPLATES is None:
        img = cv2.imread(str(TEMPLATE_PATH)) if TEMPLATE_PATH.exists() else None
        if img is None:
            _TEMPLATES = False
        else:
            h, w = img.shape[:2]
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            mask = template_mask(h, w)
            kp, des = cv2.SIFT_create(nfeatures=1500).detectAndCompute(_CLAHE.apply(gray), mask)
            _TEMPLATES = ((h, w), (np.float32([k.pt for k in kp]), des)) if des is not None and len(kp) >= 30 else False
            _HEADER_REF.clear()
            _HEADER_REF["gray"] = gray
            _ECC_REF = (cv2.resize(gray, (w // 4, h // 4), interpolation=cv2.INTER_AREA).astype(np.float32),
                        cv2.resize(mask, (w // 4, h // 4), interpolation=cv2.INTER_NEAREST))
    return _TEMPLATES or None


_ECC_REF = None
_HEADER_REF = {}  # template header detail at each card size (see frame_sharpness)


def _detail(gray):
    return float(cv2.Laplacian(cv2.GaussianBlur(gray, (0, 0), 1.0), cv2.CV_32F).var())


def frame_sharpness(flat, card_px):
    """Detail in the flattened card's printed header, relative to the template seen at the same size.
    A sharp frame gives roughly 0.3-0.5 and a motion-blurred one under 0.2; only compare values of similar frames.
    Returns None without a template."""
    tpl = _HEADER_REF.get("gray")
    if tpl is None:
        return None
    key = int(min(max(card_px, 100.0), tpl.shape[1]) // 50)
    if key not in _HEADER_REF:
        sc = (key * 50 + 25) / tpl.shape[1]
        small = cv2.resize(tpl, None, fx=min(sc, 1.0), fy=min(sc, 1.0), interpolation=cv2.INTER_AREA)
        back = cv2.resize(small, (tpl.shape[1], tpl.shape[0]), interpolation=cv2.INTER_CUBIC)
        _HEADER_REF[key] = max(_detail(crop_box(back, HEADER_BOX)), 1e-6)
    return _detail(crop_box(cv2.cvtColor(flat, cv2.COLOR_BGR2GRAY), HEADER_BOX)) / _HEADER_REF[key]


def glare_fraction(flat):
    """Share of the details area that is washed out (glare from a lamp or the sun): pixels well above
    the card's own background brightness, which is taken as the median of the area."""
    g = cv2.cvtColor(crop_box(flat, FIELDS_BOX), cv2.COLOR_BGR2GRAY)
    base = float(np.median(g))
    return float((g >= base + max(15.0, 0.4 * (255.0 - base))).mean())


def refine_flat(flat):
    """Sub-pixel alignment of a flattened card to the template (ECC on the fixed graphics).
    Makes frames line up well enough to average for far cards. Returns flat unchanged on failure."""
    if _ECC_REF is None:
        return flat
    tg, mk = _ECC_REF
    h, w = flat.shape[:2]
    cur = cv2.resize(cv2.cvtColor(flat, cv2.COLOR_BGR2GRAY), (tg.shape[1], tg.shape[0]), interpolation=cv2.INTER_AREA).astype(np.float32)
    warp = np.eye(3, dtype=np.float32)
    try:
        _, warp = cv2.findTransformECC(tg, cur, warp, cv2.MOTION_HOMOGRAPHY,
                                       (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 25, 1e-4), mk, 5)
    except cv2.error:
        return flat
    k = np.diag([0.25, 0.25, 1.0]).astype(np.float32)
    full = np.linalg.inv(k) @ warp @ k
    if abs(full[0, 2]) > 40 or abs(full[1, 2]) > 40:  # implausible correction: keep the original
        return flat
    return cv2.warpPerspective(flat, full, (w, h), flags=cv2.INTER_CUBIC + cv2.WARP_INVERSE_MAP, borderMode=cv2.BORDER_REPLICATE)


def _match_templates(frame, det, tpl):
    """Best (inliers, homography template -> frame resized by det) from SIFT matches, or None."""
    small = cv2.resize(frame, None, fx=det, fy=det,
                       interpolation=cv2.INTER_AREA if det < 1.0 else cv2.INTER_CUBIC) if abs(det - 1.0) > 1e-3 else frame
    gray = _CLAHE.apply(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY))
    kp, des = cv2.SIFT_create(nfeatures=4000).detectAndCompute(gray, None)
    tpts, tdes = tpl
    if des is None or len(kp) < MIN_INLIERS:
        return None
    pairs = cv2.FlannBasedMatcher(dict(algorithm=1, trees=4), dict(checks=48)).knnMatch(tdes, des, k=2)
    good = [m for m, n in (p for p in pairs if len(p) == 2) if m.distance < 0.8 * n.distance]
    if len(good) < MIN_INLIERS:
        return None
    src = tpts[[m.queryIdx for m in good]].reshape(-1, 1, 2)
    dst = np.float32([kp[m.trainIdx].pt for m in good]).reshape(-1, 1, 2)
    method = getattr(cv2, "USAC_MAGSAC", cv2.RANSAC)  # MAGSAC++ (OpenCV 4.5+): steadier fit than plain RANSAC
    hom, inl = cv2.findHomography(src, dst, method, 4.0, maxIters=4000, confidence=0.999)
    if hom is None or int(inl.sum()) < MIN_INLIERS:
        return None
    return int(inl.sum()), hom


def plausible_card(corners):
    """Corners (top-left, top-right, bottom-right, bottom-left) look like a real, upright, unmirrored pass."""
    c = corners.reshape(4, 2)
    if not cv2.isContourConvex(c.astype(np.float32).reshape(-1, 1, 2)):
        return False
    top, bottom = np.linalg.norm(c[1] - c[0]), np.linalg.norm(c[2] - c[3])
    left, right = np.linalg.norm(c[3] - c[0]), np.linalg.norm(c[2] - c[1])
    if min(top, bottom, left, right) < 20 or not 1.1 <= max(top, bottom) / max(left, right, 1.0) <= 2.4:
        return False
    if max(top, bottom) > 1.8 * min(top, bottom) or max(left, right) > 1.8 * min(left, right):
        return False  # more perspective than a hand-held card can show: a bad fit
    v1, v2 = c[1] - c[0], c[3] - c[0]
    return v1[0] * v2[1] - v1[1] * v2[0] > 0  # negative = mirror image, which a real card never gives


def locate_card(frame, deep=False, deep_first=False, refine=False):
    """Find the pass by its printed graphics (any rotation, tilt, distance or background).
    deep=True also searches large frames at full size, which finds a small (far) card.
    Returns Located(flat 1000x630 upright card, card width in px, inliers, found by the deep search) or None."""
    t = _load_templates()
    if t is None:
        return None
    (th, tw), tpl = t
    fh, fw = frame.shape[:2]
    base = min(1.0, MAX_DETECT_SIDE / float(max(fh, fw)))
    found = None
    # SIFT already searches a 2x enlarged image itself, so a deep pass only helps when the frame was shrunk.
    scales = [base] + ([min(1.0, base * 2.0)] if deep and base < 0.99 else [])
    if deep_first:
        scales.reverse()
    for det in scales:
        best = _match_templates(frame, det, tpl)
        if best is None:
            continue
        n, hom = best
        corners = cv2.perspectiveTransform(np.float32([[0, 0], [tw, 0], [tw, th], [0, th]]).reshape(-1, 1, 2), hom).reshape(4, 2)
        if not plausible_card(corners):
            continue
        top, bottom = np.linalg.norm(corners[1] - corners[0]), np.linalg.norm(corners[2] - corners[3])
        found = (float((top + bottom) / 2) / det, np.diag([1.0 / det, 1.0 / det, 1.0]) @ hom, n)
        break
    if found is None:
        return None
    card_px, to_frame, n = found
    flat = cv2.warpPerspective(frame, np.linalg.inv(to_frame), (tw, th), flags=cv2.INTER_CUBIC)
    if refine and card_px < STACK_PX:
        flat = refine_flat(flat)
    return Located(flat, card_px, n, det > base * 1.01)


def flatten_by_template(frame):
    loc = locate_card(frame, deep=True)
    return loc.flat if loc else None


def warp_card(frame, quad):
    """Flatten a tilted/rotated card into an upright landscape rectangle (long edge on top)."""
    centre = quad.mean(axis=0)
    order = np.argsort(np.arctan2(quad[:, 1] - centre[1], quad[:, 0] - centre[0]))
    pts = quad[order]  # consecutive corners around the card, no sum/diff ties at 45 degrees
    if np.linalg.norm(pts[2] - pts[1]) > np.linalg.norm(pts[1] - pts[0]):
        pts = np.roll(pts, -1, axis=0)  # make the first edge the long one
    out_w, out_h = 1000, 630
    dst = np.array([[0, 0], [out_w - 1, 0], [out_w - 1, out_h - 1], [0, out_h - 1]], dtype="float32")
    m = cv2.getPerspectiveTransform(pts.astype("float32"), dst)
    return cv2.warpPerspective(frame, m, (out_w, out_h), flags=cv2.INTER_CUBIC)


def prep_gray(img, target):
    """Upscale a grayscale crop (no thresholding: it hurts this card's small print)."""
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
    scale = min(3.0, max(1.0, target / g.shape[0]))
    g = cv2.resize(g, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    return cv2.copyMakeBorder(g, 15, 15, 15, 15, cv2.BORDER_CONSTANT, value=255)


def crop_box(img, box):
    h, w = img.shape[:2]
    x0, y0, x1, y1 = box
    return img[int(y0 * h):int(y1 * h), int(x0 * w):int(x1 * w)]


def ocr_text(img, psm):
    return pytesseract.image_to_string(img, config=f"--oem 3 --psm {psm}")


# ------------------------------------------------------------ OCR engines
_RAPID = None
_RAPID_LOCK = threading.Lock()


def get_rapid():
    global _RAPID
    if RapidOCR is None:
        return None
    with _RAPID_LOCK:
        if _RAPID is None:
            try:
                _RAPID = RapidOCR()
            except Exception:
                _RAPID = False
    return _RAPID or None


def engine_name():
    return "RapidOCR" if get_rapid() else "Tesseract"


def rapid_lines(img):
    """Text lines with positions, from RapidOCR: [{'text','x','y','h'}] in the image's own pixels."""
    res, _ = get_rapid()(img)
    out = []
    for box, text, _conf in (res or []):
        xs, ys = [pt[0] for pt in box], [pt[1] for pt in box]
        out.append({"text": text, "x": (min(xs) + max(xs)) / 2, "y": (min(ys) + max(ys)) / 2, "h": max(ys) - min(ys)})
    return out


def sharpen(img, amount=0.8, sigma=2.0):
    return cv2.addWeighted(img, 1.0 + amount, cv2.GaussianBlur(img, (0, 0), sigma), -amount, 0)


def read_flat(flat, quads=0, card_px=0.0, boost=False):
    """OCR an upright flattened pass. boost=True (far cards / stacked frames): upscale + sharpen first.
    Returns PassRead; read_details() reuses its lines so the details cost no extra OCR."""
    if get_rapid():
        img = crop_box(flat, FIELDS_BOX)
        if boost:
            img = sharpen(cv2.resize(img, None, fx=2.0, fy=2.0, interpolation=cv2.INTER_CUBIC), 0.8)
        lines = rapid_lines(img)
        text = "\n".join(l["text"] for l in sorted(lines, key=lambda l: (round(l["y"] / max(l["h"], 1)), l["x"])))
        shape = img.shape
    else:
        strip = crop_box(flat, STRIP_BOX)
        if boost:
            strip = sharpen(strip, 0.8)
        text, lines, shape = ocr_text(prep_gray(strip, 600), 6), None, strip.shape
    cand, known, exact = analyse_text(text)
    return PassRead(text, cand, known, exact, flat, quads, lines, shape, card_px)


def parse_details(r):
    """Name / destination / validity as printed on the pass (best effort)."""
    out = {"name": "", "destination": "", "validity": ""}
    text = r.text or ""
    dates = re.findall(r"\d{1,2}[-/][A-Za-z]{3}[-/]\d{2,4}", text)
    if len(dates) >= 2:  # issue date comes first; the later date is the validity date
        try:
            out["validity"] = max(dates, key=lambda d: datetime.strptime(d.replace("/", "-").title(), "%d-%b-%y"))
        except ValueError:
            out["validity"] = dates[-1]
    if r.lines:
        width = r.shape[1]
        values = sorted((l for l in r.lines if l["x"] > 0.42 * width), key=lambda l: l["y"])
        idx = next((i for i, l in enumerate(values)
                    if re.search(r"\d", l["text"]) and re.search(r"[A-Za-z]", l["text"])
                    and len(re.sub(r"\W", "", l["text"])) >= 8), None)
        if idx is not None:
            if idx + 2 < len(values):
                out["destination"] = " ".join(re.findall(r"[A-Za-z]{4,}", values[idx + 2]["text"]))
            above = [l for l in r.lines if l["y"] < values[idx]["y"] - 0.6 * max(values[idx]["h"], 1)
                     and len(re.findall(r"[A-Za-z]", l["text"])) >= 3 and "reg" not in l["text"].lower()]
            if above:
                out["name"] = " ".join(re.findall(r"[A-Za-z]+\.?", max(above, key=lambda l: l["y"])["text"]))
    return out


def read_details(r_or_flat, strip_text=""):
    """Details for display / name check. With a PassRead from RapidOCR this costs nothing extra;
    with Tesseract it OCRs the name and destination lines (two short reads)."""
    if isinstance(r_or_flat, PassRead):
        if r_or_flat.lines:
            return parse_details(r_or_flat)
        flat = r_or_flat.flat
        strip_text = r_or_flat.text
    else:
        flat = r_or_flat
    if flat is None:
        return {"name": "", "destination": "", "validity": ""}
    with ThreadPoolExecutor(max_workers=2) as pool:
        f_name = pool.submit(ocr_text, prep_gray(crop_box(flat, NAME_BOX), 160), 7)
        f_dest = pool.submit(ocr_text, prep_gray(crop_box(flat, DEST_BOX), 160), 7)
        name, dest = f_name.result(), f_dest.result()
    out = parse_details(PassRead(strip_text, None, False, False))
    out["name"] = " ".join(re.findall(r"[A-Za-z]+\.?", name)).strip()
    out["destination"] = " ".join(re.findall(r"[A-Za-z]{4,}", dest)).strip()
    return out


def read_pass(frame, fast=False, skip_if_no_card=False):
    """Read a pass in one frame (manual capture and tests). Returns the first PassRead holding a stored ID,
    else the best guess. fast=True is the quick path; skip_if_no_card returns at once when nothing pass-like is seen."""
    loc = locate_card(frame, deep=not skip_if_no_card)
    quads = [] if loc else find_card_quads(frame)
    if skip_if_no_card and loc is None and not quads:
        return PassRead("", None, False, False, None, 0)
    best = [PassRead("", None, False, False, None, len(quads)), -1.0]

    def keep(r):
        score = (2.0 if r.cand else 0.0) + min(len((r.text or "").strip()), 400) / 400.0
        if score > best[1]:
            best[0], best[1] = r, score
        return r if r.known else None

    if loc is not None:
        r = keep(read_flat(loc.flat, 0, loc.card_px, boost=loc.card_px < NEAR_PX))
        if r or fast:
            return r or best[0]
    for q in quads[:2]:
        flat = warp_card(frame, q)
        for f in (flat, cv2.rotate(flat, cv2.ROTATE_180)):
            r = keep(read_flat(f, len(quads)))
            if r:
                return r
    # whole-frame Tesseract read (no pass outline found, or it was off)
    def whole(view):
        text = ocr_text(view, 6)
        cand, known, exact = analyse_text(text)
        return keep(PassRead(text, cand, known, exact, None, len(quads)))

    if whole(prep_gray(frame, 1100)):
        return best[0]
    if fast:
        return best[0]
    if whole(preprocess_for_ocr(frame)):
        return best[0]
    for angle in (-10, 10, -20, 20, -30, 30, -45, 45, 90, -90, 180):  # turned card, no outline found
        img = rotate_bound(frame, angle)
        for view in (prep_gray(img, 1100), preprocess_for_ocr(img)):
            if whole(view):
                return best[0]
    return best[0]


def configured_code_candidates(text, year_codes, branch_codes, prefixes=()):
    """Extract tokens shaped [prefix]+year+branch+suffix, with codes loaded from SQLite."""
    years = sorted((x.upper() for x in year_codes), key=len, reverse=True)
    branches = sorted((x.upper() for x in branch_codes), key=len, reverse=True)
    if not years or not branches:
        return []
    year_part = "|".join(re.escape(x) for x in years)
    branch_part = "|".join(re.escape(x) for x in branches)
    prefix_part = "|".join(re.escape(x.upper()) for x in sorted(prefixes, key=len, reverse=True) if x)
    prefix_re = f"(?:{prefix_part})?" if prefix_part else ""
    pattern = rf"(?<![A-Z0-9]){prefix_re}(?:{year_part})(?:{branch_part})[A-Z0-9]{{1,12}}(?![A-Z0-9])"
    return re.findall(pattern, text.upper())


# Common OCR look-alikes. Used only to find a record that exists; the lookup stays exact
# and a human still confirms visually.
CONFUSABLE = {"A": "4", "4": "A", "I": "1", "1": "I", "O": "0", "0": "O",
              "S": "5", "5": "S", "B": "8", "8": "B", "Z": "2", "2": "Z",
              "M": "N", "N": "M"}


def _swap_hits(candidate, known, max_positions=10):
    """Stored IDs reachable from candidate by look-alike swaps (including no swap)."""
    if candidate in known:
        return {candidate}
    pos = [i for i, ch in enumerate(candidate) if ch in CONFUSABLE][:max_positions]
    hits = set()
    for mask in range(1, 1 << len(pos)):
        chars = list(candidate)
        for bit, i in enumerate(pos):
            if (mask >> bit) & 1:
                chars[i] = CONFUSABLE[chars[i]]
        variant = "".join(chars)
        if variant in known:
            hits.add(variant)
    return hits


def resolve_known(candidate, known):
    """Return the single stored ID reachable from candidate, else None.
    Tries look-alike swaps first, then one stray extra character (OCR insertion),
    then one missing character (OCR dropped a thin letter such as I)."""
    hits = _swap_hits(candidate, known)
    if not hits and len(candidate) > 5:
        lengths = {len(k) for k in known}
        if (len(candidate) - 1) in lengths:
            for i in range(len(candidate)):
                hits |= _swap_hits(candidate[:i] + candidate[i + 1:], known)
        if not hits and (len(candidate) + 1) in lengths:
            hits = {k for k in known if len(k) == len(candidate) + 1
                    and any(k[:i] + k[i + 1:] == candidate for i in range(len(k)))}
    return next(iter(hits)) if len(hits) == 1 else None


def extract_candidate(text):
    """Return an OCR guess (UPPERCASE) only; caller must do the exact SQLite lookup."""
    return analyse_text(text)[0]


def analyse_text(text):
    """Return (candidate, is_stored_id, read_exactly). Still only a guess: the lookup stays exact."""
    normalized = text.upper()
    with db() as con:
        years = [r[0] for r in con.execute("SELECT code FROM admission_year_codes")]
        branches = [r[0] for r in con.execute("SELECT code FROM branch_codes")]
        known = {r[0].upper() for r in con.execute("SELECT usn FROM students")}
        row = con.execute("SELECT setting_value FROM app_settings WHERE setting_key='id_prefixes'").fetchone()
    prefixes = [x.strip().upper() for x in (row[0] if row else "").split(",") if x.strip()]
    def candidates_in(norm):
        # Generic tokens must contain a digit, so words like MANGALORE are never offered.
        tokens = [t for t in re.findall(r"(?<![A-Z0-9])[A-Z0-9]{5,20}(?![A-Z0-9])", norm) if re.search(r"\d", t)]
        labelled = re.findall(r"REG\W{0,3}NO\W{0,3}([A-Z0-9]{5,20})", norm)
        return labelled + configured_code_candidates(norm, years, branches, prefixes) + tokens

    candidates = list(dict.fromkeys(candidates_in(normalized)))
    for c in candidates:
        resolved = resolve_known(c, known)
        if resolved:
            return resolved, True, resolved == c
    # OCR often reads a thin I inside an ID as / | \ ! or l, which splits the ID in two (4MT24A/054).
    # Read those as I and try again; such a match is never "exact", so it needs two reads in a row.
    joined = re.sub(r"(?<=[A-Z0-9])[/|\\!l](?=[A-Z0-9])", "I", text).upper()
    if joined != normalized:
        for c in dict.fromkeys(candidates_in(joined)):
            resolved = resolve_known(c, known)
            if resolved:
                return resolved, True, False
    return (candidates[0] if candidates else None), False, False


# ------------------------------------------------------ rules + auto-scan
def evaluate_student(con, guess, bus, session, now):
    """Exact SQLite lookup plus eligibility rules.
    Returns (outcome, status_text, student); student is set only if eligible."""
    if not guess:
        return "NO_ID", "NO CANDIDATE FOUND\nNo attendance recorded. Check the pass manually.", None
    rows = con.execute("SELECT usn, name, bus_number, valid_until, active FROM students "
                       "WHERE UPPER(usn)=UPPER(?)", (guess,)).fetchall()
    if not rows:
        return "UNMATCHED", f"UNMATCHED ID — not present in SQLite.\nOCR candidate: {guess}\nNo attendance recorded.", None
    if len(rows) > 1:
        return "AMBIGUOUS", f"AMBIGUOUS — multiple records differ only by letter case.\nOCR candidate: {guess}\nNo attendance recorded.", None
    usn, name, assigned_bus, valid_until, active = rows[0]
    try:
        expired = date.fromisoformat(valid_until) < now.date()
    except ValueError:
        expired = None
    if not active:
        return "INACTIVE", f"REJECTED — inactive pass\nName: {name}\nID: {usn}", None
    if expired is None:
        return "BAD_DATE", f"REJECTED — invalid valid_until in record\nName: {name}\nID: {usn}", None
    if expired:
        return "EXPIRED", f"REJECTED — expired pass\nName: {name}\nValid until: {valid_until}", None
    if assigned_bus != bus:
        return "WRONG_BUS", f"WRONG BUS — no attendance recorded\nName: {name}\nAssigned: {assigned_bus}\nSelected: {bus}", None
    status = ("EXACT DATABASE MATCH — visually compare the pass before confirming.\n"
              f"Name: {name}\nID: {usn}\nBus: {assigned_bus}\nSession: {session}\n"
              "No attendance has been recorded yet.")
    return "EXACT_MATCH_PENDING_CONFIRMATION", status, (usn, name, assigned_bus)


def log_scan(guess, text, outcome):
    with db() as con:
        con.execute("INSERT INTO scans(usn, scanned_at, ocr_text, result) VALUES (?, ?, ?, ?)",
                    (guess, datetime.now().isoformat(timespec="seconds"), text, outcome))


def auto_process(guess, text, bus, session, details=None):
    """Auto mode: exact match + rules, duplicate check, then append attendance. Returns (outcome, status)."""
    now = datetime.now()
    with db() as con:
        outcome, status, student = evaluate_student(con, guess, bus, session, now)
        if student:
            usn, name, assigned_bus = student
            dup = con.execute("SELECT recorded_at FROM attendance WHERE usn=? AND session=? "
                              "AND substr(recorded_at,1,10)=?", (usn, session, now.strftime("%Y-%m-%d"))).fetchone()
            if dup:
                outcome = "DUPLICATE"
                status = (f"ALREADY SCANNED — not recorded again\nName: {name}\nID: {usn}\n"
                          f"{session} already recorded at {dup[0][11:16]}")
            else:
                con.execute("INSERT INTO attendance(usn, student_name, bus_number, session, recorded_at, source) "
                            "VALUES (?, ?, ?, ?, ?, 'auto_scan')",
                            (usn, name, assigned_bus, session, now.isoformat(timespec="seconds")))
                outcome = "ATTENDANCE_CONFIRMED"
                status = f"ATTENDANCE RECORDED\nName: {name}\nID: {usn}\nBus: {assigned_bus}\nSession: {session}"
        con.execute("INSERT INTO scans(usn, scanned_at, ocr_text, result) VALUES (?, ?, ?, ?)",
                    (guess, now.isoformat(timespec="seconds"), text, outcome))
    if details and any(details.values()):
        status += (f"\n\nRead from pass: {details.get('name') or '?'} | "
                   f"{details.get('destination') or '?'} | valid {details.get('validity') or '?'}")
    return outcome, status


def name_matches(name, text):
    """True if a word of the stored name appears (fuzzily) in the OCR text."""
    words = re.findall(r"[A-Z]{3,}", name.upper())
    seen = set(re.findall(r"[A-Z]{3,}", text.upper()))
    return any(difflib.SequenceMatcher(None, w, o).ratio() >= 0.75 for w in words for o in seen)


class AutoScanner:
    """Continuous scanner (no Tk). Pushes events: idle, reading, status, hint, result, retry, error, fatal."""

    def __init__(self, get_frame, get_context, events):
        self.get_frame, self.get_context, self.events = get_frame, get_context, events

    def name_ok(self, usn, name_text, full_text=""):
        with db() as con:
            req = con.execute("SELECT setting_value FROM app_settings WHERE setting_key='require_name_match'").fetchone()
            if not req or req[0] != "1":
                return True
            row = con.execute("SELECT name FROM students WHERE UPPER(usn)=UPPER(?)", (usn,)).fetchone()
        return bool(row) and (name_matches(row[0], name_text) or name_matches(row[0], full_text))

    @staticmethod
    def save_debug(frame, r):
        try:
            DEBUG_DIR.mkdir(exist_ok=True)
            cv2.imwrite(str(DEBUG_DIR / "last_unreadable_frame.jpg"), frame)
            if r is not None and r.flat is not None:
                cv2.imwrite(str(DEBUG_DIR / "last_unreadable_card.jpg"), r.flat)
        except Exception:
            pass

    @staticmethod
    def stacked(stack, dark=False):
        """Align the sharpest recent frames to the best one (sub-pixel shift) and average them:
        cuts noise and adds detail on small print. dark=True averages only the darker half of the values at each
        point of the details area: glare is bright and moves between frames, so this removes it."""
        best_i = max(range(len(stack)), key=lambda i: stack[i][1])
        ref = stack[best_i][0]
        gray = lambda im: cv2.cvtColor(crop_box(im, FIELDS_BOX), cv2.COLOR_BGR2GRAY).astype(np.float32)
        ref_g = gray(ref)
        window = cv2.createHanningWindow((ref_g.shape[1], ref_g.shape[0]), cv2.CV_32F)
        acc = [ref.astype(np.float32)]
        h, w = ref.shape[:2]
        for i, (f, sc, *_rest) in enumerate(stack):
            if i == best_i or sc < 0.6 * stack[best_i][1]:
                continue
            (dx, dy), resp = cv2.phaseCorrelate(ref_g, gray(f), window)
            if abs(dx) > 25 or abs(dy) > 25 or resp < 0.03:
                continue  # not the same view of the card: leave it out
            m = np.float32([[1, 0, -dx], [0, 1, -dy]])
            acc.append(cv2.warpAffine(f, m, (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE).astype(np.float32))
        out = np.mean(acc, axis=0)
        if dark and len(acc) >= 2:
            x0, y0, x1, y1 = FIELDS_BOX
            box = (slice(int(y0 * h), int(y1 * h)), slice(int(x0 * w), int(x1 * w)))
            part = np.sort(np.stack([a[box] for a in acc]), axis=0)[:(len(acc) + 1) // 2]
            out[box] = part.mean(axis=0)
        return out.astype(np.uint8), len(acc)

    def run(self, keep_going):
        last_id, streak, fails = None, 0, 0
        locked, last_seen = None, 0.0
        prev_small, hot_until, last_probe, last_deep, last_fb = None, 0.0, 0.0, 0.0, 0.0
        stack, deep_first = [], False
        recent, best_q, sharp_ref, soft_since = [], 0.0, START_SHARP_REF, None  # sharp_ref: sharpness of frames that read well
        engine = engine_name()
        while keep_going():
            frame = self.get_frame()
            if frame is None:
                time.sleep(0.2)
                continue
            frame = frame.copy()
            now_t = time.time()
            small = cv2.resize(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), (160, 90))
            if prev_small is not None and float(cv2.absdiff(small, prev_small).mean()) > 4.0:
                hot_until = now_t + 3.0
            prev_small = small
            hot = now_t < hot_until
            if not hot and now_t - last_probe < 0.6:  # idle: look for a pass a couple of times a second
                time.sleep(0.05)
                continue
            last_probe = t0 = now_t
            deep = hot or now_t - last_deep > 3.0
            if deep:
                last_deep = now_t
            try:
                loc = locate_card(frame, deep=deep, deep_first=deep_first, refine=True)
                r, card_px, filling, soft, q = None, 0.0, False, None, None
                if loc is not None:
                    deep_first = loc.deep
                    card_px = loc.card_px
                    hot_until = max(hot_until, now_t + 3.0)
                    if card_px < MIN_PX:
                        r = PassRead("", None, False, False, loc.flat, 0, None, None, card_px)
                    else:
                        q = frame_sharpness(loc.flat, card_px)
                        if q is not None:
                            best_q = max(best_q, q)
                        glare = glare_fraction(loc.flat) > GLARE_FRACTION
                        waiting = PassRead("", None, False, False, loc.flat, 0, None, None, card_px)
                        sharp = float(cv2.Laplacian(cv2.cvtColor(crop_box(loc.flat, STRIP_BOX), cv2.COLOR_BGR2GRAY), cv2.CV_64F).var())
                        if q is not None and q < BLUR_RATIO * max(best_q, sharp_ref):
                            r, soft = waiting, "steady"  # smeared by movement: wait for a sharper frame
                        elif card_px < STACK_PX:  # small: stack aligned frames and read their average
                            stack = (stack + [(loc.flat, sharp, glare)])[-STACK_SIZE:]
                            n = len(stack)
                            filling = n < STACK_SIZE  # a failed read is not final until the stack is full
                            if n == 1 and card_px >= NEAR_PX:
                                r = read_flat(loc.flat, 0, card_px)  # middle distance: one frame is often enough
                            elif n >= STACK_SIZE or n in STACK_READ_AT or last_id:  # last_id: confirm a first read at once
                                avg, _n = self.stacked(stack, dark=any(e[2] for e in stack))
                                r = read_flat(avg, 0, card_px, boost=True)
                            else:
                                r = waiting
                        else:  # near: one frame is enough unless glare hides the text
                            stack = []
                            recent = (recent + [(loc.flat, sharp)])[-RECENT_SIZE:]
                            r = read_flat(loc.flat, 0, card_px)
                            if not r.known and glare and len(recent) >= 2:
                                combined, _n = self.stacked(recent, dark=True)
                                r2 = read_flat(combined, 0, card_px)
                                if r2.known:
                                    r = r2
                        if glare and not r.known and not filling:
                            soft = soft or "glare"
                elif hot and now_t - last_fb > 1.0:  # logo hidden or blurred: try plain outline detection
                    last_fb, stack = now_t, []
                    quads = find_card_quads(frame)
                    for q in quads[:2]:
                        flat = warp_card(frame, q)
                        for f in (flat, cv2.rotate(flat, cv2.ROTATE_180)):
                            cand_r = read_flat(f, len(quads))
                            if r is None or cand_r.known:
                                r = cand_r
                            if cand_r.known:
                                break
                        if r is not None and r.known:
                            break
                    if r is not None:
                        card_px = NEAR_PX
            except pytesseract.pytesseract.TesseractNotFoundError:
                self.events.put(("fatal", "Tesseract not found. Install it and set its path near the top of app.py."))
                return
            except Exception as exc:
                self.events.put(("error", str(exc)))
                time.sleep(1)
                continue
            present = r is not None
            if present:
                last_seen = now_t
                self.events.put(("reading",))
            else:  # a small card is missed in some frames: keep its stack until the card is really gone
                if now_t - last_seen > CARD_GONE_SECONDS:
                    locked, last_id, streak, fails, deep_first, stack = None, None, 0, 0, False, []
                    recent, best_q, soft_since = [], 0.0, None
                self.events.put(("idle",))
            dist = "" if not present or not card_px else (
                " (too far)" if card_px < MIN_PX else " (far: hold steady, %d frames)" % len(stack) if card_px < STACK_PX and stack else "")
            self.events.put(("status", f"card in view: {'yes, %d px wide%s' % (card_px, dist) if present else 'no'}"
                                       f"   |   engine: {engine}   |   last read: {(r.cand if r else None) or '-'}"
                                       f"   |   {time.time() - t0:.1f} s/frame"))
            if not present:
                time.sleep(0.05)
                continue
            if card_px < MIN_PX:
                self.events.put(("hint", "far"))
                time.sleep(0.1)
                continue
            if soft and not r.known:  # blur or glare is not a failed scan, unless it goes on too long
                soft_since = soft_since or now_t
                if now_t - soft_since < SOFT_FAIL_SECONDS:
                    self.events.put(("hint", soft))
                    continue
                fails, soft_since, best_q, sharp_ref = 99, None, 0.0, 0.7 * sharp_ref
            elif filling and not r.known:  # more frames are coming: no failure yet
                self.events.put(("hint", "steady"))
                continue
            else:
                soft_since = None
            failed = not r.known
            if r.known:
                fails = 0
                if q is not None:
                    sharp_ref = 0.7 * sharp_ref + 0.3 * q
                if r.cand == locked:  # same card still held after it was handled
                    time.sleep(0.1)
                    continue
                streak = streak + 1 if r.cand == last_id else 1
                last_id = r.cand
                # An exactly-read stored ID needs one read; a look-alike-corrected one needs two.
                if streak >= (1 if r.exact else AUTO_CONFIRM_READS):
                    details = read_details(r)
                    if self.name_ok(r.cand, details.get("name", ""), r.text):
                        bus, session = self.get_context()
                        outcome, status = auto_process(r.cand, r.text, bus, session, details)
                        self.events.put(("result", outcome, status, r.text))
                        locked, last_id, streak, stack, recent = r.cand, None, 0, [], []
                    else:
                        failed = True
            if failed:
                streak, last_id = 0, None
                fails += 1
                if fails >= (AUTO_FAIL_LIMIT + 1 if card_px < NEAR_PX else AUTO_FAIL_LIMIT):
                    log_scan(r.cand, r.text, "UNREADABLE")
                    self.save_debug(frame, r)
                    self.events.put(("retry", r.cand, r.text, "far" if card_px < NEAR_PX else None))
                    fails, stack, recent = 0, [], []
            time.sleep(0.01)


def beep(kind):
    try:
        import winsound
    except ImportError:
        return
    try:
        if kind == "ok":
            winsound.Beep(1200, 150)
        elif kind == "alert":
            winsound.Beep(500, 250)
            winsound.Beep(500, 250)
        else:
            winsound.Beep(700, 200)
    except RuntimeError:
        pass


# --------------------------------------------------------------------- GUI
def configure_camera(cap):
    """Ask for the highest resolution the webcam really delivers (text must be big enough to read far away).
    Steps down if a mode is unsupported or too slow. Returns (width, height)."""
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)  # always the freshest frame
    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))  # needed for HD at a usable frame rate
    try:
        cap.set(cv2.CAP_PROP_AUTOFOCUS, 1)
    except Exception:
        pass
    size = (0, 0)
    for w, h in ((3840, 2160), (2560, 1440), (1920, 1080), (1280, 720)):
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, w)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, h)
        ok = False
        t = time.time()
        for _ in range(4):  # the first frames after a mode change can be stale or black
            ok, frame = cap.read()
        per_frame = (time.time() - t) / 4
        if ok and frame is not None and frame.shape[1] >= 0.95 * w and per_frame < 0.2:
            return frame.shape[1], frame.shape[0]
        if ok and frame is not None:
            size = (frame.shape[1], frame.shape[0])
    return size


class OCRPrototype:
    def __init__(self, root):
        self.root = root
        self.root.title("SmartBus Pass Attendance — Demo")
        self.root.geometry("1040x720")
        self.root.minsize(850, 620)
        self.cap = None
        self.running = False
        self.latest_frame = None
        self.pending_student = None
        self.pending_session = None
        self.pending_scan_id = None
        self.pin_fails = 0
        self.locked_until = 0.0
        self.ocr_busy = False
        self.ocr_result = None
        self.auto_on = False
        self.auto_gen = 0
        self.events = queue.Queue()
        self.banner_until = 0.0
        self.cur_bus = ""
        self.cur_session = "Pickup"
        self.cam_size = (0, 0)

        header = ttk.Frame(root, padding=12)
        header.pack(fill="x")
        ttk.Label(header, text="SmartBus Pass Attendance — OCR Test",
                  font=("Segoe UI", 17, "bold")).pack(anchor="w")
        ttk.Label(header, text="Fictional demo data. Verify the pass and keep the bus parked while testing.").pack(anchor="w", pady=(4, 0))

        self.banner = tk.Label(root, text="Auto-scan is off — click Start auto-scan", font=("Segoe UI", 20, "bold"),
                               bg="#555555", fg="white", pady=10)
        self.banner.pack(fill="x", padx=12, pady=(0, 2))
        self.status_lbl = ttk.Label(root, text="", foreground="#666666")
        self.status_lbl.pack(anchor="w", padx=14, pady=(0, 6))

        controls = ttk.Frame(root, padding=(12, 0, 12, 10))
        controls.pack(fill="x")
        ttk.Label(controls, text="Selected bus:").pack(side="left")
        buses = self.get_buses()
        self.bus_var = tk.StringVar(value=buses[0])
        self.bus_combo = ttk.Combobox(controls, textvariable=self.bus_var, values=buses,
                                      state="readonly", width=16)
        self.bus_combo.pack(side="left", padx=6)
        ttk.Label(controls, text="Session:").pack(side="left", padx=(12, 0))
        self.session_var = tk.StringVar(value="Pickup")
        ttk.Combobox(controls, textvariable=self.session_var,
                     values=["Pickup", "Drop-off"], state="readonly", width=12).pack(side="left", padx=6)
        self.start_btn = ttk.Button(controls, text="Start webcam", command=self.start_camera)
        self.start_btn.pack(side="left", padx=(18, 5))
        self.stop_btn = ttk.Button(controls, text="Stop webcam", command=self.stop_camera, state="disabled")
        self.stop_btn.pack(side="left", padx=5)
        self.auto_btn = ttk.Button(controls, text="Start auto-scan", command=self.toggle_auto)
        self.auto_btn.pack(side="left", padx=(8, 4))
        self.ocr_btn = ttk.Button(controls, text="Capture + run OCR", command=self.run_ocr)
        self.ocr_btn.pack(side="left", padx=4)
        ttk.Button(controls, text="Driver/admin settings", command=self.open_settings).pack(side="right")

        body = ttk.Frame(root, padding=(12, 0, 12, 12))
        body.pack(fill="both", expand=True)
        left = ttk.Frame(body)
        left.pack(side="left", fill="both", expand=True)
        self.video = ttk.Label(left, text="Webcam stopped", anchor="center", relief="groove")
        self.video.pack(fill="both", expand=True)
        ttk.Label(left, text="Fill the frame with the pass, avoid glare, and hold it still.").pack(anchor="w", pady=(6, 0))

        right = ttk.Frame(body, width=370)
        right.pack(side="right", fill="both", padx=(12, 0))
        ttk.Label(right, text="OCR text", font=("Segoe UI", 11, "bold")).pack(anchor="w")
        self.ocr_box = tk.Text(right, height=10, width=44, wrap="word")
        self.ocr_box.pack(fill="x", pady=(4, 10))
        ttk.Label(right, text="Exact database check", font=("Segoe UI", 11, "bold")).pack(anchor="w")
        self.result_box = tk.Text(right, height=12, width=44, wrap="word")
        self.result_box.pack(fill="x", pady=(4, 8))
        self.accept_btn = ttk.Button(right, text="Confirm attendance (after visual check)",
                                     command=self.accept_attendance, state="disabled")
        self.accept_btn.pack(fill="x", pady=(0, 8))
        ttk.Label(right, text="OCR only suggests an ID. Attendance needs an exact active student match, correct bus, and your confirmation.", wraplength=350).pack(anchor="w")
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        # Changing bus/session after a scan invalidates the pending confirmation.
        self.bus_var.trace_add("write", self.clear_pending)
        self.session_var.trace_add("write", self.clear_pending)
        self.bus_var.trace_add("write", self._sync_context)
        self.session_var.trace_add("write", self._sync_context)
        self._sync_context()
        threading.Thread(target=lambda: (_load_templates(), get_rapid()), daemon=True).start()  # warm up

    def _sync_context(self, *_):
        self.cur_bus = self.bus_var.get().strip()
        self.cur_session = self.session_var.get()

    # ---------------------------------------------------------- auto-scan
    def set_banner(self, text, colour, hold=0.0):
        self.banner.configure(text=text, bg=colour)
        self.banner_until = time.time() + hold

    def toggle_auto(self):
        if self.auto_on:
            self.stop_auto()
            return
        if not self.running:
            self.start_camera()
        if not self.running:
            return
        self.clear_pending()
        self.auto_on = True
        self.auto_gen += 1
        gen = self.auto_gen
        self.auto_btn.configure(text="Stop auto-scan")
        self.ocr_btn.configure(state="disabled")
        self.set_banner("Show your bus pass to the camera", "#555555")
        scanner = AutoScanner(lambda: self.latest_frame, lambda: (self.cur_bus, self.cur_session), self.events)
        threading.Thread(target=scanner.run, args=(lambda: self.auto_on and gen == self.auto_gen,), daemon=True).start()
        self.root.after(150, self._poll_events)

    def stop_auto(self):
        self.auto_on = False
        self.auto_btn.configure(text="Start auto-scan")
        self.ocr_btn.configure(state="normal")
        self.set_banner("Auto-scan is off — click Start auto-scan", "#555555")
        self.status_lbl.configure(text="")

    def _poll_events(self):
        try:
            while True:
                self._handle_event(self.events.get_nowait())
        except queue.Empty:
            pass
        if self.auto_on:
            self.root.after(150, self._poll_events)

    def _handle_event(self, ev):
        if not self.auto_on:
            return
        kind = ev[0]
        holding = time.time() < self.banner_until
        if kind == "idle":
            if not holding:
                self.set_banner("Show your bus pass to the camera", "#555555")
        elif kind == "reading":
            if not holding:
                self.set_banner("Reading pass… hold it steady", "#1f5fa8")
        elif kind == "result":
            _, outcome, status, text = ev
            titles = {
                "ATTENDANCE_CONFIRMED": ("ATTENDANCE RECORDED", "#1b7f3b", "ok"),
                "DUPLICATE": ("ALREADY SCANNED — NOT RECORDED AGAIN", "#c0392b", "alert"),
                "WRONG_BUS": ("WRONG BUS", "#c0392b", "alert"),
                "EXPIRED": ("PASS EXPIRED", "#c0392b", "alert"),
                "INACTIVE": ("PASS INACTIVE", "#c0392b", "alert"),
            }
            title, colour, sound = titles.get(outcome, ("PLEASE SCAN AGAIN", "#d98200", "retry"))
            self.set_banner(title, colour, hold=4.0)
            self.ocr_box.delete("1.0", "end")
            self.ocr_box.insert("1.0", text.strip())
            self.result_box.delete("1.0", "end")
            self.result_box.insert("1.0", status)
            beep(sound)
        elif kind == "hint":
            if not holding:
                if ev[1] == "far":
                    self.set_banner("MOVE THE PASS CLOSER", "#d98200")
                elif ev[1] == "glare":
                    self.set_banner("GLARE ON THE PASS — TILT IT SLIGHTLY", "#d98200")
                else:
                    self.set_banner("Reading pass… hold it steady", "#1f5fa8")
        elif kind == "retry":
            far = len(ev) > 3 and ev[3] == "far"
            self.set_banner("TOO FAR — HOLD THE PASS CLOSER" if far else "COULD NOT READ — PLEASE SCAN AGAIN",
                            "#d98200", hold=3.0)
            self.ocr_box.delete("1.0", "end")
            self.ocr_box.insert("1.0", ev[2].strip() or "(No text detected)")
            self.result_box.delete("1.0", "end")
            self.result_box.insert("1.0", "UNREADABLE — no attendance recorded.\nHold the whole pass flat in view, avoid glare, and scan again.")
            beep("retry")
        elif kind == "status":
            self.status_lbl.configure(text=ev[1])
        elif kind == "fatal":
            self.stop_auto()
            messagebox.showerror("Auto-scan stopped", ev[1])
        elif kind == "error":
            self.set_banner("Scan error — retrying", "#d98200", hold=1.0)

    def clear_pending(self, *_):
        self.pending_student = None
        self.pending_scan_id = None
        self.accept_btn.configure(state="disabled")

    def get_buses(self):
        with db() as con:
            buses = [r[0] for r in con.execute("SELECT DISTINCT bus_number FROM students ORDER BY bus_number")]
        return buses or ["MITE-BUS-01"]

    def start_camera(self):
        if self.running:
            return
        self.cap = cv2.VideoCapture(0, cv2.CAP_DSHOW)
        if not self.cap.isOpened():
            self.cap.release()
            self.cap = cv2.VideoCapture(0)
        if not self.cap.isOpened():
            self.cap.release()
            self.cap = None
            messagebox.showerror("Webcam error", "Could not open webcam. Check Windows camera permissions and close other camera apps.")
            return
        self.cam_size = configure_camera(self.cap)
        self.root.title(f"SmartBus Pass Attendance — Demo   (camera {self.cam_size[0]}x{self.cam_size[1]}, OCR: {engine_name()})")
        self.running = True
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.update_frame()

    def update_frame(self):
        if not self.running or self.cap is None:
            return
        ok, frame = self.cap.read()
        if ok:
            self.latest_frame = frame
            scale = 620.0 / max(frame.shape[1], 1)
            preview = cv2.resize(frame, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA) if scale < 1 else frame
            image = Image.fromarray(cv2.cvtColor(preview, cv2.COLOR_BGR2RGB))
            image.thumbnail((620, 480))
            photo = ImageTk.PhotoImage(image)
            self.video.configure(image=photo, text="")
            self.video.image = photo
        self.root.after(30, self.update_frame)

    def run_ocr(self):
        if self.latest_frame is None:
            messagebox.showinfo("Start webcam", "Click Start webcam first.")
            return
        if self.ocr_busy:
            return
        self.clear_pending()
        self.ocr_busy = True
        self.ocr_result = None
        self.ocr_btn.configure(state="disabled")
        self.result_box.delete("1.0", "end")
        self.result_box.insert("1.0", "Reading pass... trying several angles (up to ~10 s). Hold it still.")
        threading.Thread(target=self._ocr_worker, args=(self.latest_frame.copy(),), daemon=True).start()
        self.root.after(100, self._poll_ocr)

    def _ocr_worker(self, frame):
        try:
            r = read_pass(frame)
            self.ocr_result = ("ok", r.text, r.cand, r.known)
        except pytesseract.pytesseract.TesseractNotFoundError:
            self.ocr_result = ("notfound",)
        except Exception as exc:
            self.ocr_result = ("error", str(exc))

    def _poll_ocr(self):
        if self.ocr_result is None:
            self.root.after(100, self._poll_ocr)
            return
        result, self.ocr_result = self.ocr_result, None
        self.ocr_busy = False
        self.ocr_btn.configure(state="normal")
        if result[0] == "notfound":
            messagebox.showerror("Tesseract not found", "Install Tesseract OCR and set its path near the top of app.py if needed.")
            return
        if result[0] == "error":
            messagebox.showerror("OCR error", f"Could not read the image: {result[1]}")
            return
        _, text, guess, _known = result
        self.ocr_box.delete("1.0", "end")
        self.ocr_box.insert("1.0", text.strip() or "(No text detected. Try better lighting or closer framing.)")
        self.lookup_student(guess, text)

    def lookup_student(self, guess, raw_text):
        now = datetime.now()
        with db() as con:
            outcome, status, pending = evaluate_student(con, guess, self.bus_var.get().strip(), self.session_var.get(), now)
            cur = con.execute("INSERT INTO scans(usn, scanned_at, ocr_text, result) VALUES (?, ?, ?, ?)",
                              (guess, now.isoformat(timespec="seconds"), raw_text, outcome))
            scan_id = cur.lastrowid
        if pending:
            self.pending_student = pending
            self.pending_session = self.session_var.get()
            self.pending_scan_id = scan_id
            self.accept_btn.configure(state="normal")
        self.result_box.delete("1.0", "end")
        self.result_box.insert("1.0", status)

    def accept_attendance(self):
        if not self.pending_student:
            return
        usn, name, bus = self.pending_student
        session, scan_id = self.pending_session, self.pending_scan_id
        if not messagebox.askyesno("Confirm attendance", f"You visually checked the pass for {name} ({usn})?\n\nRecord {session} on {bus}?"):
            return
        now = datetime.now()
        with db() as con:
            # Re-check the exact record at confirmation time in case it changed after OCR.
            cur = con.execute("SELECT name, bus_number, valid_until, active FROM students WHERE usn=?", (usn,)).fetchone()
            try:
                still_ok = bool(cur) and cur[3] and cur[1] == bus and date.fromisoformat(cur[2]) >= now.date()
            except ValueError:
                still_ok = False
            if not still_ok:
                self.clear_pending()
                messagebox.showwarning("Student record changed", "The record is no longer eligible. Run the scan again.")
                return
            dup = con.execute("SELECT 1 FROM attendance WHERE usn=? AND session=? AND substr(recorded_at,1,10)=?",
                              (usn, session, now.strftime("%Y-%m-%d"))).fetchone()
            if dup:
                self.clear_pending()
                messagebox.showinfo("Already recorded", f"{name} already has a {session} record today.")
                return
            con.execute("INSERT INTO attendance(usn, student_name, bus_number, session, recorded_at) VALUES (?, ?, ?, ?, ?)",
                        (usn, cur[0], bus, session, now.isoformat(timespec="seconds")))
            con.execute("UPDATE scans SET result='ATTENDANCE_CONFIRMED' WHERE id=?", (scan_id,))
        self.clear_pending()
        self.result_box.insert("end", "\n\nATTENDANCE RECORDED.")

    # ------------------------------------------------------------- admin PIN
    def open_settings(self):
        now = time.time()
        if now < self.locked_until:
            messagebox.showerror("Locked", f"Too many wrong PINs. Try again in {int(self.locked_until - now) + 1}s.")
            return
        stored = get_stored_pin()
        if stored is None or verify_pin(LEGACY_DEFAULT_PIN, stored):
            if not self.create_pin("Set a new admin PIN before using settings."):
                return
        else:
            pin = simpledialog.askstring("Driver/admin settings", "Enter the driver/admin PIN:", show="*", parent=self.root)
            if pin is None:
                return
            if not verify_pin(pin, stored):
                self.pin_fails += 1
                if self.pin_fails >= MAX_PIN_FAILS:
                    self.pin_fails = 0
                    self.locked_until = time.time() + LOCKOUT_SECONDS
                messagebox.showerror("Access denied", "That PIN is not correct.")
                return
            self.pin_fails = 0
        SettingsWindow(self)

    def create_pin(self, prompt):
        first = simpledialog.askstring("New admin PIN", f"{prompt}\n4–12 digits:", show="*", parent=self.root)
        if first is None:
            return False
        if not re.fullmatch(r"\d{4,12}", first) or first == LEGACY_DEFAULT_PIN:
            messagebox.showerror("Invalid PIN", "Use 4–12 digits, and not 2468.")
            return False
        again = simpledialog.askstring("New admin PIN", "Re-enter the PIN:", show="*", parent=self.root)
        if again != first:
            messagebox.showerror("Mismatch", "PINs did not match.")
            return False
        set_pin(first)
        return True

    def stop_camera(self):
        if self.auto_on:
            self.stop_auto()
        self.running = False
        if self.cap is not None:
            self.cap.release()
            self.cap = None
        self.video.configure(image="", text="Webcam stopped")
        self.video.image = None
        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        self.latest_frame = None

    def on_close(self):
        self.auto_on = False
        self.stop_camera()
        self.root.destroy()


class SettingsWindow(tk.Toplevel):
    def __init__(self, app):
        super().__init__(app.root)
        self.app = app
        self.title("Driver/admin settings")
        self.geometry("560x600")
        self.transient(app.root)
        self.grab_set()
        ttk.Label(self, text="Identifier codes", font=("Segoe UI", 14, "bold")).pack(anchor="w", padx=14, pady=(14, 4))
        ttk.Label(self, text="Edit the codes that appear in student IDs. Changes are stored in SQLite. Students, scans and attendance are not touched.", wraplength=520).pack(anchor="w", padx=14, pady=(0, 8))

        years_frame = ttk.LabelFrame(self, text="Admission-year codes (one per line)", padding=8)
        years_frame.pack(fill="x", padx=14, pady=6)
        self.years = tk.Text(years_frame, height=4, width=55)
        self.years.pack(fill="x")
        branches_frame = ttk.LabelFrame(self, text="Branch codes: one CODE=Branch name per line", padding=8)
        branches_frame.pack(fill="both", expand=True, padx=14, pady=6)
        self.branches = tk.Text(branches_frame, height=12, width=55)
        self.branches.pack(fill="both", expand=True)
        with db() as con:
            years = [r[0] for r in con.execute("SELECT code FROM admission_year_codes ORDER BY code")]
            branches = con.execute("SELECT code, branch_name FROM branch_codes ORDER BY code").fetchall()
        self.years.insert("1.0", "\n".join(years))
        self.branches.insert("1.0", "\n".join(f"{r[0]}={r[1]}" for r in branches))
        prefix_frame = ttk.Frame(self, padding=(14, 5))
        prefix_frame.pack(fill="x")
        ttk.Label(prefix_frame, text="ID prefixes (comma-separated, optional):").pack(side="left")
        with db() as con:
            row = con.execute("SELECT setting_value FROM app_settings WHERE setting_key='id_prefixes'").fetchone()
        self.prefix = tk.StringVar(value=row[0] if row else "")
        ttk.Entry(prefix_frame, textvariable=self.prefix, width=18).pack(side="left", padx=8)
        with db() as con:
            row = con.execute("SELECT setting_value FROM app_settings WHERE setting_key='require_name_match'").fetchone()
        self.name_match = tk.IntVar(value=1 if row and row[0] == "1" else 0)
        ttk.Checkbutton(self, text="Auto-scan: also require the name printed on the pass to match the record",
                        variable=self.name_match).pack(anchor="w", padx=14, pady=(2, 4))
        pin_frame = ttk.Frame(self, padding=(14, 5))
        pin_frame.pack(fill="x")
        ttk.Label(pin_frame, text="New admin PIN:").pack(side="left")
        self.pin = tk.StringVar()
        ttk.Entry(pin_frame, textvariable=self.pin, show="*", width=14).pack(side="left", padx=8)
        ttk.Label(pin_frame, text="(4–12 digits; leave blank to keep current)").pack(side="left")
        ttk.Button(self, text="Save settings", command=self.save).pack(anchor="e", padx=14, pady=12)

    def save(self):
        years = [x.strip().upper() for x in self.years.get("1.0", "end").splitlines() if x.strip()]
        branches = []
        for line in self.branches.get("1.0", "end").splitlines():
            line = line.strip()
            if not line:
                continue
            if "=" not in line:
                messagebox.showerror("Invalid branch", f"Use CODE=Branch name on this line:\n{line}", parent=self)
                return
            code, name = (part.strip() for part in line.split("=", 1))
            if not re.fullmatch(r"[A-Z0-9]{1,8}", code.upper()) or not name:
                messagebox.showerror("Invalid branch", f"Check this entry:\n{line}", parent=self)
                return
            branches.append((code.upper(), name))
        prefixes = [x.strip().upper() for x in self.prefix.get().split(",") if x.strip()]
        if any(not re.fullmatch(r"[A-Z0-9]{1,8}", x) for x in prefixes):
            messagebox.showerror("Invalid prefix", "Prefixes: letters/numbers, up to 8 characters, comma-separated.", parent=self)
            return
        pin = self.pin.get().strip()
        if not years or any(not re.fullmatch(r"[A-Z0-9]{1,8}", x) for x in years):
            messagebox.showerror("Invalid years", "Enter at least one code (letters/numbers, up to 8 characters).", parent=self)
            return
        if len(set(years)) != len(years) or not branches or len({b[0] for b in branches}) != len(branches):
            messagebox.showerror("Duplicate or missing codes", "Year and branch codes must be present and unique.", parent=self)
            return
        if pin and (not re.fullmatch(r"\d{4,12}", pin) or pin == LEGACY_DEFAULT_PIN):
            messagebox.showerror("Invalid PIN", "Use 4–12 digits, and not 2468. Leave blank to keep the current PIN.", parent=self)
            return
        with db() as con:  # one transaction: all or nothing
            con.execute("DELETE FROM admission_year_codes")
            con.executemany("INSERT INTO admission_year_codes(code) VALUES (?)", [(x,) for x in years])
            con.execute("DELETE FROM branch_codes")
            con.executemany("INSERT INTO branch_codes(code, branch_name) VALUES (?, ?)", branches)
            con.execute("INSERT INTO app_settings(setting_key, setting_value) VALUES ('id_prefixes', ?) "
                        "ON CONFLICT(setting_key) DO UPDATE SET setting_value=excluded.setting_value",
                        (",".join(prefixes),))
            con.execute("INSERT INTO app_settings(setting_key, setting_value) VALUES ('require_name_match', ?) "
                        "ON CONFLICT(setting_key) DO UPDATE SET setting_value=excluded.setting_value",
                        ("1" if self.name_match.get() else "0",))
            if pin:
                con.execute("UPDATE app_settings SET setting_value=? WHERE setting_key='admin_pin'", (hash_pin(pin),))
        messagebox.showinfo("Saved", "Codes saved in the local SQLite database.", parent=self)
        self.destroy()


if __name__ == "__main__":
    init_db()
    root = tk.Tk()
    app = OCRPrototype(root)
    root.mainloop()
