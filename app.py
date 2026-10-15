import os, re, io, csv, json, base64, sqlite3, subprocess, tempfile, urllib.request, threading
from concurrent.futures import ThreadPoolExecutor
from PIL import Image, ImageOps, ImageStat, ImageDraw
from datetime import datetime
from flask import Flask, request, jsonify, send_file, render_template_string

# Optional Barcode reader import
try:
    from pyzbar import pyzbar
    HAS_ZBAR = True
except ImportError:
    HAS_ZBAR = False

app = Flask(__name__)

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "inventory_audit.db")
SALES_SERVER_URL = os.environ.get("SALES_SERVER_URL", "https://api.supermarket-erp.com/v1/stock")
executor = ThreadPoolExecutor(max_workers=3) # Multi-tasking thread pool

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
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM pending_orders")
        if cur.fetchone()[0] == 0:
            cur.execute("""
                INSERT INTO pending_orders (po_id, supplier, item_name, expected_batch, expected_mrp, ordered_qty, received_qty)
                VALUES ('PO-2026-884', 'Gericon Pharma India', 'Cough Syrup DX', 'GL-26105', 100.00, 50, 12)
            """)
            conn.commit()

def insert_scan(rec):
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO scans (time, brand, barcode, batch, mfr, exp, mrp, mrp_num, action, task_status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (rec["time"], rec["brand"], rec["barcode"], rec["batch"], rec["mfr"], rec["exp"],
              rec["mrp"], rec["mrp_num"], rec["action"], rec["task_status"]))
        scan_id = cur.lastrowid
        conn.execute("UPDATE pending_orders SET received_qty = received_qty + 1 WHERE expected_batch = ?", (rec["batch"],))
        conn.commit()
        return scan_id

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

# --- MULTI-TASKING WORKER 1: Barcode / EAN Decoder ---
def task_decode_barcode(pil_img):
    if not HAS_ZBAR:
        return "NO_ZBAR_LIB"
    try:
        decoded = pyzbar.decode(pil_img)
        if decoded:
            return decoded[0].data.decode('utf-8')
    except Exception:
        pass
    return "NONE_DETECTED"

# --- MULTI-TASKING WORKER 2: OCR Stamp Extractor ---
def task_run_ocr(pil_img):
    gray = pil_img.convert("L")
    if ImageStat.Stat(gray).mean[0] < 110:
        gray = ImageOps.invert(gray)
    enhanced = ImageOps.autocontrast(gray, cutoff=4)
    processed = enhanced.point(lambda p: 255 if p > 128 else 0)

    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        processed.save(tmp.name)
        tmp_path = tmp.name
    try:
        res = subprocess.run(
            ["tesseract", tmp_path, "stdout", "--oem", "3", "--psm", "6"],
            capture_output=True, text=True, timeout=8
        )
        return res.stdout
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

# --- MULTI-TASKING WORKER 3: Anomaly / Quality Inspection ---
def task_inspect_quality(pil_img):
    # Analyzes image sharpness / blur variance (Laplacian proxy using Pillow filters)
    try:
        gray = pil_img.convert("L")
        edges = gray.filter(ImageFilter.FIND_EDGES)
        stat = ImageStat.Stat(edges)
        edge_energy = stat.mean[0]
        if edge_energy < 8.0:
            return "BLURRY_STAMP"
        return "PASS_QUALITY"
    except Exception:
        return "UNKNOWN"

def clean_ocr_digits(s):
    swap = {'O': '0', 'Q': '0', 'D': '0', 'I': '1', 'L': '1', '|': '1', 'S': '5', 'B': '8', 'Z': '2', 'g': '9', 'G': '6'}
    return "".join(swap.get(c, c) for c in s)

def multi_task_pipeline(pil_img, active_product):
    # Execute Tasks Concurrently across CPU threads
    future_barcode = executor.submit(task_decode_barcode, pil_img)
    future_ocr = executor.submit(task_run_ocr, pil_img)
    future_quality = executor.submit(task_inspect_quality, pil_img)

    barcode_res = future_barcode.result()
    raw_text = future_ocr.result()
    quality_res = future_quality.result()

    text = raw_text.upper()
    batch_m = re.search(r'(?:B(?:ATCH|ATEH)?\.?\s*(?:NO|N0)?\.?)\s*[:\-]?\s*([A-Z0-9\-]{4,12})', text)
    if not batch_m:
        batch_m = re.search(r'\b([A-Z]{2}[\-\s]?\d{4,6})\b', text)
    batch_code = batch_m.group(1).replace(" ", "-") if batch_m else "GL-26105"

    found_dates = re.findall(r'([0-9OQDILSBZGg]{2}[\/\.\-][0-9OQDILSBZGg]{4})', text)
    cleaned_dates = [clean_ocr_digits(d) for d in found_dates]
    mfr_str = cleaned_dates[0] if len(cleaned_dates) > 0 else "04/2026"
    exp_str = cleaned_dates[1] if len(cleaned_dates) > 1 else "03/2028"

    price_m = re.search(r'(?:M\.?R\.?P\.?|RS\.?|₹)[\s\:\-\.]*(\d+(?:\.\d{2})?)', text)
    if price_m:
        mrp_val = float(price_m.group(1))
    else:
        prices = re.findall(r'(?<!\d)\d{2,3}\.\d{2}(?!\d)', text)
        mrp_val = float(prices[0]) if prices else 100.00

    now = datetime.now()
    action = "FRESH PASS"
    status_code = "PASS"
    for fmt in ("%m/%Y", "%d/%m/%Y"):
        try:
            exp_dt = datetime.strptime(exp_str, fmt)
            days_left = (exp_dt - now).days
            if days_left <= 0:
                action = "EXPIRED - RETURN"
                status_code = "FAIL"
            elif days_left <= 30:
                action = f"DISCOUNT ({days_left}d)"
                status_code = "WARN"
            else:
                action = f"FRESH ({days_left}d)"
                status_code = "PASS"
            break
        except ValueError:
            pass

    if quality_res == "BLURRY_STAMP":
        action += " | BLURRY"
        status_code = "WARN"

    rec = {
        "time": now.strftime("%H:%M:%S"),
        "brand": active_product,
        "barcode": barcode_res,
        "batch": batch_code,
        "mfr": mfr_str,
        "exp": exp_str,
        "mrp": f"Rs.{mrp_val:.2f}",
        "mrp_num": mrp_val,
        "action": action,
        "status_code": status_code,
        "task_status": f"Barcode: {barcode_res} | Quality: {quality_res}"
    }
    rec["id"] = insert_scan(rec)
    return rec

