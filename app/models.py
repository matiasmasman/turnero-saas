import enum
from datetime import datetime, timezone
from sqlalchemy import (Column, Integer, String, Boolean, Enum, ForeignKey, DateTime, Time, Text,
                        UniqueConstraint, CheckConstraint, ForeignKeyConstraint, Index, func, true)
from sqlalchemy.orm import relationship
from app.database import Base


def utc_now() -> datetime:
    """Momento actual en UTC, con zona (para las tablas nuevas de la Entrega 3)."""
    return datetime.now(timezone.utc)

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

    appointments = relationship("Appointment", back_populates="employee", foreign_keys="Appointment.employee_id")
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
    status = Column(String, default="confirmed")            # confirmed | cancelled (nunca se borra un turno)
    employee_id = Column(Integer, ForeignKey("users.id"), nullable=False)

    # Entrega 3 (3a): se agregan nulas; en 3a nadie las escribe salvo la migración.
    # service (texto), price y duration quedan como están: son el "congelado" del turno.
    service_id = Column(Integer, ForeignKey("services.id", name="fk_appointments_service_id",
                                            ondelete="RESTRICT"), nullable=True)
    discount_id = Column(Integer, ForeignKey("discounts.id", name="fk_appointments_discount_id",
                                             ondelete="RESTRICT"), nullable=True)
    discount_name = Column(String(40), nullable=True)       # nombre del descuento al reservar

    # Bloque 4 (migración b4_cancelled_by): quién y cuándo canceló.
    # cancelled_by_id: el usuario del panel que lo canceló; NULL en un cancelado = canceló el cliente por WhatsApp.
    cancelled_by_id = Column(Integer, ForeignKey("users.id", name="fk_appointments_cancelled_by_id",
                                                 ondelete="RESTRICT"), nullable=True)
    cancelled_at = Column(DateTime(timezone=True), nullable=True)   # UTC; NULL en los cancelados de antes del Bloque 4

    employee = relationship("User", back_populates="appointments", foreign_keys=[employee_id])
    cancelled_by = relationship("User", foreign_keys=[cancelled_by_id])

    @property
    def employee_name(self) -> str:
        if hasattr(self, "_employee_name") and self._employee_name:
            return self._employee_name
        return self.employee.name if self.employee else "Sin asignar"

    @employee_name.setter
    def employee_name(self, value: str):
        self._employee_name = value

    @property
    def cancelled_by_name(self):
        """Nombre del usuario del panel que canceló; None si no está cancelado o si canceló el cliente por WhatsApp."""
        return self.cancelled_by.name if self.status == "cancelled" and self.cancelled_by else None

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


# ==========================================
# WHATSAPP (v2.1) — tablas nuevas, no tocan las existentes
# ==========================================


class WaOptin(Base):
    """Código de cada turno para el botón de WhatsApp, y el chat que lo usó."""
    __tablename__ = "wa_optins"

    appointment_id = Column(Integer, ForeignKey("appointments.id", ondelete="CASCADE"), primary_key=True)
    code = Column(String(6), unique=True, index=True, nullable=False)
    jid = Column(String, nullable=True)                  # chat de WhatsApp que mandó el código
    opted_in_at = Column(DateTime, nullable=True)        # UTC
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class WaNotification(Base):
    """Cola de avisos: confirmación, recordatorio y respuestas automáticas."""
    __tablename__ = "wa_notifications"
    __table_args__ = (UniqueConstraint("appointment_id", "kind", name="uq_wa_notif_appt_kind"),)

    id = Column(Integer, primary_key=True, index=True)
    appointment_id = Column(Integer, ForeignKey("appointments.id", ondelete="CASCADE"), nullable=True)
    kind = Column(String, nullable=False)                # confirmation | reminder | autoreply | invalid_code | already_confirmed | cancel_*
    to_jid = Column(String, nullable=False)
    body = Column(Text, nullable=True)                   # texto ya armado (mensajes de cancelación); si es None se arma al enviar
    scheduled_at = Column(DateTime, index=True, nullable=False)   # UTC
    status = Column(String, default="pending", index=True, nullable=False)  # pending|sending|sent|failed|cancelled
    attempts = Column(Integer, default=0, nullable=False)
    last_error = Column(String, nullable=True)
    sent_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


# ==========================================
# ENTREGA 3 — catálogo de servicios y descuentos (3a: solo datos)
# Servicios y descuentos nunca se borran: se desactivan (active = false).
# ==========================================


class Business(Base):
    """El negocio (hoy uno solo: id 1, 'mensclub')."""
    __tablename__ = "businesses"

    id = Column(Integer, primary_key=True)
    slug = Column(String(40), nullable=False, unique=True)
    name = Column(String(80), nullable=False)
    active = Column(Boolean, nullable=False, default=True, server_default=true())
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now, server_default=func.now())


