import traceback
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from sqlalchemy import or_
from typing import List, Optional
from datetime import datetime, timedelta, date, time

from app.database import get_db
from app.models import Appointment, User, UserRole, WorkingHours, ScheduleException
from app.schemas import AppointmentCreate, AppointmentResponse
from app.core.telemetry import sec_logger

router = APIRouter(prefix="/appointments", tags=["Turnos Públicos"])

# ==========================================
# FUNCIONES AUXILIARES DE HORARIOS Y BLOQUEOS
# ==========================================

def parse_time(t) -> Optional[time]:
    if not t:
        return None
    if isinstance(t, str):
        try:
            return datetime.strptime(t, "%H:%M:%S").time()
        except ValueError:
            return datetime.strptime(t, "%H:%M").time()
    return t

def generate_slots(start_time, end_time) -> List[time]:
    slots = []
    s_time = parse_time(start_time)
    e_time = parse_time(end_time)
    if not s_time or not e_time:
        return slots
    
    current = datetime.combine(date.today(), s_time)
    end = datetime.combine(date.today(), e_time)
    
    while current < end:
        slots.append(current.time())
        current += timedelta(minutes=45)
    return slots

def to_minutes(t: time) -> int:
    """Convierte un objeto time a minutos desde las 00:00 para evaluar solapamientos."""
    return t.hour * 60 + t.minute

def get_barber_block_reason(
    exceptions: List[ScheduleException],
    employee_id: int,
    slot_time: time,
    duration_min: int = 45
) -> Optional[str]:
    """
    Evalúa si un barbero está bloqueado en ese horario (por excepción propia o feriado general).
    Devuelve el motivo si está bloqueado, o None si está disponible.
    """
    slot_start = to_minutes(slot_time)
    slot_end = slot_start + duration_min

    for exc in exceptions:
        # Aplica si es un Feriado General (None) o una excepción de este barbero
        if exc.employee_id is not None and exc.employee_id != employee_id:
            continue

        # 1. Bloqueo de día completo (sin hora de inicio/fin)
        if not exc.start_time or not exc.end_time:
            return exc.reason

        # 2. Bloqueo de franja horaria parcial (verifica si se pisa con los 45 min del turno)
        exc_start_obj = parse_time(exc.start_time)
        exc_end_obj = parse_time(exc.end_time)
        if exc_start_obj and exc_end_obj:
            exc_start = to_minutes(exc_start_obj)
            exc_end = to_minutes(exc_end_obj)

            if slot_start < exc_end and slot_end > exc_start:
                return f"{exc.reason} ({exc.start_time[:5]} a {exc.end_time[:5]})"

    return None


# ==========================================
# ENDPOINTS PÚBLICOS
# ==========================================

# 1. RUTA PARA LLENAR EL COMBO DE BARBEROS
@router.get("/barbers")
def get_public_barbers(db: Session = Depends(get_db)):
    # Trae tanto al dueño como a los empleados activos
    barbers = db.query(User).filter(
        User.role.in_([UserRole.EMPLOYEE, UserRole.OWNER]), 
        User.is_active == True
    ).all()
    
    return [{"id": b.id, "name": b.name} for b in barbers]


# 2. RUTA PARA OBTENER LOS HORARIOS DISPONIBLES
@router.get("/available-slots")
def get_available_slots(
    target_date: date, 
    employee_id: Optional[int] = None, 
    db: Session = Depends(get_db)
):
    day_of_week = target_date.weekday()
    target_date_str = target_date.isoformat()  # "YYYY-MM-DD" para comparar con String en SQLite
    
    # 1. Buscamos TODOS los empleados activos
    all_active_employees = db.query(User).filter(
        User.role.in_([UserRole.EMPLOYEE, UserRole.OWNER]), 
        User.is_active == True
    ).all()

    if not all_active_employees:
        return []

    # Si el cliente eligió a uno en particular, filtramos. Si no, evaluamos a todos.
    target_employees = [e for e in all_active_employees if e.id == employee_id] if employee_id else all_active_employees

    # Traemos de una sola vez todas las excepciones de esa fecha (globales y de empleados)
    date_exceptions = db.query(ScheduleException).filter(
        ScheduleException.date == target_date_str
    ).all()

    # 2. Mapeamos qué horarios tiene CADA barbero
    barber_availability = {}  # { 1: [10:00, 10:45], 2: [] }
    all_possible_slots = set()

    for emp in target_employees:
        schedule = db.query(WorkingHours).filter(
            WorkingHours.employee_id == emp.id,
            WorkingHours.day_of_week == day_of_week,
            WorkingHours.is_working == True
        ).first()

        emp_slots = []
        if schedule:
            if schedule.shift1_start and schedule.shift1_end:
                emp_slots.extend(generate_slots(schedule.shift1_start, schedule.shift1_end))
            if schedule.shift2_start and schedule.shift2_end:
                emp_slots.extend(generate_slots(schedule.shift2_start, schedule.shift2_end))
        
        barber_availability[emp.id] = emp_slots
        for s in emp_slots:
            all_possible_slots.add(s)

    now = datetime.now()
    is_today = target_date == date.today()
    final_available_slots = []
    sorted_slots = sorted(list(all_possible_slots))

    # 3. VERIFICACIÓN ESTRICTA: Por cada bloque horario posible
    for slot in sorted_slots:
        # Descartamos horarios pasados si la consulta es para hoy
        if is_today and slot <= now.time():
            continue

        slot_str = slot.strftime("%H:%M:%S")
        slot_short_str = slot.strftime("%H:%M")
        can_be_booked = False

        # Verificamos si AL MENOS UN barbero puede tomar este turno
        for emp in target_employees:
            # A. ¿Este barbero trabaja a esta hora según su horario habitual?
            if slot not in barber_availability[emp.id]:
                continue

            # B. ¿Este barbero tiene un feriado o excepción en esta fecha/hora?
            if get_barber_block_reason(date_exceptions, emp.id, slot) is not None:
                continue

            # C. ¿Este barbero ya tiene un turno reservado a esta hora?
            is_busy = db.query(Appointment).filter(
                Appointment.employee_id == emp.id,
                Appointment.date == target_date_str,
                Appointment.time.in_([slot_str, slot_short_str]),
                Appointment.status == "confirmed"
            ).first()
            
            if not is_busy:
                can_be_booked = True
                break  # Si encontramos a uno libre y sin excepción, habilitamos el horario

        if can_be_booked:
            final_available_slots.append(slot_short_str)

    return final_available_slots


# 3. RUTA PARA CREAR EL TURNO
@router.post("/", response_model=AppointmentResponse, status_code=status.HTTP_201_CREATED)
def create_appointment(
    appointment_data: AppointmentCreate, 
    db: Session = Depends(get_db)
):
    try:
        assigned_employee_id = None
        target_time_str = appointment_data.time
        if len(target_time_str) == 5: 
            target_time_str = f"{target_time_str}:00"
        target_time_short = target_time_str[:5]
        target_time_obj = parse_time(target_time_str)

        # Convertimos la fecha a objeto date y normalizamos el string YYYY-MM-DD
        if isinstance(appointment_data.date, str):
            target_date_obj = datetime.strptime(appointment_data.date, "%Y-%m-%d").date()
        else:
            target_date_obj = appointment_data.date
        
        target_date_str = target_date_obj.isoformat()
        day_of_week = target_date_obj.weekday()

        # Consultamos excepciones activas para ese día
        date_exceptions = db.query(ScheduleException).filter(
            ScheduleException.date == target_date_str
        ).all()

        # CASO A: El cliente eligió un profesional específico
        if appointment_data.employee_id:
            # 1. Verificar si tiene una excepción o feriado en ese horario
            block_reason = get_barber_block_reason(
                date_exceptions,
                appointment_data.employee_id,
                target_time_obj
            )
            if block_reason:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"El profesional no está disponible en ese horario ({block_reason})."
                )

            # 2. Verificar si ya tiene un turno reservado
            conflict = db.query(Appointment).filter(
                Appointment.employee_id == appointment_data.employee_id,
                Appointment.date == target_date_str,
                Appointment.time.in_([target_time_str, target_time_short]),
                Appointment.status == "confirmed"
            ).first()

            if conflict:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST, 
                    detail="El profesional seleccionado ya tiene ese horario ocupado."
                )
            assigned_employee_id = appointment_data.employee_id

        # CASO B: El cliente eligió "Cualquier barbero disponible"
        else:
            all_active_employees = db.query(User).filter(
                User.role.in_([UserRole.EMPLOYEE, UserRole.OWNER]), 
                User.is_active == True
            ).all()
            
            for emp in all_active_employees:
                # 1. ¿Trabaja este día de la semana?
                schedule = db.query(WorkingHours).filter(
                    WorkingHours.employee_id == emp.id,
                    WorkingHours.day_of_week == day_of_week,
                    WorkingHours.is_working == True
                ).first()
                
                if not schedule:
                    continue 

                # 2. ¿El horario cae dentro de sus turnos laborales?
                emp_slots_objs = []
                if schedule.shift1_start and schedule.shift1_end:
                    emp_slots_objs.extend(generate_slots(schedule.shift1_start, schedule.shift1_end))
                if schedule.shift2_start and schedule.shift2_end:
                    emp_slots_objs.extend(generate_slots(schedule.shift2_start, schedule.shift2_end))
                
                emp_slots_strs = [s.strftime("%H:%M:%S") for s in emp_slots_objs]
                if target_time_str not in emp_slots_strs:
                    continue

                # 3. ¿Tiene un bloqueo o excepción en esta fecha/hora?
                if get_barber_block_reason(date_exceptions, emp.id, target_time_obj) is not None:
                    continue

                # 4. ¿Ya tiene un turno asignado a esta hora?
                is_busy = db.query(Appointment).filter(
                    Appointment.employee_id == emp.id,
                    Appointment.date == target_date_str,
                    Appointment.time.in_([target_time_str, target_time_short]),
                    Appointment.status == "confirmed"
                ).first()

                if not is_busy:
                    assigned_employee_id = emp.id
                    break 

            if not assigned_employee_id:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST, 
                    detail="No hay ningún profesional disponible en ese horario."
                )

        new_appointment = Appointment(
            client_name=appointment_data.client_name,
            client_phone=appointment_data.client_phone,
            service=appointment_data.service,
            date=target_date_str,
            time=target_time_str,
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
                    "assigned_to": assigned_employee_id
                }
            }
        )

        return new_appointment

    except HTTPException:
        # Re-lanzamos las HTTPException (400/404/409) para que no caigan en el Error 500
        raise
    except Exception as e:
        print(f"\n[!!! ERROR CRÍTICO EN CREATE APPOINTMENT !!!]")
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail="Error interno del servidor. Revisar terminal.")