import base64
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app as aoi


class AOISecurityAndFunctionalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        aoi.DB_PATH = os.path.join(cls.temp_dir.name, "test_inventory.db")
        aoi.init_db()

    @classmethod
    def tearDownClass(cls):
        cls.temp_dir.cleanup()

    def setUp(self):
        aoi.AUTH_ENABLED = False
        aoi.AOI_AUTH_USERNAME = ""
        aoi.AOI_AUTH_PASSWORD = ""
        aoi.app.config.update(TESTING=True, MAX_CONTENT_LENGTH=8 * 1024 * 1024)
        with sqlite3.connect(aoi.DB_PATH) as conn:
            conn.execute("DELETE FROM scans")
            conn.execute("DELETE FROM sqlite_sequence WHERE name='scans'")
            conn.execute("DELETE FROM scan_review_events")
            conn.execute("DELETE FROM purchase_order_events")
            conn.execute("DELETE FROM goods_receipts")
            conn.execute("DELETE FROM supplier_invoice_lines")
            conn.execute("DELETE FROM supplier_invoice_events")
            conn.execute("DELETE FROM supplier_invoices")
            conn.execute("DELETE FROM pending_orders WHERE po_id <> 'PO-2026-884'")
            conn.execute("UPDATE pending_orders SET received_qty = 12, status = 'OPEN', receiving_mode = 'UNSET'")
            conn.commit()
        self.client = aoi.app.test_client()

    def test_dashboard_loads_without_optional_auth(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"NEURAL EDGE VISION TERMINAL", response.data)
        self.assertIn(b"Operator Audit Trail", response.data)

    def test_database_initializes_required_tables(self):
        with sqlite3.connect(aoi.DB_PATH) as conn:
            tables = {
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        self.assertIn("scans", tables)
        self.assertIn("pending_orders", tables)
        self.assertIn("scan_review_events", tables)
        self.assertIn("goods_receipts", tables)
        self.assertIn("supplier_invoices", tables)
        self.assertIn("supplier_invoice_lines", tables)

    def test_scan_rejects_non_json_payload(self):
        response = self.client.post("/scan", data="not-json", content_type="text/plain")
        self.assertEqual(response.status_code, 400)

    def test_scan_rejects_missing_image(self):
        response = self.client.post("/scan", json={"brand": "Test product"})
        self.assertEqual(response.status_code, 400)

    def test_scan_rejects_invalid_base64_image(self):
        response = self.client.post(
            "/scan",
            json={"image": "data:image/jpeg;base64,not-valid-base64"},
        )
        self.assertEqual(response.status_code, 400)

    def test_scan_rejects_valid_base64_that_is_not_an_image(self):
        payload = base64.b64encode(b"not an image").decode("ascii")
        response = self.client.post(
            "/scan",
            json={"image": f"data:image/jpeg;base64,{payload}"},
        )
        self.assertEqual(response.status_code, 400)

    def test_authentication_blocks_dashboard_and_destructive_routes(self):
        aoi.AUTH_ENABLED = True
        aoi.AOI_AUTH_USERNAME = "operator"
        aoi.AOI_AUTH_PASSWORD = "test-password"

        dashboard = self.client.get("/")
        clear = self.client.post("/clear")
        purchase_orders = self.client.get("/purchase-orders")
        create_po = self.client.post("/purchase-orders", json={
            "po_id": "PO-AUTH-001", "supplier": "Vendor",
            "item_name": "Auth Test", "expected_batch": "AUTH-1",
            "expected_mrp": 10, "ordered_qty": 2,
        })

        self.assertEqual(dashboard.status_code, 401)
        self.assertEqual(clear.status_code, 401)
        self.assertEqual(purchase_orders.status_code, 401)
        self.assertEqual(create_po.status_code, 401)
        self.assertIn("WWW-Authenticate", dashboard.headers)

    def test_valid_authentication_allows_dashboard_and_export(self):
        from werkzeug.datastructures import Authorization

        aoi.AUTH_ENABLED = True
        aoi.AOI_AUTH_USERNAME = "operator"
        aoi.AOI_AUTH_PASSWORD = "test-password"
        auth = Authorization("basic", {"username": "operator", "password": "test-password"})

        dashboard = self.client.get("/", headers={"Authorization": auth.to_header()})
        export = self.client.get("/export_csv", headers={"Authorization": auth.to_header()})

        self.assertEqual(dashboard.status_code, 200)
        self.assertEqual(export.status_code, 200)
        self.assertEqual(export.mimetype, "text/csv")

    def test_authentication_prevents_unauthorized_database_clear(self):
        with sqlite3.connect(aoi.DB_PATH) as conn:
            conn.execute(
                """INSERT INTO scans
                (time, brand, barcode, batch, mfr, exp, mrp, mrp_num, action, task_status)
                VALUES ('10:00:00', 'Test', '123', 'BATCH1', '01/2026',
                        '01/2028', 'Rs.10.00', 10, 'FRESH', 'TEST')"""
            )
            conn.commit()

        aoi.AUTH_ENABLED = True
        aoi.AOI_AUTH_USERNAME = "operator"
        aoi.AOI_AUTH_PASSWORD = "test-password"
        response = self.client.post("/clear")

        self.assertEqual(response.status_code, 401)
        with sqlite3.connect(aoi.DB_PATH) as conn:
            count = conn.execute("SELECT COUNT(*) FROM scans").fetchone()[0]
        self.assertEqual(count, 1)

    def test_missing_ocr_fields_are_not_filled_with_demo_defaults(self):
        from unittest.mock import patch
        from PIL import Image

        with patch.object(aoi, "neural_detect_stamp_region", side_effect=lambda image: image), \
             patch.object(aoi, "task_decode_barcode", return_value="NONE"), \
             patch.object(aoi, "task_run_neural_ocr", return_value=""), \
             patch.object(aoi, "task_inspect_quality", return_value="PASS"):
            record = aoi.multi_task_neural_pipeline(Image.new("RGB", (80, 80)), "Test")

        self.assertEqual(record["batch"], "")
        self.assertEqual(record["mfr"], "")
        self.assertEqual(record["exp"], "")
        self.assertIsNone(record["mrp_num"])
        self.assertEqual(record["status_code"], "FAIL")
        self.assertEqual(record["action"], "REVIEW REQUIRED")
        self.assertEqual(record["inventory_status"], "REVIEW REQUIRED")

    def test_purchase_order_changes_only_after_operator_approval(self):
        before = aoi.get_pending_orders()[0]["received_qty"]
        record = {
            "time": "12:00:00", "brand": "Test", "barcode": "123",
            "batch": "GL-26105", "mfr": "04/2026", "exp": "03/2028",
            "mrp": "Rs.100.00", "mrp_num": 100.0, "action": "FRESH",
            "status_code": "PASS", "task_status": "TEST", "data_valid": True,
        }
        scan_id = aoi.insert_scan(record)
        self.assertEqual(aoi.get_pending_orders()[0]["received_qty"], before)
        with sqlite3.connect(aoi.DB_PATH) as conn:
            status = conn.execute("SELECT review_status FROM scans WHERE id=?", (scan_id,)).fetchone()[0]
        self.assertEqual(status, "PENDING")

        ok, message = aoi.review_scan(scan_id, "approve", "Physical label and unit checked")
        self.assertTrue(ok, message)
        self.assertEqual(aoi.get_pending_orders()[0]["received_qty"], before + 1)
        ok, message = aoi.review_scan(scan_id, "approve")
        self.assertFalse(ok)
        self.assertEqual(aoi.get_pending_orders()[0]["received_qty"], before + 1)

    def test_mrp_mismatch_cannot_be_approved_or_change_stock(self):
        before = aoi.get_pending_orders()[0]["received_qty"]
        record = {
            "time": "12:01:00", "brand": "Test", "barcode": "123",
            "batch": "GL-26105", "mfr": "04/2026", "exp": "03/2028",
            "mrp": "Rs.125.00", "mrp_num": 125.0, "action": "FRESH",
            "status_code": "PASS", "task_status": "TEST", "data_valid": True,
        }
        scan_id = aoi.insert_scan(record)
        self.assertEqual(record["inventory_status"], "MRP MISMATCH")
        ok, _ = aoi.review_scan(scan_id, "approve")
        self.assertFalse(ok)
        self.assertEqual(aoi.get_pending_orders()[0]["received_qty"], before)

    def test_rejected_scan_does_not_change_received_quantity(self):
        before = aoi.get_pending_orders()[0]["received_qty"]
        record = {
            "time": "12:02:00", "brand": "Test", "barcode": "123",
            "batch": "GL-26105", "mfr": "04/2026", "exp": "03/2028",
            "mrp": "Rs.100.00", "mrp_num": 100.0, "action": "FRESH",
            "status_code": "PASS", "task_status": "TEST", "data_valid": True,
        }
        scan_id = aoi.insert_scan(record)
        ok, _ = aoi.review_scan(scan_id, "reject", "Wrong physical item")
        self.assertTrue(ok)
        self.assertEqual(aoi.get_pending_orders()[0]["received_qty"], before)

    def test_flagged_scan_can_be_rejected_but_not_approved(self):
        record = {
            "time": "12:04:00", "brand": "Test", "barcode": "123",
            "batch": "", "mfr": "", "exp": "", "mrp": "", "mrp_num": None,
            "action": "REVIEW REQUIRED", "status_code": "FAIL",
            "task_status": "TEST", "data_valid": False,
        }
        scan_id = aoi.insert_scan(record)
        ok, message = aoi.review_scan(scan_id, "approve")
        self.assertFalse(ok)
        ok, message = aoi.review_scan(scan_id, "reject", "Unreadable label")
        self.assertTrue(ok, message)

    def test_review_endpoint_validates_and_approves(self):
        record = {
            "time": "12:03:00", "brand": "Test", "barcode": "123",
            "batch": "GL-26105", "mfr": "04/2026", "exp": "03/2028",
            "mrp": "Rs.100.00", "mrp_num": 100.0, "action": "FRESH",
            "status_code": "PASS", "task_status": "TEST", "data_valid": True,
        }
        scan_id = aoi.insert_scan(record)
        response = self.client.post("/review", json={
            "id": scan_id, "decision": "approve", "note": "Checked"
        })
        self.assertEqual(response.status_code, 200)
        invalid = self.client.post("/review", json={"id": scan_id, "decision": "approve"})
        self.assertEqual(invalid.status_code, 409)

    def test_invalid_or_unverified_scan_never_increments_purchase_order(self):
        before = aoi.get_pending_orders()[0]["received_qty"]
        record = {
            "time": "12:02:00", "brand": "Test", "barcode": "123",
            "batch": "GL-26105", "mfr": "", "exp": "", "mrp": "",
            "mrp_num": None, "action": "REVIEW REQUIRED", "status_code": "FAIL",
            "task_status": "TEST", "data_valid": False,
        }
        aoi.insert_scan(record)
        self.assertEqual(aoi.get_pending_orders()[0]["received_qty"], before)
        self.assertEqual(record["inventory_status"], "REVIEW REQUIRED")

    def test_label_date_parser_rejects_invalid_dates(self):
        self.assertIsNotNone(aoi.parse_label_date("04/2026"))
        self.assertIsNotNone(aoi.parse_label_date("15/04/2026"))
        self.assertIsNone(aoi.parse_label_date("19/2026"))
        self.assertIsNone(aoi.parse_label_date(""))

    def test_operator_audit_records_scan_capture_and_approval(self):
        record = {
            "time": "12:10:00", "brand": "Test", "barcode": "123",
            "batch": "GL-26105", "mfr": "04/2026", "exp": "03/2028",
            "mrp": "Rs.100.00", "mrp_num": 100.0, "action": "FRESH",
            "status_code": "PASS", "task_status": "Neural ROI | Qual: PASS",
            "data_valid": True,
        }
        scan_id = aoi.insert_scan(record)
        ok, message = aoi.review_scan(scan_id, "approve", "Physically verified")
        self.assertTrue(ok, message)

        events = aoi.get_review_events()
        event_types = [event["event_type"] for event in events]
        self.assertIn("SCAN_CAPTURED", event_types)
        self.assertIn("SCAN_APPROVED", event_types)
        approval = next(event for event in events if event["event_type"] == "SCAN_APPROVED")
        self.assertEqual(approval["note"], "Physically verified")
        self.assertEqual(approval["operator"], "local-operator")

    def test_operator_correction_rechecks_po_and_updates_mrp_numeric_value(self):
        record = {
            "time": "12:11:00", "brand": "Test", "barcode": "123",
            "batch": "GL-26105", "mfr": "04/2026", "exp": "03/2028",
            "mrp": "Rs.125.00", "mrp_num": 125.0, "action": "FRESH",
            "status_code": "PASS", "task_status": "Neural ROI | Qual: PASS",
            "data_valid": True,
        }
        scan_id = aoi.insert_scan(record)
        self.assertEqual(record["inventory_status"], "MRP MISMATCH")

        response = self.client.post("/update", json={
            "id": scan_id, "field": "mrp", "value": "Rs.100.00"
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["review_status"], "PENDING")
        with sqlite3.connect(aoi.DB_PATH) as conn:
            row = conn.execute(
                "SELECT mrp, mrp_num, review_status, matched_po_rowid FROM scans WHERE id=?",
                (scan_id,)
            ).fetchone()
        self.assertEqual(row[0], "Rs.100.00")
        self.assertEqual(row[1], 100.0)
        self.assertEqual(row[2], "PENDING")
        self.assertIsNotNone(row[3])
        with sqlite3.connect(aoi.DB_PATH) as conn:
            action = conn.execute("SELECT action FROM scans WHERE id=?", (scan_id,)).fetchone()[0]
        self.assertEqual(action, "CORRECTED - READY FOR REVIEW")

        correction = next(e for e in aoi.get_review_events() if e["event_type"] == "FIELD_CORRECTED")
        self.assertIn('"field": "mrp"', correction["details_json"])

    def test_expired_stock_never_becomes_a_receiving_candidate(self):
        before = aoi.get_pending_orders()[0]["received_qty"]
        record = {
            "time": "12:17:00", "brand": "Test expired lot", "barcode": "123",
            "batch": "GL-26105", "mfr": "01/2025", "exp": "01/2026",
            "mrp": "Rs.100.00", "mrp_num": 100.0, "action": "EXPIRED - RETURN",
            "status_code": "FAIL", "task_status": "Neural ROI | Qual: PASS",
            "data_valid": True,
        }
        scan_id = aoi.insert_scan(record)
        self.assertEqual(record["inventory_status"], "EXPIRED - DO NOT RECEIVE")
        with sqlite3.connect(aoi.DB_PATH) as conn:
            status = conn.execute(
                "SELECT review_status FROM scans WHERE id=?", (scan_id,)
            ).fetchone()[0]
        self.assertEqual(status, "NEEDS REVIEW")
        ok, _ = aoi.review_scan(scan_id, "approve")
        self.assertFalse(ok)
        self.assertEqual(aoi.get_pending_orders()[0]["received_qty"], before)

    def test_invalid_mrp_correction_is_rejected_without_mutation(self):
        record = {
            "time": "12:12:00", "brand": "Test", "barcode": "123",
            "batch": "GL-26105", "mfr": "04/2026", "exp": "03/2028",
            "mrp": "Rs.100.00", "mrp_num": 100.0, "action": "FRESH",
            "status_code": "PASS", "task_status": "Neural ROI | Qual: PASS",
            "data_valid": True,
        }
        scan_id = aoi.insert_scan(record)
        response = self.client.post("/update", json={
            "id": scan_id, "field": "mrp", "value": "not-a-price"
        })
        self.assertEqual(response.status_code, 400)
        with sqlite3.connect(aoi.DB_PATH) as conn:
            mrp = conn.execute("SELECT mrp FROM scans WHERE id=?", (scan_id,)).fetchone()[0]
        self.assertEqual(mrp, "Rs.100.00")

    def test_approved_scan_is_locked_against_field_correction(self):
        record = {
            "time": "12:13:00", "brand": "Test", "barcode": "123",
            "batch": "GL-26105", "mfr": "04/2026", "exp": "03/2028",
            "mrp": "Rs.100.00", "mrp_num": 100.0, "action": "FRESH",
            "status_code": "PASS", "task_status": "Neural ROI | Qual: PASS",
            "data_valid": True,
        }
        scan_id = aoi.insert_scan(record)
        ok, message = aoi.review_scan(scan_id, "approve", "Verified")
        self.assertTrue(ok, message)
        response = self.client.post("/update", json={
            "id": scan_id, "field": "batch", "value": "CHANGED"
        })
        self.assertEqual(response.status_code, 409)

    def test_delete_keeps_audit_trail_tombstone(self):
        record = {
            "time": "12:14:00", "brand": "Test", "barcode": "123",
            "batch": "", "mfr": "", "exp": "", "mrp": "", "mrp_num": None,
            "action": "REVIEW REQUIRED", "status_code": "FAIL",
            "task_status": "TEST", "data_valid": False,
        }
        scan_id = aoi.insert_scan(record)
        response = self.client.post("/delete", json={"id": scan_id})
        self.assertEqual(response.status_code, 200)
        events = aoi.get_review_events()
        event_types = [e["event_type"] for e in events]
        self.assertIn("SCAN_CAPTURED", event_types)
        self.assertIn("SCAN_DELETED", event_types)
        deletion = next(e for e in events if e["event_type"] == "SCAN_DELETED")
        import json
        details = json.loads(deletion["details_json"])
        self.assertEqual(details["scan_snapshot"]["brand"], "Test")
        with sqlite3.connect(aoi.DB_PATH) as conn:
            self.assertIsNone(conn.execute("SELECT id FROM scans WHERE id=?", (scan_id,)).fetchone())

    def test_shift_reset_preserves_audit_and_never_reuses_scan_ids(self):
        record = {
            "time": "12:18:00", "brand": "First item", "barcode": "123",
            "batch": "", "mfr": "", "exp": "", "mrp": "", "mrp_num": None,
            "action": "REVIEW REQUIRED", "status_code": "FAIL",
            "task_status": "TEST", "data_valid": False,
        }
        first_id = aoi.insert_scan(dict(record))
        response = self.client.post("/clear")
        self.assertEqual(response.status_code, 200)

        second_record = dict(record, brand="Second item", time="12:19:00")
        second_id = aoi.insert_scan(second_record)
        self.assertGreater(second_id, first_id)
        events = aoi.get_review_events()
        reset_event = next(e for e in events if e["event_type"] == "SCAN_DELETED_BY_RESET")
        import json
        details = json.loads(reset_event["details_json"])
        self.assertEqual(details["scan_snapshot"]["brand"], "First item")

    def test_audit_endpoint_returns_json_events(self):
        record = {
            "time": "12:15:00", "brand": "Test", "barcode": "123",
            "batch": "", "mfr": "", "exp": "", "mrp": "", "mrp_num": None,
            "action": "REVIEW REQUIRED", "status_code": "FAIL",
            "task_status": "TEST", "data_valid": False,
        }
        aoi.insert_scan(record)
        response = self.client.get("/audit")
        self.assertEqual(response.status_code, 200)
        self.assertIn("SCAN_CAPTURED", [e["event_type"] for e in response.json["events"]])

    def test_pdf_export_handles_blank_ocr_mrp(self):
        record = {
            "time": "12:16:00", "brand": "Test", "barcode": "NONE",
            "batch": "", "mfr": "", "exp": "", "mrp": "", "mrp_num": None,
            "action": "REVIEW REQUIRED", "status_code": "FAIL",
            "task_status": "TEST", "data_valid": False,
        }
        aoi.insert_scan(record)
        response = self.client.get("/export_pdf")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "application/pdf")

    def test_purchase_order_create_and_list_with_supplier_details(self):
        payload = {
            "po_id": "PO-TEST-001", "supplier": "North Distributor",
            "item_name": "Test Cereal", "expected_batch": "NC-001",
            "expected_mrp": "125.50", "ordered_qty": "20",
            "supplier_contact": "Ravi", "supplier_phone": "9876543210",
            "supplier_email": "orders@example.test", "order_date": "2026-11-01",
            "expected_delivery": "2026-11-05", "notes": "Morning delivery",
        }
        response = self.client.post("/purchase-orders", json=payload)
        self.assertEqual(response.status_code, 201)
        line_id = response.json["line_id"]
        listing = self.client.get("/purchase-orders")
        self.assertEqual(listing.status_code, 200)
        line = next(x for x in listing.json["purchase_orders"] if x["line_id"] == line_id)
        self.assertEqual(line["supplier_contact"], "Ravi")
        self.assertEqual(line["remaining_qty"], 20)
        self.assertEqual(line["line_status"], "OPEN")

    def test_purchase_order_create_rejects_invalid_quantity_and_duplicate_line(self):
        payload = {
            "po_id": "PO-2026-884", "supplier": "Gericon Pharma India",
            "item_name": "Cough Syrup DX", "expected_batch": "GL-26105",
            "expected_mrp": 100, "ordered_qty": 10,
        }
        duplicate = self.client.post("/purchase-orders", json=payload)
        self.assertEqual(duplicate.status_code, 400)
        payload["po_id"] = "PO-TEST-BAD"
        payload["item_name"] = "Test Item"
        payload["expected_batch"] = "TB-001"
        payload["ordered_qty"] = "2.5"
        invalid = self.client.post("/purchase-orders", json=payload)
        self.assertEqual(invalid.status_code, 400)

    def test_purchase_order_rejects_invalid_supplier_email_and_rounded_zero_mrp(self):
        base = {
            "po_id": "PO-VALIDATION-001", "supplier": "Vendor",
            "item_name": "Validation Item", "expected_batch": "VAL-1",
            "expected_mrp": "0.001", "ordered_qty": 1,
        }
        tiny_mrp = self.client.post("/purchase-orders", json=base)
        self.assertEqual(tiny_mrp.status_code, 400)

        base["po_id"] = "PO-VALIDATION-002"
        base["expected_mrp"] = "12.50"
        base["supplier_email"] = "not-an-email"
        bad_email = self.client.post("/purchase-orders", json=base)
        self.assertEqual(bad_email.status_code, 400)

    def test_purchase_order_csv_import_is_atomic(self):
        csv_text = (
            "po_id,supplier,item_name,expected_batch,expected_mrp,ordered_qty,supplier_phone,expected_delivery\n"
            "PO-CSV-001,Vendor A,Item A,BATCH-A,45.00,10,9876500001,2026-12-01\n"
            "PO-CSV-002,Vendor B,Item B,BATCH-B,55.00,12,9876500002,2026-12-03\n"
        )
        response = self.client.post("/purchase-orders/import", json={"csv_text": csv_text})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json["imported"], 2)

        duplicate_payload = {
            "csv_text": (
                "po_id,supplier,item_name,expected_batch,expected_mrp,ordered_qty\n"
                "PO-CSV-003,Vendor C,Item C,BATCH-C,25,4\n"
                "PO-CSV-003,Vendor C,Item C,BATCH-C,25,4\n"
            )
        }
        failed = self.client.post("/purchase-orders/import", json=duplicate_payload)
        self.assertEqual(failed.status_code, 400)
        listing = self.client.get("/purchase-orders").json["purchase_orders"]
        self.assertFalse(any(x["po_id"] == "PO-CSV-003" for x in listing))

    def test_partial_receipt_updates_remaining_and_completes_line(self):
        created = self.client.post("/purchase-orders", json={
            "po_id": "PO-RECEIVE-001", "supplier": "Vendor",
            "item_name": "Rice", "expected_batch": "RICE-1",
            "expected_mrp": 80, "ordered_qty": 10,
        })
        line_id = created.json["line_id"]

        first = self.client.post(f"/purchase-orders/{line_id}/receive", json={
            "quantity": 4, "note": "Delivery challan DC-1"
        })
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json["received_qty"], 4)
        self.assertEqual(first.json["remaining_qty"], 6)

        second = self.client.post(f"/purchase-orders/{line_id}/receive", json={
            "quantity": 6, "note": "Balance delivery DC-2"
        })
        self.assertEqual(second.status_code, 200)
        line = next(x for x in self.client.get("/purchase-orders").json["purchase_orders"]
                    if x["line_id"] == line_id)
        self.assertEqual(line["line_status"], "COMPLETE")
        self.assertEqual(line["remaining_qty"], 0)
        over = self.client.post(f"/purchase-orders/{line_id}/receive", json={
            "quantity": 1, "note": "Extra unit"
        })
        self.assertEqual(over.status_code, 409)

        events = self.client.get("/purchase-orders/events").json["events"]
        self.assertIn("PO_PARTIAL_RECEIPT", [e["event_type"] for e in events])
        self.assertIn("PO_FULL_RECEIPT", [e["event_type"] for e in events])

    def test_partial_receipt_rejects_missing_note_and_over_receipt(self):
        created = self.client.post("/purchase-orders", json={
            "po_id": "PO-RECEIVE-002", "supplier": "Vendor",
            "item_name": "Oil", "expected_batch": "OIL-1",
            "expected_mrp": 150, "ordered_qty": 5,
        })
        line_id = created.json["line_id"]
        missing_note = self.client.post(f"/purchase-orders/{line_id}/receive", json={
            "quantity": 1, "note": ""
        })
        self.assertEqual(missing_note.status_code, 400)
        over = self.client.post(f"/purchase-orders/{line_id}/receive", json={
            "quantity": 6, "note": "Checked"
        })
        self.assertEqual(over.status_code, 400)

    def test_purchase_order_edit_preserves_received_quantity_constraints(self):
        created = self.client.post("/purchase-orders", json={
            "po_id": "PO-EDIT-001", "supplier": "Vendor",
            "item_name": "Flour", "expected_batch": "FLOUR-1",
            "expected_mrp": 60, "ordered_qty": 10,
        })
        line_id = created.json["line_id"]
        self.client.post(f"/purchase-orders/{line_id}/receive", json={
            "quantity": 3, "note": "Partial counted delivery"
        })
        invalid = self.client.put(f"/purchase-orders/{line_id}", json={"ordered_qty": 2})
        self.assertEqual(invalid.status_code, 400)
        update = self.client.put(f"/purchase-orders/{line_id}", json={
            "supplier": "Updated Vendor", "supplier_phone": "9999999999",
            "expected_delivery": "2026-12-10", "ordered_qty": 12,
        })
        self.assertEqual(update.status_code, 200)
        line = next(x for x in self.client.get("/purchase-orders").json["purchase_orders"]
                    if x["line_id"] == line_id)
        self.assertEqual(line["supplier"], "Updated Vendor")
        self.assertEqual(line["received_qty"], 3)
        self.assertEqual(line["remaining_qty"], 9)

    def test_purchase_order_identity_fields_lock_after_scan_is_linked(self):
        created = self.client.post("/purchase-orders", json={
            "po_id": "PO-LOCK-001", "supplier": "Vendor",
            "item_name": "Milk", "expected_batch": "MILK-26",
            "expected_mrp": 50, "ordered_qty": 10,
        })
        line_id = created.json["line_id"]
        record = {
            "time": "12:20:00", "brand": "Milk", "barcode": "123",
            "batch": "MILK-26", "mfr": "04/2026", "exp": "03/2028",
            "mrp": "Rs.50.00", "mrp_num": 50.0, "action": "FRESH",
            "status_code": "PASS", "task_status": "Neural ROI | Qual: PASS",
            "data_valid": True,
        }
        scan_id = aoi.insert_scan(record)
        self.assertIsNotNone(scan_id)
        with sqlite3.connect(aoi.DB_PATH) as conn:
            linked = conn.execute("SELECT matched_po_rowid FROM scans WHERE id=?", (scan_id,)).fetchone()[0]
        self.assertEqual(linked, line_id)
        locked = self.client.put(f"/purchase-orders/{line_id}", json={"expected_batch": "MILK-27"})
        self.assertEqual(locked.status_code, 409)

    def test_purchase_order_close_prevents_receiving(self):
        created = self.client.post("/purchase-orders", json={
            "po_id": "PO-CLOSE-001", "supplier": "Vendor",
            "item_name": "Tea", "expected_batch": "TEA-1",
            "expected_mrp": 30, "ordered_qty": 4,
        })
        line_id = created.json["line_id"]
        closed = self.client.post(f"/purchase-orders/{line_id}/close", json={"note": "Supplier cancelled balance"})
        self.assertEqual(closed.status_code, 200)
        receive = self.client.post(f"/purchase-orders/{line_id}/receive", json={
            "quantity": 1, "note": "Late delivery"
        })
        self.assertEqual(receive.status_code, 409)
        line = next(x for x in self.client.get("/purchase-orders").json["purchase_orders"]
                    if x["line_id"] == line_id)
        self.assertEqual(line["line_status"], "CLOSED")

    def test_manual_and_scan_receiving_modes_cannot_be_mixed(self):
        manual_po = self.client.post("/purchase-orders", json={
            "po_id": "PO-MODE-MANUAL", "supplier": "Vendor",
            "item_name": "Manual Item", "expected_batch": "MANUAL-1",
            "expected_mrp": 75, "ordered_qty": 10,
        })
        manual_id = manual_po.json["line_id"]
        receipt = self.client.post(f"/purchase-orders/{manual_id}/receive", json={
            "quantity": 2, "note": "Manual counted delivery"
        })
        self.assertEqual(receipt.status_code, 200)

        manual_record = {
            "time": "12:21:00", "brand": "Manual Item", "barcode": "123",
            "batch": "MANUAL-1", "mfr": "04/2026", "exp": "03/2028",
            "mrp": "Rs.75.00", "mrp_num": 75.0, "action": "FRESH",
            "status_code": "PASS", "task_status": "Neural ROI | Qual: PASS",
            "data_valid": True,
        }
        aoi.insert_scan(manual_record)
        self.assertEqual(manual_record["inventory_status"], "MANUAL RECEIVING MODE")
        with sqlite3.connect(aoi.DB_PATH) as conn:
            mode = conn.execute("SELECT receiving_mode FROM pending_orders WHERE rowid=?", (manual_id,)).fetchone()[0]
            matched = conn.execute(
                "SELECT matched_po_rowid FROM scans WHERE brand='Manual Item' ORDER BY id DESC LIMIT 1"
            ).fetchone()[0]
        self.assertEqual(mode, "MANUAL")
        self.assertIsNone(matched)

        scan_po = self.client.post("/purchase-orders", json={
            "po_id": "PO-MODE-SCAN", "supplier": "Vendor",
            "item_name": "Scanned Item", "expected_batch": "SCAN-1",
            "expected_mrp": 85, "ordered_qty": 10,
        })
        scan_id = scan_po.json["line_id"]
        scan_record = {
            "time": "12:22:00", "brand": "Scanned Item", "barcode": "123",
            "batch": "SCAN-1", "mfr": "04/2026", "exp": "03/2028",
            "mrp": "Rs.85.00", "mrp_num": 85.0, "action": "FRESH",
            "status_code": "PASS", "task_status": "Neural ROI | Qual: PASS",
            "data_valid": True,
        }
        aoi.insert_scan(scan_record)
        self.assertEqual(scan_record["inventory_status"], "MATCH CANDIDATE")
        pending_manual_attempt = self.client.post(f"/purchase-orders/{scan_id}/receive", json={
            "quantity": 1, "note": "Attempted manual receipt while scan pending"
        })
        self.assertEqual(pending_manual_attempt.status_code, 409)
        ok, message = aoi.review_scan(
            next(x["id"] for x in aoi.get_all_scans() if x["brand"] == "Scanned Item"),
            "approve", "Physical unit verified"
        )
        self.assertTrue(ok, message)
        manual_attempt = self.client.post(f"/purchase-orders/{scan_id}/receive", json={
            "quantity": 1, "note": "Attempted manual duplicate after scan approval"
        })
        self.assertEqual(manual_attempt.status_code, 409)
        with sqlite3.connect(aoi.DB_PATH) as conn:
            mode = conn.execute(
                "SELECT receiving_mode FROM pending_orders WHERE rowid=?", (scan_id,)
            ).fetchone()[0]
        self.assertEqual(mode, "SCAN")
        grns = self.client.get("/goods-receipts").json["goods_receipts"]
        scan_grn = next(x for x in grns if x["source"] == "SCAN")
        self.assertEqual(scan_grn["delivery_ref"], f"SCAN-{next(x['id'] for x in aoi.get_all_scans() if x['brand'] == 'Scanned Item')}")
        self.assertEqual(scan_grn["quantity"], 1)

    def test_closing_po_invalidates_pending_scan_approval(self):
        created = self.client.post("/purchase-orders", json={
            "po_id": "PO-CLOSE-SCAN", "supplier": "Vendor",
            "item_name": "Close Test", "expected_batch": "CLOSE-1",
            "expected_mrp": 95, "ordered_qty": 10,
        })
        line_id = created.json["line_id"]
        record = {
            "time": "12:23:00", "brand": "Close Test", "barcode": "123",
            "batch": "CLOSE-1", "mfr": "04/2026", "exp": "03/2028",
            "mrp": "Rs.95.00", "mrp_num": 95.0, "action": "FRESH",
            "status_code": "PASS", "task_status": "Neural ROI | Qual: PASS",
            "data_valid": True,
        }
        scan_id = aoi.insert_scan(record)
        closed = self.client.post(f"/purchase-orders/{line_id}/close", json={"note": "PO cancelled"})
        self.assertEqual(closed.status_code, 200)
        ok, message = aoi.review_scan(scan_id, "approve")
        self.assertFalse(ok)
        with sqlite3.connect(aoi.DB_PATH) as conn:
            row = conn.execute(
                "SELECT review_status, matched_po_rowid FROM scans WHERE id=?", (scan_id,)
            ).fetchone()
        self.assertEqual(row[0], "NEEDS REVIEW")
        self.assertIsNone(row[1])
        events = [e["event_type"] for e in aoi.get_review_events()]
        self.assertIn("PO_CLOSED_REVIEW_INVALIDATED", events)

    def test_purchase_order_csv_export_includes_supplier_and_receiving_data(self):
        created = self.client.post("/purchase-orders", json={
            "po_id": "PO-EXPORT-001", "supplier": "Export Vendor",
            "item_name": "Export Item", "expected_batch": "EXPORT-1",
            "expected_mrp": 45, "ordered_qty": 6, "supplier_phone": "9876500000",
        })
        self.assertEqual(created.status_code, 201)
        response = self.client.get("/purchase-orders/export.csv")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "text/csv")
        self.assertIn(b"Supplier_Phone", response.data)
        self.assertIn(b"PO-EXPORT-001", response.data)

    def test_manual_receipt_creates_grn_and_supplier_invoice_reconciles(self):
        created = self.client.post("/purchase-orders", json={
            "po_id": "PO-GRN-001", "supplier": "North Distributor",
            "item_name": "Cereal", "expected_batch": "CR-101",
            "expected_mrp": 125, "expected_unit_cost": 50,
            "ordered_qty": 5,
        })
        self.assertEqual(created.status_code, 201)
        line_id = created.json["line_id"]

        receipt = self.client.post(f"/purchase-orders/{line_id}/receive", json={
            "quantity": 5, "note": "DC-GRN-001"
        })
        self.assertEqual(receipt.status_code, 200)
        grns = self.client.get("/goods-receipts").json["goods_receipts"]
        self.assertEqual(len(grns), 1)
        self.assertEqual(grns[0]["delivery_ref"], "DC-GRN-001")
        self.assertEqual(grns[0]["quantity"], 5)
        self.assertEqual(grns[0]["source"], "MANUAL")

        invoice = self.client.post("/supplier-invoices", json={
            "invoice_number": "INV-GRN-001", "supplier": "North Distributor",
            "invoice_date": "2026-11-27", "delivery_ref": "DC-GRN-001",
            "invoice_total": 250,
            "lines": [{"po_line_id": line_id, "quantity": 5, "unit_cost": 50}],
        })
        self.assertEqual(invoice.status_code, 201)
        self.assertEqual(invoice.json["reconciliation"]["status"], "MATCHED")
        invoices = self.client.get("/supplier-invoices").json["supplier_invoices"]
        self.assertEqual(invoices[0]["status"], "MATCHED")

    def test_supplier_invoice_flags_quantity_supplier_and_unit_cost_discrepancies(self):
        created = self.client.post("/purchase-orders", json={
            "po_id": "PO-GRN-002", "supplier": "Correct Supplier",
            "item_name": "Oil", "expected_batch": "OIL-2",
            "expected_mrp": 150, "expected_unit_cost": 70,
            "ordered_qty": 8,
        })
        line_id = created.json["line_id"]
        self.client.post(f"/purchase-orders/{line_id}/receive", json={
            "quantity": 8, "note": "DC-GRN-002"
        })
        invoice = self.client.post("/supplier-invoices", json={
            "invoice_number": "INV-GRN-002", "supplier": "Wrong Supplier",
            "invoice_date": "2026-11-27", "delivery_ref": "DC-GRN-002",
            "invoice_total": 320,
            "lines": [{"po_line_id": line_id, "quantity": 4, "unit_cost": 80}],
        })
        self.assertEqual(invoice.status_code, 201)
        self.assertEqual(invoice.json["reconciliation"]["status"], "DISCREPANCY")
        issues = invoice.json["reconciliation"]["discrepancies"][0]["issues"]
        self.assertIn("SUPPLIER_MISMATCH", issues)
        self.assertIn("QUANTITY_MISMATCH", issues)
        self.assertIn("UNIT_COST_MISMATCH", issues)

    def test_supplier_invoice_rejects_invalid_total_and_unknown_po(self):
        payload = {
            "invoice_number": "INV-BAD-001", "supplier": "Vendor",
            "invoice_date": "2026-11-27", "delivery_ref": "DC-BAD-001",
            "invoice_total": 101,
            "lines": [{"po_line_id": 999999, "quantity": 1, "unit_cost": 100}],
        }
        mismatch = self.client.post("/supplier-invoices", json=payload)
        self.assertEqual(mismatch.status_code, 400)
        payload["invoice_total"] = 100
        unknown_po = self.client.post("/supplier-invoices", json=payload)
        self.assertEqual(unknown_po.status_code, 400)

    def test_supplier_invoice_can_be_reconciled_again_after_grn_is_entered(self):
        created = self.client.post("/purchase-orders", json={
            "po_id": "PO-GRN-003", "supplier": "Vendor",
            "item_name": "Tea", "expected_batch": "TEA-3",
            "expected_mrp": 40, "expected_unit_cost": 25,
            "ordered_qty": 3,
        })
        line_id = created.json["line_id"]
        invoice = self.client.post("/supplier-invoices", json={
            "invoice_number": "INV-GRN-003", "supplier": "Vendor",
            "invoice_date": "2026-11-27", "delivery_ref": "DC-GRN-003",
            "invoice_total": 75,
            "lines": [{"po_line_id": line_id, "quantity": 3, "unit_cost": 25}],
        })
        self.assertEqual(invoice.status_code, 201)
        self.assertEqual(invoice.json["reconciliation"]["status"], "DISCREPANCY")
        receipt = self.client.post(f"/purchase-orders/{line_id}/receive", json={
            "quantity": 3, "note": "DC-GRN-003"
        })
        self.assertEqual(receipt.status_code, 200)
        reconcile = self.client.post(f"/supplier-invoices/{invoice.json['invoice_id']}/reconcile", json={})
        self.assertEqual(reconcile.status_code, 200)
        self.assertEqual(reconcile.json["reconciliation"]["status"], "MATCHED")

    def test_oversized_request_is_rejected(self):
        aoi.app.config["MAX_CONTENT_LENGTH"] = 64
        response = self.client.post(
            "/scan",
            data=b"x" * 128,
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 413)


if __name__ == "__main__":
    unittest.main()
