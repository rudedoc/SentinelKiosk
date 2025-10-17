# G13/g13_worker.py
from __future__ import annotations
import signal
from typing import Optional, Dict
from PySide6.QtCore import QObject, QThread, QTimer, Signal, Slot
from .g13_validator import G13Validator  # <-- lowercase filename

class G13Worker(QObject):
    # ---- Signals you can wire to your UI ----
    started = Signal()
    stopped = Signal()
    status = Signal(dict)          # {'manufacturer':..., 'product':..., 'software':..., 'inhibits':..., 'master_inhibit':...}
    event = Signal(dict)           # {'type':'credit'|'error', ...}
    error = Signal(str)

    def __init__(self, port: str, addr: Optional[int] = None, parent: Optional[QObject]=None):
        super().__init__(parent)
        self._port = port
        self._addr = addr
        self._validator: Optional[G13Validator] = None
        self._thread: Optional[QThread] = None
        self._timer: Optional[QTimer] = None
        self._interval_ms = 200
        self._running = False

    # ----- Public API (thread-safe from main thread) -----
    @Slot()
    def start(self):
        if self._running:
            return
        self._running = True

        # Thread to host the worker (so serial I/O doesn't block the UI)
        self._thread = QThread()
        self.moveToThread(self._thread)

        # When thread starts, run our _on_thread_started in the thread context
        self._thread.started.connect(self._on_thread_started)
        self._thread.finished.connect(self._on_thread_finished)
        self._thread.start()

    @Slot()
    def stop(self):
        self._running = False
        if self._timer:
            self._timer.stop()
            self._timer.deleteLater()
            self._timer = None
        if self._validator:
            try:
                self._validator.close()
            except Exception:
                pass
            self._validator = None
        if self._thread:
            self._thread.quit()
            self._thread.wait()
            self._thread = None
        self.stopped.emit()

    @Slot(int)
    def setIntervalMs(self, ms: int):
        self._interval_ms = max(50, ms)  # guard for silly values
        if self._timer:
            self._timer.setInterval(self._interval_ms)

    # ----- Private: lives on the worker thread -----
    @Slot()
    def _on_thread_started(self):
        try:
            self._validator = G13Validator(port=self._port, addr=self._addr).open()
            # Emit initial status (and also let UI read it)
            st = self._validator.status()
            self.status.emit(st)

            # Enable coin acceptance and prep mapping/counter
            self._validator.enable_all()
            self._validator._coin_types = self._validator.build_coin_type_map()
            self._validator._sync_counter()

            # Timer-based polling on this thread
            self._timer = QTimer()
            self._timer.setInterval(self._interval_ms)
            self._timer.timeout.connect(self._poll_once)
            self._timer.start()

            self.started.emit()

        except Exception as e:
            self.error.emit(f"G13Worker start failed: {e}")
            self.stop()  # will clean up and emit stopped

    @Slot()
    def _on_thread_finished(self):
        # nothing special, here for completeness
        pass

    @Slot()
    def _poll_once(self):
        if not self._running or not self._validator:
            return
        try:
            for ev in self._validator.poll_once():
                self.event.emit(ev)
        except Exception as e:
            self.error.emit(f"G13Worker poll error: {e}")
            # Optional: stop on persistent errors
            # self.stop()
