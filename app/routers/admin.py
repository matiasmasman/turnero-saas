import calendar
from typing import List, Optional, Literal
from datetime import date as dt_date, datetime, timedelta
from fastapi import APIRouter, HTTPException, Depends, status, Query
from sqlalchemy.orm import Session
from sqlalchemy import or_
from app.database import get_db
from app.models import User, UserRole, Appointment, WorkingHours, ScheduleException
from app.schemas import (
    UserResponse,
    AppointmentResponse,
    AppointmentPriceTierUpdate,
    WorkingHoursUpdate,
    WorkingHoursResponse,
    ServiceDurationsUpdate,
    ServiceDurationsResponse,
    ServicePricesUpdate,
    ServicePricesResponse,
    ExceptionCreate,
    ExceptionResponse,
    ChartPointItem,
    MetricsResponse,
)
from app.dependencies import get_owner, get_staff
from app.core.telemetry import sec_logger

router = APIRouter(prefix="/admin", tags=["Administración (Staff y Dueño)"])

def _time_str_to_minutes(t_str: str) -> int:
    parts = t_str.strip().split(":")
    return int(parts[0]) * 60 + int(parts[1])

@router.get("/me", response_model=UserResponse)
def get_current_staff_profile(current_user: User = Depends(get_staff)):
    return current_user

# ==========================================
# ENDPOINTS DE SERVICIOS (DURACIONES Y PRECIOS)
# ==========================================

@router.get("/service-durations", response_model=ServiceDurationsResponse)
def get_my_service_durations(current_user: User = Depends(get_staff)):
    return current_user

@router.post("/service-durations", response_model=ServiceDurationsResponse)
def update_my_service_durations(
    durations_data: ServiceDurationsUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_staff)
):
    current_user.duration_corte = durations_data.duration_corte
    current_user.duration_barba = durations_data.duration_barba
    current_user.duration_corte_barba = durations_data.duration_corte_barba

    db.commit()
    db.refresh(current_user)

    sec_logger.info(
        f"Duraciones de servicios actualizadas por {current_user.username}",
        extra={
            "event_type": "SERVICE_DURATIONS_UPDATED",
            "actor_id": current_user.id,
            "metadata": {
                "duration_corte": current_user.duration_corte,
                "duration_barba": current_user.duration_barba,
                "duration_corte_barba": current_user.duration_corte_barba
            }
        }
    )
    return current_user

@router.get("/service-prices", response_model=ServicePricesResponse)
def get_service_prices(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_staff)
):
    owner = db.query(User).filter(User.role == UserRole.OWNER).first()
    return owner if owner else current_user

@router.post("/service-prices", response_model=ServicePricesResponse)
def update_service_prices(
    prices_data: ServicePricesUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_owner)
):
    current_user.price_corte = prices_data.price_corte
    current_user.price_corte_promo2x = prices_data.price_corte_promo2x
    current_user.price_corte_vip = prices_data.price_corte_vip
    current_user.price_barba = prices_data.price_barba
    current_user.price_corte_barba = prices_data.price_corte_barba

    db.commit()
    db.refresh(current_user)

    sec_logger.info(
        f"Precios de servicios actualizados por el dueño {current_user.username}",
        extra={
            "event_type": "SERVICE_PRICES_UPDATED",
            "actor_id": current_user.id,
            "metadata": {
                "price_corte": current_user.price_corte,
                "price_corte_promo2x": current_user.price_corte_promo2x,
                "price_corte_vip": current_user.price_corte_vip,
                "price_barba": current_user.price_barba,
                "price_corte_barba": current_user.price_corte_barba
            }
        }
    )
    return current_user

@router.get("/working-days")
def get_working_days_config(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_staff)
):
    my_schedule = db.query(WorkingHours).filter(
        WorkingHours.employee_id == current_user.id
    ).all()

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

# ==========================================
# ENDPOINTS DE AGENDA Y HORARIOS
# ==========================================

@router.get("/appointments", response_model=list[AppointmentResponse])
def get_staff_appointments(
    current_user: User = Depends(get_staff),
    db: Session = Depends(get_db)
):
    query = db.query(Appointment)

    if current_user.role == UserRole.EMPLOYEE:
        query = query.filter(Appointment.employee_id == current_user.id)
    
    appointments = query.order_by(Appointment.date.asc(), Appointment.time.asc()).all()

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

