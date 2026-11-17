"""Pruebas del sub-bloque 3c: "Mis servicios" (API GET/PUT /admin/my-services). Datos 100 % de prueba.

Usa el fixture `api` de conftest.py (TestClient sobre SQLite en memoria ya migrada, sin WhatsApp).
"""
from datetime import datetime

import pytest

import app.catalog as catalog
from app.models import Appointment, BarberService, Business, Service, User, UserRole

URL = "/admin/my-services"
CLIENT_NAME, CLIENT_PHONE = "Cliente Secreto", "2619999999"
NOW = datetime(2026, 11, 15, 12, 0, 0)


# ---------- Datos ----------
@pytest.fixture
def staff(api, monkeypatch):
    """Negocio 1: Corte (1) base 30, Barba (2) base 15, Corte + Barba (3) base 45, Navaja (4) DESACTIVADO.
    Negocio 2: Corte (5).
    Dueño (1): Corte 35 y Barba 15 los hace; Corte + Barba 50 lo dejó de hacer; Navaja 20 (servicio desactivado).
    Empleado A (2): Corte 45. Empleado B (3): nuevo, sin filas. Empleado C (4): inactivo."""
    from app.core.security import create_access_token
    monkeypatch.setattr(catalog, "now_local", lambda: NOW)
    db = api.Session()
    db.add(Business(id=2, slug="otro", name="Otro negocio"))
    users = [User(name="Dueño", username="dueno", hashed_password="x", role=UserRole.OWNER),
             User(name="Empleado A", username="emp_a", hashed_password="x", role=UserRole.EMPLOYEE),
             User(name="Empleado B", username="emp_b", hashed_password="x", role=UserRole.EMPLOYEE),
             User(name="Empleado C", username="emp_c", hashed_password="x", role=UserRole.EMPLOYEE, is_active=False)]
    db.add_all(users)
    db.flush()
    for name, base, active in [("Corte", 30, True), ("Barba", 15, True), ("Corte + Barba", 45, True),
                               ("Navaja", 20, False)]:
        db.add(Service(business_id=1, name=name, price=1000, base_duration=base, active=active))
    db.flush()
    db.add(Service(business_id=2, name="Corte", price=1000, base_duration=30))
    db.flush()
    for emp, sid, dur, active in [(1, 1, 35, True), (1, 2, 15, True), (1, 3, 50, False), (1, 4, 20, True),
                                  (2, 1, 45, True)]:
        db.add(BarberService(business_id=1, employee_id=emp, service_id=sid, duration=dur, active=active))
    db.commit()
    tokens = {u.username: {"Authorization": f"Bearer {create_access_token({'sub': str(u.id)})}"} for u in users}
    db.close()
    return tokens


def rows(api):
    """Todas las filas de barber_services: {(barbero, servicio): (activa, duración)}."""
    db = api.Session()
    try:
        return {(r.employee_id, r.service_id): (bool(r.active), r.duration) for r in db.query(BarberService)}
    finally:
        db.close()


def add_appt(api, employee_id, date, time, service="Corte", service_id=1, status="confirmed"):
    db = api.Session()
    db.add(Appointment(client_name=CLIENT_NAME, client_phone=CLIENT_PHONE, service=service, date=date, time=time,
                       duration=30, price=15000, status=status, employee_id=employee_id, service_id=service_id))
    db.commit()
    db.close()


def my_events(events):
    return [e for e in events if e["event_type"] == "BARBER_SERVICES_UPDATED"]


def view(resp):
    return [(s["service_id"], s["name"], s["does"], s["duration"]) for s in resp.json()["services"]]


def put(api, who, services):
    return api.client.put(URL, headers=who, json={"services": services})


# ---------- Permisos y "solo lo mío" ----------
@pytest.mark.parametrize("method, body", [("GET", None), ("PUT", {"services": []})])
@pytest.mark.parametrize("who", ["sin_token", "token_basura", "inactivo"])
def test_sin_token_valido_401(api, staff, events, method, body, who):
    headers = {"sin_token": {}, "token_basura": {"Authorization": "Bearer basura"}, "inactivo": staff["emp_c"]}[who]
    before = rows(api)
    assert api.client.request(method, URL, json=body, headers=headers).status_code == 401
    assert rows(api) == before
    assert my_events(events) == []


