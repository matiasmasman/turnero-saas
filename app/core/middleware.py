import uuid
import time
from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from app.core.telemetry import sec_logger

class SecurityTelemetryMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        request_id = str(uuid.uuid4())
        request.state.request_id = request_id

        client_ip = request.client.host if request.client else "0.0.0.0"
        request.state.client_ip = client_ip

        start_time = time.time()
        
        response = await call_next(request)
        
        duration_ms = round((time.time() - start_time) * 1000, 2)

        response.headers["X-Request-ID"] = request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"

        sec_logger.info(
            f"Access to {request.url.path}",
            extra={
                "event_type": "HTTP_ACCESS",
                "request_id": request_id,
                "ip_address": client_ip,
                "metadata": {
                    "method": request.method,
                    "status_code": response.status_code,
                    "duration_ms": duration_ms
                }
            }
        )

        return response