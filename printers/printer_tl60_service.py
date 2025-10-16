# printers/printer_service.py
import io
from datetime import datetime
from typing import Dict, List, Optional, Union, Any

from escpos.printer import Usb as EscposUsb
import usb.backend.libusb1 as libusb1  # optional: allows passing a backend

from barcode import get_barcode_class
from barcode.writer import ImageWriter
from PIL import Image

# Custom TL60 printer defaults
# CP1252 covers Western European characters incl. the Euro sign commonly
# used on receipts.
DEFAULT_CODEPAGE = "CP858"
# The name for Python's character encoding library
DEFAULT_ENCODING = "cp858"
IMAGE_PRINT_IMPL = "bitImageColumn"
DEFAULT_BARCODE_IMAGE_OPTIONS = {
    "dpi": 203,
    "module_width": 0.45,  # reduce if still too wide (e.g., 0.22 or 0.20)
    "module_height": 12.0,
    "quiet_zone": 1.5,
    "font_size": 6,
    "write_text": True,
}

class PrinterTl60Service:
    """
    Simple ESC/POS printer service.

    Usage:
      svc = PrinterService(vendor_id=0x0519, product_id=0x2013, interface=0,
                           in_ep=0x81, out_ep=0x03, mock=False)
      svc.print_ticket(brand="MyBrand", message="Paid", lines=["Line1","Line2"],
                       barcode="12033506", timestamp=datetime.utcnow(), logo="logo.png")
    """

    def __init__(
        self,
        vendor_id: int,
        product_id: int,
        interface: int = 0,
        in_ep: int = 0x81,
        out_ep: int = 0x03,
        usb_backend=None,
        mock: bool = False,
        device: Optional[object] = None,
    ) -> None:
        self.mock = bool(mock)
        self._printer = None

        if device is not None:
            # allow injecting a pre-created escpos printer (useful for tests)
            self._printer = device
            self.mock = False
            return

        if self.mock:
            # don't attempt to open hardware
            return

        if EscposUsb is None:
            raise RuntimeError("escpos or usb backend is not available; run with mock=True or install dependencies")

        backend = usb_backend
        if backend is None and libusb1 is not None:
            backend = libusb1.get_backend()

        # instantiate the Usb printer
        self._printer = EscposUsb(
            vendor_id,
            product_id,
            interface=interface,
            in_ep=in_ep,
            out_ep=out_ep,
            usb_backend=backend,
             profile="TM-T88III"
        )
        
        self._printer.charcode(DEFAULT_CODEPAGE)

    def _format_timestamp(self, ts: Optional[Union[str, datetime]]) -> str:
        if ts is None:
            return ""
        if isinstance(ts, str):
            return ts
        # datetime -> ISO-ish friendly format
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
        Print a ticket.

        logo: optional path to an image file (e.g. "logo.png") or a PIL Image-like object.
        Returns True on success, False on error (or when running mock and output is printed to console).
        """
        codepage = DEFAULT_CODEPAGE
        barcode_image_options = DEFAULT_BARCODE_IMAGE_OPTIONS
        
        ts = self._format_timestamp(timestamp)
        lines = lines or []

        if self.mock or self._printer is None:
            # Simple mock output for development/tests
            out = []
            out.append("=" * 40)
            if logo:
                # show a short placeholder for the mock
                logo_repr = logo if isinstance(logo, str) else "[PIL Image]"
                out.append(f"{(f'[LOGO: {logo_repr}]').center(40)}")
                out.append("-" * 40)
            out.append(f"{brand.center(40)}")
            out.append("-" * 40)
            out.append(message)
            out.extend(lines)
            if barcode:
                out.append(f"[BARCODE {barcode_type}{'(img)'}]: {barcode}")
            if ts:
                out.append(f"{ts.rjust(40)}")
            out.append("=" * 40)
            print("\n".join(out))
            return True

        try:
            p = self._printer

            # Reset printer and apply encoding specific to the Custom TL60
            p._raw(b"\x1b\x40")  # ESC @ reset
            p.charcode(DEFAULT_CODEPAGE)
            p.encoding = DEFAULT_ENCODING

            # Print logo first (if provided)
            if logo:
                p.set(align="center")
                p.image(logo, impl=IMAGE_PRINT_IMPL)
                p.text("\n")

            # Header brand
            p.set(align="center", bold=True, width=2, height=2)
            p.text(f"{brand}\n")
            
            # Main message
            p.set(align="center", bold=False, width=1, height=1)
            p.text(f"{message}\n")
            
            # Amount (if provided)
            if amount is not None:
                p._raw(b"\x1d!\x12") # x12 = double height + bold
                p.set(align="center", bold=True)
                p.text(f"€{amount:,.2f}\n")

            # Reset back to normal
                p._raw(b"\x1d!\x00")
               
            # Additional lines
            for ln in lines:
                p.set(align="center")
                p.text(f"{ln}\n")

            # Timestamp (small, right-aligned)
            if ts:
                p.set(align="center", bold=False)
                p.text(f"{ts}\n")

            # Barcode (if provided)
            if barcode:
                p.set(align="center")
                self._print_barcode_image(p, barcode, barcode_type, barcode_image_options)

            p._raw(b"\x1b\x69")  # Full cut command for TL60
            return True
        except Exception as e:
            print(f"PrinterTl60Service.print_ticket error: {e}")
            return False

    def _print_barcode_image(
        self,
        printer: Any,
        data: str,
        barcode_type: str,
        options: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Render a barcode to an in-memory image buffer and print via ESC/POS image command."""

        barcode_cls = get_barcode_class(barcode_type.lower())

        buffer = io.BytesIO()
        provided_options = dict(options) if options else {}
        write_options = {"module_height": 12.0, "font_size": 6}
        write_options.update(provided_options)
        barcode_obj = barcode_cls(data, writer=ImageWriter())
        barcode_obj.write(buffer, options=write_options)
        buffer.seek(0)
        try:
            img = Image.open(buffer)
            img.load()
            printer.image(img, impl=IMAGE_PRINT_IMPL)
        except Exception:
            # Fall back to buffer if PIL pathway isn’t available in the escpos version in use
            buffer.seek(0)
            printer.image(buffer, impl=IMAGE_PRINT_IMPL)