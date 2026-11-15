"""
API del catálogo (Entrega 3b): servicios y descuentos. SOLO el dueño (get_owner).

- business_id nunca viene del navegador: sale de current_business_id(usuario).
- Nada se borra: desactivar = active false (y se puede reactivar).
- Cada cambio queda en security.log con actor_id y valores viejos/nuevos (sin datos de clientes).
"""
from typing import Annotated, List

from fastapi import APIRouter, Depends, HTTPException, Path, Request, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.catalog import (current_business_id, future_appointments_for_discount,
                         future_appointments_for_service, name_taken)
from app.core.telemetry import sec_logger
from app.database import get_db
from app.dependencies import get_owner
from app.models import Discount, DiscountPrice, Service, User
from app.schemas import (DiscountCreate, DiscountOut, DiscountPriceOut, DiscountStatusOut, DiscountUpdate,
                         ServiceCreate, ServiceOut, ServiceStatusOut, ServiceUpdate)

router = APIRouter(prefix="/admin/catalog", tags=["Catálogo (solo dueño)"])

# Ids de la URL: enteros de 1 a 2^31-1 (más grande, PostgreSQL daría error 500 en vez de 404)
ItemId = Annotated[int, Path(ge=1, le=2_147_483_647)]


def _ars(n: int) -> str:
    return "$" + f"{n:,}".replace(",", ".")


def _log(request: Request, actor: User, event_type: str, message: str, metadata: dict):
    sec_logger.info(message, extra={
        "event_type": event_type,
        "request_id": getattr(request.state, "request_id", None),
        "ip_address": getattr(request.state, "client_ip", None),
        "actor_id": actor.id,
        "metadata": metadata,
    })


def _commit_or_conflict(db: Session, what: str, name: str):
    """Commit. Si dos pedidos crean el mismo nombre a la vez, el índice de la base lo frena acá."""
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, f"Ya existe {what} llamado “{name}”.")


# ==========================================
# SERVICIOS
# ==========================================
def _get_service(db: Session, business_id: int, service_id: int) -> Service:
    service = db.query(Service).filter(Service.id == service_id, Service.business_id == business_id).first()
    if service is None:   # también si es de otro negocio: no se dice que existe
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Servicio no encontrado.")
    return service


def _service_snapshot(s: Service) -> dict:
    return {"name": s.name, "price": s.price, "base_duration": s.base_duration}


@router.get("/services", response_model=List[ServiceOut])
def list_services(db: Session = Depends(get_db), owner: User = Depends(get_owner)):
    """Todos los servicios del negocio, activos e inactivos."""
    return db.query(Service).filter(Service.business_id == current_business_id(owner)).order_by(Service.id).all()


@router.post("/services", response_model=ServiceOut, status_code=status.HTTP_201_CREATED)
def create_service(payload: ServiceCreate, request: Request,
                   db: Session = Depends(get_db), owner: User = Depends(get_owner)):
    business_id = current_business_id(owner)
    if name_taken(db, Service, business_id, payload.name):
        raise HTTPException(status.HTTP_409_CONFLICT, f"Ya existe un servicio llamado “{payload.name}”.")
    service = Service(business_id=business_id, name=payload.name, price=payload.price,
                      base_duration=payload.base_duration, active=True)
    db.add(service)
    _commit_or_conflict(db, "un servicio", payload.name)
    db.refresh(service)
    _log(request, owner, "SERVICE_CREATED", f"Servicio {service.id} creado por {owner.username}",
         {"business_id": business_id, "service_id": service.id, "new": _service_snapshot(service)})
    return service


@router.patch("/services/{service_id}", response_model=ServiceOut)
def update_service(service_id: ItemId, payload: ServiceUpdate, request: Request,
                   db: Session = Depends(get_db), owner: User = Depends(get_owner)):
    business_id = current_business_id(owner)
    service = _get_service(db, business_id, service_id)
    changes = {f: getattr(payload, f) for f in payload.model_fields_set if getattr(payload, f) != getattr(service, f)}
    if not changes:
        return service   # nada distinto: no se toca la base ni el log

    if "name" in changes and name_taken(db, Service, business_id, changes["name"], exclude_id=service.id):
        raise HTTPException(status.HTTP_409_CONFLICT, f"Ya existe un servicio llamado “{changes['name']}”.")
    if "price" in changes:
        # Ningún precio con descuento puede quedar por encima del precio del servicio
        too_high = (db.query(Discount.name, DiscountPrice.price)
                    .join(DiscountPrice, DiscountPrice.discount_id == Discount.id)
                    .filter(DiscountPrice.business_id == business_id, DiscountPrice.service_id == service.id,
                            DiscountPrice.price > changes["price"])
                    .order_by(Discount.name).all())
        if too_high:
            lista = ", ".join(f"{n} ({_ars(p)})" for n, p in too_high)
            raise HTTPException(status.HTTP_409_CONFLICT,
                                f"No se puede poner {_ars(changes['price'])}: estos descuentos tienen un precio "
                                f"mayor para este servicio: {lista}. Bajá primero esos precios.")

    old = {f: getattr(service, f) for f in changes}
    for f, v in changes.items():
        setattr(service, f, v)
    _commit_or_conflict(db, "un servicio", service.name)
    db.refresh(service)
    _log(request, owner, "SERVICE_UPDATED", f"Servicio {service.id} modificado por {owner.username}",
         {"business_id": business_id, "service_id": service.id, "old": old, "new": changes})
    return service


