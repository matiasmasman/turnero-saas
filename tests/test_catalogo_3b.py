"""Pruebas del sub-bloque 3b: API del catálogo (solo OWNER) y la función de precio. Datos 100 % de prueba.

Usa el fixture `api` de conftest.py (TestClient sobre SQLite en memoria ya migrada, sin WhatsApp).
"""
from datetime import datetime

import pytest

import app.catalog as catalog
from app.models import (Appointment, Business, Discount, DiscountPrice, Service, User, UserRole)

CLIENT_NAME, CLIENT_PHONE = "Cliente Secreto", "2619999999"
NOW = datetime(2026, 11, 15, 12, 0, 0)          # "ahora" en hora local para contar turnos futuros
CATALOG_EVENTS = ("SERVICE_", "DISCOUNT_")


# ---------- Datos ----------
@pytest.fixture
def cat(api, monkeypatch):
    """Negocio 1: Corte (id 1) $15.000/30, Barba (2) $10.000/15, Corte + Barba (3) $18.000/45 y el
    descuento Promo 2x (id 1) con Corte a $12.000. Negocio 2 (ajeno): Corte (id 4) y VIP (id 2)."""
    from app.core.security import create_access_token   # acá: necesita la SECRET_KEY que pone `api`
    monkeypatch.setattr(catalog, "now_local", lambda: NOW)

    db = api.Session()
    db.add(Business(id=2, slug="otro", name="Otro negocio"))
    owner = User(name="Dueño", username="dueno", hashed_password="x", role=UserRole.OWNER)
    employee = User(name="Empleado", username="empleado", hashed_password="x", role=UserRole.EMPLOYEE)
    old_owner = User(name="Ex dueño", username="exdueno", hashed_password="x", role=UserRole.OWNER, is_active=False)
    db.add_all([owner, employee, old_owner])
    db.flush()
    for name, price, dur in [("Corte", 15000, 30), ("Barba", 10000, 15), ("Corte + Barba", 18000, 45)]:
        db.add(Service(business_id=1, name=name, price=price, base_duration=dur))
    db.flush()
    db.add(Service(business_id=2, name="Corte", price=9000, base_duration=30))           # id 4
    db.add(Discount(business_id=1, name="Promo 2x"))                                       # id 1
    db.flush()
    db.add(Discount(business_id=2, name="VIP"))                                            # id 2
    db.flush()
    db.add(DiscountPrice(business_id=1, discount_id=1, service_id=1, price=12000))
    db.add(DiscountPrice(business_id=2, discount_id=2, service_id=4, price=8000))
    db.commit()
    ns = {
        "owner": {"Authorization": f"Bearer {create_access_token({'sub': str(owner.id), 'role': 'owner'})}"},
        "employee": {"Authorization": f"Bearer {create_access_token({'sub': str(employee.id), 'role': 'employee'})}"},
        "old_owner": {"Authorization": f"Bearer {create_access_token({'sub': str(old_owner.id), 'role': 'owner'})}"},
        "owner_id": owner.id, "employee_id": employee.id,
    }
    db.close()
    return ns


def add_appt(api, date, time, service="Corte", service_id=1, status="confirmed", discount_id=None,
             discount_name=None, fmt="HH:MM"):
    """time va como "HH:MM". Con fmt="HH:MM:SS" se guarda como lo hace hoy la reserva ("15:00:00")."""
    assert len(time) == 5
    db = api.Session()
    db.add(Appointment(client_name=CLIENT_NAME, client_phone=CLIENT_PHONE, service=service, date=date,
                       time=time + ":00" if fmt == "HH:MM:SS" else time, duration=30, price=15000,
                       status=status, employee_id=1,
                       service_id=service_id, discount_id=discount_id, discount_name=discount_name))
    db.commit()
    db.close()


def catalog_state(api):
    """Todas las filas del catálogo (para ver que algo NO cambió)."""
    db = api.Session()
    try:
        return (
            [(s.id, s.business_id, s.name, s.price, s.base_duration, s.active) for s in db.query(Service).order_by(Service.id)],
            [(d.id, d.business_id, d.name, d.active) for d in db.query(Discount).order_by(Discount.id)],
            sorted((p.business_id, p.discount_id, p.service_id, p.price) for p in db.query(DiscountPrice)),
        )
    finally:
        db.close()


def catalog_events(events):
    return [e for e in events if (e["event_type"] or "").startswith(CATALOG_EVENTS)]


