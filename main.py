import sys
import os
from PySide6.QtCore import QUrl
from PySide6.QtWidgets import QApplication, QMainWindow
from PySide6.QtWebEngineWidgets import QWebEngineView
# NEW: Import the interceptor base class
from PySide6.QtWebEngineCore import QWebEngineUrlRequestInterceptor

from config_manager import ConfigManager

from logger import get_logger

logger = get_logger(__name__)

# (Paste the CustomRequestInterceptor class from above here)
class CustomRequestInterceptor(QWebEngineUrlRequestInterceptor):
    def __init__(self, key, parent=None):
        super().__init__(parent)
        self.preshared_key = key
        logger.info("CustomRequestInterceptor initialized with preshared key.")

    def interceptRequest(self, info):
        header_name = b'Authorization'
        header_value = f'Bearer {self.preshared_key}'.encode('utf-8')
        info.setHttpHeader(header_name, header_value)

class MainWindow(QMainWindow):
    def __init__(self, config):
        super().__init__()
        self.config = config

        window_title = f"SentinelKiosk - ID: {self.config.kiosk_id}"
        self.setWindowTitle(window_title)
        self.setGeometry(100, 100, 1280, 720)

        self.web_view = QWebEngineView()
        self.setCentralWidget(self.web_view)

        # --- 🚀 Set up the Interceptor ---
        # 1. Create an instance of our interceptor with the key
        self.interceptor = CustomRequestInterceptor(self.config.preshared_key)

        # 2. Install it on the web engine's profile. This affects all requests.
        self.profile.setUrlRequestInterceptor(self.interceptor)
        
        # --- Load the URL ---
        # Now, when this request is made, the interceptor will add the header.
        self.web_view.setUrl(QUrl(self.config.starting_url))
        logger.info(f"Loaded URL: {self.config.starting_url}")

if __name__ == "__main__":
    config = ConfigManager()
    os.environ["QTWEBENGINE_REMOTE_DEBUGGING"] = "8080"
    app = QApplication(sys.argv)
    window = MainWindow(config=config)
    window.show()
    sys.exit(app.exec())
