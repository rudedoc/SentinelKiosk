import sys
import os
import json
import argparse
from datetime import datetime, UTC
from typing import Any, Dict, Optional
from PySide6.QtCore import Qt, QObject, QUrl, Signal, Slot, QThread, QTimer
from PySide6.QtWidgets import QApplication, QMainWindow
from PySide6.QtWebEngineWidgets import QWebEngineView

# NEW: Import the interceptor base class
from PySide6.QtWebEngineCore import QWebEngineUrlRequestInterceptor, QWebEngineUrlRequestInfo as ReqInfo
from PySide6.QtWebChannel import QWebChannel

from kiosk_config import KioskConfig

from NV9.nv9_worker import NV9Worker
from NV9.nv9_core import NV9Validator
from G13.g13_worker import G13Worker

from logger import get_logger

from printers.printer_custom_vkp80_service import PrinterCustomVkp80Service

logger = get_logger(__name__)

logger.info("STARTING APPLICATION")

class CustomRequestInterceptor(QWebEngineUrlRequestInterceptor):
    def __init__(self, key, user_id, parent=None):
        super().__init__(parent)
        self.preshared_key = key
        self.user_id = user_id

        # Only your own origins here (do NOT include S3/CDNs)
        self._auth_hosts = {
            "betxs-dev.eu.ngrok.io",
            # "api.yourdomain.tld",
        }

        RT = ReqInfo.ResourceType
        # Helper to safely grab enums that may not exist in this Qt version
        def rt(name): return getattr(RT, name, None)

        # Where it makes sense to send auth (filter out Nones)
        self._auth_types = {
            x for x in {
                rt("ResourceTypeMainFrame"),
                rt("ResourceTypeSubFrame"),
                rt("ResourceTypeXhr"),
                rt("ResourceTypeFetch"),          # not present on some builds
                rt("ResourceTypeServiceWorker"),
                rt("ResourceTypeWorker"),
            } if x is not None
        }

        # Never add auth to static assets (and prefetch)
        self._asset_types = {
            x for x in {
                rt("ResourceTypeImage"),
                rt("ResourceTypeStylesheet"),
                rt("ResourceTypeFontResource"),
                rt("ResourceTypeMedia"),
                rt("ResourceTypeFavicon"),
                rt("ResourceTypePrefetch"),
            } if x is not None
        }

    def interceptRequest(self, info: ReqInfo):
        host = info.requestUrl().host().lower()
        rtype = info.resourceType()

        # Skip assets regardless of host
        if rtype in self._asset_types:
            return

        # Attach only for your own hosts and “authful” request types
        if host in self._auth_hosts and rtype in self._auth_types:
            info.setHttpHeader(b"Authorization", f"Bearer {self.preshared_key}".encode("utf-8"))
            info.setHttpHeader(b"X-User-Id", str(self.user_id).encode("utf-8"))


