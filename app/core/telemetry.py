import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
import sys

BASE_DIR = Path(__file__).resolve().parent.parent.parent
LOG_DIR = BASE_DIR / "logs"
# SECURITY_LOG_FILE la usan solo las pruebas de navegador (log en una carpeta temporal).
# Sin la variable, es logs/security.log como siempre.
LOG_FILE = Path(os.getenv("SECURITY_LOG_FILE") or LOG_DIR / "security.log")
LOG_FILE.parent.mkdir(exist_ok=True)

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
