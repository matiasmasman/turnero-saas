import json
import logging
from datetime import datetime, timezone
from pathlib import Path
import sys

BASE_DIR = Path(__file__).resolve().parent.parent.parent
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)
LOG_FILE = LOG_DIR / "security.log"

class SecurityJSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        log_entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "event_type": getattr(record, "event_type", "GENERAL_SYSTEM"),
            "request_id": getattr(record, "request_id", None),
            "ip_address": getattr(record, "ip_address", "UNKNOWN"),
            "actor_id": getattr(record, "actor_id", None),
            "message": record.getMessage(),
            "metadata": getattr(record, "metadata", {})
        }
        return json.dumps(log_entry)

def setup_security_logger() -> logging.Logger:
    logger = logging.getLogger("security_telemetry")
    logger.setLevel(logging.INFO)
    logger.propagate = False

    formatter = SecurityJSONFormatter()

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)

    file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    file_handler.setFormatter(formatter)

    if not logger.handlers:
        logger.addHandler(stream_handler)
        logger.addHandler(file_handler)

    return logger

sec_logger = setup_security_logger()
