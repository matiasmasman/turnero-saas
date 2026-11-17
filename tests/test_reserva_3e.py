"""Pruebas del sub-bloque 3e: la reserva pública con el catálogo (API). Datos 100 % de prueba.

Usa el fixture `api` de conftest.py: SQLite en memoria y, con E3_TEST_PG_URL, también PostgreSQL.
La prueba de dos reservas simultáneas corre solo en PostgreSQL (SQLite no bloquea filas).
"""
import threading
import time
from datetime import datetime

import pytest

import app.catalog as catalog
from app.models import (Appointment, BarberService, Business, Discount, DiscountPrice, ScheduleException, Service,
                        User, UserRole, WorkingHours)

NOW = datetime(2030, 6, 5, 9, 0)        # miércoles; muy en el futuro: si el código usara la hora real, fallaría
DAY = "2030-06-07"                     # viernes
CLIENT = {"client_name": "Cliente Prueba", "client_phone": "2610000000"}
SLOT_TAKEN = "Ese horario se acaba de ocupar. Elegí otro."
CORTE, BARBA, NAVAJA, COLOR, MASAJE, OTRO_NEGOCIO = 1, 2, 3, 4, 5, 6
PROMO, VIP, SOLO_BARBA, PROMO_OTRO = 1, 2, 3, 4
DUENO, ANA, BETO, CARO, DANI = 1, 2, 3, 4, 5


@pytest.fixture
def shop(api, monkeypatch):
    """Negocio 1:
    - Servicios: Corte $10.000 · Barba $6.000 · Navaja (DESACTIVADO) · Color (nadie lo hace) ·
      Masaje (solo lo hace Caro, que está inactiva). Negocio 2: Corte.
    - Barberos: Dueño hace Barba 20 min (Corte lo dejó de hacer); Ana hace Corte 30 y Barba 15;
      Beto hace Corte 60; Caro (INACTIVA) hace Corte y Masaje; Dani no hace nada.
    - Descuentos: Promo (Corte $8.000, Barba $5.000) · VIP DESACTIVADO (Corte $7.000) · Solo Barba (Barba $4.000).
      Negocio 2: un descuento para su Corte.
    - Todos trabajan todos los días 10:00-13:00 y 17:00-20:00. "Ahora" = NOW (hora del negocio)."""
    monkeypatch.setattr(catalog, "now_local", lambda: NOW)
    db = api.Session()
    db.add(Business(id=2, slug="otro", name="Otro negocio"))
    db.add_all([User(name="Dueño", username="dueno", hashed_password="x", role=UserRole.OWNER),
                User(name="Ana", username="ana", hashed_password="x", role=UserRole.EMPLOYEE),
                User(name="Beto", username="beto", hashed_password="x", role=UserRole.EMPLOYEE),
                User(name="Caro", username="caro", hashed_password="x", role=UserRole.EMPLOYEE, is_active=False),
                User(name="Dani", username="dani", hashed_password="x", role=UserRole.EMPLOYEE)])
    db.flush()
    for name, price, active in [("Corte", 10000, True), ("Barba", 6000, True), ("Navaja", 5000, False),
                                ("Color", 9000, True), ("Masaje", 7000, True)]:
        db.add(Service(business_id=1, name=name, price=price, base_duration=30, active=active))
    db.flush()
    db.add(Service(business_id=2, name="Corte", price=9000, base_duration=30))
    db.flush()
    for emp, sid, duration, active in [(DUENO, BARBA, 20, True), (DUENO, CORTE, 45, False), (ANA, CORTE, 30, True),
                                       (ANA, BARBA, 15, True), (BETO, CORTE, 60, True), (CARO, CORTE, 30, True),
                                       (CARO, MASAJE, 30, True)]:
        db.add(BarberService(business_id=1, employee_id=emp, service_id=sid, duration=duration, active=active))
    for name, active in [("Promo", True), ("VIP", False), ("Solo Barba", True)]:
        db.add(Discount(business_id=1, name=name, active=active))
    db.flush()
    db.add(Discount(business_id=2, name="Promo otro"))
    db.flush()
    for did, sid, price in [(PROMO, CORTE, 8000), (PROMO, BARBA, 5000), (VIP, CORTE, 7000), (SOLO_BARBA, BARBA, 4000)]:
        db.add(DiscountPrice(business_id=1, discount_id=did, service_id=sid, price=price))
    db.add(DiscountPrice(business_id=2, discount_id=PROMO_OTRO, service_id=OTRO_NEGOCIO, price=8000))
    for emp in (DUENO, ANA, BETO, CARO, DANI):
        for dow in range(7):
            db.add(WorkingHours(employee_id=emp, day_of_week=dow, is_working=True,
                                shift1_start=datetime.strptime("10:00", "%H:%M").time(),
                                shift1_end=datetime.strptime("13:00", "%H:%M").time(),
                                shift2_start=datetime.strptime("17:00", "%H:%M").time(),
                                shift2_end=datetime.strptime("20:00", "%H:%M").time()))
    db.commit()
    db.close()