def detail(resp):
    d = resp.json()["detail"]
    return d if isinstance(d, str) else " | ".join(str(x.get("msg")) for x in d)


# ---------- Permisos ----------
ENDPOINTS = [
    ("GET", "/admin/catalog/services", None),
    ("POST", "/admin/catalog/services", {"name": "Nuevo", "price": 1000, "base_duration": 30}),
    ("PATCH", "/admin/catalog/services/1", {"price": 14000}),
    ("POST", "/admin/catalog/services/1/deactivate", None),
    ("POST", "/admin/catalog/services/1/reactivate", None),
    ("GET", "/admin/catalog/discounts", None),
    ("POST", "/admin/catalog/discounts", {"name": "Nuevo", "prices": [{"service_id": 2, "price": 9000}]}),
    ("PATCH", "/admin/catalog/discounts/1", {"name": "Promo dos"}),
    ("POST", "/admin/catalog/discounts/1/deactivate", None),
    ("POST", "/admin/catalog/discounts/1/reactivate", None),
]
IDS = [f"{m} {p}" for m, p, _ in ENDPOINTS]


@pytest.mark.parametrize("method, path, body", ENDPOINTS, ids=IDS)
def test_employee_recibe_403_y_queda_rbac_violation(api, cat, events, method, path, body):
    before = catalog_state(api)
    resp = api.client.request(method, path, json=body, headers=cat["employee"])
    assert resp.status_code == 403
    rbac = [e for e in events if e["event_type"] == "RBAC_VIOLATION"]
    assert len(rbac) == 1 and rbac[0]["metadata"] == {"required_roles": ["owner"]}
    assert catalog_state(api) == before
    assert catalog_events(events) == []


@pytest.mark.parametrize("method, path, body", ENDPOINTS, ids=IDS)
@pytest.mark.parametrize("who", ["sin_token", "token_basura", "owner_inactivo"])
def test_sin_token_valido_recibe_401(api, cat, events, method, path, body, who):
    headers = {"sin_token": {}, "token_basura": {"Authorization": "Bearer basura"},
               "owner_inactivo": cat["old_owner"]}[who]
    before = catalog_state(api)
    resp = api.client.request(method, path, json=body, headers=headers)
    assert resp.status_code == 401
    assert catalog_state(api) == before
    assert catalog_events(events) == []


@pytest.mark.parametrize("method, path, body", ENDPOINTS, ids=IDS)
def test_owner_puede_usar_cada_endpoint(api, cat, method, path, body):
    resp = api.client.request(method, path, json=body, headers=cat["owner"])
    assert resp.status_code in (200, 201), resp.text


# test_endpoints_viejos_siguen_igual se fue en el 3f: /admin/service-prices ya no existe (ver test_agenda_3f.py).
# GET /appointments/services (precios viejos de la web pública) se fue en el 3e: ver test_reserva_3e.py


def test_con_api_sigue_prohibido_abrir_sockets_nuevos(api):
    """El fixture `api` solo deja pasar el socketpair interno de asyncio, no sockets nuevos."""
    import socket
    for family in (socket.AF_INET, socket.AF_UNIX):
        with pytest.raises(AssertionError, match="no pueden abrir sockets"):
            socket.socket(family, socket.SOCK_STREAM)
    with pytest.raises(AssertionError, match="no pueden abrir sockets"):
        socket.create_connection(("127.0.0.1", 9))


# ---------- Campos de más y tipos ----------
@pytest.mark.parametrize("method, path, body", [
    ("POST", "/admin/catalog/services", {"name": "Nuevo", "price": 1000, "base_duration": 30, "business_id": 2}),
    ("POST", "/admin/catalog/services", {"name": "Nuevo", "price": 1000, "base_duration": 30, "id": 99}),
    ("POST", "/admin/catalog/services", {"name": "Nuevo", "price": 1000, "base_duration": 30, "active": False}),
    ("PATCH", "/admin/catalog/services/1", {"price": 14000, "business_id": 2}),
    ("PATCH", "/admin/catalog/services/1", {"active": False}),
    ("POST", "/admin/catalog/discounts", {"name": "Nuevo", "prices": [{"service_id": 2, "price": 9000}],
                                          "business_id": 2}),
    ("POST", "/admin/catalog/discounts", {"name": "Nuevo", "prices": [{"service_id": 2, "price": 9000,
                                                                       "business_id": 2}]}),
    ("PATCH", "/admin/catalog/discounts/1", {"name": "Otro", "active": False}),
], ids=lambda x: str(x) if isinstance(x, str) else None)
def test_campos_de_mas_dan_422(api, cat, events, method, path, body):
    before = catalog_state(api)
    resp = api.client.request(method, path, json=body, headers=cat["owner"])
    assert resp.status_code == 422
    assert "Extra inputs are not permitted" in detail(resp)
    assert catalog_state(api) == before
    assert catalog_events(events) == []