def _set_service_active(service_id: int, active: bool, request: Request, db: Session, owner: User):
    business_id = current_business_id(owner)
    service = _get_service(db, business_id, service_id)
    changed = service.active != active
    if changed:
        service.active = active
        db.commit()
        db.refresh(service)
    future = future_appointments_for_service(db, service)
    if changed:
        event = "SERVICE_REACTIVATED" if active else "SERVICE_DEACTIVATED"
        verb = "reactivado" if active else "desactivado"
        _log(request, owner, event, f"Servicio {service.id} {verb} por {owner.username}",
             {"business_id": business_id, "service_id": service.id, "name": service.name,
              "old": {"active": not active}, "new": {"active": active}, "future_appointments": future})
    return ServiceStatusOut(service=ServiceOut.model_validate(service), changed=changed, future_appointments=future)


@router.post("/services/{service_id}/deactivate", response_model=ServiceStatusOut)
def deactivate_service(service_id: ItemId, request: Request,
                       db: Session = Depends(get_db), owner: User = Depends(get_owner)):
    """Deja de ofrecerse. Los turnos ya reservados no cambian; la respuesta dice cuántos futuros lo usan."""
    return _set_service_active(service_id, False, request, db, owner)


@router.post("/services/{service_id}/reactivate", response_model=ServiceStatusOut)
def reactivate_service(service_id: ItemId, request: Request,
                       db: Session = Depends(get_db), owner: User = Depends(get_owner)):
    return _set_service_active(service_id, True, request, db, owner)


# ==========================================
# DESCUENTOS
# ==========================================
def _get_discount(db: Session, business_id: int, discount_id: int) -> Discount:
    discount = db.query(Discount).filter(Discount.id == discount_id, Discount.business_id == business_id).first()
    if discount is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Descuento no encontrado.")
    return discount


def _price_rows(db: Session, business_id: int, discount_id: int):
    return (db.query(DiscountPrice, Service)
            .join(Service, Service.id == DiscountPrice.service_id)
            .filter(DiscountPrice.business_id == business_id, DiscountPrice.discount_id == discount_id)
            .order_by(Service.id).all())


def _discount_out(db: Session, business_id: int, d: Discount) -> DiscountOut:
    prices = [DiscountPriceOut(service_id=s.id, service_name=s.name, service_active=s.active, price=dp.price)
              for dp, s in _price_rows(db, business_id, d.id)]
    return DiscountOut(id=d.id, name=d.name, active=d.active, prices=prices)


def _prices_for_log(db: Session, business_id: int, discount_id: int) -> list:
    return [{"service_id": dp.service_id, "price": dp.price} for dp, _ in _price_rows(db, business_id, discount_id)]


def _check_prices(db: Session, business_id: int, discount_name: str, prices) -> dict:
    """Valida la lista de precios contra los servicios del negocio. Devuelve {service_id: precio}."""
    wanted = {p.service_id: p.price for p in prices}
    services = {s.id: s for s in db.query(Service).filter(Service.business_id == business_id,
                                                         Service.id.in_(wanted)).all()}
    for service_id, price in wanted.items():
        s = services.get(service_id)
        if s is None:   # no existe o es de otro negocio: mismo mensaje
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"El servicio {service_id} no existe.")
        if not s.active:
            raise HTTPException(status.HTTP_400_BAD_REQUEST,
                                f"El servicio “{s.name}” está desactivado: no se le puede poner precio de descuento.")
        if price > s.price:
            raise HTTPException(status.HTTP_400_BAD_REQUEST,
                                f"El precio de “{discount_name}” para “{s.name}” ({_ars(price)}) no puede ser "
                                f"mayor al precio del servicio ({_ars(s.price)}).")
    return wanted


@router.get("/discounts", response_model=List[DiscountOut])
def list_discounts(db: Session = Depends(get_db), owner: User = Depends(get_owner)):
    """Todos los descuentos del negocio (activos e inactivos), cada uno con sus precios por servicio."""
    business_id = current_business_id(owner)
    discounts = db.query(Discount).filter(Discount.business_id == business_id).order_by(Discount.id).all()
    return [_discount_out(db, business_id, d) for d in discounts]


