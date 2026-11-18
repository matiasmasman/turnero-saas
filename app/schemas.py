from pydantic import BaseModel, ConfigDict, Field, model_validator, field_validator
from app import catalog
from app.models import UserRole
from typing import Annotated, Optional, List
from datetime import time, date as dt_date
import re

# Ids de la base: enteros estrictos ("3" como texto, 3.0 o true no valen)
EntityId = Annotated[int, Field(strict=True, ge=1, le=2_147_483_647)]
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def parse_iso_date(value: str) -> dt_date:
    """Fecha real con formato AAAA-MM-DD (ej. 2026-11-20). "2026-02-30" o "20261120" no valen."""
    if not isinstance(value, str) or not DATE_RE.fullmatch(value):
        raise ValueError("La fecha no es válida: tiene que ser AAAA-MM-DD.")
    try:
        return dt_date.fromisoformat(value)
    except ValueError:
        raise ValueError("La fecha no es válida: ese día no existe.") from None


class UserResponse(BaseModel):
    id: int
    name: str
    username: str
    role: UserRole
    is_active: bool
    duration_corte: int = 30
    duration_barba: int = 15
    duration_corte_barba: int = 45
    price_corte: int = 15000
    price_corte_promo2x: int = 12000
    price_corte_vip: int = 10000
    price_barba: int = 10000
    price_corte_barba: int = 18000

    class Config:
        from_attributes = True

class Token(BaseModel):
    access_token: str
    token_type: str

