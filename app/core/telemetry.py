import json
import logging
from datetime import datetime, timezone
import sys

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

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(SecurityJSONFormatter())
    
    if not logger.handlers:
        logger.addHandler(handler)
        
    return logger

sec_logger = setup_security_logger()