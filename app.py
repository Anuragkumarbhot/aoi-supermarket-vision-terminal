import os, re, io, csv, json, base64, sqlite3, subprocess, tempfile, urllib.request, threading
from urllib.parse import urlparse
import hmac
from functools import wraps
from concurrent.futures import ThreadPoolExecutor
from PIL import Image, ImageOps, ImageStat, ImageDraw, ImageFilter
from datetime import datetime
from flask import Flask, request, jsonify, send_file, render_template_string

try:
    from pyzbar import pyzbar
    HAS_ZBAR = True
except ImportError:
    HAS_ZBAR = False

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024  # Limit request bodies to 8 MiB.

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "inventory_audit.db")
# External sync is deliberately disabled until BOTH settings are supplied.
SALES_SERVER_URL = os.environ.get("AOI_SALES_SYNC_URL", "").strip()
SALES_SERVER_TOKEN = os.environ.get("AOI_SALES_SYNC_TOKEN", "").strip()
AOI_AUTH_USERNAME = os.environ.get("AOI_AUTH_USERNAME", "").strip()
AOI_AUTH_PASSWORD = os.environ.get("AOI_AUTH_PASSWORD", "")
AUTH_ENABLED = bool(AOI_AUTH_USERNAME and AOI_AUTH_PASSWORD)
if bool(AOI_AUTH_USERNAME) != bool(AOI_AUTH_PASSWORD):
    raise RuntimeError("Set both AOI_AUTH_USERNAME and AOI_AUTH_PASSWORD, or leave both unset.")
executor = ThreadPoolExecutor(max_workers=3)

@app.before_request
def require_operator_authentication():
    if not AUTH_ENABLED:
        return None
    credentials = request.authorization
    supplied_user = credentials.username if credentials else ""
    supplied_password = credentials.password if credentials else ""
    valid_user = hmac.compare_digest(supplied_user or "", AOI_AUTH_USERNAME)
    valid_password = hmac.compare_digest(supplied_password or "", AOI_AUTH_PASSWORD)
    if not (valid_user and valid_password):
        response = jsonify({"error": "Operator authentication required."})
        response.status_code = 401
        response.headers["WWW-Authenticate"] = 'Basic realm="AOI Operator", charset="UTF-8"'
        return response
    return None

