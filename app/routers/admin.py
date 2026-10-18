from typing import List
from datetime import date as dt_date
from fastapi import APIRouter, HTTPException, Depends, status, Request
from sqlalchemy.orm import Session
from sqlalchemy import or_
from app.database import get_db
from app.models import User, UserRole, Appointment, WorkingHours, ScheduleException
from app.schemas import (
    UserResponse,
    EmployeeCreate,
    AppointmentResponse,
    WorkingHoursUpdate,
    WorkingHoursResponse,
    ExceptionCreate,
    ExceptionResponse
)
from app.core.security import get_password_hash
from app.dependencies import get_owner, get_staff
from app.core.telemetry import sec_logger

router = APIRouter(prefix="/admin", tags=["Administración (Staff y Dueño)"])

# Endpoint para conocer los datos del usuario logueado (Nombre, ID y Rol)
@router.get("/me", response_model=UserResponse)
def get_current_staff_profile(current_user: User = Depends(get_staff)):
    return current_user

@router.get("/working-days")
def get_working_days_config(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_staff)
):
    """
    Devuelve los días laborales del usuario logueado y, si es OWNER,
    también la unión de los días laborales de todo el equipo activo.
    """
    my_schedule = db.query(WorkingHours).filter(
        WorkingHours.employee_id == current_user.id
    ).all()

    # Si todavía no configuró horarios, por defecto tomamos Lunes a Sábado (0 a 5)
    if my_schedule:
        my_days = [s.day_of_week for s in my_schedule if s.is_working]
    else:
        my_days = [0, 1, 2, 3, 4, 5]

    if current_user.role == UserRole.OWNER:
        active_users = db.query(User).filter(
            User.role.in_([UserRole.EMPLOYEE, UserRole.OWNER]),
            User.is_active == True
        ).all()

        team_days_set = set()
        for u in active_users:
            u_sched = db.query(WorkingHours).filter(WorkingHours.employee_id == u.id).all()
            if u_sched:
                for s in u_sched:
                    if s.is_working:
                        team_days_set.add(s.day_of_week)
            else:
                team_days_set.update([0, 1, 2, 3, 4, 5])

        team_days = sorted(list(team_days_set))
    else:
        team_days = sorted(my_days)

    return {
        "my_working_days": sorted(my_days),
        "team_working_days": team_days
    }

# Fijate cómo inyectamos "current_user: User = Depends(get_owner)" como barrera
@router.post("/employees", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
def create_employee(
    employee_data: EmployeeCreate,
    current_user: User = Depends(get_owner),
    db: Session = Depends(get_db)
):
    new_employee = User(
        name=employee_data.name,
        username=employee_data.username,
        hashed_password=get_password_hash(employee_data.password),
        role=UserRole.EMPLOYEE # Se fuerza a que nazca como empleado
    )
    
    db.add(new_employee)
    db.commit()
    db.refresh(new_employee)
    
    sec_logger.info(
        f"Dueño {current_user.username} dio de alta al empleado {new_employee.username}",
        extra={"event_type": "EMPLOYEE_CREATED"}
    )
    
    return new_employee

@router.get("/appointments", response_model=list[AppointmentResponse])
def get_staff_appointments(
    current_user: User = Depends(get_staff),
    db: Session = Depends(get_db)
):
    # Armamos la consulta base
    query = db.query(Appointment)

    # Si es un empleado normal, forzamos a que solo vea sus propios turnos
    if current_user.role == UserRole.EMPLOYEE:
        query = query.filter(Appointment.employee_id == current_user.id)
    
    # Si es el dueño, la query pasa de largo y no se filtra (ve todos)
    appointments = query.order_by(Appointment.date.asc(), Appointment.time.asc()).all()

    # Adjuntamos el nombre del barbero a cada turno
    for appt in appointments:
        appt.employee_name = appt.employee.name if appt.employee else "Sin asignar"
    
    sec_logger.info(
        f"Agenda consultada por {current_user.username}",
        extra={
            "event_type": "AGENDA_VIEWED",
            "actor_id": current_user.id,
            "metadata": {"role": current_user.role.value}
        }
    )
    
    return appointments

@router.delete("/appointments/{appointment_id}", status_code=status.HTTP_204_NO_CONTENT)
def cancel_appointment(
    appointment_id: int,
    current_user: User = Depends(get_staff),
    db: Session = Depends(get_db)
):
    # Buscamos el turno
    query = db.query(Appointment).filter(Appointment.id == appointment_id)
    
    # Si es empleado, solo puede borrar SUS propios turnos
    if current_user.role == UserRole.EMPLOYEE:
        query = query.filter(Appointment.employee_id == current_user.id)
    
    appointment = query.first()
    if not appointment:
        raise HTTPException(status_code=404, detail="Turno no encontrado o sin permisos")
    
    db.delete(appointment)
    db.commit()
    
    sec_logger.info(
        f"Turno {appointment_id} cancelado por {current_user.username}",
        extra={"event_type": "APPOINTMENT_CANCELLED"}
    )
    return

@router.get("/schedule", response_model=List[WorkingHoursResponse])
def get_my_schedule(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_staff) # Exige token válido de staff u owner
):
    # Trae únicamente los horarios del barbero logueado (Aislación de datos)
    schedule = db.query(WorkingHours).filter(WorkingHours.employee_id == current_user.id).all()
    return schedule

