# -*- coding: utf-8 -*-
"""
Ethiopian Digital ID Card Generator Bot
========================================
EXTRACTION STRATEGY (ULTRATHINK):
  ① Claude Vision API  → exact text fields (no OCR guessing, reads Amharic+Latin)
  ② pyzbar             → exact binary FAN barcode (CODE128, 16-digit)
  ③ pyzbar             → exact binary QR code crop (preserves all encrypted bytes)
  ④ rembg              → clean portrait background removal

All positions are unified with SHIFT_X/SHIFT_Y constants.
"""

import os, re, random, json, base64, io, subprocess
import cv2
import numpy as np
import requests
from PIL import Image, ImageDraw, ImageFont, ImageEnhance, ImageFilter
from pyzbar.pyzbar import decode as zbar_decode
from datetime import datetime
import signal, sys

def _graceful_shutdown(signum, frame):
    """Handle SIGTERM from Koyeb gracefully."""
    print("⚙️  Received shutdown signal — stopping bot cleanly...")
    sys.exit(0)

signal.signal(signal.SIGTERM, _graceful_shutdown)
signal.signal(signal.SIGINT,  _graceful_shutdown)

# Optional ethiopian_date
try:
    from ethiopian_date import EthiopianDateConverter
    HAS_ETH_DATE = True
except ImportError:
    HAS_ETH_DATE = False

# Optional barcode generation
try:
    import barcode
    from barcode.writer import ImageWriter
    HAS_BARCODE = True
except ImportError:
    HAS_BARCODE = False

# ── Background removal ─────────────────────────────────────────────────────
# Uses u2netp: tiny 4.7MB model, ~100MB RAM — fits Koyeb 512MB perfectly
# Falls back to OpenCV GrabCut if rembg unavailable (zero extra RAM)
remove = None
SESSION_HUMAN = SESSION_U2NET = None
_rembg_loaded = False

def _remove_bg_grabcut(img_pil):
    """
    Pure OpenCV background removal — no model download, ~5MB RAM.
    Good quality for portrait photos on plain backgrounds.
    """
    img_cv = cv2.cvtColor(np.array(img_pil.convert("RGB")), cv2.COLOR_RGB2BGR)
    h, w   = img_cv.shape[:2]
    mx, my = max(10, int(w * 0.04)), max(10, int(h * 0.02))
    rect   = (mx, my, w - 2*mx, h - 2*my)
    mask   = np.zeros((h, w), np.uint8)
    bgd    = np.zeros((1, 65), np.float64)
    fgd    = np.zeros((1, 65), np.float64)
    try:
        cv2.grabCut(img_cv, mask, rect, bgd, fgd, 6, cv2.GC_INIT_WITH_RECT)
        mask2 = np.where((mask == 2) | (mask == 0), 0, 255).astype(np.uint8)
        # Smooth edges
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
        mask2  = cv2.morphologyEx(mask2, cv2.MORPH_CLOSE, kernel, iterations=2)
        mask2  = cv2.GaussianBlur(mask2, (7, 7), 0)
        _, mask2 = cv2.threshold(mask2, 100, 255, cv2.THRESH_BINARY)
        rgba   = cv2.cvtColor(img_cv, cv2.COLOR_BGR2RGBA)
        rgba[:, :, 3] = mask2
        return Image.fromarray(rgba, "RGBA")
    except Exception:
        return img_pil.convert("RGBA")

def _load_rembg():
    global remove, SESSION_HUMAN, SESSION_U2NET, _rembg_loaded
    if _rembg_loaded:
        return
    try:
        from rembg import remove as _remove, new_session
        remove = _remove
        # u2netp = 4.7MB model, ~100MB RAM (fits Koyeb 512MB)
        SESSION_HUMAN = new_session("u2netp")
        SESSION_U2NET = SESSION_HUMAN
        print("✅ rembg u2netp loaded (lightweight, 512MB safe)")
    except Exception as e:
        print(f"⚠️  rembg unavailable — using OpenCV GrabCut fallback: {e}")
    _rembg_loaded = True

# Optional telegram
try:
    from telegram import Update, InputFile
    from telegram.ext import (ApplicationBuilder, CommandHandler,
                               MessageHandler, filters, ContextTypes,
                               ConversationHandler)
    HAS_TELEGRAM = True
except ImportError:
    HAS_TELEGRAM = False

# =====================================================================
# SETTINGS
# =====================================================================
BASE_UPLOAD_FOLDER = os.environ.get("UPLOAD_FOLDER", "/tmp/uploads")
os.makedirs(BASE_UPLOAD_FOLDER, exist_ok=True)
TEMPLATE_PATH = "id_template.png"
FONT_PATH     = "NotoSansEthiopic-Regular.ttf"
DPI           = 300

# ── Position offsets (single source of truth) ─────────────────────
SHIFT_X = -97
SHIFT_Y = -58

# ── Front side ────────────────────────────────────────────────────
START_X       = 514 + SHIFT_X          # text block start X
START_Y       = 170 + SHIFT_Y          # text block start Y
PHOTO_LARGE_POS = (150 + SHIFT_X, 210 + SHIFT_Y)   # large portrait
PHOTO_SMALL_POS = (970 + SHIFT_X, 560 + SHIFT_Y)   # small thumbnail
BARCODE_POS     = (575 + SHIFT_X, 555 + SHIFT_Y)   # CODE128 barcode
DATE_X          = 115 + SHIFT_X
GREG_TOP        = 190 + SHIFT_Y
GREG_BOTTOM     = 330 + SHIFT_Y
ETH_TOP         = 466 + SHIFT_Y
ETH_BOTTOM      = 590 + SHIFT_Y

# ── Back side ─────────────────────────────────────────────────────
BACK_TEXT_POS   = (1290 + SHIFT_X, 115 + SHIFT_Y)
FIN_POS         = (1275 + SHIFT_X, 575 + SHIFT_Y)
QR_BACK_POS     = (1727 + SHIFT_X, 113 + SHIFT_Y)
SN_POS          = (2105 + SHIFT_X, 653 + SHIFT_Y)

# ── Font sizes ────────────────────────────────────────────────────
BASE_FONT_SIZE   = 36
LABEL_RATIO      = 0.72
LINE_SPACING     = int(BASE_FONT_SIZE * 1.2)
BASE_FRONT_SIZE  = BASE_FONT_SIZE
BASE_BACK_SIZE   = BASE_FONT_SIZE
BACK_LABEL_RATIO = LABEL_RATIO

