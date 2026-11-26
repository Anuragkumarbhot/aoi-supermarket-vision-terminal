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
| `/purchase-orders` | GET / POST | List purchase-order lines or create a PO line |
| `/purchase-orders/import` | POST | Import up to 500 purchase-order lines from CSV (all-or-nothing) |
| `/purchase-orders/{line_id}` | PUT | Edit supplier, schedule, order and item details subject to receiving locks |
| `/purchase-orders/{line_id}/receive` | POST | Record a counted partial or full delivery |
| `/purchase-orders/{line_id}/close` | POST | Close an open purchase-order line |
| `/purchase-orders/events` | GET | Retrieve recent PO audit events |
| `/purchase-orders/export.csv` | GET | Export PO lines, supplier details, balances and receiving mode as CSV |
| `/update` | POST | Correct OCR fields on unreviewed scans; revalidates MRP and PO matching |
| `/review` | POST | Approve or reject a scan |
| `/audit` | GET | Retrieve the latest append-only operator audit events |
| `/delete` | POST | Delete one scan record while preserving its audit events |
| `/clear` | POST | Clear scan history while recording deletion events |
| `/export_pdf` | GET | Download a PDF audit report |
| `/export_csv` | GET | Download scan records as CSV |

## Purchase-order management V1

- Add purchase-order lines with PO number, supplier, item, expected batch, expected MRP, ordered quantity, supplier contact details, order date, expected delivery date, and notes.
- Import up to 500 lines using CSV. The required columns are `po_id,supplier,item_name,expected_batch,expected_mrp,ordered_qty`; supplier contact, phone, email, dates, and notes are optional. Imports are all-or-nothing and reject duplicate PO/item/batch lines.
- Record counted partial deliveries with a positive quantity and a required delivery note/reference. The API prevents receipts above the remaining ordered quantity and marks a line COMPLETE when fully received.
- Each PO line is assigned one receiving mode: `MANUAL` for counted quantity receipts or `SCAN` for camera-scan approvals. The modes cannot be mixed on the same PO line, preventing a delivery from being counted once manually and again through scans. Pending scan approvals temporarily block manual receipt; the line becomes SCAN mode only when the first scan is approved.
- Closing a PO line invalidates its pending scan approvals, moves those scans back to NEEDS REVIEW, and records the invalidation in the scan audit log.
- Export purchase-order data from the dashboard or `GET /purchase-orders/export.csv`. The export contains supplier contacts, item/batch/MRP, ordered/received/remaining quantities, status, receiving mode, dates and notes.
- Supplier and delivery metadata can be edited. Ordered quantity cannot be reduced below already received quantity. Item, batch and MRP are locked after scans are linked or stock is received.
- Open PO lines can be closed; closed and complete lines are not eligible for new stock matching or additional receipt entries.
- PO creation, edits, partial/full receipt and closure are stored in a separate purchase-order event log. Use `GET /purchase-orders/events` to retrieve recent activity.

CSV import example:

```csv
po_id,supplier,item_name,expected_batch,expected_mrp,ordered_qty,supplier_contact,supplier_phone,supplier_email,order_date,expected_delivery,notes
PO-2026-901,North Distributor,Cereal 500g,NC-101,125.50,20,Ravi,9876543210,orders@example.com,2026-11-01,2026-11-05,Morning delivery
```

## Goods Received Notes (GRN) and supplier invoice reconciliation V1

- Every approved camera scan and manual PO receipt writes a GRN record with PO line, delivery reference, received quantity, actual batch/MRP, source, timestamp and operator.
- Use the same delivery reference on the PO receipt and supplier invoice to reconcile the invoice line against the goods physically recorded for that delivery.
- Add an optional **expected purchase unit cost** to a PO line. This is separate from consumer-facing MRP; invoice unit cost is compared only with this purchase-cost field when it has been configured.
- Supplier invoice records include invoice number, supplier, date, delivery reference, invoice total and PO-linked lines with invoiced quantity and unit cost.
- Reconciliation checks supplier identity, invoice quantity against GRN quantity for the delivery reference, expected purchase unit cost (when configured), and invoice header total against calculated line totals.
- Invoices can be reconciled again after a missing GRN is entered. Discrepancies remain visible rather than being silently accepted.

### API routes

| Route | Method | Purpose |
|---|---|---|
| `/goods-receipts` | GET | List recent GRN records |
| `/supplier-invoices` | GET / POST | List invoices or create and reconcile an invoice |
| `/supplier-invoices/{invoice_id}/reconcile` | POST | Re-run reconciliation after GRN corrections/entries |

### Supplier invoice JSON example

```json
{
  "invoice_number": "INV-1001",
  "supplier": "North Distributor",
  "invoice_date": "2026-11-27",
  "delivery_ref": "DC-1001",
  "invoice_total": 2510.00,
  "lines": [
    {"po_line_id": 12, "quantity": 20, "unit_cost": 125.50}
  ]
}
```

The `po_line_id` is the numeric line ID shown by `GET /purchase-orders`. The invoice `delivery_ref` must match the GRN delivery reference. A supplier cost is not compared with retail MRP.

## Operator review and receiving approval

- Every scan is saved to the audit log and remains **PENDING** or **NEEDS REVIEW**; scanning alone never increments received stock.
- A scan with valid OCR fields, a non-expired lot, and an exact batch/MRP purchase-order candidate can be approved by an operator from the dashboard. Expired lots remain in review and cannot increment received stock.
- Approval rechecks the linked purchase order and its remaining ordered quantity inside a database transaction, then adds one received unit and records the review timestamp and note.
- OCR/PO exceptions can be rejected with an optional note. They cannot be approved until the scan has a valid PO match and valid required fields.
- Operators can correct product, batch, manufacturing date, expiry date, and MRP on unreviewed scans. MRP corrections are parsed into the numeric field and the application rechecks date validity, image-quality flags, batch, MRP, and open PO quantity before making the scan eligible for review.
- Approved and rejected scans are locked against field edits so receiving decisions cannot be silently altered after the fact. Expired inventory is excluded from approval even when OCR fields and the PO match.
- Every scan capture, correction, approval, rejection, individual deletion, and shift reset writes an append-only event to `scan_review_events`. Deletion and reset events retain a scan snapshot; deleting a scan does not delete its audit events.
- Shift reset preserves the scan ID sequence so a later scan never reuses an ID already referenced by older audit events. The dashboard shows the latest 100 events; `GET /audit` returns up to 500.
- Audit event details include before/after values for corrections and PO linkage/received quantity increments for approvals. In unauthenticated local mode, the default operator label is `local-operator`; set `AOI_OPERATOR_NAME` to use a device/operator label. When Basic authentication is enabled, the configured username is recorded.
- Existing SQLite databases receive additive review columns and the audit table at startup; existing scan records are not automatically approved.

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

The PDF is generated from the current scan history and safely treats missing OCR MRP as zero for aggregate totals. The CSV includes timestamps, product, barcode, batch, manufacturing and expiry dates, MRP, action, task status, review status, review timestamp, and review note. The operator event history is available separately through the dashboard and `/audit`.

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