@router.post("/schedule")
def update_my_schedule(
    schedule_data: WorkingHoursUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_staff) # Exige token válido de staff u owner
):
    # 1. Borramos los horarios viejos de este barbero para evitar registros duplicados
    db.query(WorkingHours).filter(WorkingHours.employee_id == current_user.id).delete()
    
    # 2. Insertamos los nuevos horarios que vienen del Frontend
    new_records = []
    for day_data in schedule_data.schedule:
        new_record = WorkingHours(
            employee_id=current_user.id,
            day_of_week=day_data.day_of_week,
            is_working=day_data.is_working,
            shift1_start=day_data.shift1_start,
            shift1_end=day_data.shift1_end,
            shift2_start=day_data.shift2_start,
            shift2_end=day_data.shift2_end
        )
        db.add(new_record)
        new_records.append(new_record)
    
    db.commit()

    sec_logger.info(
        f"Horarios laborales actualizados por {current_user.username}",
        extra={
            "event_type": "SCHEDULE_UPDATED",
            "actor_id": current_user.id
        }
    )
    
    return {"message": "Horarios actualizados correctamente"}

# ==========================================
# ENDPOINTS DE FERIADOS Y EXCEPCIONES
# ==========================================

@router.get("/exceptions", response_model=List[ExceptionResponse])
def get_exceptions(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_staff)
):
    today_str = dt_date.today().isoformat()
    query = db.query(ScheduleException).filter(ScheduleException.date >= today_str)

    # Aislación de datos:
    # - El empleado ve únicamente sus propias excepciones y las de Toda la Barbería
    # - El dueño ve absolutamente todas las excepciones del equipo
    if current_user.role == UserRole.EMPLOYEE:
        query = query.filter(
            or_(
                ScheduleException.employee_id == current_user.id,
                ScheduleException.employee_id.is_(None)
            )
        )

    exceptions = query.order_by(ScheduleException.date.asc(), ScheduleException.start_time.asc()).all()

    result = []
    for exc in exceptions:
        if exc.employee_id is None:
            emp_name = "Toda la Barbería"
        else:
            emp_name = exc.employee.name if exc.employee else "Barbero"

        result.append(ExceptionResponse(
            id=exc.id,
            date=exc.date,
            start_time=exc.start_time,
            end_time=exc.end_time,
            reason=exc.reason,
            employee_id=exc.employee_id,
            employee_name=emp_name
        ))

    return result