def test_owner_ve_lo_suyo(api, staff):
    resp = api.client.get(URL, headers=staff["dueno"])
    assert resp.status_code == 200
    # Solo servicios ACTIVOS del negocio 1 (ni Navaja, desactivado, ni el Corte del negocio 2)
    assert view(resp) == [(1, "Corte", True, 35), (2, "Barba", True, 15), (3, "Corte + Barba", False, 50)]


def test_empleado_ve_lo_suyo(api, staff):
    resp = api.client.get(URL, headers=staff["emp_a"])
    assert view(resp) == [(1, "Corte", True, 45), (2, "Barba", False, 15), (3, "Corte + Barba", False, 45)]


def test_barbero_nuevo_ve_todo_sin_marcar_con_la_duracion_base(api, staff):
    resp = api.client.get(URL, headers=staff["emp_b"])
    assert resp.json()["services"] == [
        {"service_id": 1, "name": "Corte", "base_duration": 30, "does": False, "duration": 30, "future_appointments": 0},
        {"service_id": 2, "name": "Barba", "base_duration": 15, "does": False, "duration": 15, "future_appointments": 0},
        {"service_id": 3, "name": "Corte + Barba", "base_duration": 45, "does": False, "duration": 45,
         "future_appointments": 0}]


def test_employee_id_en_la_url_no_sirve_para_ver_lo_de_otro(api, staff):
    resp = api.client.get(URL + "?employee_id=1", headers=staff["emp_b"])
    assert [s["does"] for s in resp.json()["services"]] == [False, False, False]


@pytest.mark.parametrize("who", ["emp_a", "dueno"])
def test_cada_uno_cambia_solo_lo_suyo(api, staff, who):
    me = {"dueno": 1, "emp_a": 2}[who]
    before = rows(api)
    assert put(api, staff[who], [{"service_id": 2, "duration": 20}]).status_code == 200
    after = rows(api)
    assert {k: v for k, v in after.items() if k[0] != me} == {k: v for k, v in before.items() if k[0] != me}
    assert after[(me, 2)] == (True, 20)


def test_barbero_nuevo_elige_y_se_crean_sus_filas(api, staff, events):
    resp = put(api, staff["emp_b"], [{"service_id": 1, "duration": 40}, {"service_id": 3, "duration": 60}])
    assert resp.status_code == 200 and resp.json()["changed"] is True
    assert view(resp) == [(1, "Corte", True, 40), (2, "Barba", False, 15), (3, "Corte + Barba", True, 60)]
    assert {k: v for k, v in rows(api).items() if k[0] == 3} == {(3, 1): (True, 40), (3, 3): (True, 60)}


# ---------- Entrada ----------
@pytest.mark.parametrize("body", [
    {"services": [], "employee_id": 1},
    {"services": [], "business_id": 2},
    {"services": [{"service_id": 1, "duration": 30, "employee_id": 1}]},
    {"services": [{"service_id": 1, "duration": 30, "business_id": 2}]},
    {"services": [{"service_id": 1, "duration": 30, "active": False}]},
    {"services": [{"service_id": 1, "duration": 30, "does": True}]},
])
def test_campos_de_mas_dan_422(api, staff, events, body):
    before = rows(api)
    resp = api.client.put(URL, headers=staff["emp_a"], json=body)
    assert resp.status_code == 422
    assert "Extra inputs are not permitted" in str(resp.json()["detail"])
    assert rows(api) == before
    assert my_events(events) == []


@pytest.mark.parametrize("body", [
    {}, {"services": None}, {"services": "1"}, [{"service_id": 1, "duration": 30}],
    {"services": [{"service_id": 1}]}, {"services": [{"duration": 30}]},
    {"services": [{"service_id": 1, "duration": 30}, {"service_id": 1, "duration": 35}]},   # repetido
    {"services": [{"service_id": "1", "duration": 30}]}, {"services": [{"service_id": 0, "duration": 30}]},
    {"services": [{"service_id": 99999999999, "duration": 30}]},
])
def test_formato_invalido_da_422(api, staff, body):
    before = rows(api)
    assert api.client.put(URL, headers=staff["emp_a"], json=body).status_code == 422
    assert rows(api) == before


@pytest.mark.parametrize("duration", [4, 7, 181, 185, 0, -5, "30", 30.0, True, None])
def test_duracion_invalida_da_422(api, staff, events, duration):
    before = rows(api)
    assert put(api, staff["emp_a"], [{"service_id": 1, "duration": duration}]).status_code == 422
    assert rows(api) == before
    assert my_events(events) == []


