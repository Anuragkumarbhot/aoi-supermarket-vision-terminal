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
            conn.execute("UPDATE pending_orders SET received_qty = 12")
            conn.commit()
        self.client = aoi.app.test_client()

    def test_dashboard_loads_without_optional_auth(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"NEURAL EDGE VISION TERMINAL", response.data)

    def test_database_initializes_required_tables(self):
        with sqlite3.connect(aoi.DB_PATH) as conn:
            tables = {
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        self.assertIn("scans", tables)
        self.assertIn("pending_orders", tables)

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

        self.assertEqual(dashboard.status_code, 401)
        self.assertEqual(clear.status_code, 401)
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
