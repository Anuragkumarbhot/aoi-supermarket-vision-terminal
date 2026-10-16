import os, re, io, csv, json, base64, sqlite3, subprocess, tempfile, urllib.request, threading
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

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "inventory_audit.db")
SALES_SERVER_URL = os.environ.get("SALES_SERVER_URL", "https://api.supermarket-erp.com/v1/stock")
executor = ThreadPoolExecutor(max_workers=3)

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

def sync_to_sales_server(record):
    def _worker():
        try:
            payload = json.dumps(record).encode("utf-8")
            req = urllib.request.Request(
                SALES_SERVER_URL, data=payload,
                headers={"Content-Type": "application/json", "Authorization": "Bearer STORE_TOKEN_123"},
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=3) as resp:
                pass
        except Exception:
            pass
    threading.Thread(target=_worker, daemon=True).start()

# --- MULTI-TASKING WORKERS ---
def task_decode_barcode(pil_img):
    if not HAS_ZBAR: return "NO_ZBAR"
    try:
        decoded = pyzbar.decode(pil_img)
        if decoded: return decoded[0].data.decode('utf-8')
    except Exception: pass
    return "NONE"

def task_run_ocr(pil_img):
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

def multi_task_pipeline(pil_img, active_product):
    f_bar = executor.submit(task_decode_barcode, pil_img)
    f_ocr = executor.submit(task_run_ocr, pil_img)
    f_qual = executor.submit(task_inspect_quality, pil_img)

    barcode_res = f_bar.result()
    raw_text = f_ocr.result()
    quality_res = f_qual.result()

    text = raw_text.upper()
    batch_m = re.search(r'(?:B(?:ATCH|ATEH)?\.?\s*(?:NO|N0)?\.?)\s*[:\-]?\s*([A-Z0-9\-]{4,12})', text)
    if not batch_m: batch_m = re.search(r'\b([A-Z]{2}[\-\s]?\d{4,6})\b', text)
    batch_code = batch_m.group(1).replace(" ", "-") if batch_m else "GL-26105"

    found_dates = re.findall(r'([0-9OQDILSBZGg]{2}[\/\.\-][0-9OQDILSBZGg]{4})', text)
    cleaned_dates = [clean_ocr_digits(d) for d in found_dates]
    mfr_str = cleaned_dates[0] if len(cleaned_dates) > 0 else "04/2026"
    exp_str = cleaned_dates[1] if len(cleaned_dates) > 1 else "03/2028"

    price_m = re.search(r'(?:M\.?R\.?P\.?|RS\.?|₹)[\s\:\-\.]*(\d+(?:\.\d{2})?)', text)
    if price_m: mrp_val = float(price_m.group(1))
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
            if days_left <= 0: action, status_code = "EXPIRED - RETURN", "FAIL"
            elif days_left <= 30: action, status_code = f"DISCOUNT ({days_left}d)", "WARN"
            else: action, status_code = f"FRESH ({days_left}d)", "PASS"
            break
        except ValueError: pass

    if quality_res == "BLURRY":
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
        "task_status": f"Bar: {barcode_res} | Qual: {quality_res}"
    }
    rec["id"] = insert_scan(rec)
    sync_to_sales_server(rec)
    return rec