@pytest.mark.parametrize("body", [{}, {"name": None}, {"price": None}, {"base_duration": None}])
def test_patch_servicio_vacio_o_con_null_da_422(api, cat, body):
    assert api.client.patch("/admin/catalog/services/1", json=body, headers=cat["owner"]).status_code == 422


@pytest.mark.parametrize("body", [{}, {"name": None}, {"prices": None}, {"prices": []}])
def test_patch_descuento_vacio_o_con_null_da_422(api, cat, body):
    assert api.client.patch("/admin/catalog/discounts/1", json=body, headers=cat["owner"]).status_code == 422


@pytest.mark.parametrize("path", ["/admin/catalog/services/0", "/admin/catalog/services/-1",
                                  "/admin/catalog/services/99999999999", "/admin/catalog/services/abc"])
def test_ids_de_url_invalidos_dan_422(api, cat, path):
    assert api.client.patch(path, json={"price": 1}, headers=cat["owner"]).status_code == 422


# ---------- Nombres ----------
@pytest.mark.parametrize("raw, stored", [
    ("Corte niño", "Corte niño"),
    ("Diseño (cejas)", "Diseño (cejas)"),
    ("Peinado d'autor", "Peinado d'autor"),
    ("Afeitado-clásico 10%", "Afeitado-clásico 10%"),
    ("Pingüino + Ñandú", "Pingüino + Ñandú"),
    ("Lavado. Express", "Lavado. Express"),
    ("  Lavado   con  masaje  ", "Lavado con masaje"),       # espacios de más: se normalizan
    ("Café", "Café"),                                  # e + tilde suelta → é (NFC)
    ("Ab", "Ab"),
    ("A" * 40, "A" * 40),
])
def test_nombres_validos(api, cat, raw, stored):
    resp = api.client.post("/admin/catalog/services", json={"name": raw, "price": 1000, "base_duration": 30},
                           headers=cat["owner"])
    assert resp.status_code == 201, resp.text
    assert resp.json()["name"] == stored


@pytest.mark.parametrize("raw", [
    "", "   ", "A", "A" * 41,
    "<script>alert(1)</script>", "Corte <b>", 'Corte "VIP"', "Corte & Barba", "Corte/Barba", "Corte_Barba",
    "Corte@casa", "Corte;", "Corte​Barba", "Corte 💈", "Corte\nBarba\x00", "Corte # 1",
])
def test_nombres_invalidos_dan_422(api, cat, events, raw):
    before = catalog_state(api)
    for path, body in [("/admin/catalog/services", {"name": raw, "price": 1000, "base_duration": 30}),
                       ("/admin/catalog/discounts", {"name": raw, "prices": [{"service_id": 2, "price": 900}]}),
                       ("/admin/catalog/services/1", {"name": raw}),
                       ("/admin/catalog/discounts/1", {"name": raw})]:
        method = "PATCH" if path[-1].isdigit() else "POST"
        resp = api.client.request(method, path, json=body, headers=cat["owner"])
        assert resp.status_code == 422, (path, raw)
    assert catalog_state(api) == before
    assert catalog_events(events) == []


@pytest.mark.parametrize("name", [123, ["Corte"], {"x": 1}, True])
def test_nombre_que_no_es_texto_da_422(api, cat, name):
    resp = api.client.post("/admin/catalog/services", json={"name": name, "price": 1000, "base_duration": 30},
                           headers=cat["owner"])
    assert resp.status_code == 422


@pytest.mark.parametrize("name", ["corte", "CORTE", " Corte ", "CORTE + BARBA", "corte + barba"])
def test_nombre_de_servicio_repetido_con_otras_mayusculas_da_409(api, cat, name):
    resp = api.client.post("/admin/catalog/services", json={"name": name, "price": 1000, "base_duration": 30},
                           headers=cat["owner"])
    assert resp.status_code == 409
    assert "Ya existe un servicio llamado" in detail(resp)


