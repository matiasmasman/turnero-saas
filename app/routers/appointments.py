import random
import traceback
from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.orm import Session
from typing import List, Optional, Tuple
from datetime import datetime, date, time

from app.database import get_db
from app.models import Appointment, User, UserRole, WorkingHours, ScheduleException
from app.schemas import AppointmentCreate, AppointmentResponse
from app.core.telemetry import sec_logger

router = APIRouter(prefix="/appointments", tags=["Turnos Públicos"])

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

def get_service_duration(barber: User, service_name: str) -> int:
    normalized = (service_name or "Corte").strip()
    if normalized == "Barba":
        return getattr(barber, "duration_barba", 15) or 15
    elif normalized == "Corte + Barba":
        return getattr(barber, "duration_corte_barba", 45) or 45
    return getattr(barber, "duration_corte", 30) or 30

def get_service_price(owner: Optional[User], service_name: str) -> int:
    normalized = (service_name or "Corte").strip()
    if normalized == "Barba":
        return getattr(owner, "price_barba", 10000) if owner else 10000
    elif normalized == "Corte + Barba":
        return getattr(owner, "price_corte_barba", 18000) if owner else 18000
    return getattr(owner, "price_corte", 15000) if owner else 15000

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

        if slot_start < exc_end if False else (slot_start < appt_end and slot_end > appt_start):
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

    cursor = to_minutes(s_time)
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
            cursor = max(cursor + 5, next_free)
        else:
            valid_slots.append(minutes_to_time(cursor))
            cursor += duration_min

    return valid_slots

def get_barber_available_slots(
    barber: User,
    schedule: Optional[WorkingHours],
    service_name: str,
    exceptions: List[ScheduleException],
    barber_appointments: List[Appointment]
) -> List[time]:
    if not schedule or not schedule.is_working:
        return []

    duration_min = get_service_duration(barber, service_name)
    slots: List[time] = []

    if schedule.shift1_start and schedule.shift1_end:
        slots.extend(generate_smart_slots_for_shift(
            schedule.shift1_start,
            schedule.shift1_end,
            duration_min,
            barber.id,
            exceptions,
            barber_appointments
        ))

    if schedule.shift2_start and schedule.shift2_end:
        slots.extend(generate_smart_slots_for_shift(
            schedule.shift2_start,
            schedule.shift2_end,
            duration_min,
            barber.id,
            exceptions,
            barber_appointments
        ))

    return slots

# ==========================================
# ENDPOINTS PÚBLICOS
# ==========================================

@router.get("/services")
def get_public_services(db: Session = Depends(get_db)):
    owner = db.query(User).filter(User.role == UserRole.OWNER).first()
    return {
        "price_corte": getattr(owner, "price_corte", 15000) if owner else 15000,
        "price_barba": getattr(owner, "price_barba", 10000) if owner else 10000,
        "price_corte_barba": getattr(owner, "price_corte_barba", 18000) if owner else 18000
    }

@router.get("/barbers")
def get_public_barbers(db: Session = Depends(get_db)):
    barbers = db.query(User).filter(
        User.role.in_([UserRole.EMPLOYEE, UserRole.OWNER]), 
        User.is_active == True
    ).all()
    
    return [
        {
            "id": b.id,
            "name": b.name,
            "duration_corte": b.duration_corte,
            "duration_barba": b.duration_barba,
            "duration_corte_barba": b.duration_corte_barba
        }
        for b in barbers
    ]

@router.get("/available-slots")
def get_available_slots(
    target_date: date,
    service: str = Query("Corte", description="Servicio seleccionado: Corte, Barba o Corte + Barba"),
    employee_id: Optional[int] = None,
    db: Session = Depends(get_db)
):
    day_of_week = target_date.weekday()
    target_date_str = target_date.isoformat()
    
    all_active_employees = db.query(User).filter(
        User.role.in_([UserRole.EMPLOYEE, UserRole.OWNER]), 
        User.is_active == True
    ).all()

    if not all_active_employees:
        return []

    target_employees = (
        [e for e in all_active_employees if e.id == employee_id]
        if employee_id
        else all_active_employees
    )

    date_exceptions = db.query(ScheduleException).filter(
        ScheduleException.date == target_date_str
    ).all()

    day_appointments = db.query(Appointment).filter(
        Appointment.date == target_date_str,
        Appointment.status == "confirmed"
    ).all()

    all_possible_slots = set()

    for emp in target_employees:
        schedule = db.query(WorkingHours).filter(
            WorkingHours.employee_id == emp.id,
            WorkingHours.day_of_week == day_of_week,
            WorkingHours.is_working == True
        ).first()

        emp_appts = [a for a in day_appointments if a.employee_id == emp.id]
        emp_slots = get_barber_available_slots(
            emp,
            schedule,
            service,
            date_exceptions,
            emp_appts
        )
        for s in emp_slots:
            all_possible_slots.add(s)

    now = datetime.now()
    is_today = target_date == date.today()
    sorted_slots = sorted(list(all_possible_slots))

    final_available_slots = [
        slot.strftime("%H:%M")
        for slot in sorted_slots
        if not (is_today and slot <= now.time())
    ]

    return final_available_slots

