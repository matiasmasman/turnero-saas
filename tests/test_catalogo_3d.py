"""Pruebas de API del 3d: GET /admin/catalog/services ahora dice cuántos barberos ACTIVOS hacen cada servicio
(para el aviso "Todavía ningún barbero hace este servicio"). Datos 100 % de prueba."""
import pytest

from app.models import BarberService, Business, Service, User, UserRole

URL = "/admin/catalog/services"


@pytest.fixture
def cat(api):
    """Negocio 1: Corte (1), Barba (2), Navaja (3, desactivado). Negocio 2: Corte (4).
    Dueño (1) hace Corte. Empleado A (2) hace Corte y Barba. Empleado B (3) dejó de hacer Barba.
    Empleado C (4, INACTIVO) hace Corte y Navaja. Nadie del negocio 2."""
    from app.core.security import create_access_token
    db = api.Session()
    db.add(Business(id=2, slug="otro", name="Otro negocio"))
    users = [User(name="Dueño", username="dueno", hashed_password="x", role=UserRole.OWNER),
             User(name="A", username="emp_a", hashed_password="x", role=UserRole.EMPLOYEE),
             User(name="B", username="emp_b", hashed_password="x", role=UserRole.EMPLOYEE),
             User(name="C", username="emp_c", hashed_password="x", role=UserRole.EMPLOYEE, is_active=False)]
    db.add_all(users)
    db.flush()
    for name, active in [("Corte", True), ("Barba", True), ("Navaja", False)]:
        db.add(Service(business_id=1, name=name, price=10000, base_duration=30, active=active))
    db.flush()
    db.add(Service(business_id=2, name="Corte", price=9000, base_duration=30))
    db.flush()
    for emp, sid, active in [(1, 1, True), (2, 1, True), (2, 2, True), (3, 2, False), (4, 1, True), (4, 3, True)]:
        db.add(BarberService(business_id=1, employee_id=emp, service_id=sid, duration=30, active=active))
    db.commit()
    tokens = {u.username: {"Authorization": f"Bearer {create_access_token({'sub': str(u.id)})}"} for u in users}
    db.close()
    return tokens


def test_cuenta_solo_barberos_activos_que_lo_hacen(api, cat):
    resp = api.client.get(URL, headers=cat["dueno"])
    assert resp.status_code == 200
    assert [(s["id"], s["name"], s["active"], s["barbers"]) for s in resp.json()] == [
        (1, "Corte", True, 2),       # dueño y A (C está inactivo)
        (2, "Barba", True, 1),       # A (B lo dejó de hacer)
        (3, "Navaja", False, 0),     # solo C, que está inactivo
    ]


def test_un_servicio_nuevo_aparece_con_cero_barberos(api, cat):
    created = api.client.post(URL, headers=cat["dueno"], json={"name": "Peinado", "price": 5000, "base_duration": 20})
    assert created.status_code == 201 and "barbers" not in created.json()   # el resto de la API no cambia
    listed = {s["name"]: s for s in api.client.get(URL, headers=cat["dueno"]).json()}
    assert listed["Peinado"] == {"id": 5, "name": "Peinado", "price": 5000, "base_duration": 20, "active": True,
                                 "barbers": 0}


def test_el_conteo_sigue_a_mis_servicios(api, cat):
    assert api.client.put("/admin/my-services", headers=cat["emp_b"],
                          json={"services": [{"service_id": 2, "duration": 30}]}).status_code == 200
    assert {s["name"]: s["barbers"] for s in api.client.get(URL, headers=cat["dueno"]).json()}["Barba"] == 2


def test_empleado_no_ve_el_catalogo(api, cat):
    assert api.client.get(URL, headers=cat["emp_a"]).status_code == 403