def test_repetidos_con_tildes_y_nombre_sin_tilde(api, cat):
    """casefold() en Python: 'PEINADO CLÁSICO' choca con 'Peinado clásico' también en SQLite,
    cuyo lower() no pasa 'Á' a 'á'. Sin tilde es otro nombre."""
    post = lambda n: api.client.post("/admin/catalog/services", headers=cat["owner"],  # noqa: E731
                                     json={"name": n, "price": 1000, "base_duration": 30})
    assert post("Peinado clásico").status_code == 201
    assert post("PEINADO CLÁSICO").status_code == 409
    assert post("Peinado Clasico").status_code == 201
    assert post("Diseño").status_code == 201
    assert post("DISEÑO").status_code == 409


def test_nombre_repetido_contra_un_servicio_desactivado_tambien_choca(api, cat):
    api.client.post("/admin/catalog/services/2/deactivate", headers=cat["owner"])
    resp = api.client.post("/admin/catalog/services", headers=cat["owner"],
                           json={"name": "barba", "price": 1000, "base_duration": 15})
    assert resp.status_code == 409


def test_el_nombre_de_otro_negocio_no_choca(api, cat):
    """El negocio 2 tiene un descuento 'VIP': el negocio 1 puede crear el suyo."""
    resp = api.client.post("/admin/catalog/discounts", headers=cat["owner"],
                           json={"name": "VIP", "prices": [{"service_id": 1, "price": 9000}]})
    assert resp.status_code == 201


def test_renombrar_a_un_nombre_ocupado_da_409_pero_cambiar_mayusculas_propias_si(api, cat):
    assert api.client.patch("/admin/catalog/services/2", json={"name": "CORTE"},
                            headers=cat["owner"]).status_code == 409
    resp = api.client.patch("/admin/catalog/services/1", json={"name": "CORTE"}, headers=cat["owner"])
    assert resp.status_code == 200 and resp.json()["name"] == "CORTE"


def test_si_dos_pedidos_crean_el_mismo_nombre_a_la_vez_la_base_frena_al_segundo(api, cat, monkeypatch):
    """Simula la carrera: el chequeo en Python no ve al otro, pero el índice lower(name) sí."""
    monkeypatch.setattr("app.routers.catalog.name_taken", lambda *a, **k: False)
    resp = api.client.post("/admin/catalog/services", headers=cat["owner"],
                           json={"name": "CORTE", "price": 1000, "base_duration": 30})
    assert resp.status_code == 409
    resp = api.client.post("/admin/catalog/discounts", headers=cat["owner"],
                           json={"name": "PROMO 2X", "prices": [{"service_id": 2, "price": 900}]})
    assert resp.status_code == 409


# ---------- Precio y duración del servicio ----------
@pytest.mark.parametrize("price", [0, -1, 1_000_001, "15000", 15000.5, 15000.0, True, None])
def test_precio_de_servicio_invalido_da_422(api, cat, price):
    resp = api.client.post("/admin/catalog/services", headers=cat["owner"],
                           json={"name": "Nuevo", "price": price, "base_duration": 30})
    assert resp.status_code == 422


@pytest.mark.parametrize("price", [1, 1_000_000])
def test_precio_de_servicio_en_los_limites(api, cat, price):
    resp = api.client.post("/admin/catalog/services", headers=cat["owner"],
                           json={"name": "Nuevo", "price": price, "base_duration": 30})
    assert resp.status_code == 201


@pytest.mark.parametrize("duration", [0, 4, 7, 181, 185, -5, "30", 30.0, None])
def test_duracion_invalida_da_422(api, cat, duration):
    resp = api.client.post("/admin/catalog/services", headers=cat["owner"],
                           json={"name": "Nuevo", "price": 1000, "base_duration": duration})
    assert resp.status_code == 422


@pytest.mark.parametrize("duration", [5, 60, 180])
def test_duracion_valida(api, cat, duration):
    resp = api.client.post("/admin/catalog/services", headers=cat["owner"],
                           json={"name": "Nuevo", "price": 1000, "base_duration": duration})
    assert resp.status_code == 201


def test_editar_servicio(api, cat, events):
    resp = api.client.patch("/admin/catalog/services/1", headers=cat["owner"],
                            json={"name": "Corte clásico", "price": 16000, "base_duration": 35})
    assert resp.status_code == 200
    assert resp.json() == {"id": 1, "name": "Corte clásico", "price": 16000, "base_duration": 35, "active": True}
    [ev] = catalog_events(events)
    assert ev["event_type"] == "SERVICE_UPDATED"
    assert ev["metadata"]["old"] == {"name": "Corte", "price": 15000, "base_duration": 30}
    assert ev["metadata"]["new"] == {"name": "Corte clásico", "price": 16000, "base_duration": 35}


