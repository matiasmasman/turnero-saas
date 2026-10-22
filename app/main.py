from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from app.core.middleware import SecurityTelemetryMiddleware
from app.core.telemetry import sec_logger
from app.database import engine
import app.models as models
from app.routers import auth, appointments, admin
from fastapi.staticfiles import StaticFiles

# Crea las tablas en la base de datos
models.Base.metadata.create_all(bind=engine)

app = FastAPI(title="Turnero SaaS - Blue Team Edition")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], # En producción acá va el dominio de Donweb, ahora permitimos todo
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.add_middleware(SecurityTelemetryMiddleware)

app.include_router(auth.router)
app.include_router(appointments.router)
app.include_router(admin.router)

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