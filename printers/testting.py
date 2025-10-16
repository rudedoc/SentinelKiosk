# printers/testing.py
from datetime import datetime
import usb.backend.libusb1 as libusb1
from config import KioskConfig

backend = libusb1.get_backend()

printer_cls = KioskConfig.printer_service_class()

svc = printer_cls(
    vendor_id=KioskConfig.PRINTER_VENDOR_ID,
    product_id=KioskConfig.PRINTER_PRODUCT_ID,
    interface=KioskConfig.PRINTER_INTERFACE,
    in_ep=KioskConfig.PRINTER_IN_ENDPOINT,
    out_ep=KioskConfig.PRINTER_OUT_ENDPOINT,
    usb_backend=backend,
    mock=False,
)

success = svc.print_ticket(
    brand=KioskConfig.BRAND_NAME,
    message="Connection Successful!",
    lines=[
        "This was sent via PrinterService using python-escpos with explicit endpoints.",
    ],
    barcode="8145829964057",
    timestamp=datetime.utcnow(),
    barcode_type="EAN13",
    logo="branding/logo-contrast.png",  # added logo path
)

print("Printed successfully." if success else "Print failed.")