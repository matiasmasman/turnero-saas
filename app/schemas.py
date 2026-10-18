from pydantic import BaseModel, validator, Field, model_validator
from app.models import UserRole
from typing import Optional, List
from datetime import time, date as dt_date
import re

# Schema para devolver datos del usuario sin exponer la clave
class UserResponse(BaseModel):
    id: int
    name: str
    username: str
    role: UserRole
    is_active: bool

    class Config:
        from_attributes = True

# Schema para la respuesta del login
class Token(BaseModel):
    access_token: str
    token_type: str

# Este es el molde de lo que envía el cliente desde la web
class AppointmentCreate(BaseModel):
    client_name: str
    client_phone: str
    service: str
    date: str
    time: str
    employee_id: Optional[int] = None # Si es None, significa "Cualquier barbero"

    # VALIDACIÓN: Bloqueamos horarios vacíos o corruptos antes de que lleguen a la base de datos
    @validator("time")
    def validate_time_format(cls, value):
        if not value or value.strip() == "":
            raise ValueError("El horario no puede estar vacío.")
        
        # Expresión regular: Validamos que el formato sea estrictamente HH:MM o HH:MM:SS
        if not re.match(r"^(?:[01]\d|2[0-3]):[0-5]\d(?::[0-5]\d)?$", value):
             raise ValueError("El formato de hora es inválido. Debe ser HH:MM.")
        return value

# Este es el molde de lo que devolvemos (para confirmar el turno)
class AppointmentResponse(BaseModel):
    id: int
    client_name: str
    client_phone: str
    service: str
    date: str
    time: str
    status: str
    employee_id: int
    employee_name: Optional[str] = None

    class Config:
        from_attributes = True

class EmployeeCreate(BaseModel):
    name: str
    username: str
    password: str

# Esquema para un día individual
class WorkingHoursBase(BaseModel):
    day_of_week: int
    is_working: bool
    shift1_start: Optional[time] = None
    shift1_end: Optional[time] = None
    shift2_start: Optional[time] = None
    shift2_end: Optional[time] = None

# Esquema para cuando el Frontend envía los horarios (puede ser una lista de días)
class WorkingHoursUpdate(BaseModel):
    schedule: List[WorkingHoursBase]

# Esquema para devolver la información al Frontend (incluye el ID)
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
    is_global: bool = False  # True = Toda la Barbería (solo permitido para UserRole.OWNER)

    @model_validator(mode="after")
    def validate_exception_fields(self):
        # 1. Normalizar strings vacíos que vengan del frontend a None
        if self.start_time is not None and self.start_time.strip() == "":
            self.start_time = None
        if self.end_time is not None and self.end_time.strip() == "":
            self.end_time = None
        if not self.reason or self.reason.strip() == "":
            self.reason = "Bloqueo"

        # 2. Validar formato estricto de fecha (YYYY-MM-DD) y que no sea pasada
        if not re.match(r"^\d{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])$", self.date):
            raise ValueError("El formato de fecha es inválido. Debe ser YYYY-MM-DD.")
        
        if self.date < dt_date.today().isoformat():
            raise ValueError("No se pueden programar excepciones en fechas pasadas.")

        # 3. Validar que si se carga una hora, estén ambas presentes
        if (self.start_time and not self.end_time) or (not self.start_time and self.end_time):
            raise ValueError("Debés especificar tanto 'Desde' como 'Hasta', o dejar ambas vacías para bloquear el día completo.")

        # 4. Si hay franja horaria, validar formato HH:MM y coherencia cronológica
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