@router.patch("/appointments/{appointment_id}/price", response_model=AppointmentResponse)
def update_appointment_price_tier(
    appointment_id: int,
    payload: AppointmentPriceTierUpdate,
    current_user: User = Depends(get_staff),
    db: Session = Depends(get_db)
):
    appointment = db.query(Appointment).filter(Appointment.id == appointment_id).first()
    if not appointment:
        raise HTTPException(status_code=404, detail="Turno no encontrado.")

    # Barrera Anti-IDOR: un empleado solo puede modificar el precio de sus propios turnos
    if current_user.role == UserRole.EMPLOYEE and appointment.employee_id != current_user.id:
        sec_logger.warning(
            f"Intento IDOR: {current_user.username} intentó cambiar el precio del turno {appointment_id} ajeno",
            extra={
                "event_type": "IDOR_ATTEMPT_APPOINTMENT_PRICE",
                "actor_id": current_user.id,
                "metadata": {"appointment_id": appointment_id, "owner_employee_id": appointment.employee_id}
            }
        )
        raise HTTPException(status_code=403, detail="No tenés permisos para modificar un turno de otro barbero.")

    owner = db.query(User).filter(User.role == UserRole.OWNER).first()
    price_source = owner if owner else current_user

    old_price = appointment.price

    if payload.tier == "promo2x":
        new_price = getattr(price_source, "price_corte_promo2x", 12000)
    elif payload.tier == "vip":
        new_price = getattr(price_source, "price_corte_vip", 10000)
    else:
        if appointment.service == "Barba":
            new_price = price_source.price_barba
        elif appointment.service == "Corte + Barba":
            new_price = price_source.price_corte_barba
        else:
            new_price = price_source.price_corte

    appointment.price = new_price
    db.commit()
    db.refresh(appointment)
    appointment.employee_name = appointment.employee.name if appointment.employee else "Sin asignar"

    sec_logger.info(
        f"Precio del turno {appointment_id} actualizado a {new_price} ({payload.tier}) por {current_user.username}",
        extra={
            "event_type": "APPOINTMENT_PRICE_UPDATED",
            "actor_id": current_user.id,
            "metadata": {
                "appointment_id": appointment_id,
                "tier": payload.tier,
                "old_price": old_price,
                "new_price": new_price
            }
        }
    )

    return appointment

@router.delete("/appointments/{appointment_id}", status_code=status.HTTP_204_NO_CONTENT)
def cancel_appointment(
    appointment_id: int,
    current_user: User = Depends(get_staff),
    db: Session = Depends(get_db)
):
    query = db.query(Appointment).filter(Appointment.id == appointment_id)
    
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
    current_user: User = Depends(get_staff)
):
    schedule = db.query(WorkingHours).filter(WorkingHours.employee_id == current_user.id).all()
    return schedule

@router.post("/schedule")
def update_my_schedule(
    schedule_data: WorkingHoursUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_staff)
):
    db.query(WorkingHours).filter(WorkingHours.employee_id == current_user.id).delete()
    
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
    target_employee_id = current_user.id

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

    appt_query = db.query(Appointment).filter(
        Appointment.date == payload.date,
        Appointment.status == "confirmed"
    )
    if target_employee_id is not None:
        appt_query = appt_query.filter(Appointment.employee_id == target_employee_id)

    existing_appts = appt_query.all()
    affected = 0
    for appt in existing_appts:
        if not payload.start_time or not payload.end_time:
            affected += 1
        else:
            exc_start_min = _time_str_to_minutes(payload.start_time)
            exc_end_min = _time_str_to_minutes(payload.end_time)
            appt_start_min = _time_str_to_minutes(appt.time)
            appt_dur = appt.duration if getattr(appt, "duration", None) else 30
            appt_end_min = appt_start_min + appt_dur

            if appt_start_min < exc_end_min and appt_end_min > exc_start_min:
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

# ==========================================
# ENDPOINT DE MÉTRICAS Y FACTURACIÓN
# ==========================================

