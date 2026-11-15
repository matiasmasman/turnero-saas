"""
Catálogo (Entrega 3): reglas de servicios y descuentos y el cálculo de precio del servidor.

- current_business_id(): el ÚNICO lugar que decide a qué negocio pertenece el usuario.
- normalize_name(): la regla de nombres (servicios y descuentos).
- quote_price(): el precio final de (servicio, descuento opcional). La reserva (3e) lo va a usar;
  el precio nunca lo decide el navegador.
"""
import os
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session

from app.models import Appointment, Discount, DiscountPrice, Service, User

NAME_MIN, NAME_MAX = 2, 40
# Letras (con tildes, ü y ñ), números, espacio y + - . ' ( ) %
NAME_RE = re.compile(r"[0-9A-Za-zÁÉÍÓÚÜÑáéíóúüñ +\-.'()%]+")
PRICE_MIN, PRICE_MAX = 1, 1_000_000            # pesos
DURATION_MIN, DURATION_MAX, DURATION_STEP = 5, 180, 5   # minutos
LOCAL_TZ = ZoneInfo(os.getenv("WA_TIMEZONE", "America/Argentina/Buenos_Aires"))


def current_business_id(user: User) -> int:
    """Negocio del usuario logueado. Hoy hay uno solo (id 1, creado por la migración e3a).
    Cuando users tenga business_id, esto pasa a ser `return user.business_id` y nada más cambia."""
    return 1


def now_local() -> datetime:
    """Hora local del negocio, sin zona (como se guardan date/time de los turnos)."""
    return datetime.now(LOCAL_TZ).replace(tzinfo=None)


def normalize_name(value: str) -> str:
    """Valida y normaliza un nombre de servicio o descuento. Lanza ValueError con un mensaje claro."""
    name = unicodedata.normalize("NFC", value)   # "é" escrita como e + tilde suelta pasa a ser "é"
    name = " ".join(name.split())                # saca espacios al principio/fin y los dobles
    if not NAME_MIN <= len(name) <= NAME_MAX:
        raise ValueError(f"El nombre tiene que tener entre {NAME_MIN} y {NAME_MAX} caracteres.")
    if not NAME_RE.fullmatch(name):
        raise ValueError("El nombre solo puede tener letras, números, espacios y + - . ' ( ) %")
    return name


def name_taken(db: Session, model, business_id: int, name: str, exclude_id: Optional[int] = None) -> bool:
    """¿Ya hay otro servicio/descuento del negocio con ese nombre, sin distinguir mayúsculas?
    En Python con casefold(): el índice lower(name) de SQLite no iguala 'Á' con 'á' (PostgreSQL sí)."""
    key = name.casefold()
    rows = db.query(model.id, model.name).filter(model.business_id == business_id).all()
    return any(n.casefold() == key for i, n in rows if i != exclude_id)


# ---------- Precio ----------
class CatalogError(Exception):
    """Error de catálogo con un código estable (para la API) y un mensaje para mostrar."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class PriceQuote:
    service_id: int
    service_name: str
    base_price: int
    final_price: int
    discount_id: Optional[int] = None
    discount_name: Optional[str] = None


def quote_price(db: Session, business_id: int, service_id: int, discount_id: Optional[int] = None) -> PriceQuote:
    """Precio final de un servicio, con o sin descuento. Todo se busca dentro de business_id:
    un servicio o descuento de otro negocio da el mismo error que uno que no existe."""
    service = db.query(Service).filter(Service.id == service_id, Service.business_id == business_id).first()
    if service is None:
        raise CatalogError("service_not_found", "El servicio elegido no existe.")
    if not service.active:
        raise CatalogError("service_inactive", "El servicio elegido no está disponible.")
    if discount_id is None:
        return PriceQuote(service.id, service.name, service.price, service.price)

    discount = db.query(Discount).filter(Discount.id == discount_id, Discount.business_id == business_id).first()
    if discount is None:
        raise CatalogError("discount_not_found", "El descuento elegido no existe.")
    if not discount.active:
        raise CatalogError("discount_inactive", "El descuento elegido no está disponible.")
    row = db.query(DiscountPrice).filter(
        DiscountPrice.business_id == business_id,
        DiscountPrice.discount_id == discount.id,
        DiscountPrice.service_id == service.id,
    ).first()
    if row is None:
        raise CatalogError("discount_not_applicable", "Ese descuento no aplica a este servicio.")
    if not PRICE_MIN <= row.price <= service.price:
        # No debería pasar (la API no lo permite), pero si alguien toca la base a mano, no cobramos de más
        raise CatalogError("price_inconsistent", "El precio con descuento no es válido. Avisale a la barbería.")
    return PriceQuote(service.id, service.name, service.price, row.price, discount.id, discount.name)


# ---------- Turnos futuros ----------
def _future(now: datetime):
    # "HH:MM" sirve para los dos formatos de appointments.time: "12:00" y "12:00:00" son >= "12:00",
    # "11:59" y "11:59:00" no. (Con "%H:%M:%S", un turno guardado como "12:00" quedaría afuera.)
    today, hour = now.date().isoformat(), now.strftime("%H:%M")
    return and_(
        Appointment.status == "confirmed",
        or_(Appointment.date > today, and_(Appointment.date == today, Appointment.time >= hour)),
    )


def future_appointments_for_service(db: Session, service: Service, now: Optional[datetime] = None,
                                    employee_id: Optional[int] = None) -> int:
    """Turnos confirmados desde ahora que usan el servicio: por service_id, o por nombre si el turno
    no tiene service_id (los reservados antes de 3e). Con employee_id, solo los de ese barbero.
    Hoy hay un solo negocio; cuando appointments tenga business_id, el filtro por nombre tiene que sumarlo."""
    uses = or_(Appointment.service_id == service.id,
               and_(Appointment.service_id.is_(None), Appointment.service == service.name))
    query = db.query(func.count(Appointment.id)).filter(_future(now or now_local()), uses)
    if employee_id is not None:
        query = query.filter(Appointment.employee_id == employee_id)
    return query.scalar()


def future_appointments_for_discount(db: Session, discount: Discount, now: Optional[datetime] = None) -> int:
    uses = or_(Appointment.discount_id == discount.id,
               and_(Appointment.discount_id.is_(None), Appointment.discount_name == discount.name))
    return db.query(func.count(Appointment.id)).filter(_future(now or now_local()), uses).scalar()