class AppointmentCreate(BaseModel):
    """Reserva de la web pública (3e). El navegador dice QUÉ quiere (service_id y, si quiere, discount_id);
    el precio y la duración los pone el servidor. extra="forbid": mandar price, duration, service... da 422."""
    model_config = ConfigDict(extra="forbid")

    client_name: str
    client_phone: str
    service_id: EntityId
    discount_id: Optional[EntityId] = None
    date: str
    time: str
    employee_id: Optional[EntityId] = None

    @model_validator(mode="before")
    @classmethod
    def _old_page(cls, data):
        # Un navegador con el index.js de antes de 3e manda "service" por nombre: que recargue la página
        if isinstance(data, dict) and "service" in data and "service_id" not in data:
            raise ValueError("La página de reservas se actualizó. Recargala para reservar tu turno.")
        return data

    @field_validator("client_name")
    @classmethod
    def validate_client_name(cls, value: str) -> str:
        name = " ".join((value or "").split())  # saca espacios de más
        if not 2 <= len(name) <= 40:
            raise ValueError("El nombre tiene que tener entre 2 y 40 caracteres.")
        if not re.fullmatch(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ' .-]+", name):
            raise ValueError("El nombre solo puede tener letras, espacios, puntos, guiones y apóstrofes.")
        return name

    @field_validator("client_phone")
    @classmethod
    def validate_client_phone(cls, value: str) -> str:
        digits = re.sub(r"[\s\-()]", "", value or "")
        if not re.fullmatch(r"\d{10}", digits):
            raise ValueError("El teléfono tiene que tener 10 números, con característica y sin 0 ni 15 (ej: 2611234567).")
        return digits

    @field_validator("date")
    @classmethod
    def validate_date(cls, value: str) -> str:
        return parse_iso_date(value).isoformat()

    @field_validator("time")
    @classmethod
    def validate_time_format(cls, value: str) -> str:
        if not value or value.strip() == "":
            raise ValueError("El horario no puede estar vacío.")
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d(?::00)?", value):
            raise ValueError("El formato de hora es inválido. Debe ser HH:MM.")
        if int(value[3:5]) % 5:                    # decisión E3 del 3e: 10:05 sí, 10:07 no
            raise ValueError("El horario tiene que ser en punto o cada 5 minutos (por ejemplo, 10:05).")
        return value[:5]

class AppointmentResponse(BaseModel):
    """Turno para el panel (agenda). La web pública usa PublicAppointmentOut (menos datos)."""
    id: int
    client_name: str
    client_phone: str
    service: str
    date: str
    time: str
    duration: int = 30
    price: int = 15000                    # precio final congelado al reservar (con el descuento, si tuvo)
    discount_name: Optional[str] = None   # nombre del descuento al reservar; None en los turnos sin descuento
    status: str
    employee_id: int
    employee_name: Optional[str] = None
    wa_link: Optional[str] = None  # link del botón de WhatsApp (None si está apagado)

    class Config:
        from_attributes = True


# ---------- Web pública de reservas (3e): solo lo que la página necesita ----------
class PublicDiscountOut(BaseModel):
    id: int
    name: str
    price: int                    # precio final con este descuento


class PublicServiceOut(BaseModel):
    id: int
    name: str
    price: int                    # precio de lista
    barber_ids: List[int]         # quiénes lo hacen (para armar la lista de barberos)
    discounts: List[PublicDiscountOut]


class PublicBarberOut(BaseModel):
    id: int
    name: str


class PublicCatalogOut(BaseModel):
    services: List[PublicServiceOut]
    barbers: List[PublicBarberOut]
    days_ahead: int               # hasta cuántos días adelante se puede reservar


class PublicAppointmentOut(BaseModel):
    """Respuesta de POST /appointments/: lo que muestra la confirmación. Sin datos del cliente ni ids internos."""
    employee_name: str
    service: str
    discount_name: Optional[str] = None
    date: str
    time: str                     # HH:MM
    price: int                    # precio final
    wa_link: Optional[str] = None

# ==========================================
# ESQUEMAS DE MÉTRICAS Y FACTURACIÓN
# ==========================================

class ChartPointItem(BaseModel):
    label: str
    sublabel: Optional[str] = None
    real_revenue: int
    pending_revenue: int
    total_revenue: int
    appointments: int

class MetricsResponse(BaseModel):
    period: str
    start_date: str
    end_date: str
    filter_employee_id: Optional[int] = None
    total_appointments: int
    completed_appointments: int
    pending_appointments: int
    real_revenue: int
    pending_revenue: int
    total_revenue: int
    chart_series: List[ChartPointItem] = []

class WorkingHoursBase(BaseModel):
    day_of_week: int
    is_working: bool
    shift1_start: Optional[time] = None
    shift1_end: Optional[time] = None
    shift2_start: Optional[time] = None
    shift2_end: Optional[time] = None

class WorkingHoursUpdate(BaseModel):
    schedule: List[WorkingHoursBase]

class WorkingHoursResponse(WorkingHoursBase):
    id: int
    employee_id: int

    class Config:
        from_attributes = True

# ==========================================
# ESQUEMAS DE FERIADOS Y EXCEPCIONES
# ==========================================

class ExceptionCreate(BaseModel):
    date: str = Field(..., min_length=10, max_length=10)
    reason: Optional[str] = "Bloqueo"
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    is_global: bool = False

    @model_validator(mode="after")
    def validate_exception_fields(self):
        if self.start_time is not None and self.start_time.strip() == "":
            self.start_time = None
        if self.end_time is not None and self.end_time.strip() == "":
            self.end_time = None
        if not self.reason or self.reason.strip() == "":
            self.reason = "Bloqueo"

        if not re.match(r"^\d{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])$", self.date):
            raise ValueError("El formato de fecha es inválido. Debe ser YYYY-MM-DD.")
        
        if self.date < catalog.now_local().date().isoformat():   # hoy en hora del negocio (3f)
            raise ValueError("No se pueden programar excepciones en fechas pasadas.")

        if (self.start_time and not self.end_time) or (not self.start_time and self.end_time):
            raise ValueError("Debés especificar tanto 'Desde' como 'Hasta', o dejar ambas vacías para bloquear el día completo.")

        time_regex = r"^(?:[01]\d|2[0-3]):[0-5]\d(?::[0-5]\d)?$"
        if self.start_time and self.end_time:
            if not re.match(time_regex, self.start_time) or not re.match(time_regex, self.end_time):
                raise ValueError("El formato de hora es inválido. Debe ser HH:MM.")
            
            self.start_time = self.start_time[:5]
            self.end_time = self.end_time[:5]

            if self.start_time >= self.end_time:
                raise ValueError("La hora 'Desde' debe ser anterior a la hora 'Hasta'.")

        return self

class ExceptionResponse(BaseModel):
    id: int
    date: str
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    reason: Optional[str] = "Bloqueo"
    employee_id: Optional[int] = None
    employee_name: str
    affected_appointments: int = 0

    class Config:
        from_attributes = True

# ==========================================
# ESQUEMAS DEL CATÁLOGO (Entrega 3b, solo OWNER)
# Entrada: extra="forbid" (nadie puede mandar business_id, id, active...) y enteros estrictos
# (no se aceptan "15000" como texto, 15000.5 ni true).
# ==========================================
from app.catalog import (DURATION_MAX, DURATION_MIN, DURATION_STEP,  # noqa: E402
                         PRICE_MAX, PRICE_MIN, normalize_name)

Price = Annotated[int, Field(strict=True, ge=PRICE_MIN, le=PRICE_MAX)]
Duration = Annotated[int, Field(strict=True, ge=DURATION_MIN, le=DURATION_MAX, multiple_of=DURATION_STEP)]


class _CatalogInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @field_validator("name", check_fields=False)
    @classmethod
    def _valid_name(cls, value: Optional[str]) -> Optional[str]:
        # None solo llega en PATCH ({"name": null}): lo rechaza _PartialUpdate con un 422 claro
        return None if value is None else normalize_name(value)


class _PartialUpdate(_CatalogInput):
    """PATCH: se manda solo lo que cambia; tiene que venir algo, y null no vale."""

    @model_validator(mode="after")
    def _something_and_no_nulls(self):
        if not self.model_fields_set:
            raise ValueError("No mandaste ningún cambio.")
        nulls = [f for f in self.model_fields_set if getattr(self, f) is None]
        if nulls:
            raise ValueError(f"Estos campos no pueden ser null: {', '.join(sorted(nulls))}.")
        return self


class ServiceCreate(_CatalogInput):
    name: str
    price: Price
    base_duration: Duration


class ServiceUpdate(_PartialUpdate):
    name: Optional[str] = None
    price: Optional[Price] = None
    base_duration: Optional[Duration] = None


class ServiceOut(BaseModel):
    id: int
    name: str
    price: int
    base_duration: int
    active: bool

    model_config = ConfigDict(from_attributes=True)


class ServiceListOut(ServiceOut):
    """GET /admin/catalog/services (3d): además, cuántos barberos ACTIVOS lo hacen hoy (para avisar si ninguno)."""
    barbers: int


class ServiceStatusOut(BaseModel):
    service: ServiceOut
    changed: bool                 # False si ya estaba en ese estado
    future_appointments: int      # turnos confirmados desde ahora que lo usan


class DiscountPriceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    service_id: EntityId
    price: Price


def _unique_services(prices: List[DiscountPriceIn]) -> List[DiscountPriceIn]:
    ids = [p.service_id for p in prices]
    if len(ids) != len(set(ids)):
        raise ValueError("Hay un servicio repetido en la lista de precios.")
    return prices


class DiscountCreate(_CatalogInput):
    name: str
    prices: List[DiscountPriceIn] = Field(..., min_length=1, max_length=100)

    @field_validator("prices")
    @classmethod
    def _check_prices(cls, prices):
        return _unique_services(prices)


class DiscountUpdate(_PartialUpdate):
    name: Optional[str] = None
    prices: Optional[List[DiscountPriceIn]] = Field(None, min_length=1, max_length=100)

    @field_validator("prices")
    @classmethod
    def _check_prices(cls, prices):
        return prices if prices is None else _unique_services(prices)


class DiscountPriceOut(BaseModel):
    service_id: int
    service_name: str
    service_active: bool
    price: int


class DiscountOut(BaseModel):
    id: int
    name: str
    active: bool
    prices: List[DiscountPriceOut]


class DiscountStatusOut(BaseModel):
    discount: DiscountOut
    changed: bool
    future_appointments: int


# ==========================================
# ESQUEMAS DE "MIS SERVICIOS" (Entrega 3c, cada barbero lo suyo)
# No hay employee_id ni business_id: siempre es el usuario logueado y su negocio.
# ==========================================
class MyServiceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    service_id: EntityId
    duration: Duration


class MyServicesUpdate(BaseModel):
    """Lista COMPLETA de los servicios activos que hago. Los que no vengan quedan desmarcados
    (se recuerda su duración). Lista vacía = no hago ninguno."""
    model_config = ConfigDict(extra="forbid")
    services: List[MyServiceIn] = Field(..., max_length=100)

    @field_validator("services")
    @classmethod
    def _check_services(cls, services):
        return _unique_services(services)


class MyServiceOut(BaseModel):
    service_id: int
    name: str
    base_duration: int
    does: bool                  # "Lo hago"
    duration: int               # la mía; si nunca lo hice, la duración base del servicio
    future_appointments: int    # mis turnos confirmados desde ahora con este servicio


class MyServicesOut(BaseModel):
    services: List[MyServiceOut]


class MyServicesSaveOut(MyServicesOut):
    changed: bool
    warnings: List[str] = []
