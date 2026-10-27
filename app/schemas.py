from pydantic import BaseModel, validator, Field, model_validator, field_validator
from app.models import UserRole
from typing import Literal, Optional, List
from datetime import time, date as dt_date
import re

VALID_SERVICES = Literal["Corte", "Barba", "Corte + Barba"]

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
    client_name: str
    client_phone: str
    service: VALID_SERVICES
    date: str
    time: str
    employee_id: Optional[int] = None

    @validator("time")
    def validate_time_format(cls, value):
        if not value or value.strip() == "":
            raise ValueError("El horario no puede estar vacío.")
        if not re.match(r"^(?:[01]\d|2[0-3]):[0-5]\d(?::[0-5]\d)?$", value):
             raise ValueError("El formato de hora es inválido. Debe ser HH:MM.")
        return value

class AppointmentResponse(BaseModel):
    id: int
    client_name: str
    client_phone: str
    service: str
    date: str
    time: str
    duration: int = 30
    price: int = 15000
    status: str
    employee_id: int
    employee_name: Optional[str] = None

    class Config:
        from_attributes = True

class AppointmentPriceTierUpdate(BaseModel):
    tier: Literal["normal", "promo2x", "vip"]

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
# ESQUEMAS DE DURACIÓN Y PRECIOS DE SERVICIOS
# ==========================================

class ServiceDurationsUpdate(BaseModel):
    duration_corte: int = Field(..., ge=30, le=45, description="Corte: 30 a 45 min")
    duration_barba: int = Field(..., ge=10, le=20, description="Barba: 10 a 20 min")
    duration_corte_barba: Optional[int] = None

    @field_validator("duration_corte", "duration_barba")
    @classmethod
    def validate_step_of_five(cls, v: int) -> int:
        if v % 5 != 0:
            raise ValueError("La duración debe ser en intervalos de 5 minutos.")
        return v

    @model_validator(mode="after")
    def compute_combined_duration(self):
        self.duration_corte_barba = self.duration_corte + self.duration_barba
        return self

class ServiceDurationsResponse(BaseModel):
    duration_corte: int
    duration_barba: int
    duration_corte_barba: int

    class Config:
        from_attributes = True

class ServicePricesUpdate(BaseModel):
    price_corte: int = Field(..., ge=0, le=1000000)
    price_corte_promo2x: int = Field(12000, ge=0, le=1000000)
    price_corte_vip: int = Field(10000, ge=0, le=1000000)
    price_barba: int = Field(..., ge=0, le=1000000)
    price_corte_barba: int = Field(..., ge=0, le=1000000)

class ServicePricesResponse(BaseModel):
    price_corte: int
    price_corte_promo2x: int
    price_corte_vip: int
    price_barba: int
    price_corte_barba: int

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
        
        if self.date < dt_date.today().isoformat():
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