def test_editar_con_los_mismos_valores_no_deja_evento(api, cat, events):
    resp = api.client.patch("/admin/catalog/services/1", json={"price": 15000}, headers=cat["owner"])
    assert resp.status_code == 200
    assert catalog_events(events) == []


def test_bajar_el_precio_del_servicio_debajo_de_un_descuento_da_409(api, cat, events):
    before = catalog_state(api)
    resp = api.client.patch("/admin/catalog/services/1", json={"price": 11999}, headers=cat["owner"])
    assert resp.status_code == 409
    assert "Promo 2x ($12.000)" in detail(resp)
    assert catalog_state(api) == before
    assert catalog_events(events) == []
    # igual al precio del descuento sí se puede
    assert api.client.patch("/admin/catalog/services/1", json={"price": 12000},
                            headers=cat["owner"]).status_code == 200


def test_bajar_precio_tambien_respeta_descuentos_desactivados(api, cat):
    """Si no, al reactivar el descuento quedaría más caro que el servicio."""
    api.client.post("/admin/catalog/discounts/1/deactivate", headers=cat["owner"])
    assert api.client.patch("/admin/catalog/services/1", json={"price": 11000},
                            headers=cat["owner"]).status_code == 409


@pytest.mark.parametrize("path", ["/admin/catalog/services/4", "/admin/catalog/services/999"])
def test_servicio_de_otro_negocio_o_inexistente_da_404(api, cat, events, path):
    before = catalog_state(api)
    for method, sub, body in [("PATCH", "", {"price": 1}), ("POST", "/deactivate", None),
                              ("POST", "/reactivate", None)]:
        resp = api.client.request(method, path + sub, json=body, headers=cat["owner"])
        assert resp.status_code == 404 and detail(resp) == "Servicio no encontrado."
    assert catalog_state(api) == before
    assert catalog_events(events) == []


def test_listar_servicios_solo_del_negocio_e_incluye_inactivos(api, cat):
    api.client.post("/admin/catalog/services/2/deactivate", headers=cat["owner"])
    data = api.client.get("/admin/catalog/services", headers=cat["owner"]).json()
    assert [(s["id"], s["name"], s["active"]) for s in data] == [
        (1, "Corte", True), (2, "Barba", False), (3, "Corte + Barba", True)]


# ---------- Descuentos ----------
def test_crear_descuento(api, cat, events):
    resp = api.client.post("/admin/catalog/discounts", headers=cat["owner"], json={
        "name": "VIP", "prices": [{"service_id": 3, "price": 15000}, {"service_id": 1, "price": 9000}]})
    assert resp.status_code == 201
    assert resp.json() == {"id": 3, "name": "VIP", "active": True, "prices": [
        {"service_id": 1, "service_name": "Corte", "service_active": True, "price": 9000},
        {"service_id": 3, "service_name": "Corte + Barba", "service_active": True, "price": 15000}]}
    [ev] = catalog_events(events)
    assert ev["event_type"] == "DISCOUNT_CREATED" and ev["actor_id"] == cat["owner_id"]
    assert ev["metadata"]["new"] == {"name": "VIP", "prices": [{"service_id": 1, "price": 9000},
                                                               {"service_id": 3, "price": 15000}]}


@pytest.mark.parametrize("prices, code, text", [
    ([{"service_id": 1, "price": 15001}], 400, "no puede ser mayor al precio del servicio ($15.000)"),
    ([{"service_id": 1, "price": 0}], 422, ""),
    ([{"service_id": 1, "price": -100}], 422, ""),
    ([{"service_id": 1, "price": "9000"}], 422, ""),
    ([], 422, ""),
    ([{"service_id": 1, "price": 9000}, {"service_id": 1, "price": 8000}], 422, "servicio repetido"),
    ([{"service_id": 999, "price": 900}], 400, "El servicio 999 no existe."),
    ([{"service_id": 4, "price": 900}], 400, "El servicio 4 no existe."),      # de otro negocio: mismo mensaje
    ([{"service_id": 1}], 422, ""),
])
def test_precios_de_descuento_invalidos(api, cat, events, prices, code, text):
    before = catalog_state(api)
    resp = api.client.post("/admin/catalog/discounts", headers=cat["owner"], json={"name": "VIP", "prices": prices})
    assert resp.status_code == code, resp.text
    assert text in detail(resp)
    assert catalog_state(api) == before
    assert catalog_events(events) == []