@router.post("/", response_model=AppointmentResponse, status_code=status.HTTP_201_CREATED)
def create_appointment(
    appointment_data: AppointmentCreate, 
    db: Session = Depends(get_db)
):
    try:
        assigned_employee_id = None
        assigned_duration = 30

        target_time_str = appointment_data.time
        if len(target_time_str) == 5: 
            target_time_str = f"{target_time_str}:00"
        target_time_obj = parse_time(target_time_str)

        if isinstance(appointment_data.date, str):
            target_date_obj = datetime.strptime(appointment_data.date, "%Y-%m-%d").date()
        else:
            target_date_obj = appointment_data.date
        
        target_date_str = target_date_obj.isoformat()
        day_of_week = target_date_obj.weekday()

        date_exceptions = db.query(ScheduleException).filter(
            ScheduleException.date == target_date_str
        ).all()

        day_appointments = db.query(Appointment).filter(
            Appointment.date == target_date_str,
            Appointment.status == "confirmed"
        ).all()

        if appointment_data.employee_id:
            barber = db.query(User).filter(
                User.id == appointment_data.employee_id,
                User.role.in_([UserRole.EMPLOYEE, UserRole.OWNER]),
                User.is_active == True
            ).first()

            if not barber:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="El profesional seleccionado no existe o no está activo."
                )

            service_duration = get_service_duration(barber, appointment_data.service)

            schedule = db.query(WorkingHours).filter(
                WorkingHours.employee_id == barber.id,
                WorkingHours.day_of_week == day_of_week,
                WorkingHours.is_working == True
            ).first()

            if not fits_in_working_shifts(schedule, target_time_obj, service_duration):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="El horario elegido excede la jornada laboral del profesional."
                )

            block_reason = get_barber_block_reason(
                date_exceptions,
                barber.id,
                target_time_obj,
                service_duration
            )
            if block_reason:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"El profesional no está disponible en ese horario ({block_reason})."
                )

            emp_appts = [a for a in day_appointments if a.employee_id == barber.id]
            if has_appointment_overlap(emp_appts, target_time_obj, service_duration):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST, 
                    detail="El profesional seleccionado ya tiene ese horario ocupado."
                )

            assigned_employee_id = barber.id
            assigned_duration = service_duration

        else:
            all_active_employees = db.query(User).filter(
                User.role.in_([UserRole.EMPLOYEE, UserRole.OWNER]), 
                User.is_active == True
            ).all()
            
            candidates: List[Tuple[User, int]] = []

            for emp in all_active_employees:
                schedule = db.query(WorkingHours).filter(
                    WorkingHours.employee_id == emp.id,
                    WorkingHours.day_of_week == day_of_week,
                    WorkingHours.is_working == True
                ).first()
                
                if not schedule:
                    continue

                emp_appts = [a for a in day_appointments if a.employee_id == emp.id]
                valid_slots = get_barber_available_slots(
                    emp,
                    schedule,
                    appointment_data.service,
                    date_exceptions,
                    emp_appts
                )

                if target_time_obj in valid_slots:
                    # Guardamos al barbero disponible y cuántos turnos ya tiene asignados en ese día
                    candidates.append((emp, len(emp_appts)))

            if not candidates:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST, 
                    detail="No hay ningún profesional disponible en ese horario para el servicio elegido."
                )

            # 1. Mezclamos al azar para desempatar de forma aleatoria
            random.shuffle(candidates)

            # 2. Elegimos al barbero disponible con MENOS turnos en ese día
            chosen_barber, _ = min(candidates, key=lambda item: item[1])
            assigned_employee_id = chosen_barber.id
            assigned_duration = get_service_duration(chosen_barber, appointment_data.service)

        # Obtener precio vigente configurado por el dueño y congelarlo en el turno
        owner = db.query(User).filter(User.role == UserRole.OWNER).first()
        assigned_price = get_service_price(owner, appointment_data.service)

        new_appointment = Appointment(
            client_name=appointment_data.client_name,
            client_phone=appointment_data.client_phone,
            service=appointment_data.service,
            date=target_date_str,
            time=target_time_str,
            duration=assigned_duration,
            price=assigned_price,
            employee_id=assigned_employee_id
        )
        
        db.add(new_appointment)
        db.commit()
        db.refresh(new_appointment)

        sec_logger.info(
            f"Nuevo turno registrado para {appointment_data.client_name}",
            extra={
                "event_type": "APPOINTMENT_CREATED",
                "metadata": {
                    "date": target_date_str,
                    "time": target_time_str,
                    "service": appointment_data.service,
                    "duration": assigned_duration,
                    "price": assigned_price,
                    "assigned_to": assigned_employee_id
                }
            }
        )

        return new_appointment

    except HTTPException:
        raise
    except Exception as e:
        print(f"\n[!!! ERROR CRÍTICO EN CREATE APPOINTMENT !!!]")
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail="Error interno del servidor. Revisar terminal.")