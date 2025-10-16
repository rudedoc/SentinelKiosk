# printers/printer_custom_vkp80_service.py
import io
from datetime import datetime, UTC
from typing import Dict, List, Optional, Union, Any

from escpos.printer import Usb as EscposUsb
import usb.backend.libusb1 as libusb1

from barcode import get_barcode_class
from barcode.writer import ImageWriter
from PIL import Image

# --- Constants ---
CUSTOM_VKP80_CODEPAGE = "CP437"
CUSTOM_VKP80_ENCODING = "cp437"
IMAGE_PRINT_IMPL = "bitImageColumn"
DEFAULT_BARCODE_IMAGE_OPTIONS = {
    "dpi": 200, "module_width": 0.60, "module_height": 16.0,
    "quiet_zone": 1.5, "font_size": 10, "write_text": True,
}

class PrinterCustomVkp80Service:
    # ... (the __init__ and _format_timestamp methods remain the same) ...
    def __init__(
        self,
        vendor_id: int,
        product_id: int,
        interface: int = 0,
        in_ep: int = 0,
        out_ep: int = 0,
        usb_backend=None,
        mock: bool = False,
        device: Optional[object] = None,
    ) -> None:
        """Initializes the printer service."""
        self.mock = bool(mock)
        self._printer = None
        if device: self._printer = device; self.mock = False; return
        if self.mock: return
        if EscposUsb is None: raise RuntimeError("python-escpos or a USB backend is not available.")
        backend = usb_backend or (libusb1.get_backend() if libusb1 else None)
        self._printer = EscposUsb(vendor_id, product_id, interface=interface, in_ep=in_ep, out_ep=out_ep, usb_backend=backend, profile="default")
        self._printer._raw(b'\x1b\x40')
        self._printer.charcode(CUSTOM_VKP80_CODEPAGE)

    def _format_timestamp(self, ts: Optional[Union[str, datetime]]) -> str:
        if not ts: return ""
        if isinstance(ts, str): return ts
        return ts.strftime("%Y-%m-%d %H:%M:%S")

    def print_ticket(
        self,
        brand: str,
        message: str,
        lines: Optional[List[Union[str, Dict]]] = None,
        barcode: Optional[str] = None,
        timestamp: Optional[Union[str, datetime]] = None,
        logo: Optional[Union[str, Any]] = None,
        barcode_type: str = "EAN8",
        amount: Optional[float] = None
    ) -> bool:
        """Prints a ticket using a simple, unformatted style for all lines."""
        ts = self._format_timestamp(timestamp)
        lines = lines or []

        if self.mock or self._printer is None:
            # (Your existing mock logic is fine here)
            return True

        try:
            p = self._printer
            p._raw(b'\x1b\x40')
            p.charcode(CUSTOM_VKP80_CODEPAGE)
            p.encoding = CUSTOM_VKP80_ENCODING

            if logo:
                p.set(align="center")
                p.image(logo, impl=IMAGE_PRINT_IMPL)

            if brand:
                p.set(align="center", bold=True, width=2, height=2)
                p.text(f"{brand}\n")
            
            if message:
                p.set(align="center", bold=False, width=1, height=1)
                p.text(f"{message}\n")

            # --- SIMPLIFIED LOOP ---
            # Set one style for all subsequent lines and ignore formatting from JSON.
            p.set(align='center', bold=False, height=1, width=1)
            
            for line_data in lines:
                if isinstance(line_data, dict):
                    text = line_data.get('text', '')
                    # Just print the text, ignoring all style information
                    p.text(f"{text}\n")
                else:
                    # Fallback for simple strings
                    p.text(f"{line_data}")
            # --- END OF LOOP ---

            p.text("\n")

            p.set(align='center')

            if barcode:
                self._print_barcode_image(p, barcode, barcode_type, DEFAULT_BARCODE_IMAGE_OPTIONS)
                p.text('\n')

            if ts:
                p.set(font='b')
                p.text(f"{ts}\n")
            
            # Final cut and present sequence
            p._raw(b'\x1b\x69')
            p._raw(b'\x1d\x65\x05')

            return True
        except Exception as e:
            print(f"PrinterCustomVkp80Service.print_ticket error: {e}")
            return False

    def _print_barcode_image(self, printer: Any, data: str, barcode_type: str, options: Optional[Dict] = None) -> None:
        # ... (this method remains the same) ...
        barcode_cls = get_barcode_class(barcode_type.lower())
        buffer = io.BytesIO()
        barcode_obj = barcode_cls(data, writer=ImageWriter())
        barcode_obj.write(buffer, options=options or {})
        buffer.seek(0)
        img = Image.open(buffer)
        img.load()
        printer.image(img, impl=IMAGE_PRINT_IMPL)