def test_precio_de_descuento_igual_al_del_servicio_se_permite(api, cat):
    resp = api.client.post("/admin/catalog/discounts", headers=cat["owner"],
                           json={"name": "VIP", "prices": [{"service_id": 1, "price": 15000}]})
    assert resp.status_code == 201


def test_no_se_puede_poner_precio_de_descuento_a_un_servicio_desactivado(api, cat):
    api.client.post("/admin/catalog/services/2/deactivate", headers=cat["owner"])
    resp = api.client.post("/admin/catalog/discounts", headers=cat["owner"],
                           json={"name": "VIP", "prices": [{"service_id": 2, "price": 9000}]})
    assert resp.status_code == 400 and "está desactivado" in detail(resp)


def test_editar_precios_reemplaza_la_lista_de_servicios_activos(api, cat, events):
    resp = api.client.patch("/admin/catalog/discounts/1", headers=cat["owner"], json={
        "prices": [{"service_id": 2, "price": 8000}, {"service_id": 3, "price": 16000}]})
    assert resp.status_code == 200
    assert [(p["service_id"], p["price"]) for p in resp.json()["prices"]] == [(2, 8000), (3, 16000)]
    [ev] = catalog_events(events)
    assert ev["event_type"] == "DISCOUNT_UPDATED"
    assert ev["metadata"]["old"] == {"prices": [{"service_id": 1, "price": 12000}]}
    assert ev["metadata"]["new"] == {"prices": [{"service_id": 2, "price": 8000}, {"service_id": 3, "price": 16000}]}


def test_editar_precios_conserva_los_de_servicios_desactivados(api, cat):
    api.client.patch("/admin/catalog/discounts/1", headers=cat["owner"], json={
        "prices": [{"service_id": 1, "price": 12000}, {"service_id": 2, "price": 8000}]})
    api.client.post("/admin/catalog/services/2/deactivate", headers=cat["owner"])
    resp = api.client.patch("/admin/catalog/discounts/1", headers=cat["owner"],
                            json={"prices": [{"service_id": 3, "price": 17000}]})
    assert resp.status_code == 200
    assert [(p["service_id"], p["service_active"], p["price"]) for p in resp.json()["prices"]] == [
        (2, False, 8000), (3, True, 17000)]
    # y al reactivar Barba vuelve a tener su precio de descuento
    api.client.post("/admin/catalog/services/2/reactivate", headers=cat["owner"])
    db = api.Session()
    assert catalog.quote_price(db, 1, 2, 1).final_price == 8000
    db.close()


def test_renombrar_descuento_y_mismos_valores_sin_evento(api, cat, events):
    resp = api.client.patch("/admin/catalog/discounts/1", json={"name": "Promo dos x uno"}, headers=cat["owner"])
    assert resp.status_code == 200 and resp.json()["name"] == "Promo dos x uno"
    [ev] = catalog_events(events)
    assert ev["metadata"]["old"] == {"name": "Promo 2x"} and ev["metadata"]["new"] == {"name": "Promo dos x uno"}
    resp = api.client.patch("/admin/catalog/discounts/1", headers=cat["owner"], json={
        "name": "Promo dos x uno", "prices": [{"service_id": 1, "price": 12000}]})
    assert resp.status_code == 200
    assert len(catalog_events(events)) == 1


@pytest.mark.parametrize("path", ["/admin/catalog/discounts/2", "/admin/catalog/discounts/999"])
def test_descuento_de_otro_negocio_o_inexistente_da_404(api, cat, events, path):
    before = catalog_state(api)
    for method, sub, body in [("PATCH", "", {"name": "Robado"}), ("POST", "/deactivate", None),
                              ("POST", "/reactivate", None)]:
        resp = api.client.request(method, path + sub, json=body, headers=cat["owner"])
        assert resp.status_code == 404 and detail(resp) == "Descuento no encontrado."
    assert catalog_state(api) == before
    assert catalog_events(events) == []


def test_listar_descuentos_solo_del_negocio(api, cat):
    data = api.client.get("/admin/catalog/discounts", headers=cat["owner"]).json()
    assert data == [{"id": 1, "name": "Promo 2x", "active": True, "prices": [
        {"service_id": 1, "service_name": "Corte", "service_active": True, "price": 12000}]}]


