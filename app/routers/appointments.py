"""
Reserva de la web pública (sin login).

Entrega 3e: la reserva usa el catálogo.
- GET  /appointments/catalog          servicios que se pueden reservar, sus descuentos y quién los hace.
- GET  /appointments/available-slots  horarios libres para (fecha, servicio, barbero o "Cualquiera").
- POST /appointments/                 reserva. El navegador manda service_id y discount_id (opcional):
                                      el precio sale de quote_price() y la duración de barber_services.
- Anti doble reserva: en PostgreSQL se bloquea la fila del barbero (SELECT ... FOR UPDATE) ANTES de mirar
  sus turnos. Un segundo pedido para el mismo barbero espera a que el primero termine y, cuando sigue,
  ya ve el turno nuevo: recibe 409. En SQLite (solo desarrollo) no hay bloqueo de filas: ver la propuesta 3e.
- "Ahora" es siempre catalog.now_local() (hora del negocio), para los horarios y para reservar.
"""
import random
import traceback
from datetime import date, datetime, time, timedelta
from typing import Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import and_
from sqlalchemy.orm import Session

from app import catalog
from app.catalog import PRICE_MIN, CatalogError, public_business_id, quote_price
from app.core.telemetry import sec_logger
from app.database import get_db
from app.models import (Appointment, BarberService, Discount, DiscountPrice, ScheduleException, Service, User,
                        UserRole, WorkingHours)
from app.schemas import (AppointmentCreate, PublicAppointmentOut, PublicBarberOut, PublicCatalogOut,
                         PublicDiscountOut, PublicServiceOut, parse_iso_date)
from app.whatsapp import create_optin_link

router = APIRouter(prefix="/appointments", tags=["Turnos Públicos"])

STAFF_ROLES = [UserRole.EMPLOYEE, UserRole.OWNER]
BOOKING_DAYS_AHEAD = 60          # se puede reservar desde ahora hasta 60 días adelante
DB_ID = dict(ge=1, le=2_147_483_647)

SLOT_TAKEN = "Ese horario se acaba de ocupar. Elegí otro."


def parse_time(t) -> Optional[time]:
    if not t:
        return None
    if isinstance(t, str):
        try:
            return datetime.strptime(t, "%H:%M:%S").time()
        except ValueError:
            return datetime.strptime(t, "%H:%M").time()
    return t

def to_minutes(t: time) -> int:
    return t.hour * 60 + t.minute

