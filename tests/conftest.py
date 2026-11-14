"""
Preparación de las pruebas: todo aislado.

- No se lee el .env real: app/database.py hace load_dotenv(override=True) al importarse,
  así que lo anulamos ANTES de importar la app y usamos variables de entorno de prueba.
- No se toca logs/security.log: app/core/telemetry.py abre ese archivo al importarse,
  así que lo reemplazamos por un logger que guarda los eventos en memoria.
- No se toca turnero_dev.db: cada prueba usa su propia base SQLite en memoria.
- No se abren sockets: el cartero es una función de prueba y socket.socket queda bloqueado.
"""
import logging
import os
import socket
import sys
import types
from datetime import datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 1) Variables de entorno de prueba (antes de importar la app)
os.environ.update({
    "WHATSAPP_ENABLED": "true",
    "WA_NUMBER": "5490000000000",
    "WA_SOCKET": "/nonexistent/wa-test.sock",
    "WA_API_TOKEN": "token-de-prueba",
    "WA_BUSINESS_NAME": "Barbería de prueba",
    "WA_TIMEZONE": "America/Argentina/Buenos_Aires",
    "WA_CANCEL_ENABLED": "true",
    "DATABASE_URL": "sqlite://",
})

# 2) Que app/database.py no lea el .env real
import dotenv  # noqa: E402

dotenv.load_dotenv = lambda *args, **kwargs: False

# 3) Logger en memoria en lugar de app/core/telemetry.py (que abre logs/security.log)
import app.core  # noqa: E402

_telemetry = types.ModuleType("app.core.telemetry")
_telemetry.sec_logger = logging.getLogger("tests.security_telemetry")
_telemetry.sec_logger.setLevel(logging.INFO)
_telemetry.sec_logger.propagate = False
sys.modules["app.core.telemetry"] = _telemetry
app.core.telemetry = _telemetry

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

import app.models as models  # noqa: E402
import app.whatsapp as wa  # noqa: E402

# Archivos reales que las pruebas no pueden tocar
_PROTECTED = [ROOT / ".env", ROOT / "turnero_dev.db", ROOT / "logs" / "security.log"]


def _snapshot():
    return {p: (p.stat().st_mtime_ns, p.stat().st_size) if p.exists() else None for p in _PROTECTED}


@pytest.fixture(scope="session", autouse=True)
def archivos_reales_intactos():
    before = _snapshot()
    yield
    assert _snapshot() == before, "Las pruebas modificaron .env, turnero_dev.db o logs/security.log"


@pytest.fixture(autouse=True)
def sin_sockets(monkeypatch):
    def _bloqueado(*args, **kwargs):
        raise AssertionError("Las pruebas no pueden abrir sockets")
    monkeypatch.setattr(socket, "socket", _bloqueado)


@pytest.fixture
def Session(monkeypatch):
    """Base SQLite en memoria, compartida por todas las sesiones de la prueba."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    assert str(engine.url) == "sqlite://"
    models.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    monkeypatch.setattr(wa, "SessionLocal", factory)
    yield factory
    engine.dispose()


class Clock:
    def __init__(self):
        self.now = datetime(2026, 11, 14, 12, 0, 0)   # UTC sin zona (09:00 en Buenos Aires)

    def advance(self, seconds: float):
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(wa, "utcnow", lambda: c.now)
    return c


class FakeCartero:
    """Imita GET /inbox, POST /inbox/ack y POST /send, sin sockets."""

    def __init__(self):
        self.inbox = []
        self.acks = []          # cada ack recibido (lista de ids), incluidos los que fallaron
        self.sent = []          # cuerpos de POST /send
        self.fail_acks = 0      # cuántos acks siguientes fallan (status 0)

    def add(self, msg_id: str, jid: str, text: str):
        self.inbox.append({"id": msg_id, "jid": jid, "text": text, "ts": 0})

    def __call__(self, method, path, body=None, timeout=0):
        if (method, path) == ("GET", "/inbox"):
            return 200, {"items": [dict(i) for i in self.inbox]}
        if (method, path) == ("POST", "/inbox/ack"):
            self.acks.append(list(body["ids"]))
            if self.fail_acks:
                self.fail_acks -= 1
                return 0, {"error": "sin respuesta"}
            self.inbox = [i for i in self.inbox if i["id"] not in body["ids"]]
            return 200, {"ok": True}
        if (method, path) == ("POST", "/send"):
            self.sent.append(dict(body))
            return 200, {"ok": True}
        return 404, {"error": "not found"}


@pytest.fixture
def cartero(monkeypatch):
    c = FakeCartero()
    monkeypatch.setattr(wa, "wa_request", c)
    return c


class _ListHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.events = []

    def emit(self, record):
        self.events.append({"event_type": getattr(record, "event_type", None),
                            "message": record.getMessage(),
                            "metadata": getattr(record, "metadata", {})})


@pytest.fixture
def events():
    handler = _ListHandler()
    _telemetry.sec_logger.addHandler(handler)
    yield handler.events
    _telemetry.sec_logger.removeHandler(handler)


@pytest.fixture
def dispatcher():
    return wa.Dispatcher()
