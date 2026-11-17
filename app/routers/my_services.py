"""
"Mis servicios" (Entrega 3c): cada barbero (EMPLOYEE y también el OWNER) elige qué servicios hace
y cuánto tarda en cada uno. No toca precios.

- SIEMPRE sobre el usuario logueado (current_user.id): no hay employee_id en ninguna entrada.
- Solo servicios ACTIVOS del negocio. Las filas de servicios desactivados no se tocan.
- Dejar de hacer un servicio = barber_services.active false (se recuerda la duración).
- La reserva pública (3e) usa estas filas: quién hace cada servicio y cuánto tarda.
"""
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.catalog import current_business_id, future_appointments_for_service
from app.core.telemetry import sec_logger
from app.database import get_db
from app.dependencies import get_staff
from app.models import BarberService, Service, User
from app.schemas import MyServiceOut, MyServicesOut, MyServicesSaveOut, MyServicesUpdate

router = APIRouter(prefix="/admin", tags=["Mis servicios (cada barbero)"])
SAVED_AT_THE_SAME_TIME = "Se guardó otro cambio al mismo tiempo. Probá de nuevo."


def _active_services(db: Session, business_id: int):
    return (db.query(Service)
            .filter(Service.business_id == business_id, Service.active.is_(True))
            .order_by(Service.id).all())


def _my_rows(db: Session, business_id: int, employee_id: int) -> dict:
    rows = db.query(BarberService).filter(BarberService.business_id == business_id,
                                          BarberService.employee_id == employee_id).all()
    return {r.service_id: r for r in rows}


def _state(rows: dict, service_ids) -> dict:
    """{service_id: (lo hago, duración o None)} — para comparar antes/después."""
    out = {}
    for sid in service_ids:
        r = rows.get(sid)
        out[sid] = (bool(r.active), r.duration) if r is not None else (False, None)
    return out


def _list(db: Session, business_id: int, user: User) -> list:
    rows = _my_rows(db, business_id, user.id)
    result = []
    for s in _active_services(db, business_id):
        r = rows.get(s.id)
        result.append(MyServiceOut(
            service_id=s.id, name=s.name, base_duration=s.base_duration,
            does=bool(r is not None and r.active),
            duration=r.duration if r is not None else s.base_duration,
            future_appointments=future_appointments_for_service(db, s, employee_id=user.id),
        ))
    return result


@router.get("/my-services", response_model=MyServicesOut)
def get_my_services(db: Session = Depends(get_db), current_user: User = Depends(get_staff)):
    """Todos los servicios activos del negocio, marcando cuáles hago yo y con qué duración."""
    return MyServicesOut(services=_list(db, current_business_id(current_user), current_user))


@router.put("/my-services", response_model=MyServicesSaveOut)
def put_my_services(payload: MyServicesUpdate, request: Request,
                    db: Session = Depends(get_db), current_user: User = Depends(get_staff)):
    """Reemplaza la lista: los servicios que vienen = los que hago (con su duración);
    los activos que no vienen = los dejo de hacer. Los desactivados no se tocan."""
    business_id = current_business_id(current_user)
    active = {s.id: s for s in _active_services(db, business_id)}
    wanted = {item.service_id: item.duration for item in payload.services}

    # Todo se valida antes de cambiar nada
    for sid in wanted:
        if sid not in active:
            exists = db.query(Service).filter(Service.id == sid, Service.business_id == business_id).first()
            if exists is not None:   # del negocio pero desactivado
                raise HTTPException(status.HTTP_400_BAD_REQUEST,
                                    f"El servicio “{exists.name}” está desactivado: no se puede elegir.")
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"El servicio {sid} no existe.")  # o es de otro negocio

    rows = _my_rows(db, business_id, current_user.id)
    before = _state(rows, active)
    for sid in active:
        r = rows.get(sid)
        if sid in wanted:
            if r is None:
                db.add(BarberService(business_id=business_id, employee_id=current_user.id,
                                     service_id=sid, duration=wanted[sid], active=True))
            else:
                if not r.active:
                    r.active = True
                if r.duration != wanted[sid]:
                    r.duration = wanted[sid]
        elif r is not None and r.active:
            r.active = False                       # la duración queda guardada
    try:
        db.flush()                                 # acá sale el INSERT: si el otro "Guardar" ya insertó, choca
    except IntegrityError:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, SAVED_AT_THE_SAME_TIME)
    after = _state(_my_rows(db, business_id, current_user.id), active)

    changed_ids = [sid for sid in active if before[sid] != after[sid]]
    warnings = []
    if changed_ids:
        try:
            db.commit()
        except IntegrityError:                     # dos "Guardar" al mismo tiempo
            db.rollback()
            raise HTTPException(status.HTTP_409_CONFLICT, SAVED_AT_THE_SAME_TIME)
        snap = lambda st, sid: {"service_id": sid, "does": st[sid][0], "duration": st[sid][1]}  # noqa: E731
        sec_logger.info(f"Servicios del barbero {current_user.id} actualizados por {current_user.username}", extra={
            "event_type": "BARBER_SERVICES_UPDATED",
            "request_id": getattr(request.state, "request_id", None),
            "ip_address": getattr(request.state, "client_ip", None),
            "actor_id": current_user.id,
            "metadata": {"business_id": business_id, "employee_id": current_user.id,
                         "old": [snap(before, sid) for sid in changed_ids],
                         "new": [snap(after, sid) for sid in changed_ids]},
        })
    else:
        db.rollback()

    services = _list(db, business_id, current_user)
    for s in services:
        if before[s.service_id][0] and not s.does and s.future_appointments:
            n = s.future_appointments
            plural = "s" if n != 1 else ""
            warnings.append(f"Dejaste de hacer “{s.name}”, pero tenés {n} turno{plural} futuro{plural} "
                            "con ese servicio: siguen reservados.")
    if active and not any(s.does for s in services):
        warnings.append("No tenés ningún servicio marcado: no vas a aparecer para que te reserven.")
    return MyServicesSaveOut(services=services, changed=bool(changed_ids), warnings=warnings)