# ---------- Desactivar / reactivar ----------
@pytest.mark.parametrize("fmt", ["HH:MM", "HH:MM:SS"])
def test_desactivar_y_reactivar_servicio_con_turnos_futuros(api, cat, events, fmt):
    add_appt(api, "2026-11-20", "15:00", fmt=fmt)                                   # futuro → cuenta
    add_appt(api, "2026-11-15", "13:00", fmt=fmt)                                   # hoy, más tarde → cuenta
    add_appt(api, "2026-11-15", "12:00", fmt=fmt)                                   # hoy, justo ahora → cuenta
    add_appt(api, "2026-11-15", "11:59", fmt=fmt)                                   # hoy, un minuto antes → no
    add_appt(api, "2026-11-15", "11:00", fmt=fmt)                                   # hoy, ya pasó → no
    add_appt(api, "2026-11-10", "15:00", fmt=fmt)                                   # pasado → no
    add_appt(api, "2026-11-21", "15:00", status="cancelled", fmt=fmt)               # cancelado → no
    add_appt(api, "2026-11-22", "15:00", service_id=None, fmt=fmt)                  # sin service_id, por nombre → cuenta
    add_appt(api, "2026-11-22", "16:00", service="Barba", service_id=None, fmt=fmt)  # otro servicio → no
    add_appt(api, "2026-11-23", "15:00", service="Barba", service_id=2, fmt=fmt)    # otro servicio → no

    resp = api.client.post("/admin/catalog/services/1/deactivate", headers=cat["owner"])
    assert resp.status_code == 200
    assert resp.json() == {"service": {"id": 1, "name": "Corte", "price": 15000, "base_duration": 30,
                                       "active": False}, "changed": True, "future_appointments": 4}
    [ev] = catalog_events(events)
    assert ev["event_type"] == "SERVICE_DEACTIVATED" and ev["actor_id"] == cat["owner_id"]
    assert ev["metadata"] == {"business_id": 1, "service_id": 1, "name": "Corte", "old": {"active": True},
                              "new": {"active": False}, "future_appointments": 4}

    # Otra vez: no cambia nada y no deja evento
    again = api.client.post("/admin/catalog/services/1/deactivate", headers=cat["owner"]).json()
    assert again["changed"] is False and again["future_appointments"] == 4
    assert len(catalog_events(events)) == 1

    # Desactivado se puede seguir editando
    assert api.client.patch("/admin/catalog/services/1", json={"price": 15500},
                            headers=cat["owner"]).status_code == 200

    resp = api.client.post("/admin/catalog/services/1/reactivate", headers=cat["owner"]).json()
    assert resp["changed"] is True and resp["service"]["active"] is True
    assert catalog_events(events)[-1]["event_type"] == "SERVICE_REACTIVATED"
    assert catalog_events(events)[-1]["metadata"]["new"] == {"active": True}


@pytest.mark.parametrize("fmt", ["HH:MM", "HH:MM:SS"])
def test_desactivar_y_reactivar_descuento_con_turnos_futuros(api, cat, events, fmt):
    add_appt(api, "2026-11-20", "15:00", discount_id=1, discount_name="Promo 2x", fmt=fmt)   # cuenta
    add_appt(api, "2026-11-21", "15:00", discount_name="Promo 2x", fmt=fmt)                  # por nombre → cuenta
    add_appt(api, "2026-11-10", "15:00", discount_id=1, discount_name="Promo 2x", fmt=fmt)   # pasado → no
    add_appt(api, "2026-11-22", "15:00", fmt=fmt)                                            # sin descuento → no

    resp = api.client.post("/admin/catalog/discounts/1/deactivate", headers=cat["owner"]).json()
    assert resp["changed"] is True and resp["future_appointments"] == 2 and resp["discount"]["active"] is False
    resp = api.client.post("/admin/catalog/discounts/1/reactivate", headers=cat["owner"]).json()
    assert resp["changed"] is True and resp["discount"]["active"] is True
    assert [e["event_type"] for e in catalog_events(events)] == ["DISCOUNT_DEACTIVATED", "DISCOUNT_REACTIVATED"]


def test_desactivar_no_toca_los_turnos(api, cat):
    add_appt(api, "2026-11-20", "15:00", discount_id=1, discount_name="Promo 2x")
    api.client.post("/admin/catalog/services/1/deactivate", headers=cat["owner"])
    api.client.post("/admin/catalog/discounts/1/deactivate", headers=cat["owner"])
    db = api.Session()
    a = db.query(Appointment).one()
    assert (a.service, a.service_id, a.price, a.discount_id, a.discount_name, a.status) == \
        ("Corte", 1, 15000, 1, "Promo 2x", "confirmed")
    db.close()


