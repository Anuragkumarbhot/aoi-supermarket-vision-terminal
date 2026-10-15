#!/data/data/com.termux/files/usr/bin/bash
echo "[*] Updating Termux packages and installing Tesseract & Pillow..."
pkg update -y
pkg install python tesseract python-pillow libjpeg-turbo -y
pip install flask pillow
echo "[✓] Setup complete! Run: python app.py"