@router.post("/discounts", response_model=DiscountOut, status_code=status.HTTP_201_CREATED)
def create_discount(payload: DiscountCreate, request: Request,
                    db: Session = Depends(get_db), owner: User = Depends(get_owner)):
    business_id = current_business_id(owner)
    if name_taken(db, Discount, business_id, payload.name):
        raise HTTPException(status.HTTP_409_CONFLICT, f"Ya existe un descuento llamado “{payload.name}”.")
    wanted = _check_prices(db, business_id, payload.name, payload.prices)
    discount = Discount(business_id=business_id, name=payload.name, active=True)
    db.add(discount)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, f"Ya existe un descuento llamado “{payload.name}”.")
    for service_id, price in wanted.items():
        db.add(DiscountPrice(business_id=business_id, discount_id=discount.id, service_id=service_id, price=price))
    _commit_or_conflict(db, "un descuento", payload.name)
    db.refresh(discount)
    _log(request, owner, "DISCOUNT_CREATED", f"Descuento {discount.id} creado por {owner.username}",
         {"business_id": business_id, "discount_id": discount.id,
          "new": {"name": discount.name, "prices": _prices_for_log(db, business_id, discount.id)}})
    return _discount_out(db, business_id, discount)


@router.patch("/discounts/{discount_id}", response_model=DiscountOut)
def update_discount(discount_id: ItemId, payload: DiscountUpdate, request: Request,
                    db: Session = Depends(get_db), owner: User = Depends(get_owner)):
    """name: renombra. prices: REEMPLAZA la lista de precios de los servicios activos (los que no vengan
    dejan de tener descuento). Los precios de servicios desactivados se conservan sin tocar."""
    business_id = current_business_id(owner)
    discount = _get_discount(db, business_id, discount_id)
    old, new = {}, {}

    if "name" in payload.model_fields_set and payload.name != discount.name:
        if name_taken(db, Discount, business_id, payload.name, exclude_id=discount.id):
            raise HTTPException(status.HTTP_409_CONFLICT, f"Ya existe un descuento llamado “{payload.name}”.")
        old["name"], new["name"] = discount.name, payload.name
        discount.name = payload.name

    if "prices" in payload.model_fields_set:
        wanted = _check_prices(db, business_id, discount.name, payload.prices)
        before = _prices_for_log(db, business_id, discount.id)
        current = {dp.service_id: dp for dp, s in _price_rows(db, business_id, discount.id) if s.active}
        for service_id, dp in current.items():
            if service_id not in wanted:
                db.delete(dp)
        for service_id, price in wanted.items():
            if service_id in current:
                current[service_id].price = price
            else:
                db.add(DiscountPrice(business_id=business_id, discount_id=discount.id,
                                     service_id=service_id, price=price))
        db.flush()
        after = _prices_for_log(db, business_id, discount.id)
        if after != before:
            old["prices"], new["prices"] = before, after

    if not new:
        db.rollback()
        return _discount_out(db, business_id, discount)
    _commit_or_conflict(db, "un descuento", discount.name)
    db.refresh(discount)
    _log(request, owner, "DISCOUNT_UPDATED", f"Descuento {discount.id} modificado por {owner.username}",
         {"business_id": business_id, "discount_id": discount.id, "old": old, "new": new})
    return _discount_out(db, business_id, discount)


def _set_discount_active(discount_id: int, active: bool, request: Request, db: Session, owner: User):
    business_id = current_business_id(owner)
    discount = _get_discount(db, business_id, discount_id)
    changed = discount.active != active
    if changed:
        discount.active = active
        db.commit()
        db.refresh(discount)
    future = future_appointments_for_discount(db, discount)
    if changed:
        event = "DISCOUNT_REACTIVATED" if active else "DISCOUNT_DEACTIVATED"
        verb = "reactivado" if active else "desactivado"
        _log(request, owner, event, f"Descuento {discount.id} {verb} por {owner.username}",
             {"business_id": business_id, "discount_id": discount.id, "name": discount.name,
              "old": {"active": not active}, "new": {"active": active}, "future_appointments": future})
    return DiscountStatusOut(discount=_discount_out(db, business_id, discount), changed=changed,
                             future_appointments=future)


@router.post("/discounts/{discount_id}/deactivate", response_model=DiscountStatusOut)
def deactivate_discount(discount_id: ItemId, request: Request,
                        db: Session = Depends(get_db), owner: User = Depends(get_owner)):
    return _set_discount_active(discount_id, False, request, db, owner)


@router.post("/discounts/{discount_id}/reactivate", response_model=DiscountStatusOut)
def reactivate_discount(discount_id: ItemId, request: Request,
                        db: Session = Depends(get_db), owner: User = Depends(get_owner)):
    return _set_discount_active(discount_id, True, request, db, owner)
