# printers/testing.py
from datetime import datetime, UTC # Use modern timezone-aware UTC
import sys
import os

# Temporarily add the project root to the path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from kiosk_config import KioskConfig
# --- IMPORTANT: Import the new, correct class ---
from printers.printer_custom_vkp80_service import PrinterCustomVkp80Service

# 1. Load configuration
print("Loading configuration from config.json...")
config = KioskConfig()
print("Configuration loaded.")

try:
    print("Initializing printer service with new VKP80II class...")
    # 2. Use the 'config' object to instantiate the NEW class
    svc = PrinterCustomVkp80Service(
        vendor_id=config.printer_vendor_id,
        product_id=config.printer_product_id,
        interface=config.printer_interface,
        in_ep=config.printer_in_endpoint,
        out_ep=config.printer_out_endpoint,
        mock=config.printer_mock,
    )
    print("Printer service initialized.")

    # 3. Call the print_ticket method on the new service instance
    success = svc.print_ticket(
        brand=config.brand_name,
        message="Connection Successful!",
        lines=[
            "This is a test from the new service class.",
            "Paper should eject correctly."
        ],
        barcode="8145829964057",
        timestamp=datetime.now(UTC),
        barcode_type="EAN13",
        logo=config.logo_path,
        amount=24.99
    )

    print("\nPrint command sent successfully." if success else "\nPrint command failed.")

except Exception as e:
    print(f"\n--- AN ERROR OCCURRED ---")
    print(f"Error: {e}")