from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from app.core.middleware import SecurityTelemetryMiddleware
from app.core.schema import ensure_schema_ready
from app.core.telemetry import sec_logger
from app.database import engine
from app.routers import auth, appointments, admin, catalog, my_services
from app.whatsapp import dispatcher
from fastapi.staticfiles import StaticFiles

# La app ya no crea tablas: si falta una migración, no arranca (igual que sin SECRET_KEY).
# Las tablas se crean con manage_staff.py init (base nueva) y scripts/migrate_e3.py.
ensure_schema_ready(engine)


@asynccontextmanager
async def lifespan(app: FastAPI):
    dispatcher.start()   # hilo que manda confirmaciones y recordatorios (si WHATSAPP_ENABLED=true)
    yield
    dispatcher.stop()


app = FastAPI(title="Turnero SaaS - Blue Team Edition", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # En producción acá va el dominio de Donweb, ahora permitimos todo
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.add_middleware(SecurityTelemetryMiddleware)

app.include_router(auth.router)
app.include_router(appointments.router)
app.include_router(admin.router)
app.include_router(catalog.router)
app.include_router(my_services.router)


@app.get("/health")
def read_root():
    return {"message": "El sistema está arriba y siendo monitoreado"}


@app.get("/test-alert")
def trigger_alert(request: Request):
    sec_logger.warning(
        "Intento de acceso a ruta de prueba de seguridad",
        extra={
            "event_type": "SECURITY_PROBE",
            "request_id": request.state.request_id,
            "ip_address": request.state.client_ip,
            "metadata": {"endpoint": "/test-alert"}
        }
    )
    return {"message": "Alerta registrada en los logs"}


app.mount("/", StaticFiles(directory="frontend", html=True), name="frontend")
