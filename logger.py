import logging
import logging.handlers
import os
import json
from typing import Any, Dict
import sys

# Define log file path (in project root)
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'app.log')

# Set maxBytes and backupCount as needed (e.g., 5MB and 3 backups)
MAX_BYTES = 10 * 1024 * 1024  # 5 MB
BACKUP_COUNT = 3

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    handlers=[
        logging.handlers.RotatingFileHandler(
            LOG_FILE, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding='utf-8'
        ),
        logging.StreamHandler()
    ]
)

logging.getLogger("apscheduler").setLevel(logging.WARNING)

def purge_log() -> None:
    """Truncate the log file."""
    with open(LOG_FILE, "w", encoding="utf-8") as f:
        pass  # Truncate the file

def get_logger(name: str) -> logging.Logger:
    """Get a logger with the specified name."""
    return logging.getLogger(name)


def log_json(logger: logging.Logger, level: int, payload: Dict[str, Any]) -> None:
    """
    Helper to emit one *single‑line* JSON object at the chosen log level.
    """
    logger.log(level, json.dumps(payload, separators=(",", ":")))