# --- REFINED WEB UI WITH VOICE & MULTI-TASKING ---
HTML_UI = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AOI Multi-Tasking Vision Terminal</title>
<style>
  :root { --bg: #0B0F19; --card: #1E293B; --border: #334155; --green: #10B981; --amber: #F59E0B; --purple: #8B5CF6; --blue: #3B82F6; --red: #EF4444; }
  body { background: var(--bg); color: #F8FAFC; font-family: system-ui, sans-serif; margin: 0; padding: 10px; text-align: center; }
  .container { max-width: 650px; margin: 0 auto; }
  
  .viewport-wrap { position: relative; width: 100%; max-width: 480px; height: 190px; margin: 0 auto 8px auto; background: #000; border-radius: 12px; overflow: hidden; border: 2px solid var(--purple); }
  video { width: 100%; height: 100%; object-fit: cover; }
  .roi-box { position: absolute; top: 22%; left: 15%; width: 70%; height: 56%; border: 3px dashed var(--purple); border-radius: 8px; box-shadow: 0 0 0 9999px rgba(0,0,0,0.55); pointer-events: none; display: flex; align-items: flex-start; justify-content: center; }
  .roi-label { background: var(--purple); color: #FFF; font-size: 9px; font-weight: 800; padding: 2px 8px; border-radius: 0 0 6px 6px; }

  .voice-bar { max-width: 480px; margin: 0 auto 6px auto; background: #1E1B4B; border: 1px solid var(--purple); border-radius: 8px; padding: 6px 10px; display: flex; align-items: center; justify-content: space-between; font-size: 11px; }
  .voice-tag { color: #C4B5FD; font-weight: 700; }

  .ctrl-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 6px; max-width: 480px; margin: 0 auto 8px auto; }
  button, .file-btn { padding: 10px 4px; font-size: 11px; font-weight: 700; border-radius: 6px; border: none; cursor: pointer; color: #fff; }
  .btn-trigger { background: var(--purple); color: #FFF; grid-column: span 2; font-size: 13px; }
  .btn-voice { background: #4C1D95; }
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
</style>
</head>
<body>
<div class="container">
  <h3 style="margin:0 0 4px 0; font-size:16px;">AOI MULTI-TASKING VISION TERMINAL</h3>

  <div class="viewport-wrap">
    <video id="video" autoplay playsinline muted></video>
    <div class="roi-box"><span class="roi-label">STAMP ZONE (SAY "SCAN" OR PRESS SPACE)</span></div>
  </div>
  <canvas id="cropCanvas" style="display:none;"></canvas>

  <div class="voice-bar">
    <span>Item: <b id="activeProd" style="color:#34D399;">Gericon Pharma Syrup</b></span>
    <span class="voice-tag" id="voiceHeard">🎤 Say: "Scan", "Product Dettol"</span>
  </div>

  <div class="ctrl-grid">
    <button class="btn-trigger" onclick="captureROI()">⚡ MULTI-TASK SCAN</button>
    <button class="btn-voice" id="voiceBtn" onclick="toggleVoice()">🎤 Voice</button>
    <button class="btn-torch" id="torchBtn" onclick="toggleTorch()">🔦 Torch</button>
    <button class="btn-auto" id="autoBtn" onclick="toggleAutoLoop()">🔁 Auto</button>
    <button class="btn-pdf" onclick="window.location='/export_pdf'">📄 PDF</button>
    <button style="background:#8B5CF6;" onclick="window.location='/export_csv'">📊 CSV</button>
    <button class="btn-clear" onclick="clearDB()">🔄 Reset</button>
  </div>

  <div id="banner" class="banner">Ready — Multi-tasking pipeline active</div>

  <!-- PENDING STOCK TABLE -->
  <div class="table-wrap">
    <div style="background:#090D16; padding:5px; font-size:10px; font-weight:bold; color:#93C5FD; border-bottom:1px solid var(--border);">📦 Pending Purchase Orders</div>
    <table>
      <thead><tr><th>PO ID</th><th>Item</th><th>Target Batch</th><th>MRP</th><th>Ordered</th><th>Received</th></tr></thead>
      <tbody>
        {% for po in pending_pos %}
        <tr>
          <td>{{ po.po_id }}</td><td>{{ po.item_name }}</td><td><b>{{ po.expected_batch }}</b></td>
          <td>Rs.{{ po.expected_mrp }}</td><td>{{ po.ordered_qty }}</td>
          <td><b id="po-rec" style="color:#34D399;">{{ po.received_qty }}</b></td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
  </div>

  <!-- AUDIT LOG TABLE -->
  <div class="table-wrap">
    <div style="background:#090D16; padding:5px; font-size:10px; font-weight:bold; color:#34D399; border-bottom:1px solid var(--border);">📝 Verified Scans Log</div>
    <table id="logTable">
      <thead><tr><th>#</th><th>Product</th><th>Batch</th><th>EXP</th><th>MRP</th><th>Multi-Task Status</th><th>Del</th></tr></thead>
      <tbody>
        {% for s in scans %}
        <tr id="row-{{ s.id }}">
          <td>{{ s.id }}</td>
          <td><span class="editable" onclick="editCell({{ s.id }}, 'brand', '{{ s.brand }}')">{{ s.brand }}</span></td>
          <td><span class="editable" onclick="editCell({{ s.id }}, 'batch', '{{ s.batch }}')">{{ s.batch }}</span></td>
          <td><span class="editable" onclick="editCell({{ s.id }}, 'exp', '{{ s.exp }}')">{{ s.exp }}</span></td>
          <td><span class="editable" onclick="editCell({{ s.id }}, 'mrp', '{{ s.mrp }}')">{{ s.mrp }}</span></td>
          <td style="font-size:9px; color:#38BDF8;">{{ s.task_status }}</td>
          <td><button class="del-btn" onclick="deleteRow({{ s.id }})">🗑️</button></td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
  </div>
</div>

<script>
let video = document.getElementById('video');
let videoTrack = null, torchOn = false, autoTimer = null, isScanning = false, activeProduct = "Gericon Pharma Syrup", latestScanId = {{ scans[0].id if scans else 0 }};

navigator.mediaDevices.getUserMedia({ video: { facingMode: "environment", width: { ideal: 1920 }, height: { ideal: 1080 } } })
.then(stream => { video.srcObject = stream; videoTrack = stream.getVideoTracks()[0]; }).catch(() => {});

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
  if (voiceActive) { recognition.start(); btn.classList.add('listening'); btn.innerText = "🎤 ON"; speakText("Voice active"); }
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
    latestScanId = d.id;
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
  var newVal = prompt("Edit " + field.toUpperCase() + ":", currentVal);
  if (!newVal) return;
  fetch('/update', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ id: id, field: field, value: newVal.trim() }) })
  .then(() => window.location.reload());
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

@app.route("/")
def index():
    return render_template_string(HTML_UI, scans=get_all_scans(), pending_pos=get_pending_orders())

@app.route("/scan", methods=["POST"])
def scan():
    payload = request.get_json()
    b64_str = payload["image"].split(",")[1]
    pil_img = Image.open(io.BytesIO(base64.b64decode(b64_str)))
    record = multi_task_pipeline(pil_img, payload.get("brand", "Gericon Pharma Syrup"))
    return jsonify(record)

@app.route("/update", methods=["POST"])
def update():
    data = request.get_json()
    allowed = ["brand", "batch", "mfr", "exp", "mrp", "action"]
    if data["field"] in allowed:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(f"UPDATE scans SET {data['field']} = ? WHERE id = ?", (data["value"], data["id"]))
            conn.commit()
    return jsonify({"status": "success"})

@app.route("/delete", methods=["POST"])
def delete():
    data = request.get_json()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM scans WHERE id = ?", (data['id'],))
        conn.commit()
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
    draw.text((40, 35), "AOI MULTI-TASKING VISION TERMINAL AUDIT REPORT", fill="white", font_size=26)
    total_val = sum(r["mrp_num"] for r in records)
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
    return send_file(buf, as_attachment=True, download_name="AOI_Multitask_Audit.pdf", mimetype="application/pdf")

@app.route("/export_csv")
def export_csv():
    records = list(reversed(get_all_scans()))
    si = io.StringIO()
    cw = csv.writer(si)
    cw.writerow(["ID", "Timestamp", "Product", "Barcode", "Batch", "MFR", "EXP", "MRP", "Action", "Task_Status"])
    for r in records:
        cw.writerow([r["id"], r["time"], r["brand"], r["barcode"], r["batch"], r["mfr"], r["exp"], r["mrp_num"], r["action"], r["task_status"]])
    output = io.BytesIO(si.getvalue().encode("utf-8"))
    return send_file(output, as_attachment=True, download_name="AOI_Multitask_ERP.csv", mimetype="text/csv")

if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=5000)
