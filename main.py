import sys
import os
import json
from typing import Any, Dict, Optional
from PySide6.QtCore import QObject, QUrl, Signal, Slot
from PySide6.QtWidgets import QApplication, QMainWindow
from PySide6.QtWebEngineWidgets import QWebEngineView
# NEW: Import the interceptor base class
from PySide6.QtWebEngineCore import QWebEngineUrlRequestInterceptor
from PySide6.QtWebChannel import QWebChannel

from kiosk_config import KioskConfig

from logger import get_logger

logger = get_logger(__name__)

# (Paste the CustomRequestInterceptor class from above here)
class CustomRequestInterceptor(QWebEngineUrlRequestInterceptor):
    def __init__(self, key, user_id, parent=None):
        super().__init__(parent)
        self.preshared_key = key
        self.user_id = user_id
        logger.info("CustomRequestInterceptor initialized with preshared key and user id.")

    def interceptRequest(self, info):
        # Attach both the Authorization and user id headers to every outbound request.
        info.setHttpHeader(b'Authorization', f'Bearer {self.preshared_key}'.encode('utf-8'))
        info.setHttpHeader(b'X-User-Id', str(self.user_id).encode('utf-8'))

class MainWindow(QMainWindow):
    def __init__(self, config):
        super().__init__()
        self.config = config

        window_title = f"SentinelKiosk - User ID: {self.config.user_id}"
        self.setWindowTitle(window_title)
        self.setGeometry(100, 100, 1280, 720)

        self.web_view = QWebEngineView()
        self.setCentralWidget(self.web_view)

        # Track when the target page finishes loading so we can react to it.
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

        # --- Load the URL ---
        # Now, when this request is made, the interceptor will add the header.
        self.web_view.setUrl(QUrl(self.config.starting_url))
        logger.info(f"Loaded URL: {self.config.starting_url}")

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
            (function() {
                if (window.__sentinelBridgeInitialized) { return; }
                window.__sentinelBridgeInitialized = true;

                function forwardEvent(bridge, type, payload) {
                    try {
                        bridge.handleEvent(JSON.stringify({ type: type, payload: payload }));
                    } catch (error) {
                        console.error('Failed to forward event', type, error);
                    }
                }

                function ensureChannelReady(callback, attempt) {
                    attempt = attempt || 0;

                    if (window.SentinelBridge) {
                        callback(window.SentinelBridge);
                        return;
                    }

                    if (typeof qt !== 'undefined' && qt.webChannelTransport) {
                        new QWebChannel(qt.webChannelTransport, function(channel) {
                            window.SentinelBridge = channel.objects.SentinelBridge;
                            callback(window.SentinelBridge);
                        });
                        return;
                    }

                    if (attempt > 20) {
                        console.warn('SentinelBridge: qt.webChannelTransport never became available.');
                        return;
                    }

                    window.setTimeout(function() {
                        ensureChannelReady(callback, attempt + 1);
                    }, 100);
                }

                function installHandlers(bridge) {
                    window.dispatchSentinelEvent = function(type, data) {
                        forwardEvent(bridge, type, data);
                    };

                    window.addEventListener('message', function(event) {
                        forwardEvent(bridge, 'message', event.data);
                    }, false);

                    document.addEventListener('click', function(event) {
                        forwardEvent(bridge, 'click', {
                            tag: event.target.tagName,
                            id: event.target.id || null,
                            classes: event.target.className || null,
                            timestamp: Date.now()
                        });
                    }, true);
                }

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

        self.web_view.page().runJavaScript(bridge_loader)

    def on_js_event(self, event_data: Dict[str, Any]) -> None:
        """React to events that come from the embedded page."""
        # Get the event type, defaulting to None if it's not present
        event_type = event_data.get('type')

        if event_type == 'print_configuration':
            logger.info(f"Configuration requested by web page: {self.config.to_dict()}")
            # print the config to the console for debugging
            print(f"Current Configuration: {self.config.to_dict()}")
            return

        # --- ADD YOUR LOGIC HERE ---
        if event_type == 'close_application':
            payload = event_data.get('payload', {})
            reason = payload.get('reason', 'No reason given.')
            
            logger.warning(f"Received logout request from web page. Reason: {reason}")
            
            # Here you would trigger your application's logout logic.
            # For example, you could close the application:
            print("LOGOUT REQUESTED! Shutting down.")
            QApplication.instance().quit()
            
        elif event_type == 'another_event_type':
            # Handle other events if needed
            pass

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

        logger.info(f"JavaScript event received: {event_data}")
        self.eventReceived.emit(event_data)

if __name__ == "__main__":
    config = KioskConfig()
    os.environ["QTWEBENGINE_REMOTE_DEBUGGING"] = "8080"
    app = QApplication(sys.argv)
    window = MainWindow(config=config)
    window.show()
    sys.exit(app.exec())
