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

The setup script installs Python, Tesseract, Pillow, and Flask. The application listens on port 5000.

## Data and exports

The application creates `inventory_audit.db` beside `app.py`. Scan records and pending purchase-order rows are stored in SQLite. The export endpoints generate:

- `v3_Neural_Audit_Report.pdf`
- `v3_Neural_ERP.csv`

The PDF is generated from the current scan history; the CSV includes timestamps, product, barcode, batch, manufacturing and expiry dates, MRP, action, and task status.

## Important deployment and safety notes

- Do not treat OCR output as verified stock data without human review and representative accuracy testing.
- The source currently contains a placeholder sales-server URL and a hard-coded placeholder bearer credential in the sync routine. **Do not enable external synchronization in production until the endpoint and credential handling are replaced with secure environment-based configuration.**
- The development server is started with `host="0.0.0.0"`. Do not expose it to an untrusted network without authentication, authorization, request-size limits, and transport security.
- Validate date formats, MRP parsing, batch matching, and FEFO decisions against real product labels and store procedures.
- This project is an inventory-assistance prototype, not a replacement for the store's authoritative ERP or receiving controls.

## Project specification

See [PROJECT_SPECIFICATION.md](PROJECT_SPECIFICATION.md) for the system specification and sample validation record.

---

Built by [Anuragkumarbhot](https://github.com/Anuragkumarbhot).
