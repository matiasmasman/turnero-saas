import enum
from datetime import datetime
from sqlalchemy import Column, Integer, String, Boolean, Enum, ForeignKey, DateTime, Time
from sqlalchemy.orm import relationship
from app.database import Base

# Definimos los dos roles posibles
class UserRole(str, enum.Enum):
    OWNER = "owner"
    EMPLOYEE = "employee"

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    # Reemplazamos 'email' por 'username'
    username = Column(String, unique=True, index=True, nullable=False)
    hashed_password = Column(String, nullable=False)
    role = Column(Enum(UserRole), default=UserRole.EMPLOYEE, nullable=False)
    is_active = Column(Boolean, default=True)

    appointments = relationship("Appointment", back_populates="employee")

class Appointment(Base):
    __tablename__ = "appointments"

    id = Column(Integer, primary_key=True, index=True)
    client_name = Column(String, nullable=False)
    client_phone = Column(String, nullable=False) # Agregamos el WhatsApp
    service = Column(String, nullable=False)      # Agregamos el Servicio (Corte, Barba, etc.)
    date = Column(String, nullable=False)         # Guardamos la fecha (ej: "2026-10-12")
    time = Column(String, nullable=False)         # Guardamos la hora (ej: "17:30")
    
    # El status nos sirve por si el dueño cancela un turno después
    status = Column(String, default="confirmed")  
    
    employee_id = Column(Integer, ForeignKey("users.id"))
    employee = relationship("User", back_populates="appointments")

class WorkingHours(Base):
    __tablename__ = "working_hours"

    id = Column(Integer, primary_key=True, index=True)
    employee_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"))
    
    # 0 = Lunes, 1 = Martes, 2 = Miércoles, 3 = Jueves, 4 = Viernes, 5 = Sábado, 6 = Domingo
    day_of_week = Column(Integer) 
    is_working = Column(Boolean, default=True)
    
    # Turno 1 (Mañana)
    shift1_start = Column(Time, nullable=True)
    shift1_end = Column(Time, nullable=True)
    
    # Turno 2 (Tarde) - Permite nulos si trabaja de corrido o medio día
    shift2_start = Column(Time, nullable=True)
    shift2_end = Column(Time, nullable=True)

class ScheduleException(Base):
    __tablename__ = "schedule_exceptions"

    id = Column(Integer, primary_key=True, index=True)
    date = Column(String, index=True, nullable=False)        # Formato: YYYY-MM-DD
    start_time = Column(String, nullable=True)               # Formato: HH:MM (None = Día completo)
    end_time = Column(String, nullable=True)                 # Formato: HH:MM (None = Día completo)
    reason = Column(String, nullable=False)                  # Ej: "Médico", "Feriado", "Vacaciones"
    employee_id = Column(Integer, ForeignKey("users.id"), nullable=True)  # None = Feriado General (Owner)

    employee = relationship("User", backref="exceptions")