"""
De dónde viene un pedido, para los eventos de security.log (Bloque 3, #5).

La IP sale SIEMPRE de request.client.host. En producción uvicorn corre detrás de Nginx con los
encabezados de proxy activados y confía en X-Forwarded-For SOLO si el pedido viene de 127.0.0.1
(--forwarded-allow-ips): uvicorn ya reemplaza request.client por la IP real del visitante.
Por eso acá NO se lee X-Forwarded-For a mano: cualquiera podría mandar ese encabezado inventado.
"""
from typing import Optional

from fastapi import Request

NO_IP = "0.0.0.0"   # pedido sin dirección de cliente (no debería pasar con uvicorn)


def client_ip(request: Request) -> str:
    """La IP del cliente. El middleware la guarda en request.state.client_ip al empezar el pedido;
    si no pasó por el middleware (por ejemplo, una prueba), se toma de request.client."""
    ip = getattr(request.state, "client_ip", None)
    if ip:
        return ip
    return request.client.host if request.client else NO_IP


def log_extra(request: Request, actor_id: Optional[int] = None, event_type: Optional[str] = None,
              metadata: Optional[dict] = None) -> dict:
    """El `extra` de sec_logger con request_id, ip_address y (si hay usuario logueado) actor_id."""
    extra = {
        "request_id": getattr(request.state, "request_id", None),
        "ip_address": client_ip(request),
        "actor_id": actor_id,
    }
    if event_type is not None:
        extra["event_type"] = event_type
    if metadata is not None:
        extra["metadata"] = metadata
    return extra
