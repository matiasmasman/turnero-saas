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
    username = Column(String, unique=True, index=True, nullable=False)
    hashed_password = Column(String, nullable=False)
    role = Column(Enum(UserRole), default=UserRole.EMPLOYEE, nullable=False)
    is_active = Column(Boolean, default=True)

    # Duración de servicios por barbero (en minutos)
    duration_corte = Column(Integer, default=30, nullable=False)        # 30 a 45 min
    duration_barba = Column(Integer, default=15, nullable=False)        # 10 a 20 min
    duration_corte_barba = Column(Integer, default=45, nullable=False)  # 40 a 65 min

    # Precios de los servicios y promociones (definidos por el OWNER)
    price_corte = Column(Integer, default=15000, nullable=False)
    price_corte_promo2x = Column(Integer, default=12000, nullable=False)
    price_corte_vip = Column(Integer, default=10000, nullable=False)
    price_barba = Column(Integer, default=10000, nullable=False)
    price_corte_barba = Column(Integer, default=18000, nullable=False)

    appointments = relationship("Appointment", back_populates="employee")
    working_hours = relationship("WorkingHours", back_populates="employee", cascade="all, delete-orphan")

class Appointment(Base):
    __tablename__ = "appointments"

    id = Column(Integer, primary_key=True, index=True)
    client_name = Column(String, index=True, nullable=False)
    client_phone = Column(String, nullable=False)
    service = Column(String, nullable=False)
    date = Column(String, index=True, nullable=False)       # Formato: YYYY-MM-DD
    time = Column(String, nullable=False)                   # Formato: HH:MM:SS
    duration = Column(Integer, nullable=False, default=30)  # Duración congelada al reservar
    price = Column(Integer, nullable=False, default=15000)  # Precio congelado al reservar
    status = Column(String, default="confirmed")
    employee_id = Column(Integer, ForeignKey("users.id"), nullable=False)

    employee = relationship("User", back_populates="appointments")

    @property
    def employee_name(self) -> str:
        if hasattr(self, "_employee_name") and self._employee_name:
            return self._employee_name
        return self.employee.name if self.employee else "Sin asignar"

    @employee_name.setter
    def employee_name(self, value: str):
        self._employee_name = value

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

    employee = relationship("User", back_populates="working_hours")

class ScheduleException(Base):
    __tablename__ = "schedule_exceptions"

    id = Column(Integer, primary_key=True, index=True)
    date = Column(String, index=True, nullable=False)        # Formato: YYYY-MM-DD
    start_time = Column(String, nullable=True)               # Formato: HH:MM (None = Día completo)
    end_time = Column(String, nullable=True)                 # Formato: HH:MM (None = Día completo)
    reason = Column(String, nullable=False)                  # Ej: "Médico", "Feriado", "Vacaciones"
    employee_id = Column(Integer, ForeignKey("users.id"), nullable=True)  # None = Feriado General (Owner)

    employee = relationship("User", backref="exceptions")