class Service(Base):
    """Un servicio del catálogo: nombre, precio de lista y duración sugerida."""
    __tablename__ = "services"
    __table_args__ = (
        # Permite que otras tablas exijan "servicio de ESTE negocio" con una FK de dos columnas
        UniqueConstraint("business_id", "id", name="uq_services_business_id_id"),
        CheckConstraint("price >= 0", name="ck_services_price"),
        CheckConstraint("base_duration > 0", name="ck_services_base_duration"),
    )

    id = Column(Integer, primary_key=True)
    business_id = Column(Integer, ForeignKey("businesses.id", name="fk_services_business_id",
                                             ondelete="RESTRICT"), nullable=False)
    name = Column(String(40), nullable=False)
    price = Column(Integer, nullable=False)              # pesos, entero
    base_duration = Column(Integer, nullable=False)      # minutos
    active = Column(Boolean, nullable=False, default=True, server_default=true())
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now,
                        server_default=func.now())


class Discount(Base):
    """Un descuento (ej. 'Promo 2x'). Su precio por servicio está en discount_prices."""
    __tablename__ = "discounts"
    __table_args__ = (
        UniqueConstraint("business_id", "id", name="uq_discounts_business_id_id"),
    )

    id = Column(Integer, primary_key=True)
    business_id = Column(Integer, ForeignKey("businesses.id", name="fk_discounts_business_id",
                                             ondelete="RESTRICT"), nullable=False)
    name = Column(String(40), nullable=False)
    active = Column(Boolean, nullable=False, default=True, server_default=true())
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now,
                        server_default=func.now())


# Nombre único por negocio sin distinguir mayúsculas ("Corte" y "corte" chocan).
# Ojo: en SQLite lower() solo pasa a minúscula A-Z (no Á/Ñ); en PostgreSQL sí. La API de 3b
# también lo valida en Python con casefold(), así que la regla es la misma en las dos bases.
Index("uq_services_business_lower_name", Service.business_id, func.lower(Service.name), unique=True)
Index("uq_discounts_business_lower_name", Discount.business_id, func.lower(Discount.name), unique=True)


class DiscountPrice(Base):
    """Precio final de un descuento para un servicio. Sin fila = el descuento no aplica a ese servicio."""
    __tablename__ = "discount_prices"
    __table_args__ = (
        # Las dos FKs incluyen business_id: el descuento y el servicio tienen que ser del mismo negocio
        ForeignKeyConstraint(["business_id", "discount_id"], ["discounts.business_id", "discounts.id"],
                             name="fk_discount_prices_discount", ondelete="RESTRICT"),
        ForeignKeyConstraint(["business_id", "service_id"], ["services.business_id", "services.id"],
                             name="fk_discount_prices_service", ondelete="RESTRICT"),
        CheckConstraint("price >= 0", name="ck_discount_prices_price"),
        Index("ix_discount_prices_service", "business_id", "service_id"),
    )

    business_id = Column(Integer, ForeignKey("businesses.id", name="fk_discount_prices_business_id",
                                             ondelete="RESTRICT"), nullable=False)
    discount_id = Column(Integer, primary_key=True)
    service_id = Column(Integer, primary_key=True)
    price = Column(Integer, nullable=False)              # precio final, pesos
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now,
                        server_default=func.now())


class BarberService(Base):
    """Qué servicios hace cada barbero y cuánto tarda. active=false: dejó de hacerlo (se recuerda la duración)."""
    __tablename__ = "barber_services"
    __table_args__ = (
        ForeignKeyConstraint(["business_id", "service_id"], ["services.business_id", "services.id"],
                             name="fk_barber_services_service", ondelete="RESTRICT"),
        CheckConstraint("duration > 0", name="ck_barber_services_duration"),
        Index("ix_barber_services_service", "business_id", "service_id"),
    )

    business_id = Column(Integer, ForeignKey("businesses.id", name="fk_barber_services_business_id",
                                             ondelete="RESTRICT"), nullable=False)
    employee_id = Column(Integer, ForeignKey("users.id", name="fk_barber_services_employee_id",
                                             ondelete="CASCADE"), primary_key=True)
    service_id = Column(Integer, primary_key=True)
    duration = Column(Integer, nullable=False)           # minutos de ESTE barbero para ESTE servicio
    active = Column(Boolean, nullable=False, default=True, server_default=true())
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now,
                        server_default=func.now())


class SchemaMigration(Base):
    """Migraciones de esquema ya aplicadas (una fila por migración)."""
    __tablename__ = "schema_migrations"

    id = Column(String(64), primary_key=True)
    applied_at = Column(DateTime(timezone=True), nullable=False, default=utc_now, server_default=func.now())
