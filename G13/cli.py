# G13/cli.py
import sys
import signal
from PySide6.QtCore import QCoreApplication, QTimer
from .g13_worker import G13Worker  # <-- lowercase filename already

def _print_status(st: dict):
    print("Status:", st)

def _print_event(ev: dict):
    if ev.get("type") == "credit":
        label = ev.get("label") or f"type {ev.get('coin_type')}"
        path = ev.get("path")
        print(f"[CREDIT] {label} (type {ev.get('coin_type')}, path {path})")
    else:
        print(f"[ERROR ] {ev.get('code')} - {ev.get('desc')}")

def _print_error(msg: str):
    print("ERROR:", msg)

def main():
    app = QCoreApplication(sys.argv)

    # Clean Ctrl+C handling for Qt event loop
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    # On Windows, add a tiny timer so SIGINT is processed
    QTimer.singleShot(0, lambda: None)

    # Instantiate the worker (same defaults you used)
    worker = G13Worker(port="COM8", addr=2)

    worker.status.connect(_print_status)
    worker.event.connect(_print_event)
    worker.error.connect(_print_error)
    worker.started.connect(lambda: print("G13Worker started. Polling credits... (Ctrl+C to quit)"))
    worker.stopped.connect(lambda: print("G13Worker stopped."))

    # Start the worker
    worker.start()

    # Start Qt event loop
    rc = app.exec()

    # Ensure clean stop if we exit the loop (e.g., Ctrl+C)
    worker.stop()
    sys.exit(rc)

if __name__ == "__main__":
    main()