# ---------- Ayudas ----------
def book(api, **fields):
    payload = {**CLIENT, "service_id": CORTE, "date": DAY, "time": "10:00", "employee_id": ANA, **fields}
    return api.client.post("/appointments/", json=payload)


def slots(api, service_id=CORTE, employee_id=None, day=DAY):
    params = {"target_date": day, "service_id": service_id}
    if employee_id is not None:
        params["employee_id"] = employee_id
    return api.client.get("/appointments/available-slots", params=params)


def appointments(api):
    db = api.Session()
    try:
        return [(a.service_id, a.service, a.discount_id, a.discount_name, a.price, a.duration, a.employee_id,
                 a.date, a.time) for a in db.query(Appointment).order_by(Appointment.id)]
    finally:
        db.close()


def add_appt(api, employee_id, time_str, duration=30, status="confirmed", day=DAY):
    db = api.Session()
    db.add(Appointment(client_name="Otro Cliente", client_phone="2610000001", service="Corte", service_id=CORTE,
                       date=day, time=time_str, duration=duration, price=10000, status=status,
                       employee_id=employee_id))
    db.commit()
    db.close()


def grid(*ranges):
    """grid(("10:00", "12:30", 30), ...) → ["10:00", "10:30", ..., "12:30", ...]"""
    out = []
    for start, end, step in ranges:
        t = datetime.strptime(start, "%H:%M")
        while t <= datetime.strptime(end, "%H:%M"):
            out.append(t.strftime("%H:%M"))
            t = t.replace(hour=(t.hour * 60 + t.minute + step) // 60, minute=(t.minute + step) % 60)
    return out


ANA_CORTE = grid(("10:00", "12:30", 30), ("17:00", "19:30", 30))
BETO_CORTE = grid(("10:00", "12:00", 60), ("17:00", "19:00", 60))


# ---------- Catálogo público ----------
def test_catalogo_solo_lo_que_se_puede_reservar_y_sin_datos_de_mas(api, shop):
    resp = api.client.get("/appointments/catalog")
    assert resp.status_code == 200
    assert resp.json() == {
        "services": [
            {"id": CORTE, "name": "Corte", "price": 10000, "barber_ids": [ANA, BETO],
             "discounts": [{"id": PROMO, "name": "Promo", "price": 8000}]},
            {"id": BARBA, "name": "Barba", "price": 6000, "barber_ids": [DUENO, ANA],
             "discounts": [{"id": PROMO, "name": "Promo", "price": 5000},
                           {"id": SOLO_BARBA, "name": "Solo Barba", "price": 4000}]},
        ],
        # Ni Caro (inactiva) ni Dani (no hace nada); sin usuario, rol ni duraciones
        "barbers": [{"id": DUENO, "name": "Dueño"}, {"id": ANA, "name": "Ana"}, {"id": BETO, "name": "Beto"}],
        "days_ahead": 60,
    }


def test_catalogo_no_ofrece_un_descuento_que_despues_se_rechazaria(api, shop):
    db = api.Session()
    db.query(DiscountPrice).filter(DiscountPrice.discount_id == PROMO, DiscountPrice.service_id == CORTE).update(
        {"price": 20000})                                   # más caro que el servicio (solo tocando la base)
    db.commit()
    db.close()
    corte = api.client.get("/appointments/catalog").json()["services"][0]
    assert corte["id"] == CORTE and corte["discounts"] == []
    assert book(api, discount_id=PROMO).status_code == 400


def test_catalogo_vacio(api):
    assert api.client.get("/appointments/catalog").json() == {"services": [], "barbers": [], "days_ahead": 60}


def test_los_endpoints_viejos_de_la_web_publica_ya_no_estan(api, shop):
    for path in ("/appointments/services", "/appointments/barbers"):
        assert api.client.get(path).status_code == 404, path


def test_las_paginas_html_se_revalidan_y_los_js_no(api):
    """Decisión C: con Cache-Control: no-cache, un ?v= nuevo en index.html llega enseguida al navegador."""
    for path in ("/", "/index.html", "/panel.html"):
        resp = api.client.get(path)
        assert resp.status_code == 200 and resp.headers["cache-control"] == "no-cache", path
    assert "cache-control" not in api.client.get("/js/index.js").headers


# ---------- Horarios ----------
def test_horarios_segun_la_duracion_de_cada_barbero(api, shop):
    assert slots(api, employee_id=ANA).json() == ANA_CORTE
    assert slots(api, employee_id=BETO).json() == BETO_CORTE
    assert slots(api).json() == sorted(set(ANA_CORTE) | set(BETO_CORTE))        # "Cualquiera"
    # Barba: Ana 15 min y el Dueño 20 min
    assert slots(api, service_id=BARBA, employee_id=DUENO).json()[:4] == ["10:00", "10:20", "10:40", "11:00"]
    assert slots(api, service_id=BARBA, employee_id=ANA).json()[:4] == ["10:00", "10:15", "10:30", "10:45"]


def test_horarios_respetan_turnos_y_la_duracion_guardada(api, shop):
    add_appt(api, BETO, "10:00:00", duration=60)
    add_appt(api, BETO, "11:00", duration=60, status="cancelled")        # cancelado: no ocupa
    assert slots(api, employee_id=BETO).json()[:2] == ["11:00", "12:00"]


@pytest.mark.parametrize("service_id, employee_id, code, detail", [
    (999, None, 404, "El servicio elegido no existe."),
    (OTRO_NEGOCIO, None, 404, "El servicio elegido no existe."),
    (NAVAJA, None, 400, "El servicio elegido no está disponible."),
    (COLOR, None, 400, "Ningún barbero hace ese servicio."),
    (MASAJE, None, 400, "Ningún barbero hace ese servicio."),
    (CORTE, CARO, 404, "El barbero elegido no existe o no está activo."),
    (CORTE, 999, 404, "El barbero elegido no existe o no está activo."),
    (CORTE, DANI, 400, "El barbero elegido no hace ese servicio."),
    (CORTE, DUENO, 400, "El barbero elegido no hace ese servicio."),    # lo dejó de hacer
])
def test_horarios_con_errores_claros(api, shop, service_id, employee_id, code, detail):
    resp = slots(api, service_id=service_id, employee_id=employee_id)
    assert (resp.status_code, resp.json()["detail"]) == (code, detail)


@pytest.mark.parametrize("bad", ["2030-02-30", "20300607", "2030-6-7", "hoy", "2030-06-07T10:00", "1717000000"])
def test_fecha_mal_escrita_da_422_y_no_500(api, shop, bad):
    resp = slots(api, day=bad)
    assert resp.status_code == 422 and "La fecha no es válida" in resp.json()["detail"]
    resp = book(api, date=bad)
    assert resp.status_code == 422 and "La fecha no es válida" in resp.json()["detail"][0]["msg"]
    assert appointments(api) == []


def test_hoy_no_muestra_horarios_que_ya_pasaron_en_hora_del_negocio(api, shop, monkeypatch):
    monkeypatch.setattr(catalog, "now_local", lambda: datetime(2030, 6, 7, 11, 10))
    assert slots(api, employee_id=ANA).json()[:2] == ["11:30", "12:00"]


def test_dias_pasados_o_demasiado_lejos_no_tienen_horarios(api, shop):
    assert slots(api, day="2030-06-04").json() == []                    # ayer
    assert slots(api, day="2030-08-04").json() != []                    # NOW + 60 días: todavía se puede
    assert slots(api, day="2030-08-05").json() == []                    # NOW + 61 días


# ---------- Reservar ----------
def test_reservar_sin_descuento_guarda_todo_lo_del_catalogo(api, shop):
    resp = book(api, employee_id=BETO)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body.pop("wa_link").startswith("https://wa.me/")
    # Solo lo que muestra la confirmación: sin teléfono, sin id del turno ni del barbero
    assert body == {"employee_name": "Beto", "service": "Corte", "discount_name": None, "date": DAY,
                    "time": "10:00", "price": 10000}
    assert appointments(api) == [(CORTE, "Corte", None, None, 10000, 60, BETO, DAY, "10:00:00")]


def test_reservar_con_descuento_precio_final_y_duracion_del_barbero(api, shop):
    resp = book(api, service_id=BARBA, discount_id=SOLO_BARBA, employee_id=DUENO, time="17:00")
    assert resp.status_code == 201, resp.text
    assert (resp.json()["price"], resp.json()["discount_name"]) == (4000, "Solo Barba")
    assert appointments(api) == [(BARBA, "Barba", SOLO_BARBA, "Solo Barba", 4000, 20, DUENO, DAY, "17:00:00")]


def test_los_nombres_y_el_precio_quedan_congelados_en_el_turno(api, shop):
    assert book(api, discount_id=PROMO).status_code == 201
    db = api.Session()
    db.get(Service, CORTE).name, db.get(Service, CORTE).price = "Corte clásico", 99000
    db.get(Discount, PROMO).name = "Promo nueva"
    db.commit()
    db.close()
    assert appointments(api) == [(CORTE, "Corte", PROMO, "Promo", 8000, 30, ANA, DAY, "10:00:00")]


@pytest.mark.parametrize("extra", [{"price": 1}, {"duration": 5}, {"service": "Corte"}, {"business_id": 2}])
def test_el_navegador_no_puede_mandar_precio_ni_duracion(api, shop, extra):
    assert book(api, **extra).status_code == 422
    assert appointments(api) == []


def test_navegador_con_el_index_js_viejo_recibe_un_mensaje_para_recargar(api, shop):
    old = {**CLIENT, "service": "Corte", "date": DAY, "time": "10:00", "employee_id": ANA}
    resp = api.client.post("/appointments/", json=old)
    assert resp.status_code == 422
    # El index.js viejo muestra detail[0].msg sin el "Value error, "
    assert resp.json()["detail"][0]["msg"] == ("Value error, La página de reservas se actualizó. "
                                               "Recargala para reservar tu turno.")
    assert slots(api).status_code == 200
    assert api.client.get("/appointments/available-slots",
                          params={"target_date": DAY, "service": "Corte"}).status_code == 422


@pytest.mark.parametrize("fields, code, detail", [
    ({"service_id": 999}, 404, "El servicio elegido no existe."),
    ({"service_id": OTRO_NEGOCIO}, 404, "El servicio elegido no existe."),
    ({"service_id": NAVAJA}, 400, "El servicio elegido no está disponible."),
    ({"discount_id": 999}, 404, "El descuento elegido no existe."),
    ({"discount_id": PROMO_OTRO}, 404, "El descuento elegido no existe."),
    ({"discount_id": VIP}, 400, "El descuento elegido no está disponible."),
    ({"discount_id": SOLO_BARBA}, 400, "Ese descuento no aplica a este servicio."),
    ({"employee_id": CARO}, 404, "El barbero elegido no existe o no está activo."),
    ({"employee_id": 999}, 404, "El barbero elegido no existe o no está activo."),
    ({"employee_id": DANI}, 400, "El barbero elegido no hace ese servicio."),
    ({"employee_id": DUENO}, 400, "El barbero elegido no hace ese servicio."),
    ({"service_id": COLOR, "employee_id": None}, 400, "Ningún barbero hace ese servicio."),
    ({"service_id": MASAJE, "employee_id": None}, 400, "Ningún barbero hace ese servicio."),
    ({"time": "13:00"}, 400, "Ese horario está fuera de la jornada del barbero."),
    ({"time": "12:45"}, 400, "Ese horario está fuera de la jornada del barbero."),     # 12:45 + 30 > 13:00
    ({"date": "2030-06-05", "time": "08:30"}, 422, "Ese horario ya pasó. Elegí otro."),
    ({"date": "2030-06-05", "time": "09:00"}, 422, "Ese horario ya pasó. Elegí otro."),  # justo ahora
    ({"date": "2030-06-04"}, 422, "Ese horario ya pasó. Elegí otro."),
    ({"date": "2030-08-05"}, 422, "Se puede reservar hasta 60 días adelante."),
])
def test_rechazos_claros_y_no_guarda_nada(api, shop, fields, code, detail):
    resp = book(api, **fields)
    assert (resp.status_code, resp.json()["detail"]) == (code, detail)
    assert appointments(api) == []


@pytest.mark.parametrize("bad", ["10:00:30", "25:00", "10", "", "10:00 "])
def test_hora_mal_escrita_da_422(api, shop, bad):
    assert book(api, time=bad).status_code == 422


@pytest.mark.parametrize("field, value", [("service_id", "1"), ("service_id", 1.0), ("service_id", True),
                                          ("service_id", 0), ("discount_id", "1"), ("employee_id", "2")])
def test_ids_que_no_son_enteros_dan_422(api, shop, field, value):
    assert book(api, **{field: value}).status_code == 422


def test_hora_fuera_de_la_grilla_multiplo_de_5_se_acepta_con_y_sin_barbero(api, shop):
    """Decisión E3: la misma regla para los dos casos (jornada, sin excepción, sin choque y minutos múltiplos de 5)."""
    assert book(api, time="10:05").status_code == 201
    assert book(api, time="17:05", employee_id=None).status_code == 201


@pytest.mark.parametrize("employee_id", [ANA, None])
@pytest.mark.parametrize("hhmm", ["10:07", "10:01", "17:59"])
def test_minutos_que_no_son_multiplo_de_5_dan_422_con_y_sin_barbero(api, shop, hhmm, employee_id):
    resp = book(api, time=hhmm, employee_id=employee_id)
    assert resp.status_code == 422
    assert resp.json()["detail"][0]["msg"] == ("Value error, El horario tiene que ser en punto o cada 5 minutos "
                                               "(por ejemplo, 10:05).")
    assert appointments(api) == []


def test_la_grilla_solo_ofrece_multiplos_de_5(api, shop):
    """Una excepción hasta las 10:07 y un turno de 10:20 a 10:47: la grilla sigue en 10:10 y 10:50, nunca 10:07/10:47."""
    db = api.Session()
    db.add(ScheduleException(date=DAY, start_time="09:00", end_time="10:07", reason="Trámite", employee_id=ANA))
    db.commit()
    db.close()
    add_appt(api, ANA, "10:40:00", duration=7)
    offered = slots(api, service_id=BARBA, employee_id=ANA).json()
    assert offered[:3] == ["10:10", "10:25", "10:50"]
    assert all(int(t[3:]) % 5 == 0 for t in offered)
    for t in offered[:3]:
        assert book(api, service_id=BARBA, time=t).status_code == 201


def test_una_excepcion_bloquea_sin_contar_el_motivo(api, shop):
    db = api.Session()
    db.add(ScheduleException(date=DAY, start_time="17:00", end_time="18:00", reason="Médico", employee_id=ANA))
    db.commit()
    db.close()
    resp = book(api, time="17:30")
    assert (resp.status_code, resp.json()["detail"]) == (400, "El barbero no atiende en ese horario.")
    assert "17:30" not in slots(api, employee_id=ANA).json()


def test_horario_ocupado_da_409_y_deja_evento_sin_datos_del_cliente(api, shop, events):
    add_appt(api, ANA, "10:00:00")
    resp = book(api, time="10:15")
    assert (resp.status_code, resp.json()["detail"]) == (409, SLOT_TAKEN)
    [event] = [e for e in events if e["event_type"] == "APPOINTMENT_SLOT_TAKEN"]
    assert event["metadata"] == {"date": DAY, "time": "10:15", "service_id": CORTE, "employee_id": ANA}
    assert CLIENT["client_name"] not in str(event) and CLIENT["client_phone"] not in str(event)


def test_cualquiera_ocupado_con_todos_da_409(api, shop, events):
    add_appt(api, ANA, "10:00:00")
    add_appt(api, BETO, "10:00:00", duration=60)
    resp = book(api, employee_id=None)
    assert (resp.status_code, resp.json()["detail"]) == (409, "Ese horario ya no está libre con ningún barbero. "
                                                              "Elegí otro.")
    assert len([e for e in events if e["event_type"] == "APPOINTMENT_SLOT_TAKEN"]) == 1


def test_cualquiera_solo_entre_los_que_hacen_el_servicio_y_con_menos_turnos(api, shop):
    add_appt(api, ANA, "12:00:00")
    resp = book(api, employee_id=None)                   # Ana tiene 1 turno, Beto 0 → Beto
    assert resp.status_code == 201 and resp.json()["employee_name"] == "Beto"
    assert appointments(api)[-1][5:7] == (60, BETO)      # con la duración de Beto
    chosen = set()
    for t in ["17:00", "18:00", "19:00"]:                # empatan 1 a 1, después al azar entre Ana y Beto
        r = book(api, employee_id=None, time=t)
        assert r.status_code == 201
        chosen.add(r.json()["employee_name"])
    assert chosen <= {"Ana", "Beto"}                     # nunca el Dueño, Caro ni Dani


def test_cualquiera_elige_al_que_esta_libre(api, shop):
    add_appt(api, BETO, "10:00:00", duration=60)
    resp = book(api, employee_id=None, time="10:30")
    assert resp.status_code == 201 and resp.json()["employee_name"] == "Ana"


def test_evento_appointment_created_con_el_catalogo(api, shop, events):
    assert book(api, discount_id=PROMO).status_code == 201
    [event] = [e for e in events if e["event_type"] == "APPOINTMENT_CREATED"]
    assert event["metadata"] == {"date": DAY, "time": "10:00:00", "service": "Corte", "service_id": CORTE,
                                 "discount_id": PROMO, "discount_name": "Promo", "duration": 30, "price": 8000,
                                 "assigned_to": ANA}
    assert CLIENT["client_phone"] not in str(event)


@pytest.mark.parametrize("appt_time, duration, slot, slot_duration, overlaps", [
    ("10:00", 30, "10:00", 30, True), ("10:00", 30, "10:30", 30, False), ("10:00", 30, "09:30", 30, False),
    ("10:00", 30, "09:45", 30, True), ("10:00:00", 60, "10:59", 5, True), ("10:00", 60, "11:00", 5, False),
])
def test_has_appointment_overlap_sigue_igual(appt_time, duration, slot, slot_duration, overlaps):
    """La limpieza de `A if False else B` (línea 91 de antes) no cambia el resultado."""
    from app.routers.appointments import has_appointment_overlap, parse_time
    appt = Appointment(time=appt_time, duration=duration)
    assert has_appointment_overlap([appt], parse_time(slot), slot_duration) is overlaps


# ---------- Dos reservas al mismo tiempo (PostgreSQL) ----------
def _at_the_same_time(api, monkeypatch, payloads):
    """Manda los pedidos a la vez. has_appointment_overlap se demora 0,5 s DESPUÉS de mirar los turnos: sin
    bloqueo, los dos ven el horario libre y los dos reservan; con el bloqueo, el segundo espera al primero."""
    if api.dialect != "postgresql":
        pytest.skip("el bloqueo de filas es de PostgreSQL; en SQLite (solo desarrollo) no hay (ver propuesta 3e)")
    import app.routers.appointments as appointments_router
    original = appointments_router.has_appointment_overlap

    def slow(*args, **kwargs):
        result = original(*args, **kwargs)
        time.sleep(0.5)
        return result
    monkeypatch.setattr(appointments_router, "has_appointment_overlap", slow)

    start = threading.Barrier(len(payloads))
    results = []

    def run(fields):
        start.wait()
        results.append(book(api, **fields))
    threads = [threading.Thread(target=run, args=(p,)) for p in payloads]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not any(t.is_alive() for t in threads), "un pedido quedó trabado"
    return sorted(results, key=lambda r: r.status_code)


def test_dos_reservas_a_la_vez_del_mismo_barbero_y_horario_entra_una_sola(api, shop, monkeypatch, events):
    ok, rejected = _at_the_same_time(api, monkeypatch, [{"client_name": "Cliente Uno"},
                                                         {"client_name": "Cliente Dos"}])
    assert (ok.status_code, rejected.status_code) == (201, 409)
    assert rejected.json()["detail"] == SLOT_TAKEN
    assert len(appointments(api)) == 1
    assert len([e for e in events if e["event_type"] == "APPOINTMENT_SLOT_TAKEN"]) == 1


def test_dos_cualquiera_a_la_vez_con_un_solo_barbero_libre_entra_una_sola(api, shop, monkeypatch):
    add_appt(api, BETO, "10:00:00", duration=60)        # queda libre solo Ana
    ok, rejected = _at_the_same_time(api, monkeypatch, [{"employee_id": None}, {"employee_id": None}])
    assert (ok.status_code, rejected.status_code) == (201, 409)
    assert [a[6] for a in appointments(api)] == [BETO, ANA]


def test_un_barbero_y_cualquiera_a_la_vez_no_se_traban(api, shop, monkeypatch):
    """Uno bloquea solo a Ana; el otro, a Ana y Beto (en orden de id): ninguno espera para siempre.
    Ana ya tiene un turno ese día, así que "Cualquiera" elige a Beto y entran los dos."""
    add_appt(api, ANA, "12:00:00")
    ok1, ok2 = _at_the_same_time(api, monkeypatch, [{"employee_id": ANA}, {"employee_id": None}])
    assert (ok1.status_code, ok2.status_code) == (201, 201)
    assert sorted(a[6] for a in appointments(api)) == [ANA, ANA, BETO]
