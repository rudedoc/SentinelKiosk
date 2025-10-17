# Python ENV

`python3 -m venv .venv`
`Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser`
`.venv\Scripts\activate`


# Context Dump

`cat config_manager.py config.json main.py logger.py requirements.txt`

# pyinstaller

pyinstaller ^
  --noconfirm ^
  --clean ^
  --name SentinelKiosk ^
  --windowed ^
  --add-data "config.json;." ^
  --add-data "logo.bmp;." ^
  --add-data "splash.png;." ^
  --add-binary "libusb/MinGW64/dll/libusb-1.0.dll;." ^
  --hidden-import PySide6.QtWebEngineCore ^
  --hidden-import PySide6.QtWebEngineWidgets ^
  --hidden-import PySide6.QtWebChannel ^
  --hidden-import PySide6.QtNetwork ^
  main.py