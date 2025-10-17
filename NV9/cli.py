# NV9/cli.py
import time, sys
from NV9.nv9_core import NV9Validator
from kiosk_config import KioskConfig


class EventPrinter:
    """Encapsulates event de-duping and pretty-printing logic."""

    def __init__(self):
        self._last_label_key = None  # e.g., "READING", "REJECTING", "NOTE_READ:ch=2", "UNKNOWN:0xED"

    def _unknown_label(self, ev):
        return f"UNKNOWN(0x{ev.code:02X})" if getattr(ev, "code", None) is not None else "UNKNOWN"

    def _emit_on_change(self, label_key: str, line: str):
        """Print only when the state (label_key) changes."""
        if self._last_label_key == label_key:
            return
        self._last_label_key = label_key
        print(line)

    def print_event(self, ev, validator: NV9Validator):
        """
        Pretty-print a single NV9Event.
        - CREDIT and REJECTED always print (no dedupe).
        - All other events only print when their label key changes.
        """
        # Normalize a display name for UNKNOWNs
        name = ev.name if ev.name != "UNKNOWN" else self._unknown_label(ev)

        # --- Always print money/outcome events and return early ---
        if name == "CREDIT":
            ch = getattr(ev, "channel", None)
            val = getattr(ev, "value", None)
            if ch is not None and val is not None:
                print(f"[CREDIT] €{val} (ch {ch})")
            else:
                print("[CREDIT] KNOWN VALUE")
            return  # IMPORTANT: do not fall through (prevents "[EVENT] CREDIT")

        if name == "REJECTED":
            reason = validator.get_last_reject_reason() or "unknown"
            print(f"[REJECTED] {reason}")
            return  # IMPORTANT: do not fall through

        # --- De-duped state prints below ---
        if name == "READING":
            self._emit_on_change("READING", "[EVENT] READING")
            return

        if name == "REJECTING":  # 0xED mapped in nv9_core
            self._emit_on_change("REJECTING", "[EVENT] REJECTING")
            return

        if name == "STACKING":
            self._emit_on_change("STACKING", "[EVENT] STACKING")
            return

        if name == "STACKED":
            self._emit_on_change("STACKED", "[EVENT] STACKED")
            return

        if name == "NOTE_READ":
            ch = getattr(ev, "channel", None)
            val = getattr(ev, "value", None)
            key = f"NOTE_READ:ch={ch}" if ch is not None else "NOTE_READ"
            if ch is not None and val is not None:
                self._emit_on_change(key, f"[EVENT] NOTE_READ (ch {ch}, EUR {val})")
            elif ch is not None:
                self._emit_on_change(key, f"[EVENT] NOTE_READ (ch {ch})")
            else:
                self._emit_on_change(key, "[EVENT] NOTE_READ")
            return

        if name in ("DISABLED", "SLAVE_RESET"):
            self._emit_on_change(name, f"[EVENT] {name}")
            return

        # Fallback (including labeled UNKNOWNs). Make code part of the key so different unknowns still show up.
        if name.startswith("UNKNOWN("):
            # extract the hex for stability of the key
            key = f"UNKNOWN:{name.split('UNKNOWN(')[1].rstrip(')')}"
            self._emit_on_change(key, f"[EVENT] {name}")
        else:
            self._emit_on_change(name, f"[EVENT] {name}")


def main():
    cfg = KioskConfig()

    port = cfg.nv9_port_name
    baud = cfg.nv9_baud_rate
    slave_id = cfg.nv9_slave_id
    proto = cfg.nv9_host_protocol_version

    v = NV9Validator(port, baud, slave_id=slave_id, host_protocol_version=proto)
    v.on_status = lambda s: print("[STATUS]", s)
    v.on_error  = lambda e: print("[ERROR]",  e)

    if not v.connect(): sys.exit(1)
    if not v.initialize_device():
        print("Init failed"); sys.exit(1)

    printer = EventPrinter()

    print("Press Ctrl+C to exit.")
    try:
        while True:
            for ev in v.poll_once():
                printer.print_event(ev, v)
            time.sleep(0.1)
    except KeyboardInterrupt:
        pass
    finally:
        v.disconnect()


if __name__ == "__main__":
    main()