@router.get("/metrics", response_model=MetricsResponse)
def get_metrics(
    period: Literal["today", "week", "month"] = Query("month", description="Período: today, week o month"),
    employee_id: Optional[int] = Query(None, ge=1, description="Filtro de barbero (Solo propio ID o None para Todo el Local)"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_staff)
):
    # 1. Regla de privacidad y Anti-IDOR:
    # Ningún usuario (ni siquiera el dueño) puede pedir métricas individuales de otro barbero.
    if employee_id is not None and employee_id != current_user.id:
        sec_logger.warning(
            f"Intento de acceso a métricas individuales ajenas por {current_user.username} (target ID {employee_id})",
            extra={
                "event_type": "IDOR_ATTEMPT_METRICS_ACCESS",
                "actor_id": current_user.id,
                "metadata": {"target_employee_id": employee_id, "period": period, "role": current_user.role.value}
            }
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Solo podés consultar tus propias métricas individuales o el total del local."
        )

    if current_user.role == UserRole.EMPLOYEE:
        effective_employee_id = current_user.id
    else:
        # Si es OWNER: employee_id == current_user.id (Mis Métricas) o None (Todo el Local)
        effective_employee_id = employee_id

    # 2. Rango de fechas en formato ISO (YYYY-MM-DD)
    today = dt_date.today()
    today_str = today.isoformat()
    now_dt = datetime.now()
    now_minutes = now_dt.hour * 60 + now_dt.minute

    if period == "today":
        start_date = today
        end_date = today
    elif period == "week":
        start_date = today - timedelta(days=today.weekday())
        end_date = start_date + timedelta(days=6)
    else:
        start_date = today.replace(day=1)
        last_day = calendar.monthrange(today.year, today.month)[1]
        end_date = today.replace(day=last_day)

    start_date_str = start_date.isoformat()
    end_date_str = end_date.isoformat()

    # 3. Consulta de turnos confirmados en el período (exclusivamente de barberos activos)
    query = db.query(Appointment).join(
        User, Appointment.employee_id == User.id
    ).filter(
        User.is_active == True,
        Appointment.date >= start_date_str,
        Appointment.date <= end_date_str,
        Appointment.status == "confirmed"
    )

    if effective_employee_id is not None:
        query = query.filter(Appointment.employee_id == effective_employee_id)

    appointments = query.all()

    # 4. Cálculo de KPIs y clasificación para gráficos
    total_appointments = len(appointments)
    completed_appointments = 0
    pending_appointments = 0
    real_revenue = 0
    pending_revenue = 0

    enriched_appts = []
    for appt in appointments:
        appt_start_min = _time_str_to_minutes(appt.time) if appt.time else 0
        is_completed = (appt.date < today_str) or (appt.date == today_str and appt_start_min <= now_minutes)
        appt_price = getattr(appt, "price", 0) or 0

        if is_completed:
            completed_appointments += 1
            real_revenue += appt_price
        else:
            pending_appointments += 1
            pending_revenue += appt_price

        enriched_appts.append({
            "date": appt.date,
            "start_min": appt_start_min,
            "price": appt_price,
            "is_completed": is_completed
        })

    total_revenue = real_revenue + pending_revenue

    # 5. Armado de serie temporal para el gráfico de barras
    chart_series: List[ChartPointItem] = []

    if period == "today":
        time_blocks = [
            ("Mañana", "08 a 12h", 0, 12 * 60 + 59),
            ("Mediodía", "13 a 15h", 13 * 60, 15 * 60 + 59),
            ("Tarde", "16 a 19h", 16 * 60, 19 * 60 + 59),
            ("Noche", "20 a 23h", 20 * 60, 23 * 60 + 59),
        ]
        for label, sublabel, min_start, min_end in time_blocks:
            b_real = sum(a["price"] for a in enriched_appts if min_start <= a["start_min"] <= min_end and a["is_completed"])
            b_pend = sum(a["price"] for a in enriched_appts if min_start <= a["start_min"] <= min_end and not a["is_completed"])
            b_cnt = sum(1 for a in enriched_appts if min_start <= a["start_min"] <= min_end)
            chart_series.append(ChartPointItem(
                label=label,
                sublabel=sublabel,
                real_revenue=b_real,
                pending_revenue=b_pend,
                total_revenue=b_real + b_pend,
                appointments=b_cnt
            ))

    elif period == "week":
        day_labels = ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"]
        for i in range(7):
            d_obj = start_date + timedelta(days=i)
            d_iso = d_obj.isoformat()
            d_sub = f"{d_obj.day:02d}/{d_obj.month:02d}"
            b_real = sum(a["price"] for a in enriched_appts if a["date"] == d_iso and a["is_completed"])
            b_pend = sum(a["price"] for a in enriched_appts if a["date"] == d_iso and not a["is_completed"])
            b_cnt = sum(1 for a in enriched_appts if a["date"] == d_iso)
            chart_series.append(ChartPointItem(
                label=day_labels[i],
                sublabel=d_sub,
                real_revenue=b_real,
                pending_revenue=b_pend,
                total_revenue=b_real + b_pend,
                appointments=b_cnt
            ))

    else:  # month (agrupado en semanas del mes)
        last_day_num = end_date.day
        week_ranges = [
            ("Sem 1", 1, 7),
            ("Sem 2", 8, 14),
            ("Sem 3", 15, 21),
            ("Sem 4", 22, 28),
        ]
        if last_day_num > 28:
            week_ranges.append(("Sem 5", 29, last_day_num))

        for label, d_start, d_end in week_ranges:
            sublabel = f"{d_start:02d}-{d_end:02d}"
            b_real = 0
            b_pend = 0
            b_cnt = 0
            for a in enriched_appts:
                day_num = int(a["date"].split("-")[2])
                if d_start <= day_num <= d_end:
                    b_cnt += 1
                    if a["is_completed"]:
                        b_real += a["price"]
                    else:
                        b_pend += a["price"]

            chart_series.append(ChartPointItem(
                label=label,
                sublabel=sublabel,
                real_revenue=b_real,
                pending_revenue=b_pend,
                total_revenue=b_real + b_pend,
                appointments=b_cnt
            ))

    sec_logger.info(
        f"Métricas consultadas por {current_user.username} ({period})",
        extra={
            "event_type": "METRICS_VIEWED",
            "actor_id": current_user.id,
            "metadata": {
                "period": period,
                "filter_employee_id": effective_employee_id
            }
        }
    )

    return MetricsResponse(
        period=period,
        start_date=start_date_str,
        end_date=end_date_str,
        filter_employee_id=effective_employee_id,
        total_appointments=total_appointments,
        completed_appointments=completed_appointments,
        pending_appointments=pending_appointments,
        real_revenue=real_revenue,
        pending_revenue=pending_revenue,
        total_revenue=total_revenue,
        chart_series=chart_series
    )