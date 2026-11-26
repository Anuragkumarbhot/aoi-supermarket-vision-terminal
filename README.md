# AOI Supermarket Vision Terminal

**Offline-first edge vision prototype for stock inspection and expiry-aware inventory reconciliation**

AOI Supermarket Vision Terminal is a Python/Flask application intended to inspect product stamp information from a camera capture, record batch and date fields, reconcile scans against pending purchase-order data, and export audit records.

The repository includes a Flask application, an Android Termux setup script, and a project specification. The current implementation is a prototype: validate OCR accuracy, inventory rules, and deployment security before using it for live stock decisions.

## Current workflow

1. Open the local Flask dashboard.
2. Capture a product label region from the camera.
3. Run the image-processing and OCR pipeline.
4. Review the extracted product, barcode, batch, manufacturing date, expiry date, MRP, and action/status fields.
5. Correct or delete scan records when needed.
6. Export scan history as PDF or CSV.

The browser UI also includes optional voice commands where the browser supports the Web Speech API.

## Implemented application routes

| Route | Method | Purpose |
|---|---|---|
| `/` | GET | Dashboard, scan history, and pending purchase orders |
| `/scan` | POST | Process a submitted image and return the scan result |
| `/update` | POST | Edit supported scan fields |
| `/delete` | POST | Delete one scan record |
| `/clear` | POST | Clear scan history |
| `/export_pdf` | GET | Download a PDF audit report |
| `/export_csv` | GET | Download scan records as CSV |

## OCR validation and purchase-order reconciliation

- OCR failures are recorded as blank fields; the application does not invent batch, manufacturing date, expiry date, or MRP values.
- A scan is marked **REVIEW REQUIRED** when a required field is missing or invalid, the manufacturing date is not earlier than expiry, the MRP is outside the accepted positive range, or image quality is not PASS.
- A scan increments a pending purchase order only when its extracted batch matches the expected batch, its MRP matches the expected MRP (within 0.005), and the order has not reached its ordered quantity.
- Batch matches with a different MRP are flagged **MRP MISMATCH**. Other scans remain in the audit log but do not change received quantities.
- These checks are conservative safeguards, not proof that OCR is accurate. Operators must verify label fields and physical quantities before accepting stock.

## Technology

- Python
- Flask
- Pillow for image processing
- Tesseract OCR (system package on Termux)
- SQLite with WAL journal mode
- Browser camera capture and optional Web Speech API

## Run on Android with Termux

1. Install [Termux](https://termux.dev/).
2. Clone this repository and enter its directory.
3. Run the setup script:

```bash
bash setup_termux.sh
```

4. Start the application:

```bash
python app.py
```

5. Open `http://127.0.0.1:5000` in the device browser.

The setup script installs Python, Tesseract, Pillow, and Flask. By default, the application listens only on `127.0.0.1:5000` so it is accessible from the same device.

## Data and exports

The application creates `inventory_audit.db` beside `app.py`. Scan records and pending purchase-order rows are stored in SQLite. The server accepts request bodies up to 8 MiB; oversized requests are rejected. The export endpoints generate:

- `v3_Neural_Audit_Report.pdf`
- `v3_Neural_ERP.csv`

The PDF is generated from the current scan history; the CSV includes timestamps, product, barcode, batch, manufacturing and expiry dates, MRP, action, and task status.

## Important deployment and safety notes

- Do not treat OCR output as verified stock data without human review and representative accuracy testing.
- External sales synchronization is **disabled by default**. It starts only when both `AOI_SALES_SYNC_URL` and `AOI_SALES_SYNC_TOKEN` are configured. The URL must use HTTPS. Never commit real credentials to the repository; inject them through the runtime environment or a secrets manager.
- The hard-coded placeholder bearer token and default placeholder sales endpoint have been removed from the application.
- The Flask process binds to `127.0.0.1` by default and refuses non-localhost `AOI_HOST` values. For remote access, keep the app local and place it behind a separately configured reverse proxy with authentication, authorization, TLS, and network restrictions. The built-in Flask server is for development, not production.
- Image upload requests are limited to 8 MiB and the scan route validates the JSON payload, image data URL, base64 data, and decoded image before processing.
- Optional operator authentication is available through `AOI_AUTH_USERNAME` and `AOI_AUTH_PASSWORD`. Set both to enable HTTP Basic authentication for the dashboard, scan, edit, delete, reset, and export routes; the browser will prompt for credentials. If either variable is set without the other, the app refuses to start.
- Authentication is not enabled when both variables are unset. In that mode, use the dashboard only on the local device. Basic authentication does not provide roles or multi-user audit identity, and it must not be exposed over plain HTTP on an untrusted network.
- These controls are defense-in-depth, not a production security certification. Do not expose the dashboard publicly.
- Validate date formats, MRP parsing, batch matching, and FEFO decisions against real product labels and store procedures.
- This project is an inventory-assistance prototype, not a replacement for the store's authoritative ERP or receiving controls.

## Project specification

See [PROJECT_SPECIFICATION.md](PROJECT_SPECIFICATION.md) for the system specification and sample validation record.

---

Built by [Anuragkumarbhot](https://github.com/Anuragkumarbhot).