# ---------- Función de precio ----------
def test_precio_sin_descuento(api, cat):
    db = api.Session()
    assert catalog.quote_price(db, 1, 1) == catalog.PriceQuote(1, "Corte", 15000, 15000, None, None)
    db.close()


def test_precio_con_descuento(api, cat):
    db = api.Session()
    assert catalog.quote_price(db, 1, 1, 1) == catalog.PriceQuote(1, "Corte", 15000, 12000, 1, "Promo 2x")
    db.close()


@pytest.mark.parametrize("setup, service_id, discount_id, code", [
    (None, 2, 1, "discount_not_applicable"),        # Promo 2x no tiene precio para Barba
    ("service_off", 1, None, "service_inactive"),
    ("service_off", 1, 1, "service_inactive"),
    ("discount_off", 1, 1, "discount_inactive"),
    (None, 4, None, "service_not_found"),           # servicio del negocio 2
    (None, 1, 2, "discount_not_found"),             # descuento del negocio 2
    (None, 999, None, "service_not_found"),
    (None, 1, 999, "discount_not_found"),
    ("price_too_high", 1, 1, "price_inconsistent"), # alguien tocó la base a mano
])
def test_precio_con_errores(api, cat, setup, service_id, discount_id, code):
    db = api.Session()
    if setup == "service_off":
        db.get(Service, 1).active = False
    elif setup == "discount_off":
        db.get(Discount, 1).active = False
    elif setup == "price_too_high":
        db.query(DiscountPrice).filter_by(discount_id=1, service_id=1).one().price = 20000
    db.commit()
    with pytest.raises(catalog.CatalogError) as err:
        catalog.quote_price(db, 1, service_id, discount_id)
    assert err.value.code == code
    db.close()


def test_negocio_2_ve_solo_lo_suyo(api, cat):
    db = api.Session()
    assert catalog.quote_price(db, 2, 4, 2).final_price == 8000
    with pytest.raises(catalog.CatalogError):
        catalog.quote_price(db, 2, 1)
    db.close()


def test_current_business_id_hoy_es_1(api, cat):
    db = api.Session()
    assert catalog.current_business_id(db.query(User).filter_by(username="empleado").one()) == 1
    db.close()


# ---------- Eventos ----------
def test_cada_cambio_deja_su_evento_con_actor_y_sin_datos_de_clientes(api, cat, events):
    add_appt(api, "2026-11-20", "15:00", discount_id=1, discount_name="Promo 2x")
    o = cat["owner"]
    c = api.client
    assert c.post("/admin/catalog/services", headers=o, json={"name": "Lavado", "price": 3000,
                                                                "base_duration": 10}).status_code == 201
    c.patch("/admin/catalog/services/5", headers=o, json={"price": 3500})
    c.post("/admin/catalog/services/5/deactivate", headers=o)
    c.post("/admin/catalog/services/5/reactivate", headers=o)
    c.post("/admin/catalog/discounts", headers=o, json={"name": "Lunes", "prices": [{"service_id": 5, "price": 2500}]})
    c.patch("/admin/catalog/discounts/3", headers=o, json={"name": "Lunes y martes"})
    c.post("/admin/catalog/discounts/1/deactivate", headers=o)
    c.post("/admin/catalog/discounts/1/reactivate", headers=o)

    evs = catalog_events(events)
    assert [e["event_type"] for e in evs] == [
        "SERVICE_CREATED", "SERVICE_UPDATED", "SERVICE_DEACTIVATED", "SERVICE_REACTIVATED",
        "DISCOUNT_CREATED", "DISCOUNT_UPDATED", "DISCOUNT_DEACTIVATED", "DISCOUNT_REACTIVATED"]
    assert all(e["actor_id"] == cat["owner_id"] for e in evs)
    assert all(e["metadata"]["business_id"] == 1 for e in evs)
    assert evs[0]["metadata"]["new"] == {"name": "Lavado", "price": 3000, "base_duration": 10}
    assert (evs[1]["metadata"]["old"], evs[1]["metadata"]["new"]) == ({"price": 3000}, {"price": 3500})
    assert evs[6]["metadata"]["future_appointments"] == 1
    for e in evs:
        assert {"old", "new"} & set(e["metadata"]), e["event_type"]
    dump = repr(events)
    assert CLIENT_NAME not in dump and CLIENT_PHONE not in dump