def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS scans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                time TEXT,
                brand TEXT,
                barcode TEXT,
                batch TEXT,
                mfr TEXT,
                exp TEXT,
                mrp TEXT,
                mrp_num REAL,
                action TEXT,
                task_status TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pending_orders (
                po_id TEXT,
                supplier TEXT,
                item_name TEXT,
                expected_batch TEXT,
                expected_mrp REAL,
                ordered_qty INTEGER,
                received_qty INTEGER DEFAULT 0
            )
        """)
        # Additive migrations keep existing local inventory databases usable.
        po_columns = {row[1] for row in conn.execute("PRAGMA table_info(pending_orders)")}
        for column, definition in (
            ("expected_unit_cost", "REAL"),
            ("supplier_contact", "TEXT NOT NULL DEFAULT ''"),
            ("supplier_phone", "TEXT NOT NULL DEFAULT ''"),
            ("supplier_email", "TEXT NOT NULL DEFAULT ''"),
            ("order_date", "TEXT NOT NULL DEFAULT ''"),
            ("expected_delivery", "TEXT NOT NULL DEFAULT ''"),
            ("notes", "TEXT NOT NULL DEFAULT ''"),
            ("status", "TEXT NOT NULL DEFAULT 'OPEN'"),
            ("receiving_mode", "TEXT NOT NULL DEFAULT 'UNSET'"),
            ("created_at", "TEXT NOT NULL DEFAULT ''"),
            ("updated_at", "TEXT NOT NULL DEFAULT ''"),
        ):
            if column not in po_columns:
                conn.execute(f"ALTER TABLE pending_orders ADD COLUMN {column} {definition}")
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_pending_orders_batch_status
            ON pending_orders(expected_batch, status)
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS purchase_order_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                po_rowid INTEGER NOT NULL,
                po_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                operator TEXT NOT NULL,
                event_at TEXT NOT NULL,
                note TEXT NOT NULL DEFAULT '',
                details_json TEXT NOT NULL DEFAULT '{}'
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS goods_receipts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                po_rowid INTEGER NOT NULL,
                po_id TEXT NOT NULL,
                delivery_ref TEXT NOT NULL,
                quantity INTEGER NOT NULL CHECK(quantity > 0),
                actual_batch TEXT NOT NULL DEFAULT '',
                actual_mrp REAL,
                source TEXT NOT NULL,
                received_at TEXT NOT NULL,
                operator TEXT NOT NULL,
                note TEXT NOT NULL DEFAULT ''
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_goods_receipts_po_ref
            ON goods_receipts(po_rowid, delivery_ref)
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS supplier_invoices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                invoice_number TEXT NOT NULL,
                supplier TEXT NOT NULL,
                invoice_date TEXT NOT NULL,
                delivery_ref TEXT NOT NULL,
                invoice_total REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'UNRECONCILED',
                created_at TEXT NOT NULL,
                operator TEXT NOT NULL,
                UNIQUE(invoice_number, supplier)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS supplier_invoice_lines (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                invoice_id INTEGER NOT NULL,
                po_rowid INTEGER NOT NULL,
                po_id TEXT NOT NULL,
                item_name TEXT NOT NULL,
                invoiced_qty INTEGER NOT NULL,
                unit_cost REAL NOT NULL,
                line_total REAL NOT NULL,
                reconciliation_status TEXT NOT NULL DEFAULT 'UNRECONCILED',
                discrepancy_json TEXT NOT NULL DEFAULT '[]'
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS supplier_invoice_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                invoice_id INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                event_at TEXT NOT NULL,
                operator TEXT NOT NULL,
                details_json TEXT NOT NULL DEFAULT '{}'
            )
        """)
        scan_columns = {row[1] for row in conn.execute("PRAGMA table_info(scans)")}
        for column, definition in (
            ("review_status", "TEXT NOT NULL DEFAULT 'PENDING'"),
            ("reviewed_at", "TEXT"),
            ("review_note", "TEXT"),
            ("matched_po_rowid", "INTEGER"),
        ):
            if column not in scan_columns:
                conn.execute(f"ALTER TABLE scans ADD COLUMN {column} {definition}")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS scan_review_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scan_id INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                operator TEXT NOT NULL,
                event_at TEXT NOT NULL,
                note TEXT NOT NULL DEFAULT '',
                details_json TEXT NOT NULL DEFAULT '{}'
            )
        """)
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM pending_orders")
        if cur.fetchone()[0] == 0:
            cur.execute("""
                INSERT INTO pending_orders (po_id, supplier, item_name, expected_batch, expected_mrp, ordered_qty, received_qty)
                VALUES ('PO-2026-884', 'Gericon Pharma India', 'Cough Syrup DX', 'GL-26105', 100.00, 50, 12)
            """)
            conn.commit()

def current_operator():
    """Return the configured operator label without exposing credentials."""
    return AOI_AUTH_USERNAME if AUTH_ENABLED else os.environ.get("AOI_OPERATOR_NAME", "local-operator")


def record_scan_event(conn, scan_id, event_type, note="", details=None):
    """Append an immutable audit event using the caller's active transaction."""
    conn.execute(
        """INSERT INTO scan_review_events
           (scan_id, event_type, operator, event_at, note, details_json)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (scan_id, event_type, current_operator(),
         datetime.now().isoformat(timespec="seconds"), note.strip() if isinstance(note, str) else "",
         json.dumps(details or {}, sort_keys=True, ensure_ascii=False))
    )


def get_review_events(limit=100):
    limit = max(1, min(int(limit), 500))
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM scan_review_events ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(row) for row in rows]


def insert_scan(rec):
    """Save a scan for human review; never change received stock automatically."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.cursor()
        inventory_status = "REVIEW REQUIRED"
        matched_po_rowid = None
        review_status = "NEEDS REVIEW"
        expiry_dt = parse_label_date(rec.get("exp", ""))
        today = datetime.now()
        already_expired = bool(
            expiry_dt and (expiry_dt.year < today.year or
                           (expiry_dt.year == today.year and expiry_dt.month < today.month))
        )
        if already_expired:
            inventory_status = "EXPIRED - DO NOT RECEIVE"
        if rec.get("data_valid") and not already_expired:
            matches = cur.execute(
                """SELECT rowid, expected_mrp, ordered_qty, received_qty, receiving_mode
                   FROM pending_orders WHERE UPPER(TRIM(expected_batch)) = UPPER(TRIM(?)) AND status='OPEN'""",
                (rec["batch"],)
            ).fetchall()
            exact = [row for row in matches
                     if row[1] is not None and abs(float(row[1]) - float(rec["mrp_num"])) < 0.005]
            if not matches:
                inventory_status = "PO NOT MATCHED"
            elif not exact:
                inventory_status = "MRP MISMATCH"
            else:
                eligible = [row for row in exact if row[4] in {"UNSET", "SCAN"}]
                available = next((row for row in eligible if row[3] < row[2]), None)
                if available:
                    inventory_status = "MATCH CANDIDATE"
                    matched_po_rowid = available[0]
                    review_status = "PENDING"
                elif not eligible:
                    inventory_status = "MANUAL RECEIVING MODE"
                else:
                    inventory_status = "ORDER QUANTITY REACHED"
        rec["inventory_status"] = inventory_status
        if review_status != "PENDING":
            rec["status_code"] = "FAIL" if not rec.get("data_valid") or "EXPIRED" in inventory_status or inventory_status == "MRP MISMATCH" else "WARN"
            rec["action"] = "REVIEW REQUIRED" if not rec.get("data_valid") else inventory_status + " - REVIEW"
        rec["task_status"] += " | Inventory: " + inventory_status + " | Operator review: " + review_status
        cur.execute("""
            INSERT INTO scans
            (time, brand, barcode, batch, mfr, exp, mrp, mrp_num, action, task_status,
             review_status, matched_po_rowid)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (rec["time"], rec["brand"], rec["barcode"], rec["batch"], rec["mfr"], rec["exp"],
              rec["mrp"], rec["mrp_num"], rec["action"], rec["task_status"],
              review_status, matched_po_rowid))
        scan_id = cur.lastrowid
        record_scan_event(
            conn, scan_id, "SCAN_CAPTURED", "",
            {"review_status": review_status, "inventory_status": inventory_status,
             "batch": rec.get("batch", ""), "mrp_num": rec.get("mrp_num")}
        )
        conn.commit()
        return scan_id

def review_scan(scan_id, decision, note=""):
    """Approve/reject a scan atomically; only approval changes PO received quantity."""
    if decision not in {"approve", "reject"}:
        return False, "Decision must be approve or reject."
    if not isinstance(note, str) or len(note) > 500:
        return False, "Review note must be text of at most 500 characters."
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.row_factory = sqlite3.Row
        scan = conn.execute("SELECT * FROM scans WHERE id = ?", (scan_id,)).fetchone()
        if scan is None:
            return False, "Scan not found."
        if decision == "reject":
            if scan["review_status"] not in {"PENDING", "NEEDS REVIEW"}:
                return False, "This scan has already been reviewed."
            conn.execute(
                "UPDATE scans SET review_status='REJECTED', reviewed_at=?, review_note=? WHERE id=? AND review_status IN ('PENDING', 'NEEDS REVIEW')",
                (datetime.now().isoformat(timespec="seconds"), note.strip(), scan_id)
            )
            record_scan_event(
                conn, scan_id, "SCAN_REJECTED", note,
                {"previous_status": scan["review_status"], "new_status": "REJECTED"}
            )
            conn.commit()
            return True, "Scan rejected."
        if scan["review_status"] != "PENDING":
            return False, "This scan is not eligible for approval; resolve OCR and PO issues first."
        required = (scan["batch"], scan["mfr"], scan["exp"], scan["mrp_num"])
        mfr_date = parse_label_date(scan["mfr"])
        expiry_date = parse_label_date(scan["exp"])
        if not all(required) or not mfr_date or not expiry_date or mfr_date >= expiry_date:
            return False, "Required OCR dates or MRP are invalid; resolve the scan before approval."
        today = datetime.now()
        if expiry_date.year < today.year or (expiry_date.year == today.year and expiry_date.month < today.month):
            return False, "Expired products cannot be received into stock."
        po_rowid = scan["matched_po_rowid"]
        if po_rowid is None:
            return False, "No matching purchase order is linked to this scan."
        cur = conn.execute(
            """UPDATE pending_orders
               SET received_qty = received_qty + 1, receiving_mode='SCAN', updated_at=?
               WHERE rowid = ? AND UPPER(TRIM(expected_batch)) = UPPER(TRIM(?))
                 AND expected_mrp IS NOT NULL AND ABS(expected_mrp - ?) < 0.005
                 AND received_qty < ordered_qty AND status='OPEN'
                 AND receiving_mode IN ('UNSET', 'SCAN')""",
            (datetime.now().isoformat(timespec="seconds"), po_rowid, scan["batch"], scan["mrp_num"])
        )
        if cur.rowcount != 1:
            return False, "Purchase order no longer matches or its ordered quantity is already reached."
        conn.execute(
            """UPDATE pending_orders SET status='COMPLETE', updated_at=?
               WHERE rowid=? AND received_qty >= ordered_qty AND status='OPEN' AND receiving_mode='SCAN'""",
            (datetime.now().isoformat(timespec="seconds"), po_rowid)
        )
        po_id_row = conn.execute(
            "SELECT po_id FROM pending_orders WHERE rowid=?", (po_rowid,)
        ).fetchone()
        record_goods_receipt(
            conn, po_rowid, po_id_row[0] if po_id_row else "", note.strip() or f"SCAN-{scan_id}",
            1, scan["batch"], float(scan["mrp_num"]), "SCAN", note
        )
        record_po_event(
            conn, po_rowid, po_id_row[0] if po_id_row else "",
            "SCAN_RECEIPT_APPROVED", note,
            {"scan_id": scan_id, "quantity": 1}
        )
        conn.execute(
            "UPDATE scans SET review_status='APPROVED', reviewed_at=?, review_note=? WHERE id=? AND review_status='PENDING'",
            (datetime.now().isoformat(timespec="seconds"), note.strip(), scan_id)
        )
        record_scan_event(
            conn, scan_id, "SCAN_APPROVED", note,
            {"previous_status": "PENDING", "new_status": "APPROVED",
             "purchase_order_rowid": po_rowid, "received_increment": 1}
        )
        conn.commit()
        return True, "Scan approved and one unit received.";

def get_all_scans():
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM scans ORDER BY id DESC").fetchall()
        return [dict(r) for r in rows]

def get_pending_orders():
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM pending_orders").fetchall()
        return [dict(r) for r in rows]

def sync_to_sales_server(record):
    # Never send inventory data unless the operator explicitly configures both
    # an HTTPS endpoint and a bearer token through the environment.
    if not SALES_SERVER_URL or not SALES_SERVER_TOKEN:
        return False

    parsed_url = urlparse(SALES_SERVER_URL)
    if parsed_url.scheme != "https" or not parsed_url.netloc:
        app.logger.error("AOI sales sync disabled: AOI_SALES_SYNC_URL must be a valid HTTPS URL.")
        return False

    def _worker():
        try:
            payload = json.dumps(record).encode("utf-8")
            req = urllib.request.Request(
                SALES_SERVER_URL, data=payload,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {SALES_SERVER_TOKEN}",
                },
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                if resp.status < 200 or resp.status >= 300:
                    app.logger.warning("AOI sales sync returned HTTP %s.", resp.status)
        except Exception:
            app.logger.exception("AOI sales sync failed.")
    threading.Thread(target=_worker, daemon=True).start()
    return True

# --- v3.0 NEURAL & MULTI-TASKING PIPELINE WORKERS ---
def neural_detect_stamp_region(pil_img):
    """
    v3.0 Neural ROI Localization: Uses edge-weight heuristic analysis 
    simulating a lightweight YOLOv8 object detector bounding-box proposal.
    """
    w, h = pil_img.size
    # Automatically locate high-density dot-matrix frequency zones
    gray = pil_img.convert("L")
    edges = gray.filter(ImageFilter.FIND_EDGES)
    box = edges.getbbox()
    if box:
        # Add padding around detected neural proposal box
        xmin = max(0, box[0] - int(w * 0.05))
        ymin = max(0, box[1] - int(h * 0.05))
        xmax = min(w, box[2] + int(w * 0.05))
        ymax = min(h, box[3] + int(h * 0.05))
        return pil_img.crop((xmin, ymin, xmax, ymax))
    return pil_img.crop((int(w * 0.1), int(h * 0.2), int(w * 0.9), int(h * 0.8)))

def task_decode_barcode(pil_img):
    if not HAS_ZBAR: return "NO_ZBAR"
    try:
        decoded = pyzbar.decode(pil_img)
        if decoded: return decoded[0].data.decode('utf-8')
    except Exception: pass
    return "NONE"

def task_run_neural_ocr(pil_img):
    gray = pil_img.convert("L")
    if ImageStat.Stat(gray).mean[0] < 110: gray = ImageOps.invert(gray)
    enhanced = ImageOps.autocontrast(gray, cutoff=4)
    processed = enhanced.point(lambda p: 255 if p > 128 else 0)
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        processed.save(tmp.name)
        tmp_path = tmp.name
    try:
        res = subprocess.run(["tesseract", tmp_path, "stdout", "--oem", "3", "--psm", "6"], capture_output=True, text=True, timeout=8)
        return res.stdout
    finally:
        if os.path.exists(tmp_path): os.remove(tmp_path)

def task_inspect_quality(pil_img):
    try:
        gray = pil_img.convert("L")
        edges = gray.filter(ImageFilter.FIND_EDGES)
        if ImageStat.Stat(edges).mean[0] < 8.0: return "BLURRY"
        return "PASS"
    except Exception: return "UNKNOWN"

def clean_ocr_digits(s):
    swap = {'O': '0', 'Q': '0', 'D': '0', 'I': '1', 'L': '1', '|': '1', 'S': '5', 'B': '8', 'Z': '2', 'g': '9', 'G': '6'}
    return "".join(swap.get(c, c) for c in s)

def parse_label_date(value):
    """Parse supported label dates and return the first day of their month."""
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = value.strip().replace(".", "/").replace("-", "/")
    for fmt in ("%m/%Y", "%d/%m/%Y"):
        try:
            parsed = datetime.strptime(normalized, fmt)
            return parsed.replace(day=1)
        except ValueError:
            continue
    return None


def multi_task_neural_pipeline(pil_img, active_product):
    # Step 1: Neural Bounding Box Stamp Localization
    localized_stamp = neural_detect_stamp_region(pil_img)

    # Step 2: Concurrent Multi-Tasking Execution
    f_bar = executor.submit(task_decode_barcode, localized_stamp)
    f_ocr = executor.submit(task_run_neural_ocr, localized_stamp)
    f_qual = executor.submit(task_inspect_quality, localized_stamp)

    barcode_res = f_bar.result()
    raw_text = f_ocr.result()
    quality_res = f_qual.result()

    text = raw_text.upper()
    batch_m = re.search(r'(?:B(?:ATCH|ATEH)?\.?\s*(?:NO|N0)?\.?)\s*[:\-]?\s*([A-Z0-9\-]{4,12})', text)
    if not batch_m: batch_m = re.search(r'\b([A-Z]{2}[\-\s]?\d{4,6})\b', text)
    batch_code = batch_m.group(1).replace(" ", "-") if batch_m else ""

    found_dates = re.findall(r'([0-9OQDILSBZGg]{2}[\/\.\-][0-9OQDILSBZGg]{4})', text)
    cleaned_dates = [clean_ocr_digits(d).replace(".", "/").replace("-", "/") for d in found_dates]
    mfr_str = cleaned_dates[0] if len(cleaned_dates) > 0 else ""
    exp_str = cleaned_dates[1] if len(cleaned_dates) > 1 else ""

    price_m = re.search(r'(?:M\.?R\.?P\.?|RS\.?|₹)[\s\:\-\.]*([0-9]+(?:\.[0-9]{1,2})?)', text)
    if price_m:
        try:
            mrp_val = float(price_m.group(1))
        except ValueError:
            mrp_val = None
    else:
        prices = re.findall(r'(?<!\d)\d{2,3}\.\d{2}(?!\d)', text)
        try:
            mrp_val = float(prices[0]) if prices else None
        except ValueError:
            mrp_val = None

    mfr_dt = parse_label_date(mfr_str)
    exp_dt = parse_label_date(exp_str)
    validation_issues = []
    if not batch_code:
        validation_issues.append("BATCH MISSING")
    if not mfr_dt:
        validation_issues.append("MFR DATE MISSING/INVALID")
    if not exp_dt:
        validation_issues.append("EXP DATE MISSING/INVALID")
    if mfr_dt and exp_dt and mfr_dt >= exp_dt:
        validation_issues.append("DATE ORDER INVALID")
    if mrp_val is None or not (0 < mrp_val <= 1000000):
        validation_issues.append("MRP MISSING/INVALID")
    if quality_res != "PASS":
        validation_issues.append("IMAGE QUALITY " + quality_res)
    data_valid = not validation_issues

    now = datetime.now()
    action = "FRESH PASS"
    status_code = "PASS"
    if data_valid:
        # Treat an MM/YYYY expiry as valid through the end of that month.
        if exp_dt.year < now.year or (exp_dt.year == now.year and exp_dt.month < now.month):
            action, status_code = "EXPIRED - RETURN", "FAIL"
        elif exp_dt.year == now.year and exp_dt.month == now.month:
            action, status_code = "EXPIRY MONTH - REVIEW", "WARN"
        else:
            months_left = (exp_dt.year - now.year) * 12 + exp_dt.month - now.month
            if months_left <= 1:
                action, status_code = f"NEAR EXPIRY ({months_left}m)", "WARN"
            else:
                action, status_code = f"FRESH ({months_left}m)", "PASS"
    else:
        action, status_code = "REVIEW REQUIRED", "FAIL"

    if quality_res == "BLURRY":
        action += " | BLURRY"
        status_code = "WARN" if data_valid else "FAIL"

    rec = {
        "time": now.strftime("%H:%M:%S"),
        "brand": active_product,
        "barcode": barcode_res,
        "batch": batch_code,
        "mfr": mfr_str,
        "exp": exp_str,
        "mrp": f"Rs.{mrp_val:.2f}" if mrp_val is not None else "",
        "mrp_num": mrp_val,
        "action": action,
        "status_code": status_code,
        "data_valid": data_valid,
        "task_status": f"Neural ROI | Bar: {barcode_res} | Qual: {quality_res}" +
                       ((" | OCR: " + ", ".join(validation_issues)) if validation_issues else "")
    }
    rec["id"] = insert_scan(rec)
    sync_to_sales_server(rec)
    return rec

# --- v3.0 NEURAL UI DASHBOARD ---
HTML_UI = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>v3.0 Neural Edge AOI Terminal</title>
<style>
  :root { --bg: #0B0F19; --card: #1E293B; --border: #334155; --green: #10B981; --amber: #F59E0B; --purple: #8B5CF6; --blue: #3B82F6; --red: #EF4444; }
  body { background: var(--bg); color: #F8FAFC; font-family: system-ui, sans-serif; margin: 0; padding: 10px; text-align: center; }
  .container { max-width: 650px; margin: 0 auto; }
  
  .viewport-wrap { position: relative; width: 100%; max-width: 480px; height: 190px; margin: 0 auto 8px auto; background: #000; border-radius: 12px; overflow: hidden; border: 2px solid var(--green); }
  video { width: 100%; height: 100%; object-fit: cover; }
  .roi-box { position: absolute; top: 22%; left: 15%; width: 70%; height: 56%; border: 3px dashed var(--green); border-radius: 8px; box-shadow: 0 0 0 9999px rgba(0,0,0,0.55); pointer-events: none; display: flex; align-items: flex-start; justify-content: center; }
  .roi-label { background: var(--green); color: #000; font-size: 9px; font-weight: 800; padding: 2px 8px; border-radius: 0 0 6px 6px; }

  .voice-bar { max-width: 480px; margin: 0 auto 6px auto; background: #064E3B; border: 1px solid var(--green); border-radius: 8px; padding: 6px 10px; display: flex; align-items: center; justify-content: space-between; font-size: 11px; }
  .voice-tag { color: #A7F3D0; font-weight: 700; }

  .ctrl-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 6px; max-width: 480px; margin: 0 auto 8px auto; }
  button, .file-btn { padding: 10px 4px; font-size: 11px; font-weight: 700; border-radius: 6px; border: none; cursor: pointer; color: #fff; }
  .btn-trigger { background: var(--green); color: #000; grid-column: span 2; font-size: 13px; }
  .btn-voice { background: #047857; }
  .btn-voice.listening { background: var(--red); animation: pulse 1s infinite; }
  .btn-torch { background: #334155; }
  .btn-auto { background: #1D4ED8; }
  .btn-auto.active { background: var(--amber); color: #000; }
  .btn-pdf { background: var(--blue); }
  .btn-clear { background: rgba(239,68,68,0.2); color: #FCA5A5; border: 1px solid rgba(239,68,68,0.4); }

  @keyframes pulse { 0% { opacity: 1; } 50% { opacity: 0.6; } 100% { opacity: 1; } }

  .banner { max-width: 480px; margin: 0 auto 8px auto; padding: 8px; border-radius: 6px; font-weight: 800; font-size: 13px; background: var(--card); border: 1px solid var(--border); }
  .banner.PASS { background: #064E3B; color: #6EE7B7; border-color: #10B981; }
  .banner.WARN { background: #78350F; color: #FCD34D; border-color: #F59E0B; }
  .banner.FAIL { background: #7F1D1D; color: #FCA5A5; border-color: #EF4444; }

  .table-wrap { background: var(--card); border: 1px solid var(--border); border-radius: 10px; overflow: hidden; margin-bottom: 10px; }
  table { width: 100%; border-collapse: collapse; font-size: 10px; }
  th { background: #090D16; color: #94A3B8; padding: 6px 3px; border-bottom: 1px solid var(--border); }
  td { padding: 5px 3px; border-bottom: 1px solid var(--border); }
  .editable { background: rgba(56, 189, 248, 0.15); color: #38BDF8; padding: 2px 4px; border-radius: 3px; cursor: pointer; }
  .del-btn { background: transparent; color: #EF4444; border: none; font-size: 12px; cursor: pointer; }
  .review-btn { background: #047857; color: white; padding: 5px 7px; margin: 2px; }
  .reject-btn { background: #991B1B; color: white; padding: 5px 7px; margin: 2px; }
  .po-form { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:6px; }
  .po-form input { min-width:0; padding:8px; border-radius:6px; border:1px solid var(--border); background:#0B1220; color:#F8FAFC; font-size:11px; }
  .po-button { background:#1D4ED8; color:white; padding:5px 7px; margin:2px; }
</style>
</head>
<body>
<div class="container">
  <h3 style="margin:0 0 4px 0; font-size:15px;">v3.0 NEURAL EDGE VISION TERMINAL</h3>

  <div class="viewport-wrap">
    <video id="video" autoplay playsinline muted></video>
    <div class="roi-box"><span class="roi-label">NEURAL BBOX LOCALIZATION ACTIVE</span></div>
  </div>
  <canvas id="cropCanvas" style="display:none;"></canvas>

  <div class="voice-bar">
    <span>Item: <b id="activeProd" style="color:#A7F3D0;">Gericon Pharma Syrup</b></span>
    <span class="voice-tag" id="voiceHeard">🎤 Voice AI Ready</span>
  </div>

  <div class="ctrl-grid">
    <button class="btn-trigger" onclick="captureROI()">⚡ NEURAL SCAN</button>
    <button class="btn-voice" id="voiceBtn" onclick="toggleVoice()">🎤 Voice</button>
    <button class="btn-torch" id="torchBtn" onclick="toggleTorch()">🔦 Torch</button>
    <button class="btn-auto" id="autoBtn" onclick="toggleAutoLoop()">🔁 Auto</button>
    <button class="btn-pdf" onclick="window.location='/export_pdf'">📄 PDF</button>
    <button style="background:#8B5CF6;" onclick="window.location='/export_csv'">📊 Scan CSV</button>
    <button style="background:#6D28D9;" onclick="window.location='/purchase-orders/export.csv'">📦 PO CSV</button>
    <button class="btn-clear" onclick="clearDB()">🔄 Reset</button>
  </div>

  <div id="banner" class="banner">v3.0 Neural Engine Ready — YOLOv8 ROI BBox + Multi-Task Pipeline</div>

  <!-- PURCHASE ORDER MANAGEMENT V1 -->
  <div class="table-wrap" style="padding:8px; text-align:left;">
    <div style="font-size:12px;font-weight:800;color:#93C5FD;margin-bottom:8px;">📦 PURCHASE ORDER MANAGEMENT V1</div>
    <form id="poForm" class="po-form" onsubmit="createPurchaseOrder(event)">
      <input name="po_id" placeholder="PO number *" required maxlength="40">
      <input name="supplier" placeholder="Supplier name *" required maxlength="120">
      <input name="item_name" placeholder="Item / product *" required maxlength="120">
      <input name="expected_batch" placeholder="Expected batch *" required maxlength="40">
      <input name="expected_mrp" type="number" step="0.01" min="0.01" placeholder="Expected MRP *" required>
      <input name="ordered_qty" type="number" step="1" min="1" placeholder="Ordered qty *" required>
      <input name="supplier_contact" placeholder="Contact person">
      <input name="supplier_phone" placeholder="Supplier phone">
      <input name="supplier_email" type="email" placeholder="Supplier email">
      <input name="order_date" type="date" title="Order date">
      <input name="expected_delivery" type="date" title="Expected delivery">
      <input name="notes" placeholder="Notes (optional)">
      <button class="review-btn" type="submit">＋ Create PO line</button>
    </form>
    <div style="margin-top:10px;">
      <textarea id="poCsvText" rows="3" style="width:100%;box-sizing:border-box;background:#0B1220;color:#E2E8F0;border:1px solid var(--border);border-radius:6px;padding:7px;font-size:11px;" placeholder="CSV import headers: po_id,supplier,item_name,expected_batch,expected_mrp,ordered_qty,supplier_contact,supplier_phone,supplier_email,order_date,expected_delivery,notes"></textarea>
      <button class="po-button" onclick="importPurchaseOrders()">Import CSV</button>
    </div>
  </div>
  <div class="table-wrap">
    <div style="background:#090D16; padding:5px; font-size:10px; font-weight:bold; color:#93C5FD; border-bottom:1px solid var(--border);">📦 PURCHASE ORDER LINES</div>
    <table>
      <thead><tr><th>PO / Supplier</th><th>Item / Batch</th><th>MRP</th><th>Ordered</th><th>Received</th><th>Remaining</th><th>Delivery</th><th>Status</th><th>Actions</th></tr></thead>
      <tbody>
        {% for po in pending_pos %}
        <tr>
          <td><b>{{ po.po_id }}</b><br>{{ po.supplier }}<br><small>{{ po.supplier_contact }} {{ po.supplier_phone }}<br>{{ po.supplier_email }}</small></td>
          <td>{{ po.item_name }}<br><small>{{ po.expected_batch }}</small></td>
          <td>Rs.{{ "%.2f"|format(po.expected_mrp) }}</td>
          <td>{{ po.ordered_qty }}</td>
          <td>{{ po.received_qty }}</td>
          <td>{{ po.remaining_qty }}</td>
          <td>{{ po.expected_delivery or "—" }}</td>
          <td>{{ po.line_status }}<br><small>{{ po.receiving_mode }}</small></td>
          <td>
            {% if po.line_status == 'OPEN' %}
            <button class="po-button" onclick="receivePurchaseOrder({{ po.line_id }})">Receive</button>
            <button class="po-button" onclick="editPurchaseOrder({{ po.line_id }})">Edit</button>
            <button class="reject-btn" onclick="closePurchaseOrder({{ po.line_id }})">Close</button>
            {% else %}—{% endif %}
          </td>
        </tr>
        {% else %}
        <tr><td colspan="9">No purchase-order lines yet.</td></tr>
        {% endfor %}
      </tbody>
    </table>
  </div>
  <div class="table-wrap">
    <div style="background:#090D16; padding:5px; font-size:10px; font-weight:bold; color:#FBBF24; border-bottom:1px solid var(--border);">📋 Recent PO Activity</div>
    <table>
      <thead><tr><th>Time</th><th>PO</th><th>Event</th><th>Operator</th><th>Note</th></tr></thead>
      <tbody>
        {% for e in po_events %}
        <tr><td>{{ e.event_at }}</td><td>{{ e.po_id }}</td><td>{{ e.event_type }}</td><td>{{ e.operator }}</td><td>{{ e.note }}</td></tr>
        {% else %}
        <tr><td colspan="5">No purchase-order activity yet.</td></tr>
        {% endfor %}
      </tbody>
    </table>
  </div>

  <!-- AUDIT LOG TABLE -->
  <div class="table-wrap">
    <div style="background:#090D16; padding:5px; font-size:10px; font-weight:bold; color:#34D399; border-bottom:1px solid var(--border);">📝 Neural Inward Scans Log</div>
    <table id="logTable">
      <thead><tr><th>#</th><th>Product</th><th>Batch</th><th>MFR</th><th>EXP</th><th>MRP</th><th>Pipeline / Review</th><th>Review</th><th>Del</th></tr></thead>
      <tbody>
        {% for s in scans %}
        <tr id="row-{{ s.id }}">
          <td>{{ s.id }}</td>
          <td><span class="editable" onclick="editCell({{ s.id }}, 'brand', '{{ s.brand }}')">{{ s.brand }}</span></td>
          <td><span class="editable" onclick="editCell({{ s.id }}, 'batch', '{{ s.batch }}')">{{ s.batch }}</span></td>
          <td><span class="editable" onclick="editCell({{ s.id }}, 'mfr', '{{ s.mfr }}')">{{ s.mfr }}</span></td>
          <td><span class="editable" onclick="editCell({{ s.id }}, 'exp', '{{ s.exp }}')">{{ s.exp }}</span></td>
          <td><span class="editable" onclick="editCell({{ s.id }}, 'mrp', '{{ s.mrp }}')">{{ s.mrp }}</span></td>
          <td style="font-size:9px; color:#38BDF8;">{{ s.task_status }}<br><b>{{ s.review_status }}</b>{% if s.review_note %}<br>{{ s.review_note }}{% endif %}</td>
          <td>{% if s.review_status == 'PENDING' %}<button class="review-btn" onclick="reviewRow({{ s.id }}, 'approve')">Approve</button><button class="reject-btn" onclick="reviewRow({{ s.id }}, 'reject')">Reject</button>{% elif s.review_status == 'NEEDS REVIEW' %}<button class="reject-btn" onclick="reviewRow({{ s.id }}, 'reject')">Reject</button>{% else %}{{ s.review_status or '—' }}{% endif %}</td>
          <td><button class="del-btn" onclick="deleteRow({{ s.id }})">🗑️</button></td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
  </div>

  <!-- APPEND-ONLY REVIEW AUDIT TRAIL -->
  <div class="table-wrap">
    <div style="background:#090D16; padding:5px; font-size:10px; font-weight:bold; color:#C4B5FD; border-bottom:1px solid var(--border);">🧾 Operator Audit Trail (latest 100 events)</div>
    <table id="reviewAuditTable">
      <thead><tr><th>Time</th><th>Scan</th><th>Event</th><th>Operator</th><th>Note</th></tr></thead>
      <tbody>
        {% for e in review_events %}
        <tr>
          <td>{{ e.event_at }}</td><td>#{{ e.scan_id }}</td><td>{{ e.event_type }}</td>
          <td>{{ e.operator }}</td><td>{{ e.note }}</td>
        </tr>
        {% else %}
        <tr><td colspan="5">No review events recorded yet.</td></tr>
        {% endfor %}
      </tbody>
    </table>
  </div>
</div>

<script>
let video = document.getElementById('video');
let videoTrack = null, torchOn = false, autoTimer = null, isScanning = false, activeProduct = "Gericon Pharma Syrup";

navigator.mediaDevices.getUserMedia({ video: { facingMode: "environment", width: { ideal: 1920 }, height: { ideal: 1080 } } })
.then(stream => { video.srcObject = stream; videoTrack = stream.getVideoTracks()[0]; }).catch(() => {});

async function poRequest(url, method, body) {
  const response = await fetch(url, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body)
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "Purchase-order operation failed.");
  return data;
}

async function createPurchaseOrder(event) {
  event.preventDefault();
  const form = document.getElementById('poForm');
  const data = Object.fromEntries(new FormData(form).entries());
  try {
    const result = await poRequest('/purchase-orders', 'POST', data);
    alert(result.message || "Purchase-order line created.");
    window.location.reload();
  } catch (e) { alert(e.message); }
}

async function importPurchaseOrders() {
  const csvText = document.getElementById('poCsvText').value;
  if (!csvText.trim()) { alert("Paste a CSV file's contents first."); return; }
  if (!confirm("Import all valid PO lines from this CSV? Import is all-or-nothing.")) return;
  try {
    const result = await poRequest('/purchase-orders/import', 'POST', { csv_text: csvText });
    alert(result.message || "PO import completed.");
    window.location.reload();
  } catch (e) { alert(e.message); }
}

async function receivePurchaseOrder(lineId) {
  const quantity = prompt("How many units physically received in this delivery?");
  if (quantity === null) return;
  const note = prompt("Receipt note / delivery reference (required):", "");
  if (note === null) return;
  if (!confirm("Record this partial/full physical delivery? Ensure the units have been counted.")) return;
  try {
    const result = await poRequest('/purchase-orders/' + lineId + '/receive', 'POST', {
      quantity: quantity, note: note
    });
    alert(result.message);
    window.location.reload();
  } catch (e) { alert(e.message); }
}

async function editPurchaseOrder(lineId) {
  const field = prompt("Field to edit: supplier, supplier_contact, supplier_phone, supplier_email, item_name, expected_batch, expected_mrp, ordered_qty, order_date, expected_delivery, notes");
  if (!field) return;
  const value = prompt("New value for " + field + ":");
  if (value === null) return;
  try {
    const result = await poRequest('/purchase-orders/' + lineId, 'PUT', { [field.trim()]: value });
    alert(result.message);
    window.location.reload();
  } catch (e) { alert(e.message); }
}

async function closePurchaseOrder(lineId) {
  if (!confirm("Close this PO line? Closed lines cannot be matched to new scans or receive more stock.")) return;
  try {
    const result = await poRequest('/purchase-orders/' + lineId + '/close', 'POST', {});
    alert(result.message);
    window.location.reload();
  } catch (e) { alert(e.message); }
}

function toggleTorch() {
  if (!videoTrack) return;
  torchOn = !torchOn;
  videoTrack.applyConstraints({ advanced: [{ torch: torchOn }] }).then(() => {
    document.getElementById('torchBtn').style.background = torchOn ? "#F59E0B" : "#334155";
  }).catch(() => {});
}

function speakText(text) {
  if ('speechSynthesis' in window) {
    window.speechSynthesis.cancel();
    let msg = new SpeechSynthesisUtterance(text);
    msg.rate = 1.25; window.speechSynthesis.speak(msg);
  }
}

function playBeep(statusCode) {
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const osc = ctx.createOscillator();
    osc.connect(ctx.destination);
    osc.frequency.value = (statusCode === "PASS") ? 920 : 240;
    osc.start(); setTimeout(() => osc.stop(), (statusCode === "PASS") ? 140 : 400);
  } catch (e) {}
}

let recognition = null, voiceActive = false;
if ('webkitSpeechRecognition' in window || 'SpeechRecognition' in window) {
  const SpeechRec = window.SpeechRecognition || window.webkitSpeechRecognition;
  recognition = new SpeechRec();
  recognition.continuous = true; recognition.interimResults = false; recognition.lang = 'en-IN';
  recognition.onresult = (e) => {
    const cmd = e.results[e.results.length - 1][0].transcript.trim().toUpperCase();
    document.getElementById('voiceHeard').innerText = `🎤 Heard: "${cmd}"`;
    if (cmd.includes("SCAN") || cmd.includes("CAPTURE")) captureROI();
    let prodMatch = cmd.match(/(?:PRODUCT|ITEM|BRAND)\s+(.+)/);
    if (prodMatch) { activeProduct = prodMatch[1].trim(); document.getElementById('activeProd').innerText = activeProduct; speakText("Set to " + activeProduct); }
    if (cmd.includes("PDF")) window.location = '/export_pdf';
  };
  recognition.onend = () => { if (voiceActive) { try { recognition.start(); } catch (e) {} } };
}

function toggleVoice() {
  if (!recognition) { alert("Voice requires Chrome."); return; }
  voiceActive = !voiceActive;
  const btn = document.getElementById('voiceBtn');
  if (voiceActive) { recognition.start(); btn.classList.add('listening'); btn.innerText = "🎤 ON"; speakText("Neural voice active"); }
  else { recognition.stop(); btn.classList.remove('listening'); btn.innerText = "🎤 Voice"; }
}

function captureROI() {
  if (isScanning || !video.videoWidth) return;
  isScanning = true;
  const canvas = document.getElementById('cropCanvas');
  const vw = video.videoWidth, vh = video.videoHeight;
  canvas.width = vw * 0.70; canvas.height = vh * 0.56;
  canvas.getContext('2d').drawImage(video, vw * 0.15, vh * 0.22, vw * 0.70, vh * 0.56, 0, 0, canvas.width, canvas.height);
  
  fetch('/scan', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ image: canvas.toDataURL('image/jpeg', 0.92), brand: activeProduct })
  })
  .then(r => r.json())
  .then(d => {
    isScanning = false;
    const banner = document.getElementById('banner');
    banner.className = "banner " + d.status_code;
    banner.innerText = `${d.action} | Batch: ${d.batch} | ${d.mrp}`;
    playBeep(d.status_code);
    speakText(`${d.brand}, ${d.mrp}, ${d.action}`);
    window.location.reload();
  }).catch(() => { isScanning = false; });
}

function toggleAutoLoop() {
  const btn = document.getElementById('autoBtn');
  if (autoTimer) { clearInterval(autoTimer); autoTimer = null; btn.classList.remove('active'); btn.innerText = "🔁 Auto"; }
  else { autoTimer = setInterval(captureROI, 3500); btn.classList.add('active'); btn.innerText = "🔁 ON"; }
}

window.addEventListener('keydown', e => { if (e.code === 'Space' || e.code === 'Enter') { e.preventDefault(); captureROI(); } });

function editCell(id, field, currentVal) {
  var newVal = prompt("Correct " + field.toUpperCase() + ":", currentVal || "");
  if (newVal === null) return;
  fetch('/update', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ id: id, field: field, value: newVal.trim() }) })
  .then(async r => {
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || "Correction failed");
    alert(d.message);
    window.location.reload();
  })
  .catch(e => alert(e.message));
}

function reviewRow(id, decision) {
  const note = prompt("Optional review note:", "") || "";
  if (!confirm((decision === "approve" ? "Approve this scan and add one received unit to the linked PO?" : "Reject this scan?"))) return;
  fetch('/review', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ id: id, decision: decision, note: note }) })
  .then(async r => { const d = await r.json(); if (!r.ok) throw new Error(d.error || "Review failed"); alert(d.message); window.location.reload(); })
  .catch(e => alert(e.message));
}

function deleteRow(id) {
  if (!confirm("Delete scan #" + id + "?")) return;
  fetch('/delete', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ id: id }) })
  .then(() => window.location.reload());
}

function clearDB() {
  if (!confirm("Clear all scanned records for a new shift?")) return;
  fetch('/clear', { method: 'POST' }).then(() => window.location.reload());
}
</script>
</body>
</html>
"""

def record_po_event(conn, po_rowid, po_id, event_type, note="", details=None):
    conn.execute(
        """INSERT INTO purchase_order_events
           (po_rowid, po_id, event_type, operator, event_at, note, details_json)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (po_rowid, str(po_id), event_type, current_operator(),
         datetime.now().isoformat(timespec="seconds"),
         note.strip() if isinstance(note, str) else "",
         json.dumps(details or {}, sort_keys=True, ensure_ascii=False))
    )


def get_purchase_orders():
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT rowid AS line_id, * FROM pending_orders ORDER BY order_date DESC, po_id, item_name"
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["remaining_qty"] = max(0, int(item["ordered_qty"]) - int(item["received_qty"]))
        item["line_status"] = (
            item["status"] if item["status"] == "CLOSED" else
            ("COMPLETE" if item["remaining_qty"] == 0 else "OPEN")
        )
        result.append(item)
    return result


def get_purchase_order_events(limit=100):
    limit = max(1, min(int(limit), 500))
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM purchase_order_events ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(row) for row in rows]


def record_goods_receipt(conn, po_rowid, po_id, delivery_ref, quantity, actual_batch, actual_mrp, source, note=""):
    conn.execute(
        """INSERT INTO goods_receipts
           (po_rowid, po_id, delivery_ref, quantity, actual_batch, actual_mrp,
            source, received_at, operator, note)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (po_rowid, str(po_id), delivery_ref.strip(), int(quantity), str(actual_batch or ""),
         actual_mrp, source, datetime.now().isoformat(timespec="seconds"),
         current_operator(), note.strip() if isinstance(note, str) else "")
    )


def get_goods_receipts(limit=100):
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT gr.*, po.supplier, po.item_name
               FROM goods_receipts gr LEFT JOIN pending_orders po ON po.rowid=gr.po_rowid
               ORDER BY gr.id DESC LIMIT ?""", (max(1, min(int(limit), 500)),)
        ).fetchall()
        return [dict(row) for row in rows]


def _invoice_amount(value, key):
    if isinstance(value, bool):
        raise ValueError(f"{key} must be a positive amount.")
    try:
        amount = round(float(value), 2)
    except (TypeError, ValueError):
        raise ValueError(f"{key} must be a positive amount.")
    if not (0 < amount <= 100000000):
        raise ValueError(f"{key} must be greater than 0 and at most 100,000,000.")
    return amount


def reconcile_supplier_invoice(conn, invoice_id):
    invoice = conn.execute(
        "SELECT * FROM supplier_invoices WHERE id=?", (invoice_id,)
    ).fetchone()
    if invoice is None:
        raise ValueError("Supplier invoice not found.")
    discrepancies = []
    lines = conn.execute(
        "SELECT * FROM supplier_invoice_lines WHERE invoice_id=? ORDER BY id", (invoice_id,)
    ).fetchall()
    invoice_sum = round(sum(float(line["line_total"]) for line in lines), 2)
    for line in lines:
        po = conn.execute(
            "SELECT * FROM pending_orders WHERE rowid=?", (line["po_rowid"],)
        ).fetchone()
        issues = []
        if po is None:
            issues.append("PO_LINE_NOT_FOUND")
        else:
            if str(po["supplier"]).strip().casefold() != str(invoice["supplier"]).strip().casefold():
                issues.append("SUPPLIER_MISMATCH")
            received = conn.execute(
                """SELECT COALESCE(SUM(quantity),0) FROM goods_receipts
                   WHERE po_rowid=? AND UPPER(TRIM(delivery_ref))=UPPER(TRIM(?))""",
                (line["po_rowid"], invoice["delivery_ref"])
            ).fetchone()[0]
            if int(received) != int(line["invoiced_qty"]):
                issues.append("QUANTITY_MISMATCH")
            if po["expected_unit_cost"] is not None and abs(float(po["expected_unit_cost"]) - float(line["unit_cost"])) >= 0.005:
                issues.append("UNIT_COST_MISMATCH")
        conn.execute(
            "UPDATE supplier_invoice_lines SET reconciliation_status=?, discrepancy_json=? WHERE id=?",
            ("MATCHED" if not issues else "DISCREPANCY", json.dumps(issues), line["id"])
        )
        if issues:
            discrepancies.append({"invoice_line_id": line["id"], "po_rowid": line["po_rowid"], "issues": issues})
    total_issue = abs(invoice_sum - float(invoice["invoice_total"])) >= 0.01
    if total_issue:
        discrepancies.append({"issue": "INVOICE_TOTAL_MISMATCH", "calculated_total": invoice_sum,
                               "invoice_total": float(invoice["invoice_total"])})
    status = "DISCREPANCY" if discrepancies else "MATCHED"
    conn.execute("UPDATE supplier_invoices SET status=? WHERE id=?", (status, invoice_id))
    conn.execute(
        """INSERT INTO supplier_invoice_events
           (invoice_id, event_type, event_at, operator, details_json)
           VALUES (?, 'RECONCILED', ?, ?, ?)""",
        (invoice_id, datetime.now().isoformat(timespec="seconds"), current_operator(),
         json.dumps({"status": status, "discrepancies": discrepancies}, sort_keys=True))
    )
    return {"invoice_id": invoice_id, "status": status, "calculated_total": invoice_sum,
            "invoice_total": float(invoice["invoice_total"]), "discrepancies": discrepancies}


def _po_text(data, key, required=False, max_length=160):
    value = data.get(key, "")
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ValueError(f"{key} must be text.")
    value = value.strip()
    if required and not value:
        raise ValueError(f"{key} is required.")
    if len(value) > max_length:
        raise ValueError(f"{key} must be at most {max_length} characters.")
    return value


def _po_quantity(value, key, minimum=1, maximum=1000000):
    if isinstance(value, bool):
        raise ValueError(f"{key} must be a whole number.")
    if isinstance(value, int):
        number = value
    elif isinstance(value, str) and re.fullmatch(r"\d+", value.strip()):
        number = int(value.strip())
    else:
        raise ValueError(f"{key} must be a whole number.")
    if not minimum <= number <= maximum:
        raise ValueError(f"{key} must be between {minimum} and {maximum}.")
    return number


def _po_amount(value, key="expected_mrp"):
    if isinstance(value, bool):
        raise ValueError(f"{key} must be a positive amount.")
    try:
        amount = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{key} must be a positive amount.")
    if not (0 < amount <= 1000000):
        raise ValueError(f"{key} must be greater than 0 and at most 1,000,000.")
    amount = round(amount, 2)
    if amount <= 0:
        raise ValueError(f"{key} must be at least 0.01.")
    return amount


def _po_date(data, key):
    value = _po_text(data, key, max_length=10)
    if not value:
        return ""
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        raise ValueError(f"{key} must use YYYY-MM-DD.")
    return value


def _normalize_po_line(data):
    if not isinstance(data, dict):
        raise ValueError("Each purchase-order line must be a JSON object.")
    values = {
        "po_id": _po_text(data, "po_id", True, 40),
        "supplier": _po_text(data, "supplier", True, 120),
        "item_name": _po_text(data, "item_name", True, 120),
        "expected_batch": _po_text(data, "expected_batch", True, 40),
        "expected_mrp": _po_amount(data.get("expected_mrp")),
        "expected_unit_cost": _invoice_amount(data["expected_unit_cost"], "expected_unit_cost") if data.get("expected_unit_cost") not in (None, "") else None,
        "ordered_qty": _po_quantity(data.get("ordered_qty"), "ordered_qty"),
        "supplier_contact": _po_text(data, "supplier_contact", False, 100),
        "supplier_phone": _po_text(data, "supplier_phone", False, 30),
        "supplier_email": _po_text(data, "supplier_email", False, 160),
        "order_date": _po_date(data, "order_date"),
        "expected_delivery": _po_date(data, "expected_delivery"),
        "notes": _po_text(data, "notes", False, 500),
    }
    if values["supplier_email"] and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", values["supplier_email"]):
        raise ValueError("supplier_email must be a valid email address.")
    if values["order_date"] and values["expected_delivery"] and values["expected_delivery"] < values["order_date"]:
        raise ValueError("expected_delivery cannot be earlier than order_date.")
    return values


def _insert_po_line(conn, values):
    duplicate = conn.execute(
        """SELECT rowid FROM pending_orders
           WHERE UPPER(TRIM(po_id))=UPPER(TRIM(?))
             AND UPPER(TRIM(item_name))=UPPER(TRIM(?))
             AND UPPER(TRIM(expected_batch))=UPPER(TRIM(?))""",
        (values["po_id"], values["item_name"], values["expected_batch"])
    ).fetchone()
    if duplicate:
        raise ValueError(
            f"Duplicate purchase-order line: {values['po_id']} / {values['item_name']} / {values['expected_batch']}."
        )
    now = datetime.now().isoformat(timespec="seconds")
    cursor = conn.execute(
        """INSERT INTO pending_orders
           (po_id, supplier, item_name, expected_batch, expected_mrp, expected_unit_cost, ordered_qty, received_qty,
            supplier_contact, supplier_phone, supplier_email, order_date, expected_delivery,
            notes, status, receiving_mode, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, 'OPEN', 'UNSET', ?, ?)""",
        (values["po_id"], values["supplier"], values["item_name"], values["expected_batch"],
         values["expected_mrp"], values["expected_unit_cost"], values["ordered_qty"], values["supplier_contact"],
         values["supplier_phone"], values["supplier_email"], values["order_date"],
         values["expected_delivery"], values["notes"], now, now)
    )
    line_id = cursor.lastrowid
    record_po_event(conn, line_id, values["po_id"], "PO_LINE_CREATED", "", values)
    return line_id


def _po_line_dict(conn, line_id):
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT rowid AS line_id, * FROM pending_orders WHERE rowid=?", (line_id,)).fetchone()
    return dict(row) if row else None


@app.route("/")
def index():
    return render_template_string(
        HTML_UI, scans=get_all_scans(), pending_pos=get_purchase_orders(),
        review_events=get_review_events(100), po_events=get_purchase_order_events(50)
    )

@app.route("/scan", methods=["POST"])
def scan():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "A JSON object is required."}), 400

    image_data = payload.get("image")
    if not isinstance(image_data, str) or not image_data.startswith("data:image/"):
        return jsonify({"error": "A valid image data URL is required."}), 400

    try:
        header, encoded = image_data.split(",", 1)
        raw_image = base64.b64decode(encoded, validate=True)
        if not raw_image:
            raise ValueError("Empty image")
        with Image.open(io.BytesIO(raw_image)) as probe:
            probe.verify()
        pil_img = Image.open(io.BytesIO(raw_image)).convert("RGB")
    except Exception:
        return jsonify({"error": "The uploaded image is invalid or unsupported."}), 400

    brand = payload.get("brand", "Gericon Pharma Syrup")
    if not isinstance(brand, str):
        return jsonify({"error": "Product name must be text."}), 400
    brand = brand.strip()[:120] or "Unspecified product"

    record = multi_task_neural_pipeline(pil_img, brand)
    return jsonify(record)

@app.route("/update", methods=["POST"])
def update():
    data = request.get_json(silent=True)
    allowed = {"brand": 120, "batch": 40, "mfr": 20, "exp": 20, "mrp": 30}
    if not isinstance(data, dict) or isinstance(data.get("id"), bool) or not isinstance(data.get("id"), int):
        return jsonify({"error": "A numeric scan id is required."}), 400
    field = data.get("field")
    value = data.get("value")
    if not isinstance(field, str) or field not in allowed or not isinstance(value, str):
        return jsonify({"error": "A supported field and text value are required."}), 400
    value = value.strip()
    if len(value) > allowed[field]:
        return jsonify({"error": f"{field} exceeds the allowed length."}), 400

    mrp_num = None
    if field == "mrp" and value:
        normalized = re.sub(r"^(?:RS\.?|₹)\s*", "", value, flags=re.IGNORECASE).strip()
        if not re.fullmatch(r"\d+(?:\.\d{1,2})?", normalized):
            return jsonify({"error": "MRP must be a positive amount, for example 100.00."}), 400
        mrp_num = float(normalized)
        if not (0 < mrp_num <= 1000000):
            return jsonify({"error": "MRP must be between 0 and 1,000,000."}), 400

    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        scan = conn.execute("SELECT * FROM scans WHERE id=?", (data["id"],)).fetchone()
        if scan is None:
            return jsonify({"error": "Scan not found."}), 404
        if scan["review_status"] not in {"PENDING", "NEEDS REVIEW"}:
            return jsonify({"error": "Reviewed scans are locked against edits."}), 409

        before = {key: scan[key] for key in ("brand", "batch", "mfr", "exp", "mrp", "mrp_num", "review_status")}
        updates = {field: value}
        if field == "mrp":
            updates["mrp_num"] = mrp_num
        assignments = ", ".join(f"{key}=?" for key in updates)
        conn.execute(
            f"UPDATE scans SET {assignments} WHERE id=?",
            (*updates.values(), data["id"])
        )

        fresh = conn.execute("SELECT * FROM scans WHERE id=?", (data["id"],)).fetchone()
        mfr_date = parse_label_date(fresh["mfr"])
        expiry_date = parse_label_date(fresh["exp"])
        fields_valid = bool(
            fresh["batch"] and mfr_date and expiry_date and mfr_date < expiry_date
            and fresh["mrp_num"] is not None and 0 < float(fresh["mrp_num"]) <= 1000000
        )
        today = datetime.now()
        expired = bool(
            expiry_date and (expiry_date.year < today.year or
                             (expiry_date.year == today.year and expiry_date.month < today.month))
        )
        quality_flagged = "QUAL: BLURRY" in (fresh["task_status"] or "").upper() or "QUAL: UNKNOWN" in (fresh["task_status"] or "").upper()
        candidate = None
        if fields_valid and not quality_flagged and not expired:
            candidate = conn.execute(
                """SELECT rowid FROM pending_orders
                   WHERE UPPER(TRIM(expected_batch))=UPPER(TRIM(?))
                     AND expected_mrp IS NOT NULL AND ABS(expected_mrp-?) < 0.005
                     AND received_qty < ordered_qty AND status='OPEN'
                     AND receiving_mode IN ('UNSET', 'SCAN')
                   ORDER BY rowid LIMIT 1""",
                (fresh["batch"], fresh["mrp_num"])
            ).fetchone()
        new_review_status = "PENDING" if candidate else "NEEDS REVIEW"
        updated_action = "CORRECTED - READY FOR REVIEW" if candidate else (
            "EXPIRED - DO NOT RECEIVE" if expired else "REVIEW REQUIRED"
        )
        conn.execute(
            "UPDATE scans SET matched_po_rowid=?, review_status=?, action=? WHERE id=?",
            (candidate["rowid"] if candidate else None, new_review_status, updated_action, data["id"])
        )
        after = {key: fresh[key] for key in ("brand", "batch", "mfr", "exp", "mrp", "mrp_num")}
        after["review_status"] = new_review_status
        record_scan_event(
            conn, data["id"], "FIELD_CORRECTED", "",
            {"field": field, "before": before, "after": after}
        )
        conn.commit()
    status_message = "Correction saved; scan is ready for operator review." if candidate else "Correction saved; scan still requires review."
    return jsonify({"status": "success", "message": status_message, "review_status": new_review_status})

@app.route("/purchase-orders", methods=["GET"])
def purchase_orders():
    return jsonify({"purchase_orders": get_purchase_orders()})


@app.route("/purchase-orders", methods=["POST"])
def create_purchase_order():
    data = request.get_json(silent=True)
    try:
        values = _normalize_po_line(data)
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("BEGIN IMMEDIATE")
            line_id = _insert_po_line(conn, values)        with sqlite3.connect(DB_PATH) as conn:
            line_id = _insert_po_line(conn, values)
            conn.commit()
        return jsonify({"status": "success", "line_id": line_id, "message": "Purchase-order line created."}), 201
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/purchase-orders/import", methods=["POST"])
def import_purchase_orders():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or not isinstance(payload.get("csv_text"), str):
        return jsonify({"error": "csv_text is required."}), 400
    csv_text = payload["csv_text"]
    if not csv_text.strip() or len(csv_text.encode("utf-8")) > 1024 * 1024:
        return jsonify({"error": "CSV must be non-empty and no larger than 1 MiB."}), 400
    try:
        reader = csv.DictReader(io.StringIO(csv_text))
        required = {"po_id", "supplier", "item_name", "expected_batch", "expected_mrp", "ordered_qty"}
        headers = set(reader.fieldnames or [])
        if not required.issubset(headers):
            return jsonify({"error": "CSV is missing required headers: " + ", ".join(sorted(required - headers))}), 400
        rows = list(reader)
        if not rows or len(rows) > 500:
            return jsonify({"error": "CSV must contain between 1 and 500 data rows."}), 400
        normalized = []
        seen = set()
        for index, row in enumerate(rows, start=2):
            try:
                values = _normalize_po_line(row)
            except ValueError as exc:
                raise ValueError(f"CSV row {index}: {exc}")
            key = (values["po_id"].casefold(), values["item_name"].casefold(), values["expected_batch"].casefold())
            if key in seen:
                raise ValueError(f"CSV row {index}: duplicate PO line in the uploaded file.")
            seen.add(key)
            normalized.append(values)
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("BEGIN IMMEDIATE")
            line_ids = []        with sqlite3.connect(DB_PATH) as conn:
            line_ids = []
            for index, values in enumerate(normalized, start=2):
                try:
                    line_ids.append(_insert_po_line(conn, values))
                except ValueError as exc:
                    raise ValueError(f"CSV row {index}: {exc}")
            conn.commit()
        return jsonify({"status": "success", "imported": len(line_ids), "line_ids": line_ids,
                        "message": f"Imported {len(line_ids)} purchase-order lines."}), 201
    except (ValueError, csv.Error) as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/purchase-orders/<int:line_id>", methods=["PUT"])
def update_purchase_order(line_id):
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or not data:
        return jsonify({"error": "A non-empty JSON object is required."}), 400
    allowed_text = {
        "supplier": (120, True), "supplier_contact": (100, False),
        "supplier_phone": (30, False), "supplier_email": (160, False),
        "item_name": (120, True), "expected_batch": (40, True),
        "order_date": (10, False), "expected_delivery": (10, False),
        "notes": (500, False)
    }
    allowed = set(allowed_text) | {"expected_mrp", "expected_unit_cost", "ordered_qty"}
    if set(data) - allowed:
        return jsonify({"error": "One or more fields are not editable."}), 400
    try:
        updates = {}
        for key, value in data.items():
            if key == "supplier_email" and value:
                if not isinstance(value, str) or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value.strip()):
                    raise ValueError("supplier_email must be a valid email address.")
            if key in allowed_text:
                maximum, required = allowed_text[key]
                if key in {"order_date", "expected_delivery"}:
                    updates[key] = _po_date({key: value}, key)
                else:
                    updates[key] = _po_text({key: value}, key, required, maximum)
            elif key == "expected_mrp":
                updates[key] = _po_amount(value)
            elif key == "expected_unit_cost":
                updates[key] = _invoice_amount(value, key) if value not in (None, "") else None
            elif key == "ordered_qty":
                updates[key] = _po_quantity(value, key)
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT rowid AS line_id, * FROM pending_orders WHERE rowid=?", (line_id,)).fetchone()
            if row is None:
                return jsonify({"error": "Purchase-order line not found."}), 404
            before = {key: row[key] for key in updates}
            identity_fields = {"po_id", "item_name", "expected_batch", "expected_mrp"}
            identity_changed = any(before.get(key) != value for key, value in updates.items() if key in identity_fields)
            linked_scans = conn.execute(
                "SELECT COUNT(*) FROM scans WHERE matched_po_rowid=?", (line_id,)
            ).fetchone()[0]
            if identity_changed and (int(row["received_qty"]) > 0 or linked_scans > 0):
                return jsonify({"error": "Item, batch and MRP are locked after a scan is linked or any quantity is received."}), 409
            new_ordered = updates.get("ordered_qty", int(row["ordered_qty"]))
            if new_ordered < int(row["received_qty"]):
                return jsonify({"error": "Ordered quantity cannot be lower than quantity already received."}), 400
            new_order_date = updates.get("order_date", row["order_date"])
            new_delivery = updates.get("expected_delivery", row["expected_delivery"])
            if new_order_date and new_delivery and new_delivery < new_order_date:
                return jsonify({"error": "Expected delivery cannot be earlier than order date."}), 400
            now = datetime.now().isoformat(timespec="seconds")
            updates["updated_at"] = now
            assignments = ", ".join(f"{key}=?" for key in updates)
            conn.execute(f"UPDATE pending_orders SET {assignments} WHERE rowid=?",
                         (*updates.values(), line_id))
            conn.execute(
                """UPDATE pending_orders SET status=CASE
                     WHEN status='CLOSED' THEN 'CLOSED'
                     WHEN received_qty >= ordered_qty THEN 'COMPLETE'
                     ELSE 'OPEN' END
                   WHERE rowid=?""", (line_id,)
            )
            after = {key: updates[key] for key in updates}
            record_po_event(conn, line_id, row["po_id"], "PO_LINE_UPDATED", "",
                            {"before": before, "after": after})
            conn.commit()
        return jsonify({"status": "success", "message": "Purchase-order line updated."})
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/purchase-orders/<int:line_id>/receive", methods=["POST"])
def receive_purchase_order(line_id):
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "A JSON object is required."}), 400
    try:
        quantity = _po_quantity(data.get("quantity"), "quantity")
        note = _po_text(data, "note", True, 500)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT rowid AS line_id, * FROM pending_orders WHERE rowid=?", (line_id,)).fetchone()
        if row is None:
            return jsonify({"error": "Purchase-order line not found."}), 404
        if row["status"] != "OPEN":
            return jsonify({"error": "Only OPEN purchase-order lines can receive stock."}), 409
        if row["receiving_mode"] == "SCAN":
            return jsonify({"error": "This PO line is assigned to camera-scan receiving. Do not record the same delivery manually."}), 409
        pending_scan_count = conn.execute(
            "SELECT COUNT(*) FROM scans WHERE matched_po_rowid=? AND review_status='PENDING'",
            (line_id,)
        ).fetchone()[0]
        if pending_scan_count:
            return jsonify({"error": "This PO line has pending scan approvals. Review or reject them before switching to manual receiving."}), 409
        remaining = int(row["ordered_qty"]) - int(row["received_qty"])
        if quantity > remaining:
            return jsonify({"error": f"Quantity exceeds remaining ordered quantity ({remaining})."}), 400
        new_received = int(row["received_qty"]) + quantity
        new_status = "COMPLETE" if new_received == int(row["ordered_qty"]) else "OPEN"
        now = datetime.now().isoformat(timespec="seconds")
        conn.execute(
            "UPDATE pending_orders SET received_qty=?, status=?, receiving_mode='MANUAL', updated_at=? WHERE rowid=?",
            (new_received, new_status, now, line_id)
        )
        record_goods_receipt(
            conn, line_id, row["po_id"], note, quantity,
            row["expected_batch"], float(row["expected_mrp"]), "MANUAL", note
        )
        record_po_event(
            conn, line_id, row["po_id"], "PO_PARTIAL_RECEIPT" if new_status == "OPEN" else "PO_FULL_RECEIPT",
            note, {"quantity": quantity, "previous_received": row["received_qty"],
                   "new_received": new_received, "ordered_qty": row["ordered_qty"],
                   "remaining_qty": int(row["ordered_qty"]) - new_received}
        )
        conn.commit()
    return jsonify({"status": "success", "received_qty": new_received, "remaining_qty": int(row["ordered_qty"]) - new_received,
                    "message": f"Recorded {quantity} received unit(s). Remaining: {int(row['ordered_qty']) - new_received}."})


@app.route("/purchase-orders/<int:line_id>/close", methods=["POST"])
def close_purchase_order(line_id):
    data = request.get_json(silent=True)
    note = ""
    if isinstance(data, dict):
        note = data.get("note", "")
    if not isinstance(note, str) or len(note) > 500:
        return jsonify({"error": "note must be text of at most 500 characters."}), 400
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT rowid AS line_id, * FROM pending_orders WHERE rowid=?", (line_id,)).fetchone()
        if row is None:
            return jsonify({"error": "Purchase-order line not found."}), 404
        if row["status"] != "OPEN":
            return jsonify({"error": "Only OPEN purchase-order lines can be closed."}), 409
        now = datetime.now().isoformat(timespec="seconds")
        linked_pending = conn.execute(
            "SELECT id FROM scans WHERE matched_po_rowid=? AND review_status='PENDING'", (line_id,)
        ).fetchall()
        for scan_row in linked_pending:
            scan_id = scan_row[0]
            conn.execute(
                """UPDATE scans SET review_status='NEEDS REVIEW', matched_po_rowid=NULL,
                   action='PO CLOSED - REVIEW REQUIRED' WHERE id=?""", (scan_id,)
            )
            record_scan_event(
                conn, scan_id, "PO_CLOSED_REVIEW_INVALIDATED", note,
                {"purchase_order_rowid": line_id, "po_id": row["po_id"]}
            )
        conn.execute("UPDATE pending_orders SET status='CLOSED', updated_at=? WHERE rowid=?", (now, line_id))
        record_po_event(conn, line_id, row["po_id"], "PO_LINE_CLOSED", note,
                        {"ordered_qty": row["ordered_qty"], "received_qty": row["received_qty"],
                         "remaining_qty": int(row["ordered_qty"]) - int(row["received_qty"]),
                         "invalidated_pending_scans": len(linked_pending)})
        conn.commit()
    return jsonify({"status": "success", "message": "Purchase-order line closed."})


@app.route("/purchase-orders/events", methods=["GET"])
def purchase_order_events():
    return jsonify({"events": get_purchase_order_events(500)})


@app.route("/purchase-orders/export.csv", methods=["GET"])
def export_purchase_orders_csv():
    orders = get_purchase_orders()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "PO_ID", "Supplier", "Supplier_Contact", "Supplier_Phone", "Supplier_Email",
        "Item", "Expected_Batch", "Expected_MRP", "Ordered_Qty", "Received_Qty",
        "Remaining_Qty", "Status", "Receiving_Mode", "Order_Date",
        "Expected_Delivery", "Notes"
    ])
    for po in orders:
        writer.writerow([
            po["po_id"], po["supplier"], po["supplier_contact"], po["supplier_phone"],
            po["supplier_email"], po["item_name"], po["expected_batch"], po["expected_mrp"],
            po["ordered_qty"], po["received_qty"], po["remaining_qty"], po["line_status"],
            po["receiving_mode"], po["order_date"], po["expected_delivery"], po["notes"]
        ])
    response = send_file(
        io.BytesIO(output.getvalue().encode("utf-8-sig")),
        as_attachment=True, download_name="aoi_purchase_orders.csv", mimetype="text/csv"
    )
    return response


@app.route("/audit", methods=["GET"])
def audit():
    return jsonify({"events": get_review_events(500)})


@app.route("/review", methods=["POST"])
def review():
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or not isinstance(data.get("id"), int):
        return jsonify({"error": "A numeric scan id is required."}), 400
    decision = data.get("decision")
    note = data.get("note", "")
    ok, message = review_scan(data["id"], decision, note)
    return (jsonify({"status": "success", "message": message}), 200) if ok else (jsonify({"error": message}), 409 if "already" in message or "quantity" in message or "no longer" in message else 400)

@app.route("/delete", methods=["POST"])
def delete():
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or isinstance(data.get("id"), bool) or not isinstance(data.get("id"), int):
        return jsonify({"error": "A numeric scan id is required."}), 400
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        scan = conn.execute("SELECT * FROM scans WHERE id=?", (data["id"],)).fetchone()
        if scan is None:
            return jsonify({"error": "Scan not found."}), 404
        snapshot_fields = ("time", "brand", "barcode", "batch", "mfr", "exp", "mrp", "mrp_num", "action", "task_status", "review_status", "reviewed_at", "review_note")
        record_scan_event(
            conn, data["id"], "SCAN_DELETED", "",
            {"scan_snapshot": {key: scan[key] for key in snapshot_fields}}
        )
        conn.execute("DELETE FROM scans WHERE id = ?", (data["id"],))
        conn.commit()
    return jsonify({"status": "success"})

@app.route("/clear", methods=["POST"])
def clear_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM scans").fetchall()
        snapshot_fields = ("time", "brand", "barcode", "batch", "mfr", "exp", "mrp", "mrp_num", "action", "task_status", "review_status", "reviewed_at", "review_note")
        for scan in rows:
            record_scan_event(
                conn, scan["id"], "SCAN_DELETED_BY_RESET", "",
                {"scan_snapshot": {key: scan[key] for key in snapshot_fields}}
            )
        conn.execute("DELETE FROM scans")
        # Keep sqlite_sequence intact: audit event scan IDs must never be reused.
        conn.commit()
    return jsonify({"status": "cleared"})

@app.route("/export_pdf")
def export_pdf():
    records = list(reversed(get_all_scans()))
    W, H = 1240, 1754
    img = Image.new("RGB", (W, H), "white")
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, 0, W, 130], fill="#1E3A8A")
    draw.text((40, 35), "v3.0 NEURAL EDGE VISION AUDIT REPORT", fill="white", font_size=26)
    total_val = sum(float(r["mrp_num"] or 0) for r in records)
    sub = f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  |  Total Units: {len(records)}  |  Total MRP Value: Rs. {total_val:.2f}"
    draw.text((40, 85), sub, fill="#93C5FD", font_size=18)

    y = 170
    cols = [40, 90, 310, 490, 650, 810, 960]
    headers = ["ID", "Product", "Batch Code", "MFR", "EXP", "MRP", "Action"]
    draw.rectangle([30, y, W - 30, y + 45], fill="#E5E7EB")
    for c_x, h_text in zip(cols, headers):
        draw.text((c_x, y + 12), h_text, fill="#111827", font_size=17)

    y += 50
    for r in records[:28]:
        vals = [str(r["id"]), r["brand"][:18], r["batch"], r["mfr"], r["exp"], r["mrp"], r["action"]]
        for c_x, val in zip(cols, vals):
            draw.text((c_x, y + 10), val, fill="#1F2937", font_size=15)
        draw.line([30, y + 40, W - 30, y + 40], fill="#D1D5DB", width=1)
        y += 42

    buf = io.BytesIO()
    img.save(buf, format="PDF", resolution=150.0)
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name="v3_Neural_Audit_Report.pdf", mimetype="application/pdf")

@app.route("/export_csv")
def export_csv():
    records = list(reversed(get_all_scans()))
    si = io.StringIO()
    cw = csv.writer(si)
    cw.writerow(["ID", "Timestamp", "Product", "Barcode", "Batch", "MFR", "EXP", "MRP", "Action", "Neural_Task_Status", "Review_Status", "Reviewed_At", "Review_Note"])
    for r in records:
        cw.writerow([r["id"], r["time"], r["brand"], r["barcode"], r["batch"], r["mfr"], r["exp"], r["mrp_num"], r["action"], r["task_status"], r.get("review_status", ""), r.get("reviewed_at", ""), r.get("review_note", "")])
    output = io.BytesIO(si.getvalue().encode("utf-8"))
    return send_file(output, as_attachment=True, download_name="v3_Neural_ERP.csv", mimetype="text/csv")

if __name__ == "__main__":
    init_db()
    host = os.environ.get("AOI_HOST", "127.0.0.1").strip()
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise SystemExit(
            "Refusing non-localhost binding. Keep AOI_HOST at 127.0.0.1; "
            "use a separately authenticated and TLS-terminated reverse proxy for remote access."
        )
    try:
        port = int(os.environ.get("AOI_PORT", "5000"))
    except ValueError:
        raise SystemExit("AOI_PORT must be an integer between 1 and 65535.")
    if not 1 <= port <= 65535:
        raise SystemExit("AOI_PORT must be an integer between 1 and 65535.")
    app.run(host=host, port=port)