@pytest.mark.parametrize("duration", [5, 180])
def test_duracion_en_los_limites(api, staff, duration):
    assert put(api, staff["emp_a"], [{"service_id": 1, "duration": duration}]).status_code == 200
    assert rows(api)[(2, 1)] == (True, duration)


@pytest.mark.parametrize("service_id, text", [
    (999, "El servicio 999 no existe."),
    (5, "El servicio 5 no existe."),                       # de otro negocio: mismo mensaje
    (4, "“Navaja” está desactivado"),
])
def test_servicio_inexistente_ajeno_o_desactivado_da_400_sin_cambios(api, staff, events, service_id, text):
    before = rows(api)
    resp = put(api, staff["dueno"], [{"service_id": 1, "duration": 40}, {"service_id": service_id, "duration": 20}])
    assert resp.status_code == 400
    assert text in resp.json()["detail"]
    assert rows(api) == before                             # tampoco se guardó el Corte 40
    assert my_events(events) == []


# ---------- Marcar / desmarcar ----------
def test_desmarcar_conserva_la_duracion_y_volver_a_marcar_la_recupera(api, staff):
    resp = put(api, staff["dueno"], [{"service_id": 2, "duration": 15}])          # deja de hacer Corte
    assert rows(api)[(1, 1)] == (False, 35)                                       # la fila queda, con 35
    assert view(resp)[0] == (1, "Corte", False, 35)                                # y la pantalla propone 35
    put(api, staff["dueno"], [{"service_id": 1, "duration": 35}, {"service_id": 2, "duration": 15}])
    assert rows(api)[(1, 1)] == (True, 35)


def test_volver_a_marcar_con_otra_duracion(api, staff):
    put(api, staff["dueno"], [{"service_id": 1, "duration": 35}, {"service_id": 2, "duration": 15},
                              {"service_id": 3, "duration": 55}])
    assert rows(api)[(1, 3)] == (True, 55)


def test_las_filas_de_servicios_desactivados_no_se_tocan(api, staff):
    put(api, staff["dueno"], [])                                                  # desmarca todo lo activo
    r = rows(api)
    assert r[(1, 4)] == (True, 20)                                                # Navaja: intacta
    assert (r[(1, 1)], r[(1, 2)], r[(1, 3)]) == ((False, 35), (False, 15), (False, 50))
    # Si el dueño reactiva Navaja en el catálogo, vuelve a aparecer como la tenía
    db = api.Session()
    db.get(Service, 4).active = True
    db.commit()
    db.close()
    resp = api.client.get(URL, headers=staff["dueno"])
    assert (4, "Navaja", True, 20) in view(resp)


def test_desmarcar_todos_se_permite_con_aviso(api, staff):
    resp = put(api, staff["emp_a"], [])
    assert resp.status_code == 200 and resp.json()["changed"] is True
    assert resp.json()["warnings"] == ["No tenés ningún servicio marcado: cuando se active la reserva nueva, "
                                       "no vas a aparecer para que te reserven."]
    assert rows(api)[(2, 1)] == (False, 45)


def test_desmarcar_con_turnos_futuros_se_permite_y_avisa_cuantos(api, staff):
    add_appt(api, 1, "2026-11-20", "15:00:00")                      # mío, futuro → cuenta
    add_appt(api, 1, "2026-11-15", "12:00")                         # mío, justo ahora → cuenta
    add_appt(api, 1, "2026-11-22", "15:00:00", service_id=None)     # mío, sin service_id (por nombre) → cuenta
    add_appt(api, 1, "2026-11-10", "15:00:00")                      # mío, pasado → no
    add_appt(api, 1, "2026-11-21", "15:00:00", status="cancelled")  # mío, cancelado → no
    add_appt(api, 2, "2026-11-20", "16:00:00")                      # de otro barbero → no
    assert api.client.get(URL, headers=staff["dueno"]).json()["services"][0]["future_appointments"] == 3

    resp = put(api, staff["dueno"], [{"service_id": 2, "duration": 15}])
    assert resp.status_code == 200
    assert resp.json()["warnings"] == ["Dejaste de hacer “Corte”, pero tenés 3 turnos futuros con ese servicio: "
                                       "siguen reservados."]
    db = api.Session()
    assert db.query(Appointment).filter(Appointment.status == "confirmed").count() == 5   # los turnos no cambian
    db.close()


def test_aviso_en_singular(api, staff):
    add_appt(api, 1, "2026-11-20", "15:00:00")
    resp = put(api, staff["dueno"], [{"service_id": 2, "duration": 15}])
    assert resp.json()["warnings"] == ["Dejaste de hacer “Corte”, pero tenés 1 turno futuro con ese servicio: "
                                       "siguen reservados."]


def test_catalogo_sin_servicios_activos(api, staff):
    db = api.Session()
    for s in db.query(Service).filter(Service.business_id == 1):
        s.active = False
    db.commit()
    db.close()
    before = rows(api)
    assert api.client.get(URL, headers=staff["emp_a"]).json() == {"services": []}
    resp = put(api, staff["emp_a"], [])
    assert resp.json() == {"services": [], "changed": False, "warnings": []}
    assert rows(api) == before


# ---------- Log ----------
def test_evento_con_actor_y_solo_lo_que_cambio(api, staff, events):
    add_appt(api, 1, "2026-11-20", "15:00:00")
    resp = put(api, staff["dueno"], [{"service_id": 1, "duration": 40},     # cambia la duración
                                     {"service_id": 2, "duration": 15},     # igual
                                     {"service_id": 3, "duration": 50}])    # vuelve a marcar
    assert resp.status_code == 200
    [ev] = my_events(events)
    assert ev["actor_id"] == 1
    assert ev["metadata"] == {
        "business_id": 1, "employee_id": 1,
        "old": [{"service_id": 1, "does": True, "duration": 35}, {"service_id": 3, "does": False, "duration": 50}],
        "new": [{"service_id": 1, "does": True, "duration": 40}, {"service_id": 3, "does": True, "duration": 50}]}
    assert CLIENT_NAME not in repr(events) and CLIENT_PHONE not in repr(events)


def test_evento_de_barbero_nuevo_dice_que_antes_no_tenia_fila(api, staff, events):
    put(api, staff["emp_b"], [{"service_id": 2, "duration": 10}])
    [ev] = my_events(events)
    assert ev["actor_id"] == 3
    assert ev["metadata"]["old"] == [{"service_id": 2, "does": False, "duration": None}]
    assert ev["metadata"]["new"] == [{"service_id": 2, "does": True, "duration": 10}]


def test_sin_cambios_no_hay_evento(api, staff, events):
    resp = put(api, staff["dueno"], [{"service_id": 1, "duration": 35}, {"service_id": 2, "duration": 15}])
    assert resp.status_code == 200 and resp.json()["changed"] is False and resp.json()["warnings"] == []
    assert my_events(events) == []


# ---------- La reserva no cambia en 3c ----------
def test_no_toca_las_duraciones_viejas_que_usa_la_reserva(api, staff):
    db = api.Session()
    before = [(u.id, u.duration_corte, u.duration_barba, u.duration_corte_barba) for u in db.query(User)]
    db.close()
    put(api, staff["dueno"], [{"service_id": 1, "duration": 60}])
    db = api.Session()
    assert [(u.id, u.duration_corte, u.duration_barba, u.duration_corte_barba) for u in db.query(User)] == before
    db.close()


def test_el_endpoint_viejo_de_duraciones_sigue_y_no_toca_barber_services(api, staff):
    before = rows(api)
    resp = api.client.post("/admin/service-durations", headers=staff["emp_a"],
                           json={"duration_corte": 40, "duration_barba": 20})
    assert resp.status_code == 200 and resp.json()["duration_corte_barba"] == 60
    assert rows(api) == before


# ---------- Dos "Guardar" casi a la vez (pendiente #33, arreglado en 3e) ----------
def test_si_otro_guardar_ya_inserto_la_fila_da_409_y_no_500(api, staff, events, monkeypatch):
    """Simula la carrera: este pedido leyó "no tengo fila para Corte" justo antes de que el otro la insertara.
    Su INSERT choca con la clave (barbero, servicio) en el flush: tiene que ser 409, no 500."""
    import app.routers.my_services as my_services
    original, calls = my_services._my_rows, []

    def stale_first_read(db, business_id, employee_id):
        calls.append(employee_id)
        return {} if len(calls) == 1 else original(db, business_id, employee_id)
    monkeypatch.setattr(my_services, "_my_rows", stale_first_read)

    resp = put(api, staff["emp_a"], [{"service_id": 1, "duration": 40}])   # Empleado A ya tiene (Corte, 45)
    assert resp.status_code == 409
    assert resp.json()["detail"] == "Se guardó otro cambio al mismo tiempo. Probá de nuevo."
    assert rows(api)[(2, 1)] == (True, 45)          # quedó lo del "otro"
    assert my_events(events) == []