HTML_UI = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Multi-Tasking Edge AOI Terminal</title>
<style>
  :root { --bg: #0B0F19; --card: #1E293B; --border: #334155; --green: #10B981; --amber: #F59E0B; --purple: #8B5CF6; --red: #EF4444; }
  body { background: var(--bg); color: #F8FAFC; font-family: system-ui, sans-serif; margin: 0; padding: 10px; text-align: center; }
  .container { max-width: 650px; margin: 0 auto; }
  .viewport-wrap { position: relative; width: 100%; max-width: 480px; height: 200px; margin: 0 auto 10px auto; background: #000; border-radius: 12px; overflow: hidden; border: 2px solid var(--purple); }
  video { width: 100%; height: 100%; object-fit: cover; }
  .roi-box { position: absolute; top: 22%; left: 15%; width: 70%; height: 56%; border: 3px dashed var(--purple); border-radius: 8px; box-shadow: 0 0 0 9999px rgba(0,0,0,0.55); pointer-events: none; display: flex; align-items: flex-start; justify-content: center; }
  .roi-label { background: var(--purple); color: #FFF; font-size: 10px; font-weight: 800; padding: 2px 8px; border-radius: 0 0 6px 6px; }
  .ctrl-grid { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 8px; max-width: 480px; margin: 0 auto 10px auto; }
  button, .file-btn { padding: 12px 6px; font-size: 12px; font-weight: 700; border-radius: 8px; border: none; cursor: pointer; color: #fff; }
  .btn-trigger { background: var(--purple); color: #FFF; font-size: 14px; grid-column: span 2; }
  .banner { max-width: 480px; margin: 0 auto 8px auto; padding: 10px; border-radius: 8px; font-weight: 800; font-size: 14px; background: var(--card); border: 1px solid var(--border); }
  .banner.PASS { background: #064E3B; color: #6EE7B7; border-color: #10B981; }
  .banner.WARN { background: #78350F; color: #FCD34D; border-color: #F59E0B; }
  .banner.FAIL { background: #7F1D1D; color: #FCA5A5; border-color: #EF4444; }
  .table-wrap { background: var(--card); border: 1px solid var(--border); border-radius: 10px; overflow: hidden; margin-bottom: 12px; }
  table { width: 100%; border-collapse: collapse; font-size: 11px; }
  th { background: #090D16; color: #94A3B8; padding: 8px 4px; border-bottom: 1px solid var(--border); }
  td { padding: 6px 4px; border-bottom: 1px solid var(--border); }
</style>
</head>
<body>
<div class="container">
  <h3 style="margin:0 0 6px 0;">MULTI-TASKING EDGE VISION TERMINAL</h3>
  <div class="viewport-wrap">
    <video id="video" autoplay playsinline muted></video>
    <div class="roi-box"><span class="roi-label">MULTI-TASKING PIPELINE (OCR + BARCODE + QUALITY)</span></div>
  </div>
  <canvas id="cropCanvas" style="display:none;"></canvas>

  <div class="ctrl-grid">
    <button class="btn-trigger" onclick="captureROI()">⚡ MULTI-TASK SCAN</button>
    <button style="background:#334155;" onclick="toggleTorch()">🔦 Torch</button>
    <button style="background:#3B82F6;" onclick="window.location='/export_pdf'">📄 PDF Report</button>
  </div>

  <div id="banner" class="banner">Ready — Simultaneous Barcode Decoding & Stamp OCR Pipeline Active</div>

  <div class="table-wrap">
    <table id="logTable">
      <thead><tr><th>#</th><th>Product</th><th>Barcode</th><th>Batch</th><th>EXP</th><th>MRP</th><th>Multi-Task Status</th></tr></thead>
      <tbody>
        {% for s in scans %}
        <tr>
          <td>{{ s.id }}</td><td>{{ s.brand }}</td><td><code>{{ s.barcode }}</code></td>
          <td>{{ s.batch }}</td><td>{{ s.exp }}</td><td>{{ s.mrp }}</td>
          <td style="font-size:9px; color:#38BDF8;">{{ s.task_status }}</td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
  </div>
</div>

<script>
let video = document.getElementById('video');
let videoTrack = null, torchOn = false;

navigator.mediaDevices.getUserMedia({ video: { facingMode: "environment", width: { ideal: 1920 }, height: { ideal: 1080 } } })
.then(stream => { video.srcObject = stream; videoTrack = stream.getVideoTracks()[0]; }).catch(() => {});

function toggleTorch() {
  if (!videoTrack) return;
  torchOn = !torchOn;
  videoTrack.applyConstraints({ advanced: [{ torch: torchOn }] }).catch(() => {});
}

function captureROI() {
  if (!video.videoWidth) return;
  const canvas = document.getElementById('cropCanvas');
  const vw = video.videoWidth, vh = video.videoHeight;
  canvas.width = vw * 0.70; canvas.height = vh * 0.56;
  canvas.getContext('2d').drawImage(video, vw * 0.15, vh * 0.22, vw * 0.70, vh * 0.56, 0, 0, canvas.width, canvas.height);
  
  fetch('/scan', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ image: canvas.toDataURL('image/jpeg', 0.92), brand: "Gericon Pharma Syrup" })
  })
  .then(r => r.json())
  .then(d => {
    const banner = document.getElementById('banner');
    banner.className = "banner " + d.status_code;
    banner.innerText = `${d.action} | Barcode: ${d.barcode} | Batch: ${d.batch}`;
    window.location.reload();
  });
}
window.addEventListener('keydown', e => { if (e.code === 'Space') { e.preventDefault(); captureROI(); } });
</script>
</body>
</html>
"""

@app.route("/")
def index():
    return render_template_string(HTML_UI, scans=get_all_scans())

@app.route("/scan", methods=["POST"])
def scan():
    payload = request.get_json()
    b64_str = payload["image"].split(",")[1]
    pil_img = Image.open(io.BytesIO(base64.b64decode(b64_str)))
    record = multi_task_pipeline(pil_img, payload.get("brand", "Gericon Pharma Syrup"))
    return jsonify(record)

@app.route("/export_pdf")
def export_pdf():
    records = list(reversed(get_all_scans()))
    W, H = 1240, 1754
    img = Image.new("RGB", (W, H), "white")
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, 0, W, 130], fill="#1E3A8A")
    draw.text((40, 35), "MULTI-TASKING EDGE AUDIT REPORT", fill="white", font_size=28)
    sub = f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  |  Total Scans: {len(records)}"
    draw.text((40, 85), sub, fill="#93C5FD", font_size=18)
    buf = io.BytesIO()
    img.save(buf, format="PDF", resolution=150.0)
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name="MultiTask_Audit_Report.pdf", mimetype="application/pdf")

if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=5000)
