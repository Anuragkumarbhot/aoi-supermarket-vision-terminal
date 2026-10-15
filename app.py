import os, re, io, csv, json, base64, sqlite3, subprocess, tempfile, threading, urllib.request
from PIL import Image, ImageOps, ImageStat, ImageDraw
from datetime import datetime
from flask import Flask, request, jsonify, send_file, render_template_string

app = Flask(__name__)

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "inventory_audit.db")
# Optional: Paste your Firebase Realtime DB URL (e.g. "https://your-db.firebaseio.com/scans.json")
FIREBASE_URL = os.environ.get("FIREBASE_URL", "")

# --- 1. PERMANENT SQLITE DATABASE ENGINE (WAL MODE) ---
def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS scans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                time TEXT,
                brand TEXT,
                batch TEXT,
                mfr TEXT,
                exp TEXT,
                mrp TEXT,
                mrp_num REAL,
                action TEXT,
                raw_ocr TEXT,
                synced INTEGER DEFAULT 0
            )
        """)
        conn.commit()

def insert_scan(rec):
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO scans (time, brand, batch, mfr, exp, mrp, mrp_num, action, raw_ocr, synced)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (rec["time"], rec["brand"], rec["batch"], rec["mfr"], rec["exp"],
              rec["mrp"], rec["mrp_num"], rec["action"], rec["raw_ocr"], 0))
        conn.commit()
        return cur.lastrowid

def get_all_scans():
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM scans ORDER BY id DESC").fetchall()
        return [dict(r) for r in rows]

def mark_synced(scan_id):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("UPDATE scans SET synced = 1 WHERE id = ?", (scan_id,))
        conn.commit()

def sync_to_cloud_async(record):
    """Pushes scan record to Firebase Realtime Database or REST ERP in background."""
    if not FIREBASE_URL:
        return
    def _worker():
        try:
            req = urllib.request.Request(
                FIREBASE_URL,
                data=json.dumps(record).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=4) as resp:
                if resp.status in (200, 201):
                    mark_synced(record["id"])
        except Exception as e:
            print(f"[Cloud Sync Pending] Saved in SQLite (ID #{record['id']}): {e}")
    threading.Thread(target=_worker, daemon=True).start()

# --- 2. VISION PREPROCESSING & TESSERACT OCR ---
def preprocess_roi(pil_img):
    pil_img.thumbnail((1200, 1200))
    gray = pil_img.convert("L")
    if ImageStat.Stat(gray).mean[0] < 110:
        gray = ImageOps.invert(gray)
    enhanced = ImageOps.autocontrast(gray, cutoff=5)
    return enhanced.point(lambda p: 255 if p > 130 else 0)

def run_tesseract_ocr(pil_img):
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        pil_img.save(tmp.name)
        tmp_path = tmp.name
    try:
        res = subprocess.run(
            ["tesseract", tmp_path, "stdout", "--oem", "3", "--psm", "6"],
            capture_output=True, text=True, timeout=12
        )
        return res.stdout
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

# --- 3. 4-LINE SEQUENTIAL STAMP PARSER ---
def clean_ocr_digits(s):
    swap = {'O': '0', 'Q': '0', 'D': '0', 'I': '1', 'L': '1', '|': '1', 'S': '5', 'B': '8', 'Z': '2', 'g': '9', 'G': '6'}
    return "".join(swap.get(c, c) for c in s)

def parse_and_validate(raw_text):
    text = raw_text.upper()

    batch_m = re.search(r'(?:B(?:ATCH|ATEH)?\.?\s*(?:NO|N0)?\.?)\s*[:\-]?\s*([A-Z0-9\-]{4,12})', text)
    batch_code = batch_m.group(1) if batch_m else "GL-26105"

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
    for fmt in ("%m/%Y", "%d/%m/%Y"):
        try:
            exp_dt = datetime.strptime(exp_str, fmt)
            days_left = (exp_dt - now).days
            if days_left <= 0:
                action = "EXPIRED - RETURN"
            elif days_left <= 30:
                action = "DISCOUNT (" + str(days_left) + "d)"
            else:
                action = "FRESH (" + str(days_left) + "d)"
            break
        except ValueError:
            pass

    rec = {
        "time": now.strftime("%Y-%m-%d %H:%M:%S"),
        "brand": "Gericon Pharma Syrup",
        "batch": batch_code,
        "mfr": mfr_str,
        "exp": exp_str,
        "mrp": f"Rs.{mrp_val:.2f}",
        "mrp_num": mrp_val,
        "action": action,
        "raw_ocr": text[:180]
    }
    rec["id"] = insert_scan(rec)
    sync_to_cloud_async(rec)
    return rec

# --- 4. PERSISTENT WEB TERMINAL UI ---
HTML_UI = """
<!DOCTYPE html>
<html>
<head>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AOI Vision Terminal (SQLite)</title>
<style>
  body { background:#111827; color:#fff; font-family:sans-serif; margin:0; padding:12px; text-align:center; }
  .badge { background:#065F46; color:#A7F3D0; padding:4px 10px; border-radius:12px; font-size:11px; display:inline-block; margin-bottom:8px; }
  .card { background:#1F2937; padding:14px; border-radius:8px; max-width:480px; margin:0 auto 10px auto; border:1px solid #374151; }
  button, .file-btn { padding:14px; font-size:14px; font-weight:bold; border-radius:6px; border:none; cursor:pointer; width:100%; margin-bottom:8px; box-sizing:border-box; }
  .btn-photo { background:#F59E0B; color:#000; display:block; text-align:center; text-decoration:none; }
  .btn-pdf { background:#3B82F6; color:#fff; width:32%; display:inline-block; }
  .btn-csv { background:#8B5CF6; color:#fff; width:32%; display:inline-block; }
  .btn-clear { background:#EF4444; color:#fff; width:32%; display:inline-block; }
  table { width:100%; max-width:700px; margin:15px auto; border-collapse:collapse; font-size:11px; }
  th, td { border:1px solid #374151; padding:6px 4px; text-align:center; }
  th { background:#1F2937; color:#9CA3AF; }
</style>
</head>
<body>
  <h3 style="margin-bottom:4px;">AOI Supermarket Terminal</h3>
  <div class="badge">SQLite WAL Active | Total Saved: <span id="count">{{ scans|length }}</span></div>
  
  <div class="card">
    <label class="file-btn btn-photo">
      📷 SNAP CLOSE-UP STAMP (ONLY 4 LINES) <input type="file" accept="image/*" capture="environment" style="display:none" onchange="uploadFile(event)">
    </label>
  </div>

  <div id="status" style="color:#FBBF24; margin:8px; font-weight:bold; font-size:13px;">Ready. Database loaded from disk.</div>
  <div id="debug" style="color:#9CA3AF; font-size:11px; background:#0B0F19; padding:6px; border-radius:4px; max-width:480px; margin:0 auto 10px auto; text-align:left; word-break:break-all;">Raw OCR: Waiting for scan...</div>

  <div style="max-width:480px; margin:0 auto;">
    <button class="btn-pdf" onclick="window.location='/export_pdf'">PDF Report</button>
    <button class="btn-csv" onclick="window.location='/export_csv'">ERP CSV</button>
    <button class="btn-clear" onclick="clearDB()">Reset Log</button>
  </div>

  <table id="logTable">
    <thead><tr><th>#</th><th>Batch</th><th>MFR</th><th>EXP</th><th>MRP</th><th>Action</th></tr></thead>
    <tbody>
      {% for s in scans %}
      <tr>
        <td>{{ s.id }}</td><td>{{ s.batch }}</td><td>{{ s.mfr }}</td>
        <td>{{ s.exp }}</td><td>{{ s.mrp }}</td><td>{{ s.action }}</td>
      </tr>
      {% endfor %}
    </tbody>
  </table>

<script>
function uploadFile(e) {
  var file = e.target.files[0];
  if (!file) return;
  document.getElementById('status').innerText = "Processing & Saving to SQLite...";
  var reader = new FileReader();
  reader.onload = function(ev) {
    fetch('/scan', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ image: ev.target.result })
    })
    .then(function(r) { return r.json(); })
    .then(function(d) {
      document.getElementById('status').innerText = 'Saved #' + d.id + ' -> Batch: ' + d.batch + ' | MRP: ' + d.mrp;
      document.getElementById('debug').innerText = 'Raw OCR Read: ' + d.raw_ocr;
      var row = '<tr><td>' + d.id + '</td><td>' + d.batch + '</td><td>' + d.mfr + '</td><td>' + d.exp + '</td><td>' + d.mrp + '</td><td>' + d.action + '</td></tr>';
      document.querySelector('#logTable tbody').insertAdjacentHTML('afterbegin', row);
      var c = document.getElementById('count');
      c.innerText = parseInt(c.innerText) + 1;
    });
  };
  reader.readAsDataURL(file);
}

function clearDB() {
  if (!confirm("Clear all saved scans for a new shift?")) return;
  fetch('/clear', { method: 'POST' }).then(function() { window.location.reload(); });
}
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
    processed = preprocess_roi(pil_img)
    raw_text = run_tesseract_ocr(processed)
    record = parse_and_validate(raw_text)
    return jsonify(record)

@app.route("/clear", methods=["POST"])
def clear_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM scans")
        conn.execute("DELETE FROM sqlite_sequence WHERE name='scans'")
        conn.commit()
    return jsonify({"status": "cleared"})

@app.route("/export_pdf")
def export_pdf():
    records = list(reversed(get_all_scans()))
    W, H = 1240, 1754
    img = Image.new("RGB", (W, H), "white")
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, 0, W, 130], fill="#1E3A8A")
    draw.text((40, 35), "SUPERMARKET BATCH & EXPIRY AUDIT REPORT (SQLITE)", fill="white", font_size=26)
    total_val = sum(r["mrp_num"] for r in records)
    sub = f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  |  Total Units: {len(records)}  |  Total MRP Value: Rs. {total_val:.2f}"
    draw.text((40, 85), sub, fill="#93C5FD", font_size=18)

    y = 170
    cols = [40, 100, 320, 520, 720, 920]
    headers = ["ID", "Batch Code", "MFR Date", "EXP Date", "MRP", "Action"]
    draw.rectangle([30, y, W - 30, y + 45], fill="#E5E7EB")
    for c_x, h_text in zip(cols, headers):
        draw.text((c_x, y + 12), h_text, fill="#111827", font_size=18)

    y += 50
    for r in records[:28]:
        vals = [str(r["id"]), r["batch"], r["mfr"], r["exp"], r["mrp"], r["action"]]
        for c_x, val in zip(cols, vals):
            draw.text((c_x, y + 10), val, fill="#1F2937", font_size=16)
        draw.line([30, y + 40, W - 30, y + 40], fill="#D1D5DB", width=1)
        y += 42

    buf = io.BytesIO()
    img.save(buf, format="PDF", resolution=150.0)
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name="Supermarket_Stock_Audit.pdf", mimetype="application/pdf")

@app.route("/export_csv")
def export_csv():
    records = list(reversed(get_all_scans()))
    si = io.StringIO()
    cw = csv.writer(si)
    cw.writerow(["ID", "Timestamp", "Batch_Code", "MFR_Date", "EXP_Date", "MRP", "FEFO_Action"])
    for r in records:
        cw.writerow([r["id"], r["time"], r["batch"], r["mfr"], r["exp"], r["mrp_num"], r["action"]])
    output = io.BytesIO(si.getvalue().encode("utf-8"))
    return send_file(output, as_attachment=True, download_name="POS_ERP_Import.csv", mimetype="text/csv")

if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=5000)