def minutes_to_time(mins: int) -> time:
    return time(hour=mins // 60, minute=mins % 60)

def round_up_to_5(mins: int) -> int:
    """La reserva solo acepta minutos múltiplos de 5 (decisión E3): la grilla tampoco ofrece otros,
    aunque un turno o una excepción termine a las 10:07 o un turno de trabajo empiece a las 9:07."""
    return -(-mins // 5) * 5

def get_barber_block_reason(
    exceptions: List[ScheduleException],
    employee_id: int,
    slot_time: time,
    duration_min: int = 30
) -> Optional[str]:
    slot_start = to_minutes(slot_time)
    slot_end = slot_start + duration_min

    for exc in exceptions:
        if exc.employee_id is not None and exc.employee_id != employee_id:
            continue

        if not exc.start_time or not exc.end_time:
            return exc.reason

        exc_start_obj = parse_time(exc.start_time)
        exc_end_obj = parse_time(exc.end_time)
        if exc_start_obj and exc_end_obj:
            exc_start = to_minutes(exc_start_obj)
            exc_end = to_minutes(exc_end_obj)

            if slot_start < exc_end and slot_end > exc_start:
                return f"{exc.reason} ({exc.start_time[:5]} a {exc.end_time[:5]})"

    return None

def has_appointment_overlap(
    appointments: List[Appointment],
    slot_time: time,
    duration_min: int
) -> bool:
    slot_start = to_minutes(slot_time)
    slot_end = slot_start + duration_min

    for appt in appointments:
        appt_t = parse_time(appt.time)
        if not appt_t:
            continue
        appt_start = to_minutes(appt_t)
        appt_dur = getattr(appt, "duration", 30) or 30
        appt_end = appt_start + appt_dur

        if slot_start < appt_end and slot_end > appt_start:
            return True

    return False

def fits_in_working_shifts(
    schedule: Optional[WorkingHours],
    slot_time: time,
    duration_min: int
) -> bool:
    if not schedule or not schedule.is_working:
        return False

    slot_start = to_minutes(slot_time)
    slot_end = slot_start + duration_min

    for s_start_raw, s_end_raw in [
        (schedule.shift1_start, schedule.shift1_end),
        (schedule.shift2_start, schedule.shift2_end)
    ]:
        s_start = parse_time(s_start_raw)
        s_end = parse_time(s_end_raw)
        if s_start and s_end:
            if to_minutes(s_start) <= slot_start and slot_end <= to_minutes(s_end):
                return True

    return False

def generate_smart_slots_for_shift(
    shift_start_raw,
    shift_end_raw,
    duration_min: int,
    barber_id: int,
    exceptions: List[ScheduleException],
    barber_appointments: List[Appointment]
) -> List[time]:
    s_time = parse_time(shift_start_raw)
    e_time = parse_time(shift_end_raw)
    if not s_time or not e_time or duration_min <= 0:
        return []

    cursor = round_up_to_5(to_minutes(s_time))
    end_min = to_minutes(e_time)

    blockers: List[Tuple[int, int]] = []

    for exc in exceptions:
        if exc.employee_id is not None and exc.employee_id != barber_id:
            continue
        if not exc.start_time or not exc.end_time:
            return []
        exc_s = parse_time(exc.start_time)
        exc_e = parse_time(exc.end_time)
        if exc_s and exc_e:
            blockers.append((to_minutes(exc_s), to_minutes(exc_e)))

    for appt in barber_appointments:
        appt_t = parse_time(appt.time)
        if appt_t:
            a_start = to_minutes(appt_t)
            a_dur = getattr(appt, "duration", 30) or 30
            blockers.append((a_start, a_start + a_dur))

    valid_slots: List[time] = []

    while cursor + duration_min <= end_min:
        slot_end = cursor + duration_min
        overlapping_ends = [
            b_end for (b_start, b_end) in blockers
            if cursor < b_end and slot_end > b_start
        ]

        if overlapping_ends:
            next_free = max(overlapping_ends)
            cursor = round_up_to_5(max(cursor + 5, next_free))
        else:
            valid_slots.append(minutes_to_time(cursor))
            cursor += duration_min

    return valid_slots

def get_barber_available_slots(
    barber: User,
    schedule: Optional[WorkingHours],
    duration_min: int,
    exceptions: List[ScheduleException],
    barber_appointments: List[Appointment]
) -> List[time]:
    """Horarios libres de un barbero en un día, con SU duración para el servicio elegido."""
    if not schedule or not schedule.is_working:
        return []

    slots: List[time] = []
    for shift_start, shift_end in [(schedule.shift1_start, schedule.shift1_end),
                                   (schedule.shift2_start, schedule.shift2_end)]:
        if shift_start and shift_end:
            slots.extend(generate_smart_slots_for_shift(
                shift_start, shift_end, duration_min, barber.id, exceptions, barber_appointments))
    return slots


# ==========================================
# CATÁLOGO PARA LA RESERVA
# ==========================================

def _catalog_http_error(e: CatalogError) -> HTTPException:
    code = status.HTTP_404_NOT_FOUND if e.code.endswith("_not_found") else status.HTTP_400_BAD_REQUEST
    return HTTPException(code, e.message)


def _bookable_service(db: Session, business_id: int, service_id: int):
    """El servicio (activo y del negocio) o un 4xx claro. Usa las mismas reglas que quote_price."""
    try:
        return quote_price(db, business_id, service_id)
    except CatalogError as e:
        raise _catalog_http_error(e)


def _service_barbers(db: Session, business_id: int, service_id: int) -> Dict[int, Tuple[User, int]]:
    """Barberos ACTIVOS que hacen el servicio → (barbero, SU duración para ese servicio), por id."""
    rows = (db.query(User, BarberService.duration)
            .join(BarberService, BarberService.employee_id == User.id)
            .filter(BarberService.business_id == business_id, BarberService.service_id == service_id,
                    BarberService.active.is_(True), User.is_active.is_(True), User.role.in_(STAFF_ROLES))
            .order_by(User.id).all())
    return {user.id: (user, duration) for user, duration in rows}


def _barber_error(db: Session, employee_id: int) -> HTTPException:
    """Por qué no sirve el barbero elegido: no existe / está inactivo, o no hace el servicio."""
    exists = db.query(User.id).filter(User.id == employee_id, User.role.in_(STAFF_ROLES),
                                      User.is_active.is_(True)).first()
    if exists is None:
        return HTTPException(status.HTTP_404_NOT_FOUND, "El barbero elegido no existe o no está activo.")
    return HTTPException(status.HTTP_400_BAD_REQUEST, "El barbero elegido no hace ese servicio.")


def _targets(db: Session, business_id: int, service_id: int, employee_id: Optional[int]):
    """Los barberos entre los que se busca: el elegido (si hace el servicio) o todos los que lo hacen."""
    barbers = _service_barbers(db, business_id, service_id)
    if employee_id is not None:
        if employee_id not in barbers:
            raise _barber_error(db, employee_id)
        return {employee_id: barbers[employee_id]}
    if not barbers:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Ningún barbero hace ese servicio.")
    return barbers


def _day_data(db: Session, day_str: str):
    exceptions = db.query(ScheduleException).filter(ScheduleException.date == day_str).all()
    appointments = db.query(Appointment).filter(Appointment.date == day_str,
                                                Appointment.status == "confirmed").all()
    return exceptions, appointments


def _schedule(db: Session, employee_id: int, day: date) -> Optional[WorkingHours]:
    return db.query(WorkingHours).filter(WorkingHours.employee_id == employee_id,
                                         WorkingHours.day_of_week == day.weekday(),
                                         WorkingHours.is_working.is_(True)).first()


def _last_day(now: datetime) -> date:
    return now.date() + timedelta(days=BOOKING_DAYS_AHEAD)


@router.get("/catalog", response_model=PublicCatalogOut)
def get_public_catalog(db: Session = Depends(get_db)):
    """Servicios activos que hace al menos un barbero activo, con sus descuentos activos (con precio para
    ese servicio) y los ids de quiénes lo hacen; y la lista de esos barberos (solo id y nombre)."""
    business_id = public_business_id()
    links = (db.query(BarberService.service_id, User.id, User.name)
             .join(User, User.id == BarberService.employee_id)
             .join(Service, and_(Service.id == BarberService.service_id, Service.business_id == business_id))
             .filter(BarberService.business_id == business_id, BarberService.active.is_(True),
                     Service.active.is_(True), User.is_active.is_(True), User.role.in_(STAFF_ROLES))
             .order_by(User.id).all())
    barber_ids: Dict[int, List[int]] = {}
    barbers: Dict[int, str] = {}
    for service_id, user_id, user_name in links:
        barber_ids.setdefault(service_id, []).append(user_id)
        barbers[user_id] = user_name
    if not barber_ids:
        return PublicCatalogOut(services=[], barbers=[], days_ahead=BOOKING_DAYS_AHEAD)

    services = (db.query(Service).filter(Service.id.in_(barber_ids), Service.business_id == business_id)
                .order_by(Service.id).all())
    discounts: Dict[int, List[PublicDiscountOut]] = {}
    rows = (db.query(DiscountPrice.service_id, DiscountPrice.price, Discount.id, Discount.name)
            .join(Discount, and_(Discount.id == DiscountPrice.discount_id,
                                 Discount.business_id == DiscountPrice.business_id))
            .filter(DiscountPrice.business_id == business_id, DiscountPrice.service_id.in_(barber_ids),
                    Discount.active.is_(True))
            .order_by(Discount.id).all())
    list_price = {s.id: s.price for s in services}
    for service_id, price, discount_id, discount_name in rows:
        if PRICE_MIN <= price <= list_price.get(service_id, 0):   # igual que quote_price: nada que después rechace
            discounts.setdefault(service_id, []).append(
                PublicDiscountOut(id=discount_id, name=discount_name, price=price))

    return PublicCatalogOut(
        services=[PublicServiceOut(id=s.id, name=s.name, price=s.price, barber_ids=barber_ids[s.id],
                                   discounts=discounts.get(s.id, [])) for s in services],
        barbers=[PublicBarberOut(id=i, name=n) for i, n in sorted(barbers.items())],
        days_ahead=BOOKING_DAYS_AHEAD,
    )


@router.get("/available-slots", response_model=List[str])
def get_available_slots(
    target_date: str = Query(..., description="Fecha AAAA-MM-DD"),
    service_id: int = Query(..., **DB_ID),
    employee_id: Optional[int] = Query(None, **DB_ID, description="Sin barbero = cualquiera que haga el servicio"),
    db: Session = Depends(get_db)
):
    try:
        day = parse_iso_date(target_date)
    except ValueError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(e))
    business_id = public_business_id()
    _bookable_service(db, business_id, service_id)
    targets = _targets(db, business_id, service_id, employee_id)

    now = catalog.now_local()
    if not now.date() <= day <= _last_day(now):
        return []

    exceptions, day_appointments = _day_data(db, day.isoformat())
    slots = set()
    for emp_id, (barber, duration) in targets.items():
        emp_appts = [a for a in day_appointments if a.employee_id == emp_id]
        slots.update(get_barber_available_slots(barber, _schedule(db, emp_id, day), duration,
                                                exceptions, emp_appts))
    return [s.strftime("%H:%M") for s in sorted(slots) if datetime.combine(day, s) > now]


# ==========================================
# RESERVAR
# ==========================================

def _slot_problem(schedule, exceptions, appts, employee_id: int, start: time, duration: int):
    """None si el barbero puede atender en [start, start + duration); si no, el error para mostrar."""
    if not fits_in_working_shifts(schedule, start, duration):
        return HTTPException(status.HTTP_400_BAD_REQUEST, "Ese horario está fuera de la jornada del barbero.")
    if get_barber_block_reason(exceptions, employee_id, start, duration):
        # Sin el motivo: es un dato interno de la barbería ("Médico", "Vacaciones"...)
        return HTTPException(status.HTTP_400_BAD_REQUEST, "El barbero no atiende en ese horario.")
    if has_appointment_overlap(appts, start, duration):
        return HTTPException(status.HTTP_409_CONFLICT, SLOT_TAKEN)
    return None


def _lock_barbers(db: Session, employee_ids: List[int]) -> None:
    """PostgreSQL: SELECT ... FOR UPDATE de las filas de los barberos, en orden de id (dos pedidos que bloquean
    varios barberos los toman en el mismo orden: no se traban entre sí). Dura hasta el commit/rollback.
    SQLite no tiene bloqueo de filas: SQLAlchemy no agrega FOR UPDATE y esto es una consulta común."""
    db.query(User.id).filter(User.id.in_(employee_ids)).order_by(User.id).with_for_update().all()


@router.post("/", response_model=PublicAppointmentOut, status_code=status.HTTP_201_CREATED)
def create_appointment(
    appointment_data: AppointmentCreate,
    request: Request,
    db: Session = Depends(get_db)
):
    try:
        business_id = public_business_id()
        day = date.fromisoformat(appointment_data.date)          # el esquema ya validó AAAA-MM-DD real
        start = parse_time(appointment_data.time)                # HH:MM
        now = catalog.now_local()
        if datetime.combine(day, start) <= now:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Ese horario ya pasó. Elegí otro.")
        if day > _last_day(now):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                                f"Se puede reservar hasta {BOOKING_DAYS_AHEAD} días adelante.")

        try:
            quote = quote_price(db, business_id, appointment_data.service_id, appointment_data.discount_id)
        except CatalogError as e:
            raise _catalog_http_error(e)
        targets = _targets(db, business_id, quote.service_id, appointment_data.employee_id)

        # Bloqueo ANTES de mirar los turnos del día: lo que se lee de acá en adelante ya incluye
        # cualquier reserva de estos barberos que haya terminado mientras esperábamos.
        _lock_barbers(db, list(targets))
        exceptions, day_appointments = _day_data(db, day.isoformat())

        free, problems = [], []
        for emp_id, (barber, duration) in targets.items():
            emp_appts = [a for a in day_appointments if a.employee_id == emp_id]
            problem = _slot_problem(_schedule(db, emp_id, day), exceptions, emp_appts, emp_id, start, duration)
            if problem is None:
                free.append((barber, duration, len(emp_appts)))
            else:
                problems.append(problem)

        if not free:
            if appointment_data.employee_id is not None:
                error = problems[0]
            elif any(p.status_code == status.HTTP_409_CONFLICT for p in problems):
                error = HTTPException(status.HTTP_409_CONFLICT,
                                      "Ese horario ya no está libre con ningún barbero. Elegí otro.")
            else:
                error = HTTPException(status.HTTP_400_BAD_REQUEST,
                                      "No hay ningún barbero disponible en ese horario para ese servicio.")
            if error.status_code == status.HTTP_409_CONFLICT:
                sec_logger.info("Reserva rechazada: horario ocupado", extra={
                    "event_type": "APPOINTMENT_SLOT_TAKEN",
                    "request_id": getattr(request.state, "request_id", None),
                    "ip_address": getattr(request.state, "client_ip", None),
                    "metadata": {"date": day.isoformat(), "time": appointment_data.time,
                                 "service_id": quote.service_id, "employee_id": appointment_data.employee_id},
                })
            raise error

        # "Cualquiera": el que tiene menos turnos ese día; si empatan, al azar
        random.shuffle(free)
        chosen, duration, _ = min(free, key=lambda item: item[2])

        new_appointment = Appointment(
            client_name=appointment_data.client_name,
            client_phone=appointment_data.client_phone,
            service=quote.service_name,              # nombres congelados: si el dueño los cambia, el turno queda igual
            service_id=quote.service_id,
            discount_id=quote.discount_id,
            discount_name=quote.discount_name,
            date=day.isoformat(),
            time=start.strftime("%H:%M:%S"),
            duration=duration,                       # la de ESTE barbero para ESTE servicio
            price=quote.final_price,
            employee_id=chosen.id
        )
        db.add(new_appointment)
        db.commit()                                  # acá se libera el bloqueo
        db.refresh(new_appointment)

        sec_logger.info(
            f"Nuevo turno registrado para {appointment_data.client_name}",
            extra={
                "event_type": "APPOINTMENT_CREATED",
                "metadata": {
                    "date": new_appointment.date,
                    "time": new_appointment.time,
                    "service": new_appointment.service,
                    "service_id": new_appointment.service_id,
                    "discount_id": new_appointment.discount_id,
                    "discount_name": new_appointment.discount_name,
                    "duration": new_appointment.duration,
                    "price": new_appointment.price,
                    "assigned_to": new_appointment.employee_id
                }
            }
        )

        result = PublicAppointmentOut(
            employee_name=chosen.name, service=new_appointment.service,
            discount_name=new_appointment.discount_name, date=new_appointment.date,
            time=new_appointment.time[:5], price=new_appointment.price)

        # WhatsApp: código del turno + link del botón. Si algo falla, el turno igual queda reservado.
        try:
            result.wa_link = create_optin_link(db, new_appointment)
        except Exception:
            db.rollback()
            print(traceback.format_exc())

        return result

    except HTTPException:
        db.rollback()                                # suelta el bloqueo si lo había
        raise
    except Exception:
        db.rollback()
        print("\n[!!! ERROR CRÍTICO EN CREATE APPOINTMENT !!!]")
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail="Error interno del servidor. Revisar terminal.")
