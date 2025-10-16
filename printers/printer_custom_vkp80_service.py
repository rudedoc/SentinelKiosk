# printers/printer_custom_vkp80_service.py
import io
from datetime import datetime, UTC
from typing import Dict, List, Optional, Union, Any

from escpos.printer import Usb as EscposUsb
import usb.backend.libusb1 as libusb1

from barcode import get_barcode_class
from barcode.writer import ImageWriter
from PIL import Image

# --- Constants for the CUSTOM VKP80II-SX ---
# Based on the user and command manuals provided.
CUSTOM_VKP80_CODEPAGE = "CP858"  # Default from manual page 79
CUSTOM_VKP80_ENCODING = "cp858"  # Python's name for PC437
IMAGE_PRINT_IMPL = "bitImageColumn"
DEFAULT_BARCODE_IMAGE_OPTIONS = {
    "dpi": 203, "module_width": 0.45, "module_height": 12.0,
    "quiet_zone": 1.5, "font_size": 6, "write_text": True,
}

class PrinterCustomVkp80Service:
    """
    An ESC/POS printer service tailored for the CUSTOM VKP80II-SX kiosk printer.

    This class uses the correct CUSTOM/POS command sequences for printing,
    cutting, and ejecting tickets as documented in the official manuals.
    """

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

        if device:
            self._printer = device
            self.mock = False
            return

        if self.mock:
            return

        if EscposUsb is None:
            raise RuntimeError("python-escpos or a USB backend is not available.")

        backend = usb_backend or (libusb1.get_backend() if libusb1 else None)

        # Instantiate the Usb printer with a generic profile.
        # We will control the printer using raw commands based on the manual.
        self._printer = EscposUsb(
            vendor_id,
            product_id,
            interface=interface,
            in_ep=in_ep,
            out_ep=out_ep,
            usb_backend=backend,
            profile="default"
        )
        
        # Initialize the printer to a known state.
        self._printer._raw(b'\x1b\x40')  # ESC @ (Initialize)
        self._printer.charcode(CUSTOM_VKP80_CODEPAGE)

    def _format_timestamp(self, ts: Optional[Union[str, datetime]]) -> str:
        """Formats a datetime object into a string."""
        if not ts:
            return ""
        if isinstance(ts, str):
            return ts
        return ts.strftime("%Y-%m-%d %H:%M:%S")

    def print_ticket(
        self,
        brand: str,
        message: str,
        lines: Optional[List[str]] = None,
        barcode: Optional[str] = None,
        timestamp: Optional[Union[str, datetime]] = None,
        logo: Optional[Union[str, Any]] = None,
        barcode_type: str = "CODE39",
        amount: Optional[float] = None
    ) -> bool:
        """
        Prints a ticket using the specific CUSTOM/POS command set.
        
        logo: optional path to an image file (e.g. "logo.png") or a PIL Image-like object.
        Returns True on success, False on error.
        """
        ts = self._format_timestamp(timestamp)
        lines = lines or []

        if self.mock or self._printer is None:
            # Mock output for development without hardware
            out = ["=" * 40, f"{brand.center(40)}", "-" * 40, message]
            if logo:
                logo_repr = logo if isinstance(logo, str) else "[PIL Image]"
                out.insert(1, f"[LOGO: {logo_repr}]".center(40))
            if amount is not None:
                out.append(f"Amount: €{amount:,.2f}")
            out.extend(lines)
            if barcode:
                out.append(f"[BARCODE {barcode_type}]: {barcode}")
            if ts:
                out.append(f"{ts.rjust(40)}")
            out.append("=" * 40)
            print("\n".join(out))
            return True

        try:
            p = self._printer

            # Initialize printer and set encoding for this job
            p._raw(b'\x1b\x40')  # ESC @ (Initialize)
            p.charcode(CUSTOM_VKP80_CODEPAGE)
            p.encoding = CUSTOM_VKP80_ENCODING

            if logo:
                p.set(align="center")
                p.image(logo, impl=IMAGE_PRINT_IMPL)
                p.text("\n")

            p.set(align="center", bold=True, width=2, height=2)
            p.text(f"{brand}\n")
            
            p.set(align="center", bold=False, width=1, height=1)
            p.text(f"{message}\n")
            
            if amount is not None:
                p.set(align="center", bold=True, height=2)
                p.text(f"€{amount:,.2f}\n")
                p.set(height=1) # Reset height

            for ln in lines:
                p.set(align="center")
                p.text(f"{ln}\n")

            if ts:
                p.set(align="center", bold=False)
                p.text(f"{ts}\n")

            if barcode:
                p.set(align="center")
                self._print_barcode_image(p, barcode, barcode_type, DEFAULT_BARCODE_IMAGE_OPTIONS)

            # --- CORRECT CUT AND EJECT SEQUENCE FOR VKP80II ---
            # This sequence is crucial and based on the command manual.
            
            # 1. Send the raw "Total Cut" command (ESC i)
            p._raw(b'\x1b\x69')
            
            # 2. Send the raw "Ticket Ejected" command (GS e, n=5)
            p._raw(b'\x1d\x65\x05')
            # --- END OF SEQUENCE ---

            return True
        except Exception as e:
            print(f"PrinterCustomVkp80Service.print_ticket error: {e}")
            return False

    def _print_barcode_image(self, printer: Any, data: str, barcode_type: str, options: Optional[Dict] = None) -> None:
        """Renders a barcode to an in-memory image and prints it."""
        barcode_cls = get_barcode_class(barcode_type.lower())
        buffer = io.BytesIO()
        barcode_obj = barcode_cls(data, writer=ImageWriter())
        barcode_obj.write(buffer, options=options or {})
        buffer.seek(0)
        img = Image.open(buffer)
        img.load()
        printer.image(img, impl=IMAGE_PRINT_IMPL)