@router.post("/exceptions", response_model=ExceptionResponse, status_code=status.HTTP_201_CREATED)
def create_exception(
    payload: ExceptionCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_staff)
):
    # Por defecto, cualquier bloqueo (día completo o parcial) aplica SOLO al usuario logueado
    target_employee_id = current_user.id

    # Control RBAC: Solo el OWNER puede bloquear a toda la barbería (is_global=True)
    if payload.is_global:
        if current_user.role != UserRole.OWNER:
            sec_logger.warning(
                f"Violación RBAC: Empleado {current_user.username} intentó crear un bloqueo global",
                extra={
                    "event_type": "RBAC_VIOLATION_GLOBAL_EXCEPTION",
                    "actor_id": current_user.id,
                    "metadata": {"date": payload.date}
                }
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Solo el dueño puede bloquear toda la barbería."
            )
        target_employee_id = None

    # Evitar duplicados exactos para la misma fecha y empleado
    duplicate_query = db.query(ScheduleException).filter(
        ScheduleException.date == payload.date,
        ScheduleException.employee_id == target_employee_id,
        ScheduleException.start_time == payload.start_time,
        ScheduleException.end_time == payload.end_time
    ).first()

    if duplicate_query:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Ya existe un bloqueo idéntico programado para esa fecha y horario."
        )

    new_exc = ScheduleException(
        date=payload.date,
        start_time=payload.start_time,
        end_time=payload.end_time,
        reason="Bloqueo",
        employee_id=target_employee_id
    )
    db.add(new_exc)
    db.commit()
    db.refresh(new_exc)

    # Verificar cuántos turnos previamente confirmados quedan atrapados en este bloqueo
    appt_query = db.query(Appointment).filter(
        Appointment.date == payload.date,
        Appointment.status == "confirmed"
    )
    if target_employee_id is not None:
        appt_query = appt_query.filter(Appointment.employee_id == target_employee_id)

    existing_appts = appt_query.all()
    affected = 0
    for appt in existing_appts:
        if not payload.start_time:  # Día completo bloqueado
            affected += 1
        elif payload.start_time <= appt.time < payload.end_time:
            affected += 1

    emp_name = "Toda la Barbería" if target_employee_id is None else current_user.name

    sec_logger.info(
        f"Excepción de horario creada por {current_user.username} para el {payload.date}",
        extra={
            "event_type": "EXCEPTION_CREATED",
            "actor_id": current_user.id,
            "metadata": {
                "exception_id": new_exc.id,
                "date": payload.date,
                "start_time": payload.start_time,
                "end_time": payload.end_time,
                "is_global": payload.is_global,
                "affected_appointments": affected
            }
        }
    )

    return ExceptionResponse(
        id=new_exc.id,
        date=new_exc.date,
        start_time=new_exc.start_time,
        end_time=new_exc.end_time,
        reason=new_exc.reason,
        employee_id=new_exc.employee_id,
        employee_name=emp_name,
        affected_appointments=affected
    )


@router.delete("/exceptions/{exception_id}", status_code=status.HTTP_200_OK)
def delete_exception(
    exception_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_staff)
):
    exc = db.query(ScheduleException).filter(ScheduleException.id == exception_id).first()
    if not exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Excepción no encontrada."
        )

    # Protección anti-IDOR: Un empleado solo puede eliminar sus propias excepciones
    if current_user.role == UserRole.EMPLOYEE and exc.employee_id != current_user.id:
        sec_logger.warning(
            f"Intento IDOR: {current_user.username} intentó borrar la excepción {exception_id} ajena",
            extra={
                "event_type": "IDOR_ATTEMPT_EXCEPTION_DELETE",
                "actor_id": current_user.id,
                "metadata": {"target_exception_id": exception_id, "owner_id": exc.employee_id}
            }
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No tenés permisos para eliminar el bloqueo de otro barbero o de toda la barbería."
        )

    db.delete(exc)
    db.commit()

    sec_logger.info(
        f"Excepción {exception_id} eliminada por {current_user.username}",
        extra={
            "event_type": "EXCEPTION_DELETED",
            "actor_id": current_user.id,
            "metadata": {"exception_id": exception_id, "date": exc.date}
        }
    )

    return {"message": "Excepción eliminada correctamente"}