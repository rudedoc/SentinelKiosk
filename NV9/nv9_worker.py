# nv9_worker.py
from PySide6.QtCore import QObject, Signal, Slot
from .nv9_core import NV9Validator, NV9Event
import time
from typing import Optional

class NV9Worker(QObject):
    """
    Qt-friendly worker that owns an NV9Validator and polls it in a loop.
    Intended to be moved to a dedicated QThread via QObject.moveToThread().
    """

    # UI-friendly signals
    status = Signal(str)
    error = Signal(str)
    connected = Signal()
    disconnected = Signal()
    eventReceived = Signal(object)   # NV9Event
    credit = Signal(int, int)        # value, channel

    def __init__(self,
                 port: str,
                 baud: int = 9600,
                 poll_ms: int = 100,
                 parent: Optional[QObject] = None,
                 validator: Optional[NV9Validator] = None):
        super().__init__(parent)
        self.port = port
        self.baud = baud
        self.poll_interval = max(0.01, poll_ms / 1000.0)  # floor to 10 ms
        self._running = False

        # Allow DI for tests; otherwise create a real validator
        self.validator = validator or NV9Validator(port, baud)

        # Wire core callbacks to Qt signals
        self.validator.on_status = self.status.emit
        self.validator.on_error = self.error.emit
        self.validator.on_event = self._on_event

    @Slot()
    def start(self):
        """
        Worker thread entry point.
        - Opens the serial port, initializes the validator, then polls at a steady cadence.
        - Emits Qt signals for status/errors/connection and forwards events as they arrive.
        - Safe to call once; subsequent calls while running are ignored.
        """
        if self._running:
            return

        self._running = True
        connected_ok = False  # track whether we actually connected (affects disconnected signal)
        try:
            # --- 1) Connect the device (open serial port) -----------------------
            if not self.validator.connect():
                self.error.emit("Connect failed")
                self._running = False
                return
            connected_ok = True
            self.connected.emit()

            # --- 2) Initialize the device (SYNC -> SETUP -> optional protocol -> INHIBITS -> ENABLE)
            if not self.validator.initialize_device():
                self.error.emit("Init failed")
                # Hard-stop on init failure: there’s nothing useful we can do without ENABLE
                self._running = False
                return

            # --- 3) Poll loop ----------------------------------------------------
            # Use a monotonic clock to avoid issues if the system clock changes.
            next_poll_time = time.monotonic()

            while self._running:
                try:
                    # Ask the device for any pending events since the last poll.
                    events = self.validator.poll_once()

                    # Convenience: bubble up credits as a dedicated signal.
                    for ev in events:
                        if ev.code == self.validator.RSP_SSP_CREDIT_NOTE and ev.value is not None and ev.channel is not None:
                            self.credit.emit(ev.value * 100, ev.channel)

                except Exception as e:
                    # Keep the loop alive on unexpected errors; surface details to UI/logging.
                    self.error.emit(f"Worker loop error ({type(e).__name__}): {e}")

                # --- 4) Maintain a steady polling cadence ------------------------
                # Target the next tick time; sleep the remaining duration if we're early.
                next_poll_time += self.poll_interval
                sleep_for = next_poll_time - time.monotonic()

                if sleep_for > 0:
                    time.sleep(sleep_for)
                else:
                    # If we’re behind schedule (e.g., slow poll), reset the next target to "now"
                    # to avoid drift growing unbounded.
                    next_poll_time = time.monotonic()

        finally:
            # --- 5) Always clean up ----------------------------------------------
            # Try to leave the device in a safe state; ignore errors on shutdown.
            try:
                self.validator.disable()
            except Exception:
                pass
            self.validator.disconnect()

            # Mark not running so this worker can be started again later if desired.
            self._running = False

            # Only emit 'disconnected' if we actually emitted 'connected'
            if connected_ok:
                self.disconnected.emit()


    @Slot()
    def stop(self):
        """Request the polling loop to stop gracefully."""
        self._running = False
        # Keep the core's stop flag in sync in case it gets used later
        try:
            self.validator.stop()
        except Exception:
            pass

    # --- Core event bridge ---
    def _on_event(self, ev: NV9Event):
        # Re-emit as Qt signal for UI or logging
        self.eventReceived.emit(ev)