class MainWindow(QMainWindow):
    def __init__(self, config):
        super().__init__()

        self._bridge_ready = False
        self._pending_web_events = []
        self.config = config
        self.printer_service = None

        window_title = f"SentinelKiosk - User ID: {self.config.user_id}"
        self.setWindowTitle(window_title)
        self.setGeometry(100, 100, 1280, 720)

        self.web_view = QWebEngineView()
        self.setCentralWidget(self.web_view)

        # Track when the target page finishes loading so we can react to it.
        self.web_view.loadStarted.connect(self._on_load_started)
        self.web_view.loadFinished.connect(self.on_load_finished)

        # Prepare a bridge so JavaScript inside the page can talk back to Python.
        self.channel = QWebChannel(self.web_view.page())
        self.js_bridge = PageEventBridge(parent=self)
        self.js_bridge.eventReceived.connect(self.on_js_event)
        self.channel.registerObject("SentinelBridge", self.js_bridge)
        self.web_view.page().setWebChannel(self.channel)

        # --- 🚀 Set up the Interceptor ---
        #  Create an instance of our interceptor with the key and attach it to the profile
        self.interceptor = CustomRequestInterceptor(self.config.preshared_key, self.config.user_id)
        profile = self.web_view.page().profile()
        profile.setUrlRequestInterceptor(self.interceptor)

        # --- 🖨️ Initialize the Printer Service ---
        self._initialize_printer() # <-- ADD THIS CALL

        # --- 💶 Initialize the NV9 bill validator worker (background polling) ---
        self._initialize_nv9()

        # --- 💰 Initialize the G13 coin validator worker ---
        self._initialize_g13()

        # --- Load the URL ---
        # Now, when this request is made, the interceptor will add the header.
        self.web_view.setUrl(QUrl(self.config.starting_url))
        logger.info(f"Loaded URL: {self.config.starting_url}")
        logger.info("APPLICATION STARTED")

    # --- ADD THIS ENTIRE METHOD to the MainWindow class ---
    def _initialize_printer(self):
        """Safely initializes the printer service based on the configuration."""
        # Don't try to connect to hardware if mock mode is on
        if self.config.printer_mock:
            logger.info("Printer is in mock mode. Real hardware will not be used.")
            self.printer_service = PrinterCustomVkp80Service(vendor_id=0, product_id=0, mock=True)
            return
            
        try:
            logger.info("Initializing POS printer...")
            self.printer_service = PrinterCustomVkp80Service(
                vendor_id=self.config.printer_vendor_id,
                product_id=self.config.printer_product_id,
                interface=self.config.printer_interface,
                in_ep=self.config.printer_in_endpoint,
                out_ep=self.config.printer_out_endpoint,
                mock=self.config.printer_mock,
            )
            logger.info("POS printer initialized successfully.")
        except Exception as e:
            # If the printer can't be found, log it but don't crash the app
            logger.error(f"FATAL: Could not initialize printer: {e}")
            logger.warning("Application will continue without printer functionality.")
            self.printer_service = None # Ensure it's None on failure

    def _initialize_nv9(self):
        """Create NV9 worker in its own QThread and start background polling."""
        # Prepare thread + worker
        self._nv9_thread = QThread(self)

        validator = NV9Validator(
            port=self.config.nv9_port_name,
            baud=self.config.nv9_baud_rate,
            slave_id=self.config.nv9_slave_id,
            host_protocol_version=self.config.nv9_host_protocol_version,
        )

        self._nv9_worker = NV9Worker(
            port=self.config.nv9_port_name,
            baud=self.config.nv9_baud_rate,
            poll_ms=100,           # 10 Hz; adjust if needed
            validator=validator,   # <-- pass the validator here
        )
        self._nv9_worker.moveToThread(self._nv9_thread)

        # Lifecycle wiring
        self._nv9_thread.started.connect(self._nv9_worker.start)
        self._nv9_worker.disconnected.connect(self._nv9_thread.quit)
        self._nv9_thread.finished.connect(self._nv9_worker.deleteLater)

        # Logging hooks
        self._nv9_worker.rejected.connect(lambda reason: logger.info(f"[NV9][EVENT] REJECTED — {reason}"))
        self._nv9_worker.status.connect(lambda s: logger.info(f"[NV9][STATUS] {s}"))
        self._nv9_worker.error.connect(lambda e: logger.error(f"[NV9][ERROR] {e}"))
        self._nv9_worker.connected.connect(lambda: logger.info("[NV9] Connected"))
        self._nv9_worker.disconnected.connect(lambda: logger.info("[NV9] Disconnected"))
        self._nv9_worker.credit.connect(self._on_bill_credit)

        # Raw event stream (lightweight log for now)
        def _unknown_label(ev):
            return f"UNKNOWN(0x{ev.code:02X})" if getattr(ev, "code", None) is not None else "UNKNOWN"

        def _log_event(ev):
            name = ev.name if ev.name != "UNKNOWN" else _unknown_label(ev)

            if name == "CREDIT":
                # ev.value is major units (e.g., euros). The worker also emits credit (minor units) separately.
                logger.info(f"[NV9][EVENT] CREDIT value={ev.value} channel={ev.channel}")

            elif name == "REJECTED":
                # Ask the validator for the last reject reason code/message
                try:
                    reason = self._nv9_worker.validator.get_last_reject_reason() or "unknown"
                except Exception:
                    reason = "unknown"
                logger.info(f"[NV9][EVENT] REJECTED — {reason}")

            elif name == "READING":
                logger.debug("[NV9][EVENT] READING")

            elif name == "REJECTING":
                logger.debug("[NV9][EVENT] REJECTING")

            elif name == "REJECTED":
                # Reason already logged via self._nv9_worker.rejected signal
                logger.info("[NV9][EVENT] REJECTED")

            elif name == "STACKING":
                logger.debug("[NV9][EVENT] STACKING")

            elif name == "STACKED":
                logger.info("[NV9][EVENT] STACKED")

            elif name == "NOTE_READ":
                ch = getattr(ev, "channel", None)
                val = getattr(ev, "value", None)
                if ch is not None and val is not None:
                    logger.info(f"[NV9][EVENT] NOTE_READ (ch {ch}, EUR {val})")
                elif ch is not None:
                    logger.info(f"[NV9][EVENT] NOTE_READ (ch {ch})")
                else:
                    logger.info("[NV9][EVENT] NOTE_READ")

            elif name == "DISABLED":
                logger.warning("[NV9][EVENT] DISABLED")

            elif name == "SLAVE_RESET":
                logger.warning("[NV9][EVENT] SLAVE_RESET")

            else:
                # Any other or labeled-UNKNOWN falls back here
                logger.info(f"[NV9][EVENT] {name}")

        self._nv9_worker.eventReceived.connect(_log_event)

        # Go!
        self._nv9_thread.start()

    def _initialize_g13(self):
        """Create G13 worker in its own QThread and start background polling."""
        self._g13_thread = QThread(self)
        self._g13_worker = G13Worker(
            port=self.config.g13_port_name,
            addr=self.config.g13_address
        )
        self._g13_worker.moveToThread(self._g13_thread)

        # Lifecycle
        self._g13_thread.started.connect(self._g13_worker.start)
        self._g13_worker.stopped.connect(self._g13_thread.quit)
        self._g13_thread.finished.connect(self._g13_worker.deleteLater)

        # Logging + event bridge
        self._g13_worker.started.connect(lambda: logger.info("[G13] Started"))
        self._g13_worker.stopped.connect(lambda: logger.info("[G13] Stopped"))
        self._g13_worker.error.connect(lambda e: logger.error(f"[G13][ERROR] {e}"))
        self._g13_worker.status.connect(lambda st: logger.info(f"[G13][STATUS] {st}"))
        self._g13_worker.event.connect(self._on_g13_event)

        self._g13_thread.start()

    def _on_g13_event(self, ev: dict):
        """
        ev example (credit):
        {
        'type':'credit', 'coin_type':4, 'coin_id':'EU050A',
        'label':'€0.50 (EU050A)', 'value_cents':50, 'path':1, 'counter':123
        }
        or error: {'type':'error','code':14,'desc':'Credit sensor blocked','counter':124}
        """
        try:
            if ev.get("type") == "credit":
                value_minor = int(ev.get("value_cents") or 0)
                coin_type = int(ev.get("coin_type") or 0)
                coin_id = ev.get("coin_id")
                label = ev.get("label")
                path = int(ev.get("path") or 0)

                payload = {
                    "value_minor": value_minor,
                    "value_major": value_minor / 100.0,
                    "coin_type": coin_type,
                    "coin_id": coin_id,
                    "label": label,
                    "path": path,
                    "counter": int(ev.get("counter") or 0),
                    "ts": int(datetime.now(UTC).timestamp() * 1000)
                }
                logger.info(f"[G13][CREDIT] {label} (type {coin_type}, path {path})")
                logger.info(f"[G13][CREDIT→WEB] €{payload['value_major']:.2f}, type={coin_type}, path={path}")
                self._send_to_web("g13_credit", payload)
            else:
                # error event
                payload = {
                    "code": int(ev.get("code") or 0),
                    "desc": ev.get("desc"),
                    "counter": int(ev.get("counter") or 0),
                    "ts": int(datetime.now(UTC).timestamp() * 1000)
                }
                logger.warning(f"[G13][ERROR EVENT] {payload['code']} - {payload['desc']}")
                self._send_to_web("g13_error", payload)
        except Exception as ex:
            logger.exception(f"[G13] Failed to handle event: {ex}")

    def _on_g13_status(self, st: dict):
        # If you want to forward status to the web too:
        self._send_to_web("g13_status", st)


    def _on_load_started(self):
        # Page navigating; pause direct sends until bridge is reinjected & confirmed
        self._bridge_ready = False

    def on_load_finished(self, ok: bool) -> None:
        """Handle the page load event from the embedded web view."""
        if not ok:
            logger.error(f"Failed to load URL: {self.web_view.url().toString()}")
            return

        logger.info(f"Page finished loading: {self.web_view.url().toString()}")
        self.inject_event_capture()

    def inject_event_capture(self) -> None:
        """Inject helper JavaScript that forwards browser events to Python."""
        event_script = """
            (function () {
                if (window.__sentinelBridgeInitialized) { return; }
                window.__sentinelBridgeInitialized = true;

                // 1) Define Python → JS immediately (does NOT depend on WebChannel)
                if (typeof window.receiveSentinelEvent !== 'function') {
                    console.log('defining receiveSentinelEvent (eager)');
                    window.receiveSentinelEvent = function (type, data) {
                    try {
                        window.dispatchEvent(new CustomEvent(type, { detail: data }));
                    } catch (e) {
                        console.warn('Failed to dispatch CustomEvent', e);
                    }
                    };
                }

                function forwardEvent(bridge, type, payload) {
                    try { bridge.handleEvent(JSON.stringify({ type, payload })); }
                    catch (error) { console.error('Failed to forward event', type, error); }
                }

                function ensureChannelReady(callback, attempt) {
                    attempt = attempt || 0;

                    if (window.SentinelBridge) {
                    callback(window.SentinelBridge);
                    return;
                    }

                    if (typeof qt !== 'undefined' && qt.webChannelTransport) {
                    new QWebChannel(qt.webChannelTransport, function (channel) {
                        window.SentinelBridge = channel.objects.SentinelBridge;
                        callback(window.SentinelBridge);
                    });
                    return;
                    }

                    if (attempt > 20) {
                    console.warn('SentinelBridge: qt.webChannelTransport never became available.');
                    return;
                    }

                    window.setTimeout(function () {
                    ensureChannelReady(callback, attempt + 1);
                    }, 100);
                }

                function installHandlers(bridge) {
                    // 2) JS → Python (only needs WebChannel)
                    window.dispatchSentinelEvent = function (type, data) {
                    forwardEvent(bridge, type, data);
                    };

                    // Whatever listeners you want (optional)
                    window.addEventListener('message', function (event) {
                    forwardEvent(bridge, 'message', event.data);
                    }, false);

                    document.addEventListener('click', function (event) {
                    forwardEvent(bridge, 'click', {
                        tag: event.target.tagName,
                        id: event.target.id || null,
                        classes: event.target.className || null,
                        timestamp: Date.now()
                    });
                    }, true);
                }

            // Only the JS→Python side waits for WebChannel
            ensureChannelReady(installHandlers);
            })();
        """

        # Load the web channel helper if the page has not already done so.
        bridge_loader = """
            (function(){
                function injectBridge(){ %s }

                if (typeof QWebChannel !== 'undefined') {
                    injectBridge();
                    return;
                }

                var script = document.createElement('script');
                script.src = 'qrc:///qtwebchannel/qwebchannel.js';
                script.type = 'text/javascript';
                script.onload = injectBridge;
                document.head.appendChild(script);
            })();
        """ % event_script

        self.web_view.page().runJavaScript(bridge_loader, self._on_bridge_injected)

    def _on_bridge_injected(self, _=None):
        # probe for the helper the page actually defines
        self.web_view.page().runJavaScript(
            "typeof window.receiveSentinelEvent === 'function'",
            lambda ok: self._on_bridge_ready(bool(ok))
        )

    def _on_bridge_ready(self, ok: bool):
        self._bridge_ready = ok
        if ok:
            logger.info("[WEB] SentinelBridge ready; flushing pending events.")
            self._flush_pending_web_events()
        else:
            logger.warning("[WEB] SentinelBridge not ready yet.")

    def on_js_event(self, event_data: Dict[str, Any]) -> None:
        """React to events that come from the embedded page."""
        # Get the event type, defaulting to None if it's not present
        event_type = event_data.get('type')

        if event_type == 'print_configuration':
            logger.info("Configuration print requested by web page.")
            
            # 1. Check if the printer is available
            if not self.printer_service:
                logger.error("Print command ignored: printer service is not available.")
                logger.info(f"Current Configuration: {self.config.to_dict()}")
                return

            # 2. Format the config details for printing
            config_lines = [
                f"User ID: {self.config.user_id}",
                f"URL: {self.config.starting_url}",
                f"Heartbeat: {self.config.heartbeat_url}",
                f"Printer Mock: {self.config.printer_mock}",
            ]
            
            # 3. Call the print_ticket method with the formatted data
            success = self.printer_service.print_ticket(
                brand="System Info",
                message="Current Configuration",
                lines=config_lines,
                timestamp=datetime.now(UTC)
            )
            
            if success:
                logger.info("Configuration receipt sent to printer successfully.")
            else:
                logger.error("Failed to send configuration receipt to printer.")
            
            return # End the function here

        # --- ADD THIS NEW EVENT HANDLER ---
        elif event_type == 'print_receipt':
            logger.info("Received 'print_receipt' event from web page.")
            
            if not self.printer_service:
                logger.error("Print command ignored: printer service is not available.")
                return

            payload = event_data.get('payload', {})
            
            # Extract the correct barcode key ('ean_code')
            barcode_to_print = payload.get('ean_code')

            # Pass the complex 'lines' array directly to the new printer service
            success = self.printer_service.print_ticket(
                brand=payload.get('pos_headline', self.config.brand_name),
                message=payload.get('pos_marketing_message', ''),
                lines=payload.get('lines', []),
                barcode=barcode_to_print,
                timestamp=datetime.now(UTC),
                logo=self.config.logo_path,
                # Note: 'amount' wasn't in your sample JSON, so it will be None
                amount=payload.get('amount') 
            )

            if success:
                logger.info("Receipt sent to printer successfully.")
            else:
                logger.error("Failed to send receipt to printer.")          

        elif event_type == 'close_application':
            payload = event_data.get('payload', {})
            reason = payload.get('reason', 'No reason given.')
            
            logger.warning(f"Received logout request from web page. Reason: {reason}")
            
            # Here you would trigger your application's logout logic.
            # For example, you could close the application:
            print("LOGOUT REQUESTED! Shutting down.")
            QApplication.instance().quit()

    def _on_bill_credit(self, value_minor: int, channel: int):
        """Forward CREDIT events to the embedded web app via dispatchSentinelEvent."""
        logger.info(f"[NV9][CREDIT] value={value_minor} channel={channel}")
        value_major = value_minor / 100.0
        payload = {
            "value_minor": int(value_minor),
            "value_major": value_major,     # convenience for JS
            "channel": int(channel),
            "ts": int(datetime.now(UTC).timestamp() * 1000)  # ms epoch
        }

        logger.info(f"[NV9][CREDIT→WEB] value_minor={value_minor} (≈€{value_major:.2f}), ch={channel}")
        self._send_to_web("nv9_credit", payload)

    def _send_to_web(self, event_name: str, payload: dict):
        js = (
            "window.receiveSentinelEvent && "
            f"window.receiveSentinelEvent({json.dumps(event_name)}, {json.dumps(payload)});"
        )
        if self._bridge_ready:
            self.web_view.page().runJavaScript(js)
        else:
            self._pending_web_events.append((event_name, payload))

    def _flush_pending_web_events(self):
        if not self._bridge_ready:
            return
        for name, payload in self._pending_web_events:
            self._send_to_web(name, payload)
        self._pending_web_events.clear()

    def _on_bridge_injected(self, _=None):
        # poll up to ~2s for receiveSentinelEvent to exist
        self._poll_bridge_ready(tries=20)

    def _poll_bridge_ready(self, tries: int):
        if tries <= 0:
            self._on_bridge_ready(False)
            return
        self.web_view.page().runJavaScript(
            "typeof window.receiveSentinelEvent === 'function'",
            lambda ok: self._bridge_ready_callback(bool(ok), tries)
        )

    def _bridge_ready_callback(self, ok: bool, tries_left: int):
        if ok:
            self._on_bridge_ready(True)
        else:
            QTimer.singleShot(100, lambda: self._poll_bridge_ready(tries_left - 1))

    def closeEvent(self, e):
        # stop NV9 worker cleanly
        try:
            if hasattr(self, "_nv9_worker") and self._nv9_worker is not None:
                self._nv9_worker.stop()
            if hasattr(self, "_nv9_thread") and self._nv9_thread is not None:
                self._nv9_thread.quit()
                self._nv9_thread.wait()
        except Exception as ex:
            logger.warning(f"Error during NV9 shutdown: {ex}")

        # stop G13 worker cleanly
        try:
            if hasattr(self, "_g13_worker") and self._g13_worker is not None:
                self._g13_worker.stop()
            if hasattr(self, "_g13_thread") and self._g13_thread is not None:
                self._g13_thread.quit()
                self._g13_thread.wait()
        except Exception as ex:
            logger.warning(f"Error during G13 shutdown: {ex}")

        super().closeEvent(e)


class PageEventBridge(QObject):
    """Expose slots that JavaScript can call through the Qt WebChannel."""

    eventReceived = Signal(dict)

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)

    @Slot(str)
    def handleEvent(self, payload: str) -> None:
        try:
            event_data = json.loads(payload)
        except json.JSONDecodeError:
            logger.warning(f"Received non-JSON payload from web page: {payload}")
            return

        self.eventReceived.emit(event_data)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--fullscreen", action="store_true",
                        help="Start the app in fullscreen")
    args = parser.parse_args()

    config = KioskConfig()
    os.environ["QTWEBENGINE_REMOTE_DEBUGGING"] = "8080"

    app = QApplication(sys.argv)
    window = MainWindow(config=config)

    if args.fullscreen:
        window.showFullScreen()
    else:
        window.show()

    sys.exit(app.exec())