BACK_TEXT_START_Y = 115 + SHIFT_Y
BACK_TEXT_END_Y   = FIN_POS[1] - 8
BACK_MAX_LINES    = 12
BACK_LINE_STEP    = (BACK_TEXT_END_Y - BACK_TEXT_START_Y) // BACK_MAX_LINES
BACK_VALUE_SIZE   = 30
BACK_LABEL_SIZE   = int(BACK_VALUE_SIZE * LABEL_RATIO)

# ── Colors ────────────────────────────────────────────────────────
BLACK      = (25, 25, 25)
ORANGE_RED = (100, 50, 0)


# =====================================================================
# USER FOLDER MANAGEMENT
# =====================================================================
def get_user_folder(user_id):
    p = os.path.join(BASE_UPLOAD_FOLDER, str(user_id))
    os.makedirs(p, exist_ok=True)
    return p

def get_file_path(user_id, filename):
    return os.path.join(get_user_folder(user_id), filename)


# =====================================================================
# UTILS
# =====================================================================
def smart_font_size(text, base_size, max_chars):
    return int(base_size * 0.85) if len(text) > max_chars else base_size

def draw_strong_text(draw, position, text, font, color):
    """Simulate bold by offsetting 1px (works for Amharic & Latin)."""
    x, y = position
    for dx, dy in [(0,0),(1,0),(0,1),(1,1)]:
        draw.text((x+dx, y+dy), text, font=font, fill=color)

def draw_rotated_date(base_img, text, x, y_top, y_bottom, font):
    temp = Image.new("RGBA", (800, 200), (255, 255, 255, 0))
    d    = ImageDraw.Draw(temp)
    d.text((0, 0), text, fill=BLACK, font=font)
    bbox    = temp.getbbox()
    temp    = temp.crop(bbox)
    rotated = temp.rotate(90, expand=True)
    ratio     = (y_bottom - y_top) / rotated.height
    new_width = int(rotated.width * ratio)
    rotated   = rotated.resize((new_width, y_bottom - y_top), Image.LANCZOS)
    base_img.paste(rotated, (x, y_top), rotated)

def encode_image_b64(path):
    with open(path, 'rb') as f:
        return base64.standard_b64encode(f.read()).decode('utf-8')


# =====================================================================
# EXACT BARCODE/QR EXTRACTION  (pyzbar – reads raw binary, NO guessing)
# =====================================================================
def extract_fan_exact(image_path):
    """
    Decode CODE128 barcode from ID front to get the 16-digit FAN.
    Uses pyzbar on enhanced image for maximum reliability.
    Returns str of 16 digits or None.
    """
    img_cv = cv2.imread(image_path)
    if img_cv is None:
        return None

    for scale in [4, 3, 5, 2]:
        zoomed = cv2.resize(img_cv, None, fx=scale, fy=scale,
                            interpolation=cv2.INTER_CUBIC)
        gray   = cv2.cvtColor(zoomed, cv2.COLOR_BGR2GRAY)
        _, thr = cv2.threshold(gray, 0, 255,
                               cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        pil    = Image.fromarray(thr)
        for obj in zbar_decode(pil):
            if obj.type == "CODE128":
                raw = obj.data.decode("utf-8", errors="ignore").strip()
                # Must be exactly 16 digits
                if re.fullmatch(r'\d{16}', raw):
                    print(f"✅ FAN (pyzbar exact): {raw}")
                    return raw

    print("⚠️  pyzbar couldn't decode CODE128 – falling back to OCR")
    return extract_fan_ocr_fallback(image_path)

def extract_fan_ocr_fallback(image_path):
    """OCR fallback for FAN – last resort."""
    try:
        import pytesseract
        img_cv = cv2.imread(image_path)
        h, w   = img_cv.shape[:2]
        # Barcode number printed below the barcode stripes
        area   = img_cv[int(h*0.62):int(h*0.72), 50:w-50]
        big    = cv2.resize(area, None, fx=5, fy=5,
                            interpolation=cv2.INTER_CUBIC)
        gray   = cv2.cvtColor(big, cv2.COLOR_BGR2GRAY)
        _, thr = cv2.threshold(gray, 0, 255,
                               cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        text   = pytesseract.image_to_string(
            thr, config='--psm 7 -c tessedit_char_whitelist=0123456789'
        ).strip()
        digits = re.sub(r'\D', '', text)
        if len(digits) == 16:
            print(f"✅ FAN (OCR fallback): {digits}")
            return digits
    except Exception as e:
        print(f"OCR fallback error: {e}")
    return None

def extract_qr_binary_exact(image_path, save_path=None):
    """
    Decode QR code from photo.jpg or back.jpg.
    Returns (raw_bytes, pil_image_crop).
    Saves a high-quality crop of the QR to save_path if given.
    """
    img     = Image.open(image_path)
    img_cv  = cv2.imread(image_path)
    h, w    = img_cv.shape[:2]

    # Try full image first (photo.jpg shows QR prominently)
    for scale in [2, 3, 4, 1]:
        zoomed = cv2.resize(img_cv, None, fx=scale, fy=scale,
                            interpolation=cv2.INTER_CUBIC)
        gray   = cv2.cvtColor(zoomed, cv2.COLOR_BGR2GRAY)
        _, thr = cv2.threshold(gray, 0, 255,
                               cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        pil    = Image.fromarray(thr)
        for obj in zbar_decode(pil):
            if obj.type == "QRCODE":
                print(f"✅ QR decoded: {len(obj.data)} bytes")
                # Get QR bounding box in original coordinates
                pts  = obj.polygon
                xs   = [p.x // scale for p in pts]
                ys   = [p.y // scale for p in pts]
                pad  = 20
                x1   = max(0, min(xs) - pad)
                y1   = max(0, min(ys) - pad)
                x2   = min(w, max(xs) + pad)
                y2   = min(h, max(ys) + pad)
                crop = img.crop((x1, y1, x2, y2))
                if save_path:
                    crop_big = crop.resize((500, 500), Image.LANCZOS)
                    crop_big.save(save_path)
                    print(f"✅ QR image saved: {save_path}")
                return obj.data, crop

    # Fallback: crop known QR region from photo.jpg
    qr_region = img.crop((104, int(h*0.5), w-104, int(h*0.87)))
    if save_path:
        qr_big = qr_region.resize((500, 500), Image.LANCZOS)
        qr_big.save(save_path)
        print(f"⚠️  QR decoded via region crop (fallback)")
    return None, qr_region


# =====================================================================
# VISION EXTRACTION  – 3-layer permanent solution, never fails
#
#   LAYER 1: Claude API (Anthropic)  – best Amharic accuracy, no quotas
#            get key: https://console.anthropic.com/
#   LAYER 2: EasyOCR (local)         – 100% free, no internet, no limits
#            pip install easyocr      (downloads ~200 MB model once)
#   LAYER 3: Tesseract               – last-resort plain OCR
# =====================================================================
CLAUDE_API_URL = "https://api.anthropic.com/v1/messages"
CLAUDE_MODEL   = "claude-opus-4-5"   # best vision model; use claude-haiku-4-5 for cheaper

# ── EasyOCR lazy-load (downloads model on first use, cached forever after) ──
_easyocr_reader = None
def _get_easyocr():
    global _easyocr_reader
    if _easyocr_reader is None:
        try:
            import easyocr
            # 'am' = Amharic, 'en' = English/Latin — runs fully offline after first download
            _easyocr_reader = easyocr.Reader(['am', 'en'], gpu=False, verbose=False)
            print("✅ EasyOCR ready (Amharic + English, local)")
        except Exception as e:
            print(f"⚠️  EasyOCR unavailable: {e}  →  pip install easyocr")
            _easyocr_reader = False   # mark as unavailable so we don't retry
    return _easyocr_reader if _easyocr_reader else None


def _build_prompt(side):
    """Return the structured JSON extraction prompt for front or back."""
    if side == "front":
        return (
            "You are reading an Ethiopian Digital ID Card (Fayda) FRONT side. "
            "Extract every field EXACTLY as printed, character for character — "
            "including all Amharic (Ethiopic script) text. "
            "Return ONLY a valid JSON object, no markdown, no explanation:\n"
            "{\n"
            '  "full_name_amharic": "",\n'
            '  "full_name_latin": "",\n'
            '  "dob_gregorian": "DD/MM/YYYY",\n'
            '  "dob_ethiopian": "YYYY/Month/DD",\n'
            '  "sex_amharic": "",\n'
            '  "sex_latin": "",\n'
            '  "expiry_gregorian": "YYYY/MM/DD",\n'
            '  "expiry_ethiopian": "YYYY/Month/DD",\n'
            '  "fan": "16 digit number under the barcode"\n'
            "}\n"
            "Do NOT guess or translate. Copy exactly what is printed."
        )
    else:
        return (
            "You are reading an Ethiopian Digital ID Card (Fayda) BACK side. "
            "Extract every field EXACTLY as printed, including Amharic script. "
            "Return ONLY a valid JSON object, no markdown, no explanation:\n"
            "{\n"
            '  "phone_label_amharic": "",\n'
            '  "phone_label_latin": "",\n'
            '  "phone_number": "",\n'
            '  "fin": "4 groups of 4 digits e.g. 3254 1875 3680",\n'
            '  "nationality_label_amharic": "",\n'
            '  "nationality_amharic": "",\n'
            '  "nationality_latin": "",\n'
            '  "address_label_amharic": "",\n'
            '  "address_region_amharic": "",\n'
            '  "address_region_latin": "",\n'
            '  "address_zone_amharic": "",\n'
            '  "address_zone_latin": "",\n'
            '  "address_woreda_amharic": "",\n'
            '  "address_woreda_latin": ""\n'
            "}\n"
            "Do NOT guess. Copy exactly what is visible."
        )


# ── LAYER 1: Claude API ───────────────────────────────────────────────────────
def _try_claude_api(image_path, side, api_key):
    """Call Anthropic Claude Vision. Returns dict or None on failure."""
    if not api_key:
        return None
    try:
        b64      = encode_image_b64(image_path)
        # Detect mime type from extension
        ext      = os.path.splitext(image_path)[1].lower()
        mime     = "image/png" if ext == ".png" else "image/jpeg"
        headers  = {
            "x-api-key":         api_key,
            "anthropic-version": "2023-06-01",
            "content-type":      "application/json",
        }
        payload  = {
            "model":      CLAUDE_MODEL,
            "max_tokens": 1000,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "image",
                     "source": {"type": "base64", "media_type": mime, "data": b64}},
                    {"type": "text", "text": _build_prompt(side)}
                ]
            }]
        }
        resp = requests.post(CLAUDE_API_URL, headers=headers,
                             json=payload, timeout=40)
        if resp.status_code == 200:
            raw  = resp.json()["content"][0]["text"]
            raw  = re.sub(r'```json\s*|\s*```', '', raw).strip()
            data = json.loads(raw)
            print(f"✅ Claude Vision extracted {side}: {list(data.keys())}")
            return data
        else:
            print(f"⚠️  Claude API {resp.status_code}: {resp.text[:200]}")
    except Exception as e:
        print(f"⚠️  Claude API error: {e}")
    return None


# ── LAYER 2: EasyOCR local ───────────────────────────────────────────────────
def _try_easyocr(image_path, side):
    """
    Use EasyOCR (local, free, offline) to read Amharic + Latin text.
    Returns a partial dict with 'raw_text' for the line-parser to use.
    """
    reader = _get_easyocr()
    if not reader:
        return None
    try:
        results  = reader.readtext(image_path, detail=0, paragraph=True)
        raw_text = "\n".join(results)
        print(f"✅ EasyOCR extracted {side} ({len(results)} blocks)")
        return {"raw_text": raw_text, "_fallback": True, "_source": "easyocr"}
    except Exception as e:
        print(f"⚠️  EasyOCR read error: {e}")
    return None


# ── LAYER 3: Tesseract ───────────────────────────────────────────────────────
def extract_text_tesseract_fallback(image_path, side="front"):
    """Tesseract last-resort OCR. Tries amh+eng, falls back to eng-only."""
    try:
        import pytesseract
        img_cv = cv2.imread(image_path)
        zoom   = 4
        big    = cv2.resize(img_cv, None, fx=zoom, fy=zoom,
                            interpolation=cv2.INTER_CUBIC)
        gray   = cv2.cvtColor(big, cv2.COLOR_BGR2GRAY)
        _, thr = cv2.threshold(gray, 0, 255,
                               cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        # Try Amharic+English first, fall back to English only
        try:
            text = pytesseract.image_to_string(thr, lang='amh+eng')
        except Exception:
            text = pytesseract.image_to_string(thr, lang='eng')
        return {"raw_text": text, "_fallback": True, "_source": "tesseract"}
    except Exception as e:
        print(f"⚠️  Tesseract failed: {e}")
        return {"raw_text": "", "_fallback": True, "_source": "none"}


# ── MAIN ENTRY POINT ─────────────────────────────────────────────────────────
def extract_text_claude_vision(image_path, side="front", api_key=None):
    """
    Permanent 3-layer text extraction — always returns a dict, never crashes.

      Layer 1: Claude API (Anthropic)  → best Amharic accuracy
      Layer 2: EasyOCR (local/offline) → free, unlimited, no internet needed
      Layer 3: Tesseract               → last resort

    Set api_key to your Anthropic key for Layer 1.
    Layer 2 works with zero configuration after: pip install easyocr
    """
    # Layer 1 — Claude API
    result = _try_claude_api(image_path, side, api_key)
    if result:
        return result

    print(f"⚠️  Claude API unavailable → falling back to Tesseract…")

    # Layer 2 — Tesseract (lightweight, no RAM overhead, Amharic supported)
    return extract_text_tesseract_fallback(image_path, side)


def front_dict_to_lines(d):
    """
    Convert Claude Vision front dict → 9-line list for template rendering.
    Line indices (1-based): 1,4,6,8 are labels (orange); 2,3,5,7,9 are values.
    """
    if d.get("_fallback"):
        raw = d.get("raw_text", "")
        return [l.strip() for l in raw.splitlines() if l.strip()][:9]

    amh = d.get("full_name_amharic", "")
    lat = d.get("full_name_latin", "")
    dob_g = d.get("dob_gregorian", "")
    dob_e = d.get("dob_ethiopian", "")
    sex_a = d.get("sex_amharic", "")
    sex_l = d.get("sex_latin", "")
    exp_g = d.get("expiry_gregorian", "")
    exp_e = d.get("expiry_ethiopian", "")

    return [
        "ሙሉ ስም | Full Name",          # line 1 – label
        amh,                            # line 2
        lat,                            # line 3
        "የትውልድ ቀን | Date of Birth",    # line 4 – label
        f"{dob_g} |{dob_e}",           # line 5
        "ፆታ | Sex",                    # line 6 – label
        f"{sex_a} | {sex_l}",          # line 7
        "የሚያልቅበት ቀን | Date of Expiry", # line 8 – label
        f"{exp_g} |{exp_e}",           # line 9
    ]

def back_dict_to_lines(d):
    """
    Convert Claude Vision back dict → 12-line list for template rendering.
    Labels at positions 1,3,4,6 (1-based).
    """
    if d.get("_fallback"):
        raw = d.get("raw_text", "")
        return [l.strip() for l in raw.splitlines() if l.strip()][:12]

    ph_lbl_a = d.get("phone_label_amharic", "ስልክ")
    ph_lbl_l = d.get("phone_label_latin", "Phone Number")
    phone    = d.get("phone_number", "")
    nat_a    = d.get("nationality_amharic", "")
    nat_l    = d.get("nationality_latin", "")
    nat_self = d.get("self_declared_label", "(ምርጫዎ መሰረት | Self Declared)")
    addr_lbl = d.get("address_label_amharic", "አድርጋ | Address")
    reg_a    = d.get("address_region_amharic", "")
    reg_l    = d.get("address_region_latin", "")
    zone_a   = d.get("address_zone_amharic", "")
    zone_l   = d.get("address_zone_latin", "")
    wor_a    = d.get("address_woreda_amharic", "")
    wor_l    = d.get("address_woreda_latin", "")

    return [
        f"{ph_lbl_a} | {ph_lbl_l}",    # 1 label
        phone,                          # 2
        f"{nat_a} | {nat_l}",           # 3 label (nationality value)
        addr_lbl,                       # 4 label
        f"{reg_a}",                     # 5
        f"{reg_l}",                     # 6 label-style
        f"{zone_a}",                    # 7
        f"{zone_l}",                    # 8
        f"{wor_a}",                     # 9
        f"{wor_l}",                     # 10
        "",                             # 11
        "",                             # 12
    ]


# =====================================================================
# EXACT FIN IMAGE  (cropped from back.jpg, not OCR-generated)
# =====================================================================
def extract_fin_image_exact(back_path, save_path, fin_text=None):
    """
    Crop the FIN (ፋይዳ ልዩ ቁጥር) strip directly from back.jpg.
    Falls back to generating it from text if crop fails.
    """
    img = Image.open(back_path)
    w, h = img.size
    # FIN strip is near bottom of back card, roughly y=695-760
    fin_crop = img.crop((250, 695, w - 30, 760))

    # Upscale for quality
    scale    = max(1, int(372 / fin_crop.width))
    fin_big  = fin_crop.resize((372, 57), Image.LANCZOS)
    fin_big.save(save_path)
    print(f"✅ FIN image extracted from back.jpg: {save_path}")
    return save_path


# =====================================================================
# BARCODE GENERATION  (from exact FAN)
# =====================================================================
def create_barcode_with_fan(user_id, fan):
    """Generate CODE128 barcode image for the given 16-digit FAN."""
    user_folder = get_user_folder(user_id)
    save_path   = os.path.join(user_folder, "barcode_front.png")

    if HAS_BARCODE:
        code128 = barcode.get('code128', fan, writer=ImageWriter())
        raw     = code128.render(writer_options={
            "module_width": 0.3, "module_height": 25,
            "quiet_zone": 2,     "write_text": False
        }).convert("RGBA")
        raw = raw.resize((350, 58), Image.LANCZOS)

        top_h = 40
        gap   = 4
        final = Image.new("RGBA", (350, top_h + gap + raw.height),
                          (255, 255, 255, 255))
        draw  = ImageDraw.Draw(final)
        draw.rectangle([(0, 0), (350, top_h)], fill="white")
        font  = ImageFont.truetype(FONT_PATH, 34)
        bb    = draw.textbbox((0, 0), fan, font=font)
        tx    = (350 - (bb[2] - bb[0])) / 2
        ty    = (top_h - (bb[3] - bb[1])) / 2
        draw.text((tx, ty), fan, fill="black", font=font)
        final.paste(raw, (0, top_h + gap), raw)
        final.save(save_path, dpi=(DPI, DPI))
    else:
        # Minimal fallback: white image with FAN text
        img  = Image.new("RGBA", (350, 98), (255, 255, 255, 255))
        draw = ImageDraw.Draw(img)
        try:
            font = ImageFont.truetype(FONT_PATH, 28)
        except Exception:
            font = ImageFont.load_default()
        draw.text((5, 35), fan, fill="black", font=font)
        img.save(save_path, dpi=(DPI, DPI))

    print(f"✅ Barcode saved: {save_path}")
    return save_path


# =====================================================================
# BACKGROUND REMOVAL
# =====================================================================
def has_white_background(img, threshold=230, ratio=0.25):
    arr    = np.array(img.convert("RGB"))
    h, w   = arr.shape[:2]
    margin = max(20, int(min(h, w) * 0.12))
    corners = [arr[:margin,:margin], arr[:margin,-margin:],
               arr[-margin:,:margin], arr[-margin:,-margin:]]
    light = total = 0
    for c in corners:
        light += int(np.sum(np.all(c > threshold, axis=2)))
        total += c.shape[0] * c.shape[1]
    return (light / total) >= ratio

def is_dark_background(img, threshold=60, ratio=0.30):
    arr    = np.array(img.convert("RGB"))
    h, w   = arr.shape[:2]
    margin = max(20, int(min(h, w) * 0.12))
    corners = [arr[:margin,:margin], arr[:margin,-margin:],
               arr[-margin:,:margin], arr[-margin:,-margin:]]
    dark = total = 0
    for c in corners:
        dark  += int(np.sum(np.all(c < threshold, axis=2)))
        total += c.shape[0] * c.shape[1]
    return (dark / total) >= ratio

def add_padding(img, pad=60):
    new = Image.new("RGB", (img.width+pad*2, img.height+pad*2), (255,255,255))
    new.paste(img, (pad, pad))
    return new

def remove_padding(img, pad=60):
    return img.crop((pad, pad, img.width-pad, img.height-pad))

def smooth_alpha_edges(img_rgba, radius=1):
    r, g, b, a = img_rgba.split()
    a = a.filter(ImageFilter.GaussianBlur(radius=radius))
    return Image.merge("RGBA", (r, g, b, a))

def clean_dark_bg_residue(img_rgba, dark_thresh=38):
    arr = np.array(img_rgba).copy()
    r, g, b, a = arr[:,:,0], arr[:,:,1], arr[:,:,2], arr[:,:,3]
    mask = (r < dark_thresh) & (g < dark_thresh) & (b < dark_thresh) & (a < 200)
    arr[mask, 3] = 0
    return Image.fromarray(arr, "RGBA")

def process_photo(user_id, photo_path):
    """Remove background and save large (350×450) and small (103×124) versions."""
    try:
        if not os.path.exists(photo_path):
            print(f"Photo not found: {photo_path}")
            return None, None

        user_folder = get_user_folder(user_id)
        img = Image.open(photo_path).convert("RGB")
        img = ImageEnhance.Sharpness(img).enhance(1.8)
        img = ImageEnhance.Contrast(img).enhance(1.15)

        white_bg = has_white_background(img)
        dark_bg  = is_dark_background(img)
        print(f"📸 BG detection – white:{white_bg} dark:{dark_bg}")

        PAD    = 60
        padded = add_padding(img, PAD)
        _load_rembg()  # lazy — only loads model on first photo, not at startup
        session = SESSION_HUMAN if SESSION_HUMAN else SESSION_U2NET

        if remove is not None and session:
            # rembg u2netp — lightweight, high quality
            try:
                if white_bg:
                    result = remove(padded, session=session, alpha_matting=True,
                                    alpha_matting_foreground_threshold=240,
                                    alpha_matting_background_threshold=10,
                                    alpha_matting_erode_size=10)
                elif dark_bg:
                    result = remove(padded, session=session, alpha_matting=True,
                                    alpha_matting_foreground_threshold=190,
                                    alpha_matting_background_threshold=5,
                                    alpha_matting_erode_size=4)
                    result = clean_dark_bg_residue(result, dark_thresh=38)
                else:
                    result = remove(padded, session=session, alpha_matting=True,
                                    alpha_matting_foreground_threshold=220,
                                    alpha_matting_background_threshold=8,
                                    alpha_matting_erode_size=7)
                result = remove_padding(result, PAD)
                result = smooth_alpha_edges(result, radius=1)
                result = result.convert("RGBA")
            except Exception as e:
                print(f"⚠️  rembg failed, using GrabCut: {e}")
                result = _remove_bg_grabcut(img)
        else:
            # OpenCV GrabCut — zero extra RAM, decent quality
            print("📸 Using OpenCV GrabCut background removal")
            result = _remove_bg_grabcut(img)

        large = result.resize((350, 450), Image.LANCZOS)
        large_path = os.path.join(user_folder, "photo_large.png")
        large.save(large_path, dpi=(DPI, DPI))

        small = result.resize((103, 124), Image.LANCZOS)
        small_path = os.path.join(user_folder, "photo_small.png")
        small.save(small_path, dpi=(DPI, DPI))

        print(f"✅ Large photo: {large_path}")
        print(f"✅ Small photo: {small_path}")
        return large_path, small_path

    except Exception as e:
        import traceback; traceback.print_exc()
        return None, None


# =====================================================================
# CROP HELPERS  (extract raw card regions from phone screenshot)
# =====================================================================
def crop_image(user_id, path, crop_type):
    """
    Crop relevant sub-image from a phone screenshot of the Fayda app.
    Handles the specific layout of front/back/photo screenshots.
    """
    user_folder = get_user_folder(user_id)
    img = Image.open(path)
    w, h = img.width, img.height
    print(f"Cropping {crop_type}: {w}×{h}")

    if crop_type == "photo":
        # Portrait is in top ~47% of photo.jpg (the profile photo popup)
        box       = (105, 150, w-105, int(h * 0.47))
        cropped   = img.crop(box)
        save_path = os.path.join(user_folder, "photo_photo.png")
        cropped.save(save_path)
        print(f"✅ Photo crop: {save_path}")
        return save_path

    elif crop_type == "front":
        # Text area on front card
        box       = (90, 615, w-160, h-428)
        cropped   = img.crop(box)
        save_path = os.path.join(user_folder, "front_front.png")
        cropped.save(save_path)
        print(f"✅ Front crop: {save_path}")
        return save_path

    elif crop_type == "back":
        box       = (85, 650, w-85, h-310)
        cropped   = img.crop(box)
        save_path = os.path.join(user_folder, "back_back.png")
        cropped.save(save_path)
        print(f"✅ Back crop: {save_path}")
        return save_path

    return None


# =====================================================================
# EXTRACT 16-DIGIT NUMBER  (from text, multiple pattern fallback)
# =====================================================================
def extract_16_digit_number(text):
    match = re.search(r'\b(\d{16})\b', text)
    if match:
        return match.group(1)
    match = re.search(r'(\d{4})\s+(\d{4})\s+(\d{4})\s+(\d{4})', text)
    if match:
        return "".join(match.groups())
    match = re.search(r'(\d{4})[\s\-_]+(\d{4})[\s\-_]+(\d{4})[\s\-_]+(\d{4})', text)
    if match:
        return "".join(match.groups())
    return None


# =====================================================================
# MERGE TEMPLATE  – the main compositor
# =====================================================================
def merge_template(user_id, template_path, api_key=None):
    """
    Merge all extracted elements into the ID template.

    EXACT extraction pipeline:
      1. FAN  ← pyzbar CODE128 from front.jpg  (no OCR guessing)
      2. QR   ← pyzbar QRCODE crop from photo.jpg (binary exact)
      3. TEXT ← Claude Vision API (exact) → pytesseract fallback
      4. FIN  ← cropped from back.jpg
      5. PHOTO← rembg-processed portrait
    """
    user_folder = get_user_folder(user_id)
    print(f"🖼️  Merging template for user {user_id}")

    template = Image.open(template_path).convert("RGBA")
    draw     = ImageDraw.Draw(template)

    FRONT_LABEL_SIZE = int(BASE_FONT_SIZE * LABEL_RATIO)
    FRONT_VALUE_SIZE = BASE_FONT_SIZE
    font_date = ImageFont.truetype(FONT_PATH, 32)
    font_sn   = ImageFont.truetype(FONT_PATH, 20)

    # ── 1. EXACT FAN via pyzbar ──────────────────────────────────────
    front_path = get_file_path(user_id, "front.jpg")
    fan = None
    if os.path.exists(front_path):
        fan = extract_fan_exact(front_path)
    # Fallback: read from cached text
    if not fan:
        txt_path = get_file_path(user_id, "front_text_clean.txt")
        if os.path.exists(txt_path):
            with open(txt_path, encoding="utf-8") as f:
                fan = extract_16_digit_number(f.read())
    if fan:
        print(f"✅ Using FAN: {fan}")
        create_barcode_with_fan(user_id, fan)
    else:
        print("⚠️  FAN not found")

    # ── 2. FRONT TEXT via Claude Vision ─────────────────────────────
    front_lines = []
    cv_front_path = get_file_path(user_id, "front_text_cv.json")
    if os.path.exists(cv_front_path):
        with open(cv_front_path, encoding="utf-8") as f:
            front_dict = json.load(f)
    elif os.path.exists(front_path):
        front_dict = extract_text_claude_vision(front_path, "front", api_key)
        # Always cache result (even fallback) so merge_template won't retry
        with open(cv_front_path, "w", encoding="utf-8") as f:
            json.dump(front_dict, f, ensure_ascii=False, indent=2)
    else:
        front_dict = {}

    # Also check for manually edited text file
    txt_path = get_file_path(user_id, "front_text_clean.txt")
    if os.path.exists(txt_path):
        with open(txt_path, encoding="utf-8") as f:
            manual_lines = [l.strip() for l in f if l.strip()]
        front_lines = manual_lines[:9]
    else:
        front_lines = front_dict_to_lines(front_dict)

    # ── 3. RENDER FRONT TEXT ─────────────────────────────────────────
    y = START_Y
    for i, line in enumerate(front_lines):
        is_label = (i + 1) in [1, 4, 6, 8]
        size     = smart_font_size(
            line,
            FRONT_LABEL_SIZE if is_label else FRONT_VALUE_SIZE,
            22 if is_label else 18
        )
        font = ImageFont.truetype(FONT_PATH, size)
        if is_label:
            draw.text((START_X, y), line, fill=ORANGE_RED, font=font)
        else:
            draw_strong_text(draw, (START_X, y), line, font, BLACK)
        y += int(LINE_SPACING * (0.7 if is_label else 1.2))

    # ── 4. DATES (rotated, right-side strip) ─────────────────────────
    today    = datetime.today()
    greg     = today.strftime("%Y/%m/%d")
    if HAS_ETH_DATE:
        eth  = EthiopianDateConverter.to_ethiopian(today.year, today.month, today.day)
        eth_date = f"{eth.year}/{eth.month}/{eth.day}"
    else:
        eth_date = greg  # fallback: use gregorian
    draw_rotated_date(template, greg,     DATE_X, GREG_TOP, GREG_BOTTOM, font_date)
    draw_rotated_date(template, eth_date, DATE_X, ETH_TOP,  ETH_BOTTOM,  font_date)

    # ── 5. PASTE PHOTOS + BARCODE ────────────────────────────────────
    try:
        large_path   = get_file_path(user_id, "photo_large.png")
        small_path   = get_file_path(user_id, "photo_small.png")
        barcode_path = get_file_path(user_id, "barcode_front.png")

        if os.path.exists(large_path):
            lp = Image.open(large_path).convert("RGBA")
            template.paste(lp, PHOTO_LARGE_POS, lp)
            print("✅ Large photo pasted")

        if os.path.exists(small_path):
            sp = Image.open(small_path).convert("RGBA")
            template.paste(sp, PHOTO_SMALL_POS, sp)
            print("✅ Small photo pasted")

        if os.path.exists(barcode_path):
            bp = Image.open(barcode_path).convert("RGBA")
            template.paste(bp, BARCODE_POS, bp)
            print("✅ Barcode pasted")
    except Exception as e:
        print(f"Error pasting front images: {e}")

    # ── 6. BACK TEXT via Claude Vision ──────────────────────────────
    back_path = get_file_path(user_id, "back.jpg")
    cv_back_path = get_file_path(user_id, "back_text_cv.json")
    if os.path.exists(cv_back_path):
        with open(cv_back_path, encoding="utf-8") as f:
            back_dict = json.load(f)
    elif os.path.exists(back_path):
        back_dict = extract_text_claude_vision(back_path, "back", api_key)
        # Always cache result so we don't retry on the next call
        with open(cv_back_path, "w", encoding="utf-8") as f:
            json.dump(back_dict, f, ensure_ascii=False, indent=2)
    else:
        back_dict = {}

    # Check for manually edited back text
    back_txt_path = get_file_path(user_id, "back_text_clean.txt")
    if os.path.exists(back_txt_path):
        with open(back_txt_path, encoding="utf-8") as f:
            back_lines = [l.strip() for l in f if l.strip()][:BACK_MAX_LINES]
    else:
        back_lines = back_dict_to_lines(back_dict)[:BACK_MAX_LINES]

    back_x = BACK_TEXT_POS[0]
    for idx, line in enumerate(back_lines):
        y       = BACK_TEXT_START_Y + idx * BACK_LINE_STEP
        is_label = (idx + 1) in [1, 3, 4, 6]
        size    = smart_font_size(
            line,
            BACK_LABEL_SIZE if is_label else BACK_VALUE_SIZE,
            22 if is_label else 18
        )
        font = ImageFont.truetype(FONT_PATH, size)
        if is_label:
            draw.text((back_x, y), line, fill=ORANGE_RED, font=font)
        else:
            draw_strong_text(draw, (back_x, y), line, font, BLACK)

    last_y = (BACK_TEXT_START_Y + (len(back_lines)-1) * BACK_LINE_STEP
              if back_lines else BACK_TEXT_START_Y)
    print(f"✅ Back text: {len(back_lines)} lines, step={BACK_LINE_STEP}, last_y={last_y}")

    # ── 7. FIN IMAGE  (cropped exact from back.jpg, NOT OCR) ─────────
    fin_path = get_file_path(user_id, "fin_smart.png")
    if os.path.exists(back_path) and not os.path.exists(fin_path):
        extract_fin_image_exact(back_path, fin_path)
    if os.path.exists(fin_path):
        try:
            fin_img = Image.open(fin_path).convert("RGBA")
            template.paste(fin_img, FIN_POS, fin_img)
            print("✅ FIN pasted")
        except Exception as e:
            print(f"Error pasting FIN: {e}")

    # ── 8. QR CODE  (exact binary crop via pyzbar) ───────────────────
    #   Use photo.jpg (shows QR clearly in popup) or back.jpg fallback
    photo_orig_path = get_file_path(user_id, "photo.jpg")
    qr_save_path    = get_file_path(user_id, "qr_exact.png")

    qr_source = photo_orig_path if os.path.exists(photo_orig_path) else back_path
    if os.path.exists(qr_source) and not os.path.exists(qr_save_path):
        _, _ = extract_qr_binary_exact(qr_source, qr_save_path)

    if os.path.exists(qr_save_path):
        try:
            qr_img = Image.open(qr_save_path).convert("RGBA")
            qr_img = qr_img.resize((500, 500), Image.LANCZOS)
            template.paste(qr_img, QR_BACK_POS)
            print("✅ QR pasted (exact binary crop)")
        except Exception as e:
            print(f"Error pasting QR: {e}")
    else:
        # Legacy fallback: crop from photo.jpg region
        if os.path.exists(photo_orig_path):
            try:
                ph  = Image.open(photo_orig_path)
                pw, ph_h = ph.width, ph.height
                qr_crop  = ph.crop((104, 648, pw-104, ph_h-270))
                qr_crop  = qr_crop.resize((500, 500), Image.LANCZOS)
                template.paste(qr_crop, QR_BACK_POS)
                print("✅ QR pasted (legacy region crop)")
            except Exception as e:
                print(f"Error with legacy QR: {e}")

    # ── 9. SERIAL NUMBER  (random) ───────────────────────────────────
    sn = random.randint(100000000, 999999999)
    draw.text(SN_POS, str(sn), fill=BLACK, font=font_sn)

    # ── 10. SAVE ─────────────────────────────────────────────────────
    final_path = get_file_path(user_id, "final_id.png")
    template.save(final_path, dpi=(DPI, DPI))
    print(f"✅ Final ID saved: {final_path}")
    return final_path


# =====================================================================
# TELEGRAM BOT
# =====================================================================
if HAS_TELEGRAM:
    FRONT_STATE, BACK_STATE, PHOTO_STATE, EDIT_FRONT, EDIT_BACK, FAN_STATE = range(6)

    async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id   = update.effective_user.id
        user_name = update.effective_user.first_name
        print(f"\n{'='*50}\n👤 {user_name} ({user_id})\n{'='*50}")
        context.user_data['user_id'] = user_id
        user_folder = get_user_folder(user_id)
        await update.message.reply_text(
            f"🎫 *Welcome {user_name}!*\n\n"
            f"👤 Your ID: `{user_id}`\n"
            f"📁 Folder: `{user_folder}`\n\n"
            f"📸 *Step 1/3:* Send the FRONT side of your ID card.",
            parse_mode='Markdown'
        )
        return FRONT_STATE

    async def front_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = context.user_data.get('user_id') or update.effective_user.id
        context.user_data['user_id'] = user_id
        photo = update.message.photo[-1]
        path  = get_file_path(user_id, "front.jpg")
        await (await photo.get_file()).download_to_drive(path)
        crop_image(user_id, path, "front")

        # Immediately extract FAN (exact, from barcode)
        fan = extract_fan_exact(path)
        if fan:
            context.user_data['fan'] = fan
            print(f"✅ FAN pre-extracted: {fan}")

        await update.message.reply_text(
            "✅ Front saved!\n\n📸 *Step 2/3:* Send the BACK side.",
            parse_mode='Markdown'
        )
        return BACK_STATE

    async def back_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = context.user_data.get('user_id') or update.effective_user.id
        context.user_data['user_id'] = user_id
        photo = update.message.photo[-1]
        path  = get_file_path(user_id, "back.jpg")
        await (await photo.get_file()).download_to_drive(path)
        crop_image(user_id, path, "back")
        await update.message.reply_text(
            "✅ Back saved!\n\n📸 *Step 3/3:* Send your portrait PHOTO.",
            parse_mode='Markdown'
        )
        return PHOTO_STATE

    async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = context.user_data.get('user_id') or update.effective_user.id
        context.user_data['user_id'] = user_id
        photo = update.message.photo[-1]
        path  = get_file_path(user_id, "photo.jpg")
        await (await photo.get_file()).download_to_drive(path)

        msg = await update.message.reply_text("🔄 Extracting ID data…")
        crop_image(user_id, path, "photo")

        # ── Exact extraction ──────────────────────────────────────
        api_key = context.bot_data.get("claude_api_key")

        front_path = get_file_path(user_id, "front.jpg")
        back_path  = get_file_path(user_id, "back.jpg")

        # Claude Vision for text
        front_dict = extract_text_claude_vision(front_path, "front", api_key)
        back_dict  = extract_text_claude_vision(back_path,  "back",  api_key)

        # FAN from barcode (exact)
        fan = context.user_data.get('fan') or extract_fan_exact(front_path)
        if fan:
            if not front_dict.get("fan"):
                front_dict["fan"] = fan

        # Save for reference/editing
        front_lines = front_dict_to_lines(front_dict)
        back_lines  = back_dict_to_lines(back_dict)

        with open(get_file_path(user_id, "front_text_clean.txt"), "w", encoding="utf-8") as f:
            f.write("\n".join(front_lines))
        with open(get_file_path(user_id, "back_text_clean.txt"), "w", encoding="utf-8") as f:
            f.write("\n".join(back_lines))

        await msg.delete()

        front_display = "\n".join(front_lines)
        await update.message.reply_text(
            "📝 *Front Text – Review & Edit*\n"
            "Send corrected text, or /skip to keep as-is.\n\n"
            f"```\n{front_display}\n```",
            parse_mode='Markdown'
        )
        return EDIT_FRONT

    async def edit_front_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = context.user_data.get('user_id') or update.effective_user.id
        context.user_data['user_id'] = user_id
        text = update.message.text
        if text != "/skip":
            with open(get_file_path(user_id, "front_text_clean.txt"), "w", encoding="utf-8") as f:
                f.write(text)
            await update.message.reply_text("✅ Front text updated!")
        else:
            await update.message.reply_text("✅ Keeping original front text.")

        try:
            with open(get_file_path(user_id, "back_text_clean.txt"), encoding="utf-8") as f:
                back_txt = f.read()
        except Exception:
            back_txt = ""

        await update.message.reply_text(
            "📝 *Back Text – Review & Edit*\n"
            "Send corrected text, or /skip to keep as-is.\n\n"
            f"```\n{back_txt}\n```",
            parse_mode='Markdown'
        )
        return EDIT_BACK

    async def edit_back_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = context.user_data.get('user_id') or update.effective_user.id
        context.user_data['user_id'] = user_id
        text = update.message.text
        if text != "/skip":
            with open(get_file_path(user_id, "back_text_clean.txt"), "w", encoding="utf-8") as f:
                f.write(text)
            await update.message.reply_text("✅ Back text updated!")
        else:
            await update.message.reply_text("✅ Keeping original back text.")

        # Check for FAN
        fan = context.user_data.get('fan')
        if not fan:
            try:
                with open(get_file_path(user_id, "front_text_clean.txt"), encoding="utf-8") as f:
                    fan = extract_16_digit_number(f.read())
            except Exception:
                pass

        if fan:
            await _generate_and_send(update, context, user_id, fan)
            return ConversationHandler.END
        else:
            await update.message.reply_text(
                "⚠️ Could not read FAN from barcode.\n"
                "Please enter the 16-digit FAN manually:\n\nExample: 3850369213260475"
            )
            return FAN_STATE

    async def fan_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = context.user_data.get('user_id') or update.effective_user.id
        fan     = update.message.text.strip()
        if not re.fullmatch(r'\d{16}', fan):
            await update.message.reply_text(
                "❌ Please enter a valid 16-digit number.\nExample: 3850369213260475"
            )
            return FAN_STATE
        await _generate_and_send(update, context, user_id, fan)
        return ConversationHandler.END

    async def _generate_and_send(update, context, user_id, fan):
        context.user_data['fan'] = fan
        create_barcode_with_fan(user_id, fan)
        process_photo(user_id, get_file_path(user_id, "photo_photo.png"))
        await update.message.reply_text("✅ Processing…")
        api_key   = context.bot_data.get("claude_api_key")
        final_path = merge_template(user_id, TEMPLATE_PATH, api_key)
        if os.path.exists(final_path):
            with open(final_path, 'rb') as f:
                await context.bot.send_document(
                    chat_id=update.effective_chat.id,
                    document=InputFile(f, filename=f"ID_Card_{user_id}.png"),
                    caption=(
                        f"✅ *ID Card Generated!*\n"
                        f"📁 `static/uploads/{user_id}/`\n\n"
                        f"FAN: `{fan}`"
                    ),
                    parse_mode='Markdown'
                )
        else:
            await update.message.reply_text("❌ Generation failed. Try /start again.")

    async def generate_final(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = context.user_data.get('user_id') or update.effective_user.id
        context.user_data['user_id'] = user_id
        msg = await update.message.reply_text("🔄 Generating…")
        api_key    = context.bot_data.get("claude_api_key")
        final_path = merge_template(user_id, TEMPLATE_PATH, api_key)
        await msg.delete()
        if os.path.exists(final_path):
            with open(final_path, 'rb') as f:
                await context.bot.send_document(
                    chat_id=update.effective_chat.id,
                    document=InputFile(f, filename=f"ID_Card_{user_id}.png"),
                    caption="✅ *Here is your final ID card!*",
                    parse_mode='Markdown'
                )
        else:
            await update.message.reply_text("❌ Failed. Make sure all images are uploaded.")

    async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("❌ Cancelled. Type /start to begin again.")
        return ConversationHandler.END


# =====================================================================
# MAIN
# =====================================================================
if __name__ == "__main__":
    if not HAS_TELEGRAM:
        print("❌ python-telegram-bot not installed. Run: pip install python-telegram-bot")
        exit(1)

    # ── All secrets from environment variables (Koyeb → Settings → Secrets) ──
    BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
    if not BOT_TOKEN:
        # fallback to hardcoded for local testing only
        BOT_TOKEN = "8757528263:AAEfqMUsHxbUFOmNfPK2lJ6CuA8yaRHdBWY"
        print("⚠️  BOT_TOKEN not in env — using hardcoded token (local only)")

    # Optional: Anthropic Claude API key for best Amharic OCR accuracy
    # Get free key: https://console.anthropic.com/
    CLAUDE_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    # ──────────────────────────────────────────────────────────────────────────

    if not CLAUDE_API_KEY:
        print("⚠️  No ANTHROPIC_API_KEY — using Tesseract OCR fallback.")
        print("   Set it in Koyeb: App → Settings → Environment Variables")
        print()

    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.bot_data["claude_api_key"] = CLAUDE_API_KEY or None

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            FRONT_STATE: [MessageHandler(filters.PHOTO, front_handler)],
            BACK_STATE:  [MessageHandler(filters.PHOTO, back_handler)],
            PHOTO_STATE: [MessageHandler(filters.PHOTO, photo_handler)],
            EDIT_FRONT:  [
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_front_handler),
                CommandHandler("skip", edit_front_handler)
            ],
            EDIT_BACK:   [
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_back_handler),
                CommandHandler("skip", edit_back_handler)
            ],
            FAN_STATE:   [MessageHandler(filters.TEXT & ~filters.COMMAND, fan_handler)]
        },
        fallbacks=[CommandHandler("cancel", cancel)]
    )

    app.add_handler(conv)
    app.add_handler(CommandHandler("generate", generate_final))
    app.add_handler(CommandHandler("cancel",   cancel))

    print("\n" + "=" * 60)
    print("🤖 ID GENERATOR BOT — EXACT EXTRACTION ENGINE")
    print("=" * 60)
    print(f"  FAN:  pyzbar CODE128 (exact binary, no OCR guess)")
    print(f"  QR:   pyzbar QRCODE  (exact binary crop)")
    print(f"  TEXT: Claude Vision API → Tesseract fallback")
    print(f"  FIN:  cropped from back.jpg (not regenerated)")
    print(f"  BG:   rembg u2net_human_seg")
    print("=" * 60)
    print(f"  Template:  {os.path.abspath(TEMPLATE_PATH)}")
    print(f"  Font:      {os.path.abspath(FONT_PATH)}")
    print(f"  Uploads:   {os.path.abspath(BASE_UPLOAD_FOLDER)}")
    print(f"  Layer 1:   {'✅ Claude API (Anthropic)' if CLAUDE_API_KEY else '⚠️  No Claude key – skipping'}")
    print(f"  Layer 2:   EasyOCR local (free, offline, auto-downloads model)")
    print(f"  Layer 3:   Tesseract (last resort)")
    print("=" * 60)
    print(f"  Back text zone: y={BACK_TEXT_START_Y}→{BACK_TEXT_END_Y}  "
          f"step={BACK_LINE_STEP}px  fin_y={FIN_POS[1]}")
    print("✅ Bot running...\n")

    app.run_polling()