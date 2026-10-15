# AOI Supermarket Vision Terminal — System Specification Sheet

**Developer:** Anurag Raju (@Anuragkumarbhot)  
**Architecture:** Offline Edge Vision & FEFO Inventory Reconciliation Terminal  
**Local Endpoint:** http://127.0.0.1:5000  

## 1. Executive Abstract
Standard 1D barcodes (EAN-13) cannot encode dynamic batch numbers, manufacturing dates, expiry dates, or revised MRPs. The AOI Supermarket Vision Terminal is an offline, edge-deployed inspection system built on Python 3.14, Flask 3.1.3, Pillow 12.3.0, and Tesseract OCR 5.5.3. It uses flash-assisted ROI capture to extract 4-line dot-matrix stamps (Batch No. -> Mfg. Date -> Exp. Date -> M.R.P.), reconciles incoming stock against pending Purchase Orders in SQLite (WAL mode), and exports A4 PDF Audit Reports and ERP CSV logs.

## 2. Technical Stack
- **OS / Runtime:** Android Termux (ARM64) / Python 3.14
- **Web Server:** Flask 3.1.3 (Port 5000)
- **Vision & PDF Engine:** Pillow 12.3.0 (Zero C++ cv2/reportlab build dependencies)
- **OCR Engine:** Tesseract 5.5.3 + Leptonica 1.87.0
- **Storage & Sync:** SQLite3 (WAL Mode) + Async Sales Server REST Sync
- **Deliverables:** `Supermarket_Stock_Audit.pdf` & `POS_ERP_Import.csv`

## 3. Benchmark Validation (Gericon Pharma India Sample)
- **Line 1 (Batch No.):** `GL-26105` -> Extracted: `GL-26105`
- **Line 2 (Mfg. Date):** `04/2026` -> Extracted: `04/2026`
- **Line 3 (Exp. Date):** `03/2028` -> Extracted: `03/2028`
- **Line 4 (M.R.P.):** `100.00` -> Extracted: `Rs.100.00`
- **FEFO Status:** `FRESH PASS`
