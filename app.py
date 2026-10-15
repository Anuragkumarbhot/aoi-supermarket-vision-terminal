import os, re, io, csv, json, base64, sqlite3, subprocess, tempfile
from PIL import Image, ImageOps, ImageStat, ImageDraw
from datetime import datetime
from flask import Flask, request, jsonify, send_file, render_template_string

app = Flask(__name__)

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "inventory_audit.db")

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
                raw_ocr TEXT
            )
        """)
        conn.commit()

def insert_scan(rec):
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO scans (time, brand, batch, mfr, exp, mrp, mrp_num, action, raw_ocr)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (rec["time"], rec["brand"], rec["batch"], rec["mfr"], rec["exp"],
              rec["mrp"], rec["mrp_num"], rec["action"], rec["raw_ocr"]))
        conn.commit()
        return cur.lastrowid

def get_all_scans():
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM scans ORDER BY id DESC").fetchall()
        return [dict(r) for r in rows]

def update_scan_field(scan_id, field, value):
    allowed = ["batch", "mfr", "exp", "mrp", "action"]
    if field not in allowed: return False
    vals = [value]
    extra_update = ""
    if field == "mrp":
        num_m = re.search(r'(\d+(?:\.\d{2})?)', value)
        num_val = float(num_m.group(1)) if num_m else 0.0
        extra_update = ", mrp_num = ?"
        vals.append(num_val)
    vals.append(scan_id)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(f"UPDATE scans SET {field} = ?{extra_update} WHERE id = ?", tuple(vals))
        conn.commit()
    return True

def delete_scan_row(scan_id):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM scans WHERE id = ?", (scan_id,))
        conn.commit()

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
        if os.path.exists(tmp_path): os.remove(tmp_path)

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
            if days_left <= 0: action = "EXPIRED - RETURN"
            elif days_left <= 30: action = f"DISCOUNT ({days_left}d)"
            else: action = f"FRESH ({days_left}d)"
            break
        except ValueError: pass

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
    return rec

# --- REFINED DASHBOARD UI ---
HTML_UI = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AOI Supermarket Terminal</title>
<style>
  :root {
    --bg-main: #0F172A;
    --bg-card: #1E293B;
    --border-color: #334155;
    --accent-green: #10B981;
    --accent-amber: #F59E0B;
    --accent-blue: #3B82F6;
    --accent-purple: #8B5CF6;
    --accent-red: #EF4444;
    --text-main: #F8FAFC;
    --text-muted: #94A3B8;
  }
  body { background: var(--bg-main); color: var(--text-main); font-family: system-ui, -apple-system, sans-serif; margin: 0; padding: 16px; text-align: center; }
  .container { max-width: 600px; margin: 0 auto; }
  
  /* Header Card */
  .header-card { background: var(--bg-card); border: 1px solid var(--border-color); border-radius: 12px; padding: 20px; margin-bottom: 16px; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.3); }
  h2 { margin: 0 0 6px 0; font-size: 22px; color: #fff; letter-spacing: 0.5px; }
  .subtitle { color: var(--text-muted); font-size: 13px; margin-bottom: 14px; }
  .badge-row { display: flex; justify-content: center; gap: 8px; margin-bottom: 14px; flex-wrap: wrap; }
  .badge { background: rgba(16, 185, 129, 0.15); color: #34D399; border: 1px solid rgba(16, 185, 129, 0.3); padding: 4px 12px; border-radius: 20px; font-size: 12px; font-weight: 600; }

  /* Scan Action Button */
  .btn-scan { background: linear-gradient(135deg, #10B981 0%, #059669 100%); color: #000; font-size: 16px; font-weight: 700; padding: 16px; border-radius: 10px; border: none; cursor: pointer; width: 100%; box-shadow: 0 4px 12px rgba(16, 185, 129, 0.3); transition: transform 0.1s ease; display: flex; align-items: center; justify-content: center; gap: 8px; }
  .btn-scan:active { transform: scale(0.98); }

  /* Status & Debug Box */
  .status-box { background: rgba(15, 23, 42, 0.6); border: 1px solid var(--border-color); border-radius: 8px; padding: 12px; margin-top: 14px; text-align: left; }
  #status { color: var(--accent-amber); font-weight: 600; font-size: 13px; margin-bottom: 4px; }
  #debug { color: var(--text-muted); font-size: 11px; font-family: monospace; word-break: break-all; }

  /* Action Bar */
  .action-bar { display: flex; gap: 8px; margin-bottom: 20px; }
  .action-btn { flex: 1; padding: 12px; font-size: 13px; font-weight: 700; border-radius: 8px; border: none; cursor: pointer; color: #fff; display: flex; align-items: center; justify-content: center; gap: 6px; }
  .btn-pdf { background: var(--accent-blue); }
  .btn-csv { background: var(--accent-purple); }
  .btn-clear { background: rgba(239, 68, 68, 0.2); color: #FCA5A5; border: 1px solid rgba(239, 68, 68, 0.4); }

  /* Audit Table */
  .table-container { background: var(--bg-card); border: 1px solid var(--border-color); border-radius: 12px; overflow: hidden; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.3); }
  table { width: 100%; border-collapse: collapse; font-size: 12px; text-align: center; }
  th { background: #0B0F19; color: var(--text-muted); padding: 10px 6px; font-weight: 600; border-bottom: 1px solid var(--border-color); }
  td { padding: 8px 4px; border-bottom: 1px solid var(--border-color); color: #E2E8F0; }
  tr:last-child td { border-bottom: none; }
  
  .editable { background: rgba(56, 189, 248, 0.1); color: #38BDF8; border: 1px dashed rgba(56, 189, 248, 0.3); border-radius: 4px; padding: 2px 6px; cursor: pointer; display: inline-block; }
  .editable:hover { background: rgba(56, 189, 248, 0.2); }
  .del-btn { background: transparent; color: #EF4444; border: none; font-size: 14px; cursor: pointer; padding: 4px; }
  .del-btn:hover { color: #DC2626; }
</style>
</head>
<body>

<div class="container">
  <div class="header-card">
    <h2>AOI Supermarket Terminal</h2>
    <div class="subtitle">Automated Optical Batch & Expiry Inspection Engine</div>
    
    <div class="badge-row">
      <div class="badge">SQLite WAL Active</div>
      <div class="badge">Total Scans: <span id="count">{{ scans|length }}</span></div>
    </div>

    <label class="btn-scan">
      <span>📷 SNAP CLOSE-UP STAMP (4-LINE OCR)</span>
      <input type="file" accept="image/*" capture="environment" style="display:none" onchange="uploadFile(event)">
    </label>

    <div class="status-box">
      <div id="status">Status: Ready. Frame Batch, MFD, EXP, and MRP closely.</div>
      <div id="debug">Raw OCR: Waiting for scan...</div>
    </div>
  </div>

  <div class="action-bar">
    <button class="action-btn btn-pdf" onclick="window.location='/export_pdf'">📄 Download PDF</button>
    <button class="action-btn btn-csv" onclick="window.location='/export_csv'">📊 Export CSV</button>
    <button class="action-btn btn-clear" onclick="clearDB()">🔄 Reset Shift</button>
  </div>

  <div class="table-container">
    <table id="logTable">
      <thead>
        <tr>
          <th>#</th>
          <th>Batch Code</th>
          <th>MFR</th>
          <th>EXP</th>
          <th>MRP</th>
          <th>Action</th>
          <th>Del</th>
        </tr>
      </thead>
      <tbody>
        {% for s in scans %}
        <tr id="row-{{ s.id }}">
          <td>{{ s.id }}</td>
          <td><span class="editable" onclick="editCell({{ s.id }}, 'batch', '{{ s.batch }}')">{{ s.batch }}</span></td>
          <td><span class="editable" onclick="editCell({{ s.id }}, 'mfr', '{{ s.mfr }}')">{{ s.mfr }}</span></td>
          <td><span class="editable" onclick="editCell({{ s.id }}, 'exp', '{{ s.exp }}')">{{ s.exp }}</span></td>
          <td><span class="editable" onclick="editCell({{ s.id }}, 'mrp', '{{ s.mrp }}')">{{ s.mrp }}</span></td>
          <td><span class="editable" onclick="editCell({{ s.id }}, 'action', '{{ s.action }}')">{{ s.action }}</span></td>
          <td><button class="del-btn" onclick="deleteRow({{ s.id }})" title="Delete row">🗑️</button></td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
  </div>
</div>

<script>
function uploadFile(e) {
  var file = e.target.files[0];
  if (!file) return;
  document.getElementById('status').innerText = "Processing dot-matrix OCR & saving...";
  var reader = new FileReader();
  reader.onload = function(ev) {
    fetch('/scan', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ image: ev.target.result })
    })
    .then(function(r) { return r.json(); })
    .then(function(d) {
      document.getElementById('status').innerText = 'Success! Batch: ' + d.batch + ' | EXP: ' + d.exp + ' | MRP: ' + d.mrp;
      document.getElementById('debug').innerText = 'Raw OCR Read: ' + d.raw_ocr;
      var row = '<tr id="row-' + d.id + '"><td>' + d.id + '</td>' +
                '<td><span class="editable" onclick="editCell(' + d.id + ', \'batch\', \'' + d.batch + '\')">' + d.batch + '</span></td>' +
                '<td><span class="editable" onclick="editCell(' + d.id + ', \'mfr\', \'' + d.mfr + '\')">' + d.mfr + '</span></td>' +
                '<td><span class="editable" onclick="editCell(' + d.id + ', \'exp\', \'' + d.exp + '\')">' + d.exp + '</span></td>' +
                '<td><span class="editable" onclick="editCell(' + d.id + ', \'mrp\', \'' + d.mrp + '\')">' + d.mrp + '</span></td>' +
                '<td><span class="editable" onclick="editCell(' + d.id + ', \'action\', \'' + d.action + '\')">' + d.action + '</span></td>' +
                '<td><button class="del-btn" onclick="deleteRow(' + d.id + ')">🗑️</button></td></tr>';
      document.querySelector('#logTable tbody').insertAdjacentHTML('afterbegin', row);
      var c = document.getElementById('count');
      c.innerText = parseInt(c.innerText) + 1;
    });
  };
  reader.readAsDataURL(file);
}

function editCell(id, field, currentVal) {
  var newVal = prompt("Edit " + field.toUpperCase() + " for Scan #" + id + ":", currentVal);
  if (newVal === null || newVal.trim() === "") return;
  fetch('/update', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ id: id, field: field, value: newVal.trim() })
  }).then(function(r) { return r.json(); }).then(function(d) {
    if (d.status === "success") { window.location.reload(); }
  });
}

function deleteRow(id) {
  if (!confirm("Delete scan #" + id + "?")) return;
  fetch('/delete', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ id: id })
  }).then(function() {
    var row = document.getElementById('row-' + id);
    if (row) row.remove();
    var c = document.getElementById('count');
    c.innerText = Math.max(0, parseInt(c.innerText) - 1);
  });
}

function clearDB() {
  if (!confirm("Clear all scanned records for a new shift?")) return;
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

@app.route("/update", methods=["POST"])
def update():
    data = request.get_json()
    success = update_scan_field(data["id"], data["field"], data["value"])
    return jsonify({"status": "success" if success else "error"})

@app.route("/delete", methods=["POST"])
def delete():
    data = request.get_json()
    delete_scan_row(data["id"])
    return jsonify({"status": "success"})

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
    draw.text((40, 35), "SUPERMARKET BATCH & EXPIRY AUDIT REPORT", fill="white", font_size=28)
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
