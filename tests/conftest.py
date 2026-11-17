"""
Preparación de las pruebas: todo aislado.

- No se lee el .env real: app/database.py hace load_dotenv(override=True) al importarse,
  así que lo anulamos ANTES de importar la app y usamos variables de entorno de prueba.
- No se toca logs/security.log: app/core/telemetry.py abre ese archivo al importarse,
  así que lo reemplazamos por un logger que guarda los eventos en memoria.
- No se toca turnero_dev.db: cada prueba usa su propia base SQLite en memoria (y, si existe la variable
  E3_TEST_PG_URL, las pruebas de API corren además en un schema propio de ese PostgreSQL de prueba).
- No se abren sockets: el cartero es una función de prueba y socket.socket queda bloqueado.
"""
import logging
import os
import socket
import sys
import types
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
_REAL_SOCKET = socket.socket   # el original, antes de que `sin_sockets` lo bloquee

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

from sqlalchemy import create_engine, text  # noqa: E402
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
                            "actor_id": getattr(record, "actor_id", None),
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


# ---------- API (TestClient) — Entrega 3b; variante PostgreSQL en 3e ----------
PG_URL = os.getenv("E3_TEST_PG_URL")   # ej. postgresql+psycopg2://usuario@/postgres?host=/ruta/socket&port=5499


def _sqlite_engine():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    return engine, engine.dispose


def _postgres_engine():
    """Un schema nuevo (t_xxxxxxxxxx) en el PostgreSQL de prueba; se borra al terminar la prueba.
    lock_timeout: si una prueba dejara una transacción abierta, el DROP falla en vez de colgarse."""
    name = f"t_{uuid.uuid4().hex[:10]}"
    admin = create_engine(PG_URL)
    with admin.begin() as conn:
        conn.execute(text(f"CREATE SCHEMA {name}"))
    sep = "&" if "?" in PG_URL else "?"
    engine = create_engine(f"{PG_URL}{sep}options=-csearch_path%3D{name}")

    def cleanup():
        engine.dispose()
        with admin.begin() as conn:
            conn.execute(text("SET LOCAL lock_timeout = '10s'"))
            conn.execute(text(f"DROP SCHEMA {name} CASCADE"))
        admin.dispose()
    return engine, cleanup


@pytest.fixture(params=["sqlite", "postgres"] if PG_URL else ["sqlite"])
def api(request, monkeypatch):
    """La app de FastAPI lista para TestClient, sobre una base ya "migrada": SQLite en memoria y, si existe
    E3_TEST_PG_URL, también PostgreSQL (cada prueba de API corre una vez en cada base).

    - app.main tiene la protección de arranque (exige las migraciones): la base de prueba se arma
      con create_all + el negocio 1 + las filas de schema_migrations, y se le pasa a app.main
      reemplazando app.database.engine ANTES de importarlo (app.main se importa de cero).
    - SECRET_KEY de prueba: app.core.security la exige al importarse.
    - WhatsApp apagado: el TestClient se usa SIN "with", así no corre el lifespan (que arranca el
      dispatcher); además dispatcher.start queda anulado por las dudas.
    - get_db se reemplaza por sesiones de esta base (lo usan los endpoints y get_current_user).
    - api.dialect dice en qué base corre ("sqlite" o "postgresql").
    """
    import importlib
    from fastapi.testclient import TestClient
    import app.database
    from app.core.schema import REQUIRED_MIGRATIONS

    engine, cleanup = _postgres_engine() if request.param == "postgres" else _sqlite_engine()
    models.Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(models.Business.__table__.insert().values(id=1, slug="mensclub", name="Men's Club"))
        for migration_id in REQUIRED_MIGRATIONS:
            conn.execute(models.SchemaMigration.__table__.insert().values(id=migration_id))
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)

    monkeypatch.setenv("SECRET_KEY", "k" * 40)
    monkeypatch.setattr(app.database, "engine", engine)
    monkeypatch.setattr(wa.dispatcher, "start", lambda: None)

    # TestClient usa un event loop de asyncio, que crea un socketpair local (sin red) para despertarse.
    # Solo se permite "envolver" un descriptor que ya existe (lo que hace socketpair); crear un socket
    # nuevo, que es lo único que podría conectarse a algo, sigue prohibido como en `sin_sockets`.
    def _solo_socketpair(family=-1, type=-1, proto=-1, fileno=None):
        if fileno is None:
            raise AssertionError("Las pruebas no pueden abrir sockets")
        return _REAL_SOCKET(family, type, proto, fileno)
    monkeypatch.setattr(socket, "socket", _solo_socketpair)
    monkeypatch.chdir(ROOT)                      # StaticFiles("frontend") es relativo
    sys.modules.pop("app.main", None)
    main = importlib.import_module("app.main")

    def _db():
        db = factory()
        try:
            yield db
        finally:
            db.close()

    main.app.dependency_overrides[app.database.get_db] = _db
    client = TestClient(main.app)                # sin "with": no corre el lifespan
    try:
        yield types.SimpleNamespace(client=client, Session=factory, engine=engine, dialect=engine.dialect.name)
    finally:
        main.app.dependency_overrides.clear()
        sys.modules.pop("app.main", None)